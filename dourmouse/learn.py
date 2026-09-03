"""Store & Learn loop (v2.9) — the system stores data and learns from it.

Connects the two halves that already existed but never touched each other:

- STORING: every completed chat turn is already persisted as a hash-chained
  audit record in <workspace>/sessions/*.jsonl. The long-term SQLite FTS5
  store (memory_store.py) already has ``ingest_session_file`` — but nothing
  called it. Now ChatSession auto-ingests after every completed turn, so
  everything the system does becomes searchable knowledge on disk.
- LEARNING: the store already had ``search`` — but nothing fed recalled
  knowledge back into the model's context, so stored data never influenced
  future behavior. Now, before each new user turn, the top FTS5 matches for
  that prompt are injected into the system message as a REMEMBERED CONTEXT
  block, so the model actually uses what it learned. Operator 👍/👎 feedback
  is stored as facts too, so "this answer was good/bad" steers recall.

Deterministic (Rule 2.8): recall ranking is plain FTS5 bm25, never an LLM
judgment call. Honest (Rule 2.2): a missing/unavailable store or a
DOURMOUSE_LEARN=0 gate simply disables the loop — the system still works, it
just doesn't learn; nothing is fabricated either way.

Env gates:
- DOURMOUSE_LEARN=0 / false / no / off  -> whole loop off (no store created).
- DOURMOUSE_MEMORY_DB                    -> where the store lives (default:
  <workspace>/memory/atlas_memory.db).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from dourmouse.memory_store import MemoryStore, MemoryStoreUnavailable

_LEARN_ENV = "DOURMOUSE_LEARN"
_MEMORY_DB_ENV = "DOURMOUSE_MEMORY_DB"
_WORKSPACE_ENV = "DOURMOUSE_WORKSPACE"
_MEMORY_REMOTE_URL_ENV = "DOURMOUSE_MEMORY_REMOTE_URL"
_MEMORY_REMOTE_TOKEN_ENV = "DOURMOUSE_MEMORY_REMOTE_TOKEN"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_OFF_VALUES = {"", "0", "false", "no", "off"}


def learn_enabled(value: str | None = None) -> bool:
    """DOURMOUSE_LEARN gate. Anything but 0/false/no/off/empty enables learning.

    ``value`` overrides the env (test seam, mirroring live_runtime.live_enabled).
    """
    if value is None:
        value = os.environ.get(_LEARN_ENV, "1")
    return str(value).strip().lower() not in _OFF_VALUES


def default_store_path() -> Path:
    """Where the long-term memory store lives: DOURMOUSE_MEMORY_DB env, else
    <workspace>/memory/atlas_memory.db (same convention as general_roster)."""
    raw = os.environ.get(_MEMORY_DB_ENV)
    if raw:
        return Path(raw).expanduser()
    wraw = os.environ.get(_WORKSPACE_ENV)
    root = Path(wraw).expanduser() if wraw else _PROJECT_ROOT / "workspace"
    return root / "memory" / "atlas_memory.db"


def open_default_store() -> "MemoryStore | RemoteMemoryStore | None":
    """Open the long-term store for the learning loop, or None (honestly).

    Returns None when DOURMOUSE_LEARN is off (the loop is disabled by design) or
    when SQLite FTS5 is unavailable on this build (MemoryStoreUnavailable).
    Never raises for the caller — a learning loop that cannot run must not
    take down the app. The memory SUBAGENT's tools still report NOT
    CONFIGURED loudly when the user explicitly asks for memory.

    v13.4: real bug found live deploying "move the actual rag to [the
    desktop]" — this function used to ALWAYS open a local MemoryStore,
    completely independent of general_roster._open_memory_store()'s own
    (correct) DOURMOUSE_MEMORY_REMOTE_URL check. The individual memory
    tools (remember/recall/query_shared_memory) went through the general_
    roster path and correctly reached the remote desktop; this server-
    level store (server.memory — the Store & Learn background loop AND
    the /api/memory stats the dashboard shows) silently opened a brand
    new, empty LOCAL file instead — caught immediately after a real
    restart reported "1 fact(s)" instead of the real remote count.
    Same remote-first check as general_roster now, so both paths agree.
    """
    if not learn_enabled():
        return None
    remote_url = os.environ.get(_MEMORY_REMOTE_URL_ENV, "").strip()
    if remote_url:
        from dourmouse.memory_store import RemoteMemoryStore

        token = os.environ.get(_MEMORY_REMOTE_TOKEN_ENV, "").strip() or None
        return RemoteMemoryStore(remote_url, token=token)
    try:
        return MemoryStore(default_store_path())
    except MemoryStoreUnavailable:
        return None


# --------------------------------------------------------------------------- #
# Recall — turn stored knowledge into model context
# --------------------------------------------------------------------------- #

# Deterministic stopwords dropped before FTS5 recall. Keeps the query down
# to the DISTINCTIVE terms of a prompt — "tell me about project nebula"
# recalls on [project, nebula], not on an AND over every word (which would
# match almost nothing). Plain data, never an LLM judgment (Rule 2.8).
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "of", "to", "in", "on",
        "for", "with", "at", "by", "from", "as", "is", "are", "was",
        "were", "be", "been", "do", "does", "did", "can", "could",
        "would", "should", "will", "shall", "may", "might", "must",
        "i", "we", "you", "they", "he", "she", "it", "its", "my",
        "your", "our", "their", "this", "that", "these", "those",
        "what", "when", "where", "which", "who", "whom", "how", "why",
        "tell", "me", "about", "please", "give", "show", "find", "make",
        "need", "want", "use", "using", "used", "know", "like", "just",
        "remember", "then", "also", "not", "no", "yes", "there", "here",
        "have", "has", "had", "get", "got", "let", "something",
        "anything", "everything", "some", "any", "all", "more", "most",
        "very", "really", "much", "many", "one", "two", "first", "last",
    }
)


def distill_query(prompt: str, max_terms: int = 5) -> str:
    """Reduce a natural-language prompt to its most distinctive terms.

    Lowercases, drops stopwords, keeps the longest alphanumeric tokens
    (deduped, deterministic order) and joins with spaces — store.search then
    ANDs them via its safe FTS5 quoting. Empty result means "nothing
    distinctive to recall on". Public since v4.1 (P6): the semantic layer's
    FTS5 fallback uses the same distillation so conversational queries
    ("why did we change the risk parameters") recall correctly."""
    tokens = re.findall(r"[A-Za-z0-9_]{2,}", prompt.lower())
    terms = sorted(
        {t for t in tokens if t not in _STOPWORDS},
        key=lambda t: (-len(t), t),
    )[: max(1, min(int(max_terms), 8))]
    return " ".join(terms)


def recall_block(store: MemoryStore, prompt: str, limit: int = 5) -> str:
    """Deterministic FTS5 recall of stored knowledge for ``prompt``.

    Returns a formatted REMEMBERED CONTEXT block, or "" when there are no
    matches (or the prompt has no distinctive terms) — the caller then
    leaves the system message untouched. Everything here is real stored
    data; the snippet field comes from SQLite's snippet() with the match
    highlighted.

    v13.6, real severe bug found live (this session's own "final app"
    polish pass): this module's own docstring promises "a missing/
    unavailable store... simply disables the loop — the system still
    works," but ``store.search()`` was called here with NO error
    handling at all — fine for a local ``MemoryStore`` (which never
    raises), but ``store`` can also be a ``RemoteMemoryStore``
    (``DOURMOUSE_MEMORY_REMOTE_URL``), whose ``.search()`` genuinely
    RAISES ``RemoteMemoryStoreUnavailable`` the moment the remote host
    is unreachable — confirmed live: with the default ``DOURMOUSE_LEARN=1``
    and a remote store configured, EVERY chat turn failed outright the
    instant the remote host went offline, the raw exception text
    replacing the entire assistant reply. Recall must never break a
    turn (the exact same principle ``dispatch._maybe_ingest_memory``
    and ``memory_embed.semantic_search`` already apply to their own
    memory calls) — restored here, not a new rule.
    """
    query = distill_query(prompt)
    if not query:
        return ""
    try:
        hits = store.search(query, limit=max(1, min(int(limit), 10)))
    except Exception:  # noqa: BLE001 - a remote/unavailable store must never break a turn
        return ""
    if not hits:
        return ""
    lines = [
        f"- [{h['source']}] {h['title']} (score {h['score']}): {h['snippet']}"
        for h in hits
    ]
    return (
        "\n\n=== REMEMBERED CONTEXT (long-term memory, auto-recalled) ===\n"
        "The system automatically recalled stored knowledge from past "
        "sessions that may be relevant to this request. Use it only if "
        "relevant; never present it as new research or current facts.\n"
        + "\n".join(lines)
        + "\n=== END REMEMBERED CONTEXT ==="
    )


# --------------------------------------------------------------------------- #
# Feedback — operator ratings steer what the model learns
# --------------------------------------------------------------------------- #

_VALID_RATINGS = ("good", "bad")


def _last_session_record(session_file: Path | str) -> dict[str, Any] | None:
    """The last completed turn record from a session JSONL, or None."""
    path = Path(session_file)
    if not path.is_file():
        return None
    last: dict[str, Any] | None = None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("user") is not None:
                last = rec
    except (json.JSONDecodeError, OSError):
        return None
    return last


def record_feedback(
    store: MemoryStore, session_file: Path | str, rating: str
) -> str:
    """Store an operator rating (good/bad) of the LAST completed turn.

    The fact carries the exact user prompt + final answer + rating, so a
    later recall surfaces "the operator liked/disliked this" and the model
    can learn from it. Raises ValueError on an invalid rating.
    """
    rating = (rating or "").strip().lower()
    if rating not in _VALID_RATINGS:
        raise ValueError(f"rating must be one of {_VALID_RATINGS!r}")
    rec = _last_session_record(session_file)
    if rec is None:
        return "ERROR: no completed turn found to rate in this session."
    user = (rec.get("user") or "").strip()[:400]
    answer = (rec.get("final_text") or "").strip()[:1200]
    turn = rec.get("turn", "?")
    stem = Path(session_file).stem
    body = f"USER: {user}\nANSWER: {answer}\nRATED: {rating} by operator"
    store.remember("feedback", f"{stem} turn {turn} rated {rating}", body)
    return (
        f"MEMORY STORED: feedback {rating!r} for {stem} turn {turn} — "
        "recall will surface this so the model learns from the rating."
    )
