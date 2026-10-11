"""Real, persisted usage accounting — Claude (real cost + tokens, from
the Claude Code CLI's own real result event) and Ollama (real request +
token counts, local or Ollama Cloud) — v13.6.

Explicit user request: "add a usage bar that indicates how much usage
you have used on claude and ollama api key." Honest scope:

- **Claude: real, exact.** ``code_backends.stream_claude``'s ``on_usage``
  callback (see that module) now surfaces the CLI's own real ``result``
  event fields — ``total_cost_usd``, ``usage.input_tokens``,
  ``usage.output_tokens``, cache read/creation tokens — live-verified
  against one real ``claude -p`` call this session before this module
  was written. Never estimated, never fabricated: whatever the CLI
  didn't report for a given call simply isn't counted for it.
- **Ollama: real, tokens only, no dollar cost.** Local Ollama has no
  cost at all; Ollama Cloud (``OLLAMA_API_KEY``, see ``config.py``)
  bills per-request but exposes no usage/cost endpoint this codebase
  has found — so this tracks real prompt/completion TOKEN counts
  (``dispatch._usage_of``'s existing, already-correct extraction) and a
  real request count, honestly with no ``$`` figure attached, rather
  than inventing a per-token price that isn't real.

Persisted (survives a server restart — a "usage bar" that resets to
zero every boot would be a running total pretending not to be one) as
a small JSON file under ``config.user_config_dir()``, the same
per-user, survives-updates location every other piece of Dourmouse
config uses. Every write is a real, atomic read-modify-write under one
process-wide lock — concurrent chat turns from different panels must
never lose a count to a race.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dourmouse.state_file import atomic_write_text, load_json_or_aside

_log = logging.getLogger(__name__)
_LOCK = threading.Lock()

_EMPTY_TOTALS: dict[str, Any] = {
    "claude": {
        "requests": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    },
    "ollama": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0},
}


def _usage_path() -> Path:
    from dourmouse.config import user_config_dir

    return user_config_dir() / "usage.json"


def _normalize(raw: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(_EMPTY_TOTALS))  # cheap deep copy
    if not isinstance(raw, dict):
        return out
    for backend in ("claude", "ollama"):
        entry = raw.get(backend)
        if not isinstance(entry, dict):
            continue
        for key, default in out[backend].items():
            value = entry.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[backend][key] = value
    return out


def _load(path: Path) -> dict[str, Any]:
    """Real read of the persisted totals, for READING. A missing/corrupt file
    is honestly reported as "nothing recorded yet" (Rule 2.2) -- never a
    crash, never silently invented nonzero numbers -- and is left untouched
    here: only a writer moves an unreadable file aside (_load_for_update)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    return _normalize(raw)


def _load_for_update(path: Path) -> dict[str, Any]:
    """The totals to add to. A file that exists but cannot be read is kept
    aside as ``usage.json.corrupt-<time>`` (not zeroed by the write that
    follows) and the failure is logged."""
    raw, note = load_json_or_aside(path, dict)
    if note:
        _log.error("usage_tracker: %s", note)
        if path.exists():
            # It could not be moved aside: writing now would overwrite it.
            raise OSError(note)
    return _normalize(raw)


def _save(path: Path, totals: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(totals, indent=2))


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-process lock for the read-modify-write (the Electron shell's
    server and a CLI can both record). ``fcntl.flock`` on a sibling lock file;
    a no-op where fcntl is missing."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _num(value: Any, cast: Any) -> Any:
    """``cast(value)``, or 0 for a null / non-numeric field: one bad field in a
    usage report must not discard the whole call's count."""
    if isinstance(value, bool) or value is None:
        return cast(0)
    try:
        return cast(value)
    except (TypeError, ValueError):
        return cast(0)


def record_claude_usage(usage: dict[str, Any]) -> None:
    """Add one real Claude call's usage (from stream_claude's on_usage
    callback) to the persisted running total. Wrapped so a disk/JSON
    failure can never break the chat turn it's tracking — the same
    "observability must never break the thing it observes" discipline
    ActivityTracker.on_event and _maybe_ingest_memory already follow."""
    try:
        path = _usage_path()
        with _LOCK, _file_lock(path):
            totals = _load_for_update(path)
            c = totals["claude"]
            c["requests"] += 1
            c["cost_usd"] = round(c["cost_usd"] + _num(usage.get("cost_usd"), float), 6)
            for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                c[key] += _num(usage.get(key), int)
            _save(path, totals)
    except Exception as exc:  # noqa: BLE001 - usage tracking must never break a real chat turn
        _log.warning("usage_tracker: could not record a Claude call: %s", exc)


def record_ollama_usage(usage: dict[str, int]) -> None:
    """Same real, non-breaking contract as record_claude_usage, for the
    Ollama/local-agent path (dispatch._usage_of's existing extraction —
    prompt_tokens/completion_tokens, whichever the backend reported)."""
    try:
        path = _usage_path()
        with _LOCK, _file_lock(path):
            totals = _load_for_update(path)
            o = totals["ollama"]
            o["requests"] += 1
            o["prompt_tokens"] += _num(usage.get("prompt_tokens"), int)
            o["completion_tokens"] += _num(usage.get("completion_tokens"), int)
            _save(path, totals)
    except Exception as exc:  # noqa: BLE001 - usage tracking must never break a real chat turn
        _log.warning("usage_tracker: could not record an Ollama call: %s", exc)


def get_totals() -> dict[str, Any]:
    """Real, current persisted totals — {"claude": {...}, "ollama": {...}}.
    Never raises; an unreadable/missing file reports the honest all-zero
    starting state (Rule 2.2), same as _load's own contract."""
    try:
        return _load(_usage_path())
    except Exception:  # noqa: BLE001 - a status read must never itself crash
        return json.loads(json.dumps(_EMPTY_TOTALS))
