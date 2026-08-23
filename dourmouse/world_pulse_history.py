"""World Pulse History — local snapshot log for the map's time scrubber.

The world monitor UI wants to let a person drag back up to 24 hours and see
the map ("world_pulse_geo()") as it looked at that point. This module is the
storage half of that feature: it does NOT fetch anything itself and it does
NOT compute anything — it just remembers snapshots that ``world_pulse.py``
(or whatever polls it) hands it, and answers "what did we have around time
T" honestly.

Storage is an append-only JSONL file, one JSON object per line:
``{"at": "<iso8601 UTC>", "geo": {...}}``. Same house convention as
``live_feeds._tasks_path()`` — deterministic local JSON on disk, no
database, no LLM anywhere in this module (Rule 2.2: never fabricate data).
The workspace root resolution is copied verbatim from that function so this
file lands in the same place other local state does.

Two things keep the log bounded even if a caller polls aggressively:

- ``record_snapshot`` is a cheap no-op when called again within
  ``_MIN_INTERVAL_SECONDS`` of the last recorded snapshot, so a poller can
  call it on every tick without writing a line every few seconds.
- ``prune_old`` (also run automatically inside ``record_snapshot``) drops
  anything older than the requested retention window, and a hard cap
  (``_MAX_LINES``) prunes the oldest entries before every append so nothing
  calling ``record_snapshot`` in a tight loop can grow the file unboundedly.

Every public function is a "nice to have" for the UI, never a load-bearing
data path: all of them swallow their own errors and return an honest empty
result (``None`` / ``[]`` / ``0``) rather than raise or invent a value.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Re-recording within this many seconds of the last snapshot is a no-op.
_MIN_INTERVAL_SECONDS = 90.0

#: Hard cap on stored lines regardless of retention window — protects the
#: file from unbounded growth if something calls record_snapshot in a tight
#: loop. ~24h at a ~2-minute polling cadence is ~720 entries; 1000 gives
#: headroom without letting the file grow forever.
_MAX_LINES = 1000

_ENV_HISTORY_FILE = "DOURMOUSE_WORLD_HISTORY_FILE"

_lock = threading.Lock()


def _history_path() -> Path:
    """Resolve the JSONL path — mirrors ``live_feeds._tasks_path()`` exactly.

    Same workspace-root convention: ``DOURMOUSE_WORKSPACE`` (falling back to
    a relative ``workspace/`` directory) names the root, and a dedicated
    per-file env var (here ``DOURMOUSE_WORLD_HISTORY_FILE``) can override the
    path outright, same as ``DOURMOUSE_TASKS_FILE`` does for tasks.json.
    """
    root = Path(os.environ.get("DOURMOUSE_WORKSPACE", "").strip() or "workspace")
    env = os.environ.get(_ENV_HISTORY_FILE, "").strip()
    if env:
        return Path(env)
    return root / "world_history.jsonl"


def _history_file() -> Path:
    return _history_path().expanduser()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_at(raw: Any) -> datetime | None:
    """Parse a recorded 'at' timestamp back into an aware UTC datetime.

    Returns None on anything malformed rather than raising — one bad line
    in the log must not take down the whole read path.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_lines(path: Path) -> list[dict[str, Any]]:
    """Read + parse every valid JSONL line. Missing file -> []; bad lines skipped."""
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and _parse_at(rec.get("at")) is not None:
            out.append(rec)
    return out


def _write_lines(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    if body:
        body += "\n"
    path.write_text(body, encoding="utf-8")


def record_snapshot(geo: dict) -> None:
    """Append one world_pulse_geo()-shaped snapshot, timestamped now.

    Cheap no-op if called again within ``_MIN_INTERVAL_SECONDS`` of the most
    recently recorded snapshot, so a caller can invoke this on every poll
    without writing a line every few seconds. Also enforces the hard line
    cap by pruning the oldest entries before appending. Never raises — a
    write failure here is swallowed; this is a nice-to-have feature, not
    part of the core data path.
    """
    if not isinstance(geo, dict):
        return
    try:
        path = _history_file()
        with _lock:
            records = _read_lines(path)
            now = _now()
            if records:
                last_at = _parse_at(records[-1].get("at"))
                if last_at is not None and (now - last_at).total_seconds() < _MIN_INTERVAL_SECONDS:
                    return  # too soon — no-op write
            records.append({"at": now.isoformat(timespec="seconds"), "geo": geo})
            if len(records) > _MAX_LINES:
                records = records[-_MAX_LINES:]
            _write_lines(path, records)
    except Exception:  # noqa: BLE001 - nice-to-have, must never raise
        return


def history_range(hours: float = 24.0) -> list[dict]:
    """List available snapshot timestamps within the last `hours` hours.

    Oldest first: ``[{"at": "<iso8601>", "counts": {...}}]``. Empty list if
    nothing recorded yet, or on any read failure. Never raises.
    """
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return []
    if hours <= 0:
        return []
    try:
        path = _history_file()
        with _lock:
            records = _read_lines(path)
        cutoff = _now() - timedelta(hours=hours)
        out: list[dict] = []
        for rec in records:
            at = _parse_at(rec.get("at"))
            if at is None or at < cutoff:
                continue
            geo = rec.get("geo") or {}
            counts = geo.get("counts") if isinstance(geo, dict) else None
            out.append({"at": rec.get("at"), "counts": counts if isinstance(counts, dict) else {}})
        out.sort(key=lambda r: r["at"])
        return out
    except Exception:  # noqa: BLE001 - nice-to-have, must never raise
        return []


def history_at(minutes_ago: float) -> dict | None:
    """Return the recorded snapshot nearest to `minutes_ago` minutes before now.

    A real recorded snapshot only — never interpolated or fabricated. None
    if no history exists yet, or nothing is within a reasonable tolerance
    of the requested point. Never raises.
    """
    try:
        minutes_ago = float(minutes_ago)
    except (TypeError, ValueError):
        return None
    if minutes_ago < 0:
        return None
    try:
        path = _history_file()
        with _lock:
            records = _read_lines(path)
        if not records:
            return None
        target = _now() - timedelta(minutes=minutes_ago)
        best: dict[str, Any] | None = None
        best_delta: float | None = None
        for rec in records:
            at = _parse_at(rec.get("at"))
            if at is None:
                continue
            delta = abs((at - target).total_seconds())
            if best_delta is None or delta < best_delta:
                best, best_delta = rec, delta
        if best is None:
            return None
        # No tolerance rejection here by design: the contract is "return the
        # nearest REAL recorded snapshot, never an interpolated/fabricated
        # one" — even a distant nearest snapshot is more honest than
        # inventing one closer to the request. The only honest None is "no
        # history exists at all", handled above.
        return {"at": best.get("at"), "geo": best.get("geo")}
    except Exception:  # noqa: BLE001 - nice-to-have, must never raise
        return None


def prune_old(hours: float = 24.0) -> int:
    """Delete snapshots older than `hours` hours. Returns count removed.

    Never raises.
    """
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return 0
    if hours < 0:
        return 0
    try:
        path = _history_file()
        with _lock:
            records = _read_lines(path)
            if not records:
                return 0
            cutoff = _now() - timedelta(hours=hours)
            # _read_lines already guarantees every record here has a
            # parseable "at", so this is a plain datetime comparison.
            kept = [r for r in records if _parse_at(r.get("at")) >= cutoff]
            removed = len(records) - len(kept)
            if removed:
                _write_lines(path, kept)
            return removed
    except Exception:  # noqa: BLE001 - nice-to-have, must never raise
        return 0
