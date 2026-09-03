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

import json
import threading
from pathlib import Path
from typing import Any

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


def _load(path: Path) -> dict[str, Any]:
    """Real read of the persisted totals. A missing/corrupt file is
    honestly treated as "nothing recorded yet" (Rule 2.2) — never a
    crash, never silently invented nonzero numbers."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    out = json.loads(json.dumps(_EMPTY_TOTALS))  # cheap deep copy
    for backend in ("claude", "ollama"):
        entry = raw.get(backend)
        if not isinstance(entry, dict):
            continue
        for key, default in out[backend].items():
            value = entry.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[backend][key] = value
    return out


def _save(path: Path, totals: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(totals, indent=2), encoding="utf-8")


def record_claude_usage(usage: dict[str, Any]) -> None:
    """Add one real Claude call's usage (from stream_claude's on_usage
    callback) to the persisted running total. Wrapped so a disk/JSON
    failure can never break the chat turn it's tracking — the same
    "observability must never break the thing it observes" discipline
    ActivityTracker.on_event and _maybe_ingest_memory already follow."""
    try:
        path = _usage_path()
        with _LOCK:
            totals = _load(path)
            c = totals["claude"]
            c["requests"] += 1
            c["cost_usd"] = round(c["cost_usd"] + float(usage.get("cost_usd", 0.0)), 6)
            for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                c[key] += int(usage.get(key, 0))
            _save(path, totals)
    except Exception:  # noqa: BLE001 - usage tracking must never break a real chat turn
        pass


def record_ollama_usage(usage: dict[str, int]) -> None:
    """Same real, non-breaking contract as record_claude_usage, for the
    Ollama/local-agent path (dispatch._usage_of's existing extraction —
    prompt_tokens/completion_tokens, whichever the backend reported)."""
    try:
        path = _usage_path()
        with _LOCK:
            totals = _load(path)
            o = totals["ollama"]
            o["requests"] += 1
            o["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
            o["completion_tokens"] += int(usage.get("completion_tokens", 0))
            _save(path, totals)
    except Exception:  # noqa: BLE001 - usage tracking must never break a real chat turn
        pass


def get_totals() -> dict[str, Any]:
    """Real, current persisted totals — {"claude": {...}, "ollama": {...}}.
    Never raises; an unreadable/missing file reports the honest all-zero
    starting state (Rule 2.2), same as _load's own contract."""
    try:
        return _load(_usage_path())
    except Exception:  # noqa: BLE001 - a status read must never itself crash
        return json.loads(json.dumps(_EMPTY_TOTALS))
