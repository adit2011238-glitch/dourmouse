"""Structured observability — errors, agent calls, and timings as JSONL.

Before this, failures were strings returned to the model and printed to
stdout, which meant a bug report was "it said 404" with no way to find the
request behind it. Three append-only JSONL logs fix that:

    logs/errors.log   — every handled failure, with transport detail
    logs/agents.log   — every tool invocation and its outcome
    logs/perf.log     — durations, for latency baselines

JSONL because it is append-safe under concurrent writers, greppable, and
parseable without a schema migration when fields get added.

Design constraints, all deliberate:
  * Stdlib only, like the rest of the package.
  * Never raises. Observability that can break the request path is worse
    than no observability, so every write is best-effort.
  * Bounded: files rotate at a size cap so a long-running desktop install
    cannot fill the disk.
  * Off-by-default fields stay absent rather than null, keeping lines small.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "log_error",
    "log_agent_call",
    "log_perf",
    "timed",
    "logs_dir",
    "read_recent",
]

# One lock per process. Appends of a single short line are effectively atomic
# on POSIX, but the lock also serialises the rotation check, which is not.
_LOCK = threading.Lock()

_MAX_BYTES = 5 * 1024 * 1024  # rotate at 5 MB
_KEEP = 2                     # keep .1 and .2 alongside the live file


def logs_dir() -> Path:
    """Directory for log files, honouring DOURMOUSE_LOG_DIR."""
    override = os.environ.get("DOURMOUSE_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "logs"


def _enabled() -> bool:
    """Logging is on unless explicitly disabled (tests set this)."""
    return os.environ.get("DOURMOUSE_OBS_DISABLED", "").strip() not in ("1", "true", "TRUE")


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= _MAX_BYTES:
            for i in range(_KEEP, 0, -1):
                src = path.with_suffix(path.suffix + f".{i}")
                dst = path.with_suffix(path.suffix + f".{i + 1}")
                if i == _KEEP and src.exists():
                    src.unlink()
                elif src.exists():
                    src.rename(dst)
            path.rename(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass  # rotation is best-effort; a failed rotate must not break logging


def _write(filename: str, payload: dict[str, Any]) -> None:
    """Append one JSON line. Never raises."""
    if not _enabled():
        return
    try:
        payload = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
        line = json.dumps(payload, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    try:
        d = logs_dir()
        with _LOCK:
            d.mkdir(parents=True, exist_ok=True)
            path = d / filename
            _rotate_if_needed(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        return


def log_error(
    *,
    source: str,
    kind: str,
    what: str,
    detail: str,
    status: int | None = None,
    retryable: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a handled failure with the detail chat no longer shows."""
    payload: dict[str, Any] = {
        "source": source,
        "kind": kind,
        "what": what,
        "detail": detail[:2000],
    }
    if status is not None:
        payload["status"] = status
    if retryable is not None:
        payload["retryable"] = retryable
    if extra:
        payload["extra"] = extra
    _write("errors.log", payload)


def log_agent_call(
    *,
    tool: str,
    agent: str = "",
    ok: bool,
    duration_ms: float | None = None,
    detail: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a tool invocation and whether it succeeded."""
    payload: dict[str, Any] = {"tool": tool, "ok": ok}
    if agent:
        payload["agent"] = agent
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 1)
    if detail:
        payload["detail"] = detail[:500]
    if extra:
        payload["extra"] = extra
    _write("agents.log", payload)


def log_perf(*, op: str, duration_ms: float, extra: dict[str, Any] | None = None) -> None:
    """Record how long something took, for latency baselines."""
    payload: dict[str, Any] = {"op": op, "duration_ms": round(duration_ms, 1)}
    if extra:
        payload["extra"] = extra
    _write("perf.log", payload)


@contextmanager
def timed(op: str, *, extra: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Time a block and write it to perf.log, including on failure.

    Yields a dict the caller may add fields to; they are merged into `extra`.
    The timing is recorded whether or not the block raises, so a slow failure
    is as visible as a slow success.
    """
    scratch: dict[str, Any] = {}
    start = time.perf_counter()
    ok = True
    try:
        yield scratch
    except BaseException:
        ok = False
        raise
    finally:
        elapsed = (time.perf_counter() - start) * 1000.0
        merged = {**(extra or {}), **scratch, "ok": ok}
        log_perf(op=op, duration_ms=elapsed, extra=merged)


def read_recent(filename: str, limit: int = 50) -> list[dict[str, Any]]:
    """Read back the last `limit` parsed lines. Used by tests and diagnostics."""
    path = logs_dir() / filename
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
