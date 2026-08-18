"""schedules.py — user-defined recurring workflows (v5.x).

The fixed live poll table (live_runtime.py) covers the built-in agents;
this module is the USER-DEFINED half: "do this every Monday". The model
maps the user's intent to a concrete tool call ONCE at schedule time —
deterministic after that (Rule 2.8: no LLM in the runner). Schedules
persist as JSONL under <workspace>/schedules.jsonl and survive restarts.

Accepted schedule phrases (deterministic parser, no LLM):
  "every <weekday>[s] [at HH:MM]"     e.g. "every Monday at 9:00", "every fri at 2pm"
  "every day at HH:MM" / "daily [at HH:MM]" / "at HH:MM"
  "every N minutes|hours|days"        e.g. "every 30 minutes"
  "weekly [at HH:MM]"                 same weekday + time (default Mon 09:00)
Anything else is rejected with the accepted shapes listed honestly.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

_WORKSPACE_ENV = "DOURMOUSE_WORKSPACE"
_SCHEDULE_FILE = "schedules.jsonl"
_DEFAULT_TICK_SECONDS = 15.0

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _workspace_root() -> Path:
    """Same resolution as general_roster (kept local to avoid a circular
    import — general_roster imports the schedule tools)."""
    raw = os.environ.get(_WORKSPACE_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent.parent / "workspace"


def _parse_clock(text: str) -> str:
    """Parse '9:00', '09:00', '9am', '2pm' -> 'HH:MM' (24h), or None."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*(am|pm)?\s*", text, re.I)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        if minute > 59 or hour > 23:
            raise ValueError("time must be a valid HH:MM (minutes 0-59, hours 0-23)")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour > 23:
            raise ValueError("time must be a valid HH:MM")
        return f"{hour:02d}:{minute:02d}"
    m = re.fullmatch(r"\s*(\d{1,2})\s*(am|pm)\s*", text, re.I)
    if m:
        hour = int(m.group(1))
        if hour > 12 or hour < 1:
            raise ValueError("time must be a valid 12h clock")
        ampm = m.group(2).lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:00"
    raise ValueError(
        "time must look like '9:00', '09:00', '9am' or '2pm'"
    )


def parse_schedule(text: str) -> dict[str, Any]:
    """Deterministically parse a user schedule phrase into a spec dict.

    Returns {kind, time, weekday, interval_seconds}. Raises ValueError with
    the accepted shapes when the phrase is not understood (honest, Rule 2.2).
    """
    s = (text or "").strip().lower()
    if not s:
        raise ValueError(
            "schedule must describe when: e.g. 'every Monday at 9:00', "
            "'daily at 8:30', 'every 30 minutes', 'weekly'"
        )

    # --- interval: "every N minutes|hours|days" ---
    m = re.fullmatch(
        r"every\s+(\d+)\s+(minute|minutes|hour|hours|day|days)", s
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if n < 1:
            raise ValueError("the interval must be at least 1")
        mult = 60 if unit.startswith("minute") else 3600 if unit.startswith("hour") else 86400
        if unit.startswith("day") and n > 31:
            raise ValueError("daily intervals longer than 31 days are not supported")
        return {
            "kind": "interval",
            "time": None,
            "weekday": None,
            "interval_seconds": n * mult,
        }

    # --- "every <weekday>[s] [at HH:MM]" ---
    m = re.fullmatch(r"every\s+([a-z]+)s?\s*(?:at\s+(.+))?", s)
    if m and m.group(1) in _WEEKDAYS:
        time_s = _parse_clock(m.group(2)) if m.group(2) else "09:00"
        return {
            "kind": "weekday",
            "time": time_s,
            "weekday": _WEEKDAYS[m.group(1)],
            "interval_seconds": None,
        }

    # --- "weekly [at HH:MM]" ---
    m = re.fullmatch(r"weekly\s*(?:at\s+(.+))?", s)
    if m:
        time_s = _parse_clock(m.group(1)) if m.group(1) else "09:00"
        return {"kind": "weekday", "time": time_s, "weekday": 0, "interval_seconds": None}

    # --- "every day [at HH:MM]" / "daily [at HH:MM]" / "at HH:MM" ---
    m = re.fullmatch(r"(?:every\s+day|daily)\s*(?:at\s+(.+))?", s)
    if m:
        time_s = _parse_clock(m.group(1)) if m.group(1) else "09:00"
        return {"kind": "daily", "time": time_s, "weekday": None, "interval_seconds": None}
    m = re.fullmatch(r"at\s+(.+)", s)
    if m:
        time_s = _parse_clock(m.group(1))
        return {"kind": "daily", "time": time_s, "weekday": None, "interval_seconds": None}

    raise ValueError(
        "schedule not understood. Accepted shapes: 'every Monday at 9:00', "
        "'daily at 8:30', 'every 30 minutes', 'weekly'. Nothing was scheduled."
    )


def describe_spec(spec: dict[str, Any]) -> str:
    """Human description of a parsed spec (used in list output)."""
    kind = spec.get("kind")
    if kind == "interval":
        secs = int(spec.get("interval_seconds") or 0)
        if secs % 86400 == 0:
            return f"every {secs // 86400} day(s)"
        if secs % 3600 == 0:
            return f"every {secs // 3600} hour(s)"
        return f"every {secs // 60} minute(s)"
    t = spec.get("time") or "09:00"
    if kind == "weekday":
        name = next(k for k, v in _WEEKDAYS.items() if v == spec.get("weekday"))
        return f"every {name.title()} at {t}"
    return f"daily at {t}"


def next_run(spec: dict[str, Any], after: datetime) -> datetime:
    """Next occurrence of ``spec`` strictly after ``after`` (local time)."""
    kind = spec.get("kind")
    if kind == "interval":
        return after + timedelta(seconds=int(spec.get("interval_seconds") or 0))
    hh, mm = (spec.get("time") or "09:00").split(":")
    hour, minute = int(hh), int(mm)
    if kind == "daily":
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate
    if kind == "weekday":
        target = int(spec.get("weekday") or 0)
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for _ in range(8):
            if candidate.weekday() == target and candidate > after:
                return candidate
            candidate += timedelta(days=1)
        raise RuntimeError("could not compute next run (internal)")
    raise ValueError(f"unknown schedule kind: {kind}")


def _now() -> datetime:
    return datetime.now()


class Schedules:
    """JSONL-backed store of user schedules (workspace/schedules.jsonl)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else (
            _workspace_root() / _SCHEDULE_FILE
        )

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a corrupt line never breaks the store (Rule 2.2)
        return out

    def _save(self, entries: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e) for e in entries]
        self._path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def add(self, tool: str, arguments: dict[str, Any], spec: dict[str, Any],
            schedule_text: str, created_at: str | None = None) -> dict[str, Any]:
        entries = self._load()
        entry = {
            "id": f"sched-{len(entries) + 1:03d}",
            "tool": tool,
            "arguments": arguments,
            "schedule_text": schedule_text,
            "spec": spec,
            "created_at": created_at or _now().isoformat(timespec="seconds"),
            "last_run": None,
            "enabled": True,
        }
        entries.append(entry)
        self._save(entries)
        return entry

    def list(self) -> list[dict[str, Any]]:
        return self._load()

    def remove(self, schedule_id: str) -> bool:
        entries = self._load()
        kept = [e for e in entries if e.get("id") != schedule_id]
        if len(kept) == len(entries):
            return False
        self._save(kept)
        return True

    def mark_run(self, schedule_id: str, at: datetime | None = None) -> None:
        entries = self._load()
        for e in entries:
            if e.get("id") == schedule_id:
                e["last_run"] = (at or _now()).isoformat(timespec="seconds")
                break
        self._save(entries)


class SchedulerRunner:
    """Background thread that runs user schedules when they come due.

    Same honesty contract as LiveRuntime: real tool handlers only (or an
    injected fetcher for tests), failures reported, never fabricated. A
    missed run (app was closed) fires once on the next tick — catch-up,
    not compounding.
    """

    def __init__(
        self,
        registry: Any,
        tracker: Any,
        *,
        store: Schedules | None = None,
        tick: float = _DEFAULT_TICK_SECONDS,
        fetcher: Callable[[str, dict[str, Any]], str] | None = None,
        now_fn: Callable[[], datetime] = _now,
        bus: Any | None = None,
    ) -> None:
        self._registry = registry
        self._tracker = tracker
        self._store = store or Schedules()
        self._tick = max(1.0, float(tick))
        self._fetcher = fetcher
        self._now_fn = now_fn
        self._bus = bus
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="scheduler-runner"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._tick):
            self._tick_once()

    def _tick_once(self) -> None:
        now = self._now_fn()
        for entry in self._store.list():
            if not entry.get("enabled"):
                continue
            try:
                if not self._due(entry, now):
                    continue
            except Exception as exc:  # malformed entry — report, don't crash the loop
                self._tracker.on_event({
                    "type": "schedule",
                    "name": entry.get("tool", "?"),
                    "raw_arguments": "{}",
                    "text": f"SCHEDULE SKIPPED (reported honestly): {exc}"[:600],
                })
                continue
            result = self._run_one(entry)
            self._store.mark_run(entry["id"], at=now)
            self._tracker.on_event({
                "type": "schedule",
                "name": entry.get("tool", "?"),
                "raw_arguments": json.dumps(entry.get("arguments") or {}),
                "text": (f"[{entry.get('id')} due] {result}")[:600],
            })
            if self._bus is not None:
                try:
                    self._bus.post(
                        from_agent="scheduler",
                        to_agent="*",
                        subject=f"schedule:{entry.get('tool')}",
                        body=(f"[{entry.get('id')}] {result}")[:600],
                    )
                except Exception:
                    pass

    def _due(self, entry: dict[str, Any], now: datetime) -> bool:
        spec = entry.get("spec") or {}
        # Interval tasks fire immediately at schedule time (same convention
        # as the LiveRuntime poll loops), then every interval after.
        if spec.get("kind") == "interval" and not entry.get("last_run"):
            return True
        after = now
        if entry.get("last_run"):
            after = datetime.fromisoformat(str(entry["last_run"]))
        else:
            created = entry.get("created_at")
            if created:
                try:
                    after = datetime.fromisoformat(str(created))
                except ValueError:
                    after = now
        return next_run(spec, after) <= now

    def _run_one(self, entry: dict[str, Any]) -> str:
        tool = str(entry.get("tool") or "")
        args = entry.get("arguments") or {}
        try:
            if self._fetcher is not None:
                return str(self._fetcher(tool, args))
            spec = self._registry.lookup(tool)
            if spec is None:
                return f"ERROR: no such tool: {tool} (was it removed?)"
            # Defense in depth: _schedule_recurring_tool already refuses to
            # create a schedule for a non-REGULAR tool, but this is the fail-
            # safe for anything that reaches the store another way (an entry
            # written before that check existed, a tool re-tiered to
            # REQUIRES_CONFIRMATION/PROHIBITED after it was scheduled, a
            # future caller of Schedules.add() that skips the tool helper).
            # The runner fires unattended — no human is present to answer a
            # confirmation prompt — so calling spec.handler() directly here
            # would silently execute a gated tool with no gate at all. Fail
            # closed and say so honestly (Rule 2.2), the same shape
            # _execute_tool uses when no confirmation_gate is attached.
            if spec.permission.value != "regular":
                return (
                    f"CONFIRMATION REQUIRED: {tool} requires human confirmation "
                    f"({spec.permission.value}) and cannot run on an unattended "
                    "schedule; NOT executed. Disable this schedule or point it "
                    "at a regular-tier tool."
                )
            return str(spec.handler(args))
        except Exception as exc:  # honest failure, never a fabricated result
            return f"SCHEDULE RUN FAILED (reported honestly): {exc}"


def describe_next_run(entry: dict[str, Any], now_fn: Callable[[], datetime] = _now) -> str:
    """'next run' description for list output (used by the tools)."""
    try:
        spec = entry.get("spec") or {}
        after = datetime.fromisoformat(str(entry.get("last_run"))) if entry.get("last_run") else (
            datetime.fromisoformat(str(entry.get("created_at"))) if entry.get("created_at") else now_fn()
        )
        return next_run(spec, after).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown"
