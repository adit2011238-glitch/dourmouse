"""Response cache for the inference path — SQLite, TTL'd, correctness-first.

A cache is only a speedup if it never serves a wrong answer, so the design
starts from what must *not* be cached rather than from what can be:

  * Tool-carrying calls are never cached. A model turn that may call
    `delete_path` or `gmail_send` must reach the model every time; replaying a
    tool decision from cache would repeat a side effect the user approved once.
  * Only deterministic requests are cached. A non-zero temperature means the
    caller explicitly asked for variety, and handing back a stored answer
    silently overrides that.
  * The key covers everything that changes the answer — model, every message,
    and the sampling parameters. Anything omitted from the key is a wrong-
    answer bug, so the key is built from a canonical JSON dump rather than a
    hand-picked subset of fields.
  * Entries expire. Live questions ("today's headlines") go stale, and a stale
    right-looking answer is worse than a slow one, hence a conservative
    default TTL.

Stdlib only (sqlite3), so it works on the desktop with no new dependency and
survives a restart, which an in-process dict would not.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["make_key", "get", "put", "invalidate", "stats", "is_cacheable", "clear"]

_DEFAULT_TTL_SECONDS = 3600  # 1 hour
_LOCK = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key         TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_expires ON responses(expires_at);
"""


def _enabled() -> bool:
    """Opt-in, deliberately.

    The dispatch path does not pin `temperature`, so the backend applies its
    own default, which is generally non-zero. Caching those responses would
    quietly convert a varying assistant into a fixed one: ask the same thing
    twice and get a byte-identical answer for the whole TTL. That is a change
    to how the product behaves, not a transparent optimisation, so it is the
    operator's call rather than a default.

    Turn on with DOURMOUSE_CACHE_ENABLED=1. Once the dispatch path pins
    temperature=0 for tool-less turns, this default can safely flip.
    """
    return os.environ.get("DOURMOUSE_CACHE_ENABLED", "").strip() in ("1", "true", "TRUE")


def ttl_seconds() -> int:
    raw = os.environ.get("DOURMOUSE_CACHE_TTL", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _DEFAULT_TTL_SECONDS


def db_path() -> Path:
    override = os.environ.get("DOURMOUSE_CACHE_DB", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "logs" / "response_cache.db"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def is_cacheable(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float | None = None,
    stream: bool = False,
) -> bool:
    """Whether this request may be served from cache.

    Conservative by construction: anything that could carry a side effect or
    an explicit request for variety is excluded.
    """
    if not _enabled():
        return False
    if tools:
        return False          # a cached tool decision could repeat a side effect
    if stream:
        return False          # a replayed stream is not a stream
    if temperature is not None and temperature > 0:
        return False          # caller asked for variety
    if not messages:
        return False
    return True


def make_key(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Stable key over everything that can change the answer.

    Canonical JSON (sorted keys, no incidental whitespace) so that two
    semantically identical requests hash identically regardless of dict order.
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(key: str) -> str | None:
    """Return cached content, or None on miss/expiry. Never raises."""
    if not _enabled():
        return None
    now = time.time()
    try:
        with _LOCK, _connect() as conn:
            row = conn.execute(
                "SELECT content, expires_at FROM responses WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            content, expires_at = row
            if expires_at <= now:
                conn.execute("DELETE FROM responses WHERE key = ?", (key,))
                return None
            conn.execute(
                "UPDATE responses SET hits = hits + 1 WHERE key = ?", (key,)
            )
            return content
    except (sqlite3.Error, OSError):
        return None


def put(key: str, content: str, *, model: str, ttl: int | None = None) -> None:
    """Store a response. Never raises; a cache write must not fail a request."""
    if not _enabled() or not content:
        return
    seconds = ttl_seconds() if ttl is None else ttl
    if seconds <= 0:
        return
    now = time.time()
    try:
        with _LOCK, _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO responses "
                "(key, model, content, created_at, expires_at, hits) "
                "VALUES (?, ?, ?, ?, ?, COALESCE("
                "  (SELECT hits FROM responses WHERE key = ?), 0))",
                (key, model, content, now, now + seconds, key),
            )
    except (sqlite3.Error, OSError):
        return


def invalidate(*, model: str | None = None) -> int:
    """Drop entries (all, or one model's). Returns rows removed."""
    try:
        with _LOCK, _connect() as conn:
            if model:
                cur = conn.execute("DELETE FROM responses WHERE model = ?", (model,))
            else:
                cur = conn.execute("DELETE FROM responses")
            return cur.rowcount or 0
    except (sqlite3.Error, OSError):
        return 0


def clear() -> int:
    """Alias for a full invalidate — used by tests and the CLI."""
    return invalidate()


def purge_expired() -> int:
    """Remove expired rows so the file does not grow without bound."""
    try:
        with _LOCK, _connect() as conn:
            cur = conn.execute("DELETE FROM responses WHERE expires_at <= ?", (time.time(),))
            return cur.rowcount or 0
    except (sqlite3.Error, OSError):
        return 0


def stats() -> dict[str, Any]:
    """Cache contents summary, for diagnostics and the perf report."""
    try:
        with _LOCK, _connect() as conn:
            total, hits, expired = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits), 0), "
                "COALESCE(SUM(CASE WHEN expires_at <= ? THEN 1 ELSE 0 END), 0) "
                "FROM responses",
                (time.time(),),
            ).fetchone()
            return {
                "entries": int(total),
                "total_hits": int(hits),
                "expired": int(expired),
                "db": str(db_path()),
                "ttl_seconds": ttl_seconds(),
                "enabled": _enabled(),
            }
    except (sqlite3.Error, OSError) as exc:
        return {"error": str(exc), "enabled": _enabled()}
