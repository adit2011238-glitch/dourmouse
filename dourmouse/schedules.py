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
    m = re.fullmatch(r"\s*(\d{1,2})\s*", text)  # a bare hour is the 24 hour clock: "8" is 08:00
    if m:
        if int(m.group(1)) > 23:
            raise ValueError("time must be a valid hour (0-23)")
        return f"{int(m.group(1)):02d}:00"
    raise ValueError(
        "time must look like '9:00', '09:00', '9am', '2pm' or just '8'"
    )


def parse_schedule(text: str, now: datetime | None = None) -> dict[str, Any]:
    """Deterministically parse a user schedule phrase into a spec dict.

    Returns {kind, time, weekday, interval_seconds}. Raises ValueError with
    the accepted shapes when the phrase is not understood (honest, Rule 2.2).
    Plan 2.2 adds "every weekday [at HH:MM]" (Monday to Friday) and the two
    one-time shapes used by reminders, "tomorrow at HH:MM" and "in N minutes|hours",
    which return kind "once" with the moment in ``when`` (``now`` is injectable).
    """
    s = (text or "").strip().lower()
    if not s:
        raise ValueError(
            "schedule must describe when: e.g. 'every Monday at 9:00', "
            "'daily at 8:30', 'every 30 minutes', 'weekly'"
        )

    # --- one time: "tomorrow at 5pm", "in 20 minutes" (reminders) ---
    m = re.fullmatch(r"tomorrow\s*(?:at\s+(.+))?", s)
    if m:
        hh, mm = _parse_clock(m.group(1) if m.group(1) else "09:00").split(":")
        base = (now or _now()) + timedelta(days=1)
        when = base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return {"kind": "once", "time": f"{hh}:{mm}", "weekday": None, "interval_seconds": None,
                "when": when.isoformat(timespec="minutes")}
    m = re.fullmatch(r"in\s+(\d+)\s+(minute|minutes|hour|hours)", s)
    if m:
        n = int(m.group(1))
        if n < 1 or n > 720:
            raise ValueError("a reminder in minutes or hours must be between 1 and 720")
        delta = timedelta(minutes=n) if m.group(2).startswith("minute") else timedelta(hours=n)
        when = (now or _now()).replace(second=0, microsecond=0) + delta
        return {"kind": "once", "time": when.strftime("%H:%M"), "weekday": None, "interval_seconds": None,
                "when": when.isoformat(timespec="minutes")}

    # --- "every weekday [at HH:MM]" (Monday to Friday) ---
    m = re.fullmatch(r"(?:every\s+weekday|weekdays)\s*(?:at\s+(.+))?", s)
    if m:
        time_s = _parse_clock(m.group(1)) if m.group(1) else "09:00"
        return {"kind": "weekdays", "time": time_s, "weekday": None, "interval_seconds": None}

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
    m = re.fullmatch(r"every\s+([a-z]+)\s*(?:at\s+(.+))?", s)
    # "mondays" is "monday" plus a plural s, but "tues" and "thurs" are
    # abbreviations that already end in s: try the word as written first.
    day = m.group(1) if m else ""
    if m and day not in _WEEKDAYS and day.endswith("s"):
        day = day[:-1]
    if m and day in _WEEKDAYS:
        time_s = _parse_clock(m.group(2)) if m.group(2) else "09:00"
        return {
            "kind": "weekday",
            "time": time_s,
            "weekday": _WEEKDAYS[day],
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
        "schedule not understood. Accepted shapes: 'every Monday at 9:00', 'every weekday at 8:00', "
        "'daily at 8:30', 'every 30 minutes', 'tomorrow at 5pm', 'in 20 minutes', 'weekly'. Nothing was scheduled."
    )


def describe_spec(spec: dict[str, Any]) -> str:
    """Human description of a parsed spec (used in list output)."""
    kind = spec.get("kind")
    if not kind:
        return ""  # an event trigger has no clock; describe_trigger words it
    if kind == "once":
        return f"once on {str(spec.get('when') or '').replace('T', ' ')}"
    if kind == "weekdays":
        return f"every weekday at {spec.get('time') or '09:00'}"
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
    if kind == "once":
        when = datetime.fromisoformat(str(spec.get("when")))
        return when if when > after else datetime.max.replace(microsecond=0)
    if kind == "weekdays":
        hh, mm = (spec.get("time") or "09:00").split(":")
        candidate = after.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        for _ in range(8):
            if candidate.weekday() < 5 and candidate > after:
                return candidate
            candidate += timedelta(days=1)
        raise RuntimeError("could not compute next run (internal)")
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


# Every load-modify-save of the schedule file runs under this lock: the runner
# thread's mark_run and the tool/UI edits from request threads used to read the
# same file and overwrite each other (finding P5-48).
_STORE_LOCK = threading.RLock()


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
        # Temp file then rename: a crash mid-write cannot leave a truncated
        # file that _load would silently shorten.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e) for e in entries]
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _next_id(entries: list[dict[str, Any]]) -> str:
        """One more than the highest numeric suffix in use, so an id is never
        reused after a removal (finding P5-46)."""
        highest = 0
        for e in entries:
            m = re.fullmatch(r"sched-(\d+)", str(e.get("id") or ""))
            if m:
                highest = max(highest, int(m.group(1)))
        return f"sched-{highest + 1:03d}"

    def add(self, tool: str, arguments: dict[str, Any], spec: dict[str, Any],
            schedule_text: str, created_at: str | None = None) -> dict[str, Any]:
        with _STORE_LOCK:
            entries = self._load()
            entry = {
                "id": self._next_id(entries),
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

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        return next((e for e in self._load() if e.get("id") == schedule_id), None)

    # ---- store v2 (plan 2.2, finding 207) --------------------------------------------------------------------
    # A v2 entry is the same JSONL row with more fields, so every v1 reader keeps working: ``tool`` and
    # ``arguments`` stay (a prompt action carries tool "automation_prompt"), and an entry without ``version`` is
    # a v1 entry that runs exactly as before.

    def add_v2(
        self, name: str, trigger: dict[str, Any], action: dict[str, Any], *, spec: dict[str, Any] | None = None,
        schedule_text: str = "", delivery: dict[str, Any] | None = None, caps: dict[str, Any] | None = None,
        template: str = "", created_at: str | None = None,
    ) -> dict[str, Any]:
        """A clean entry. ``trigger`` is {"type": "time"|"file_appears"|"security_finding"|"mail_match", ...}; a time
        trigger carries the parsed ``spec``. ``action`` is {"type": "tool", "tool", "arguments"} or
        {"type": "prompt", "prompt", "owner"}. Validation lives in automation_runs.clean_entry, which the API calls."""
        with _STORE_LOCK:
            entries = self._load()
            is_prompt = action.get("type") == "prompt"
            entry = {
                "id": self._next_id(entries),
                "version": 2,
                "name": name,
                "tool": "automation_prompt" if is_prompt else str(action.get("tool") or ""),
                "arguments": {"prompt": action.get("prompt", "")} if is_prompt else dict(action.get("arguments") or {}),
                "action": action,
                "trigger": trigger,
                "schedule_text": schedule_text,
                "spec": spec or {},
                "delivery": delivery or {"notify": True},
                "caps": caps or {},
                "template": template,
                "state": {"fail_count": 0, "runs_today": 0, "runs_day": "", "last_status": "", "last_run_id": ""},
                "created_at": created_at or _now().isoformat(timespec="seconds"),
                "last_run": None,
                "enabled": True,
            }
            entries.append(entry)
            self._save(entries)
            return entry

    def patch_state(self, schedule_id: str, **changes: Any) -> dict[str, Any] | None:
        """Merge ``changes`` into the entry's ``state`` under the store lock (the runner and the screen both write)."""
        with _STORE_LOCK:
            entries = self._load()
            hit = None
            for e in entries:
                if e.get("id") == schedule_id:
                    state = dict(e.get("state") or {})
                    state.update(changes)
                    e["state"] = state
                    hit = e
                    break
            if hit is not None:
                self._save(entries)
            return hit

    def count_run(self, schedule_id: str, now: datetime | None = None) -> int:
        """One more run today; returns the new count. The day rolls over by itself."""
        with _STORE_LOCK:
            entries = self._load()
            count = 0
            today = (now or _now()).date().isoformat()
            for e in entries:
                if e.get("id") == schedule_id:
                    state = dict(e.get("state") or {})
                    if state.get("runs_day") != today:
                        state["runs_day"], state["runs_today"] = today, 0
                    state["runs_today"] = int(state.get("runs_today") or 0) + 1
                    count = state["runs_today"]
                    e["state"] = state
                    break
            self._save(entries)
            return count

    def runs_today(self, entry: dict[str, Any], now: datetime | None = None) -> int:
        state = entry.get("state") or {}
        return int(state.get("runs_today") or 0) if state.get("runs_day") == (now or _now()).date().isoformat() else 0

    def record_outcome(self, schedule_id: str, ok: bool, status: str, *, max_failures: int = 3) -> dict[str, Any] | None:
        """Fold one finished run into the state. A success clears the failure count; the third failure in a row
        switches the automation off (it keeps its history and its place, and Resume turns it back on)."""
        with _STORE_LOCK:
            entries = self._load()
            hit = None
            for e in entries:
                if e.get("id") == schedule_id:
                    state = dict(e.get("state") or {})
                    state["last_status"] = status
                    state["fail_count"] = 0 if ok else int(state.get("fail_count") or 0) + 1
                    if state["fail_count"] >= max_failures and e.get("enabled"):
                        e["enabled"] = False
                        state["auto_disabled"] = True
                    e["state"] = state
                    hit = e
                    break
            if hit is not None:
                self._save(entries)
            return hit

    def remove(self, schedule_id: str) -> bool:
        with _STORE_LOCK:
            entries = self._load()
            kept = [e for e in entries if e.get("id") != schedule_id]
            if len(kept) == len(entries):
                return False
            self._save(kept)
            return True

    def mark_run(self, schedule_id: str, at: datetime | None = None) -> None:
        with _STORE_LOCK:
            entries = self._load()
            for e in entries:
                if e.get("id") == schedule_id:
                    e["last_run"] = (at or _now()).isoformat(timespec="seconds")
                    break
            self._save(entries)

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        """Pause/resume (2026-09-18, the TIMETABLE UI's one-click pause) --
        deliberately separate from remove(): a paused schedule keeps its
        id, its history (last_run), and its place in the file, so resuming
        it is not the same as recreating it from scratch. _tick_once
        already skips any entry where enabled is falsy, so this alone is
        the complete implementation -- no runner change needed."""
        with _STORE_LOCK:
            entries = self._load()
            changed = False
            for e in entries:
                if e.get("id") == schedule_id:
                    e["enabled"] = bool(enabled)
                    if enabled and isinstance(e.get("state"), dict):
                        e["state"] = {**e["state"], "fail_count": 0, "auto_disabled": False}
                    changed = True
                    break
            if changed:
                self._save(entries)
            return changed

    def update_spec(self, schedule_id: str, schedule_text: str) -> dict[str, Any]:
        """Domain C acceptance test 4 (2026-09-18): "editing a routine's
        schedule from the UI actually reschedules the real underlying
        job" -- the deliberately scoped edit this ships. Changes WHEN a
        routine runs, never WHAT it does: re-parses schedule_text through
        the exact same parse_schedule() the create path already uses and
        already validates, and replaces only spec/schedule_text, keeping
        id/tool/arguments/created_at/last_run/enabled untouched. Editing
        the tool or its arguments would need the same validation the
        create path already does (a real, existing, REGULAR-tier tool) --
        real, separate, deliberately not bundled into this one method so
        a schedule's own identity and history are never at risk of being
        silently replaced by a bad edit to something else entirely.

        Raises ValueError with the parser's own honest message on an
        unparseable schedule_text -- the SAME real validation the create
        path already gets, no double standard for an edit."""
        spec = parse_schedule(schedule_text)  # raises ValueError with an honest reason
        with _STORE_LOCK:
            entries = self._load()
            updated: dict[str, Any] | None = None
            for e in entries:
                if e.get("id") == schedule_id:
                    e["spec"] = spec
                    e["schedule_text"] = schedule_text
                    updated = e
                    break
            if updated is None:
                raise ValueError(f"no such schedule: {schedule_id}")
            self._save(entries)
            return updated


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
        engine: Any | None = None,
    ) -> None:
        self._engine_override = engine
        self._engine_cached: Any | None = None
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
            # One bad tick (a read error on the schedule file, a tracker that
            # raises) must not end the thread: every user schedule would stop
            # silently until the app restarted (finding P5-47).
            try:
                self._tick_once()
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive, and say why
                from dourmouse import obs

                obs.log_error(
                    source="scheduler", kind="tick_failed", what="schedule tick",
                    detail=f"{type(exc).__name__}: {exc}",
                )

    def _engine(self) -> Any:
        """The automation engine (plan 2.2), built on first use: v1 schedules never need it."""
        if self._engine_override is not None:
            return self._engine_override
        if self._engine_cached is None:
            from dourmouse.automation_runs import Engine

            self._engine_cached = Engine(self._store, self._registry, now_fn=self._now_fn)
        return self._engine_cached

    def _tick_v2(self, entry: dict[str, Any], now: datetime) -> None:
        """One automation: an event trigger is polled, a clock trigger is judged with the same ``_due`` as before.
        A failure in one automation is recorded as a failed run by the engine; it never stops the others."""
        from dourmouse import automation_triggers as triggers

        engine = self._engine()
        fired = ""
        if triggers.is_event(entry):
            events, change = triggers.poll(entry)
            if change:
                self._store.patch_state(entry["id"], **change)
            if events:
                fired = triggers.describe(entry, describe_spec)
        elif (entry.get("trigger") or {}).get("type", "time") == "time" and self._due(entry, now):
            fired = triggers.describe(entry, describe_spec)
        if not fired:
            return
        row = engine.run(entry, fired)
        self._store.mark_run(entry["id"], at=now)
        if (entry.get("spec") or {}).get("kind") == "once":
            self._store.set_enabled(entry["id"], False)  # a reminder happens once
        self._tracker.on_event({
            "type": "schedule", "name": entry.get("name") or entry.get("tool", "?"), "raw_arguments": "{}",
            "text": f"[{entry.get('id')} {row.get('status')}] {row.get('result', '')}"[:600],
        })

    def _tick_once(self) -> None:
        now = self._now_fn()
        try:
            self._engine().refresh_running()  # prompt runs finish later; read their jobs
        except Exception as exc:  # noqa: BLE001 - a job that cannot be read must not stop the schedules
            from dourmouse import obs

            obs.log_error(source="scheduler", kind="refresh_failed", what="automation jobs",
                          detail=f"{type(exc).__name__}: {exc}")
        for entry in self._store.list():
            if not entry.get("enabled"):
                continue
            if entry.get("version") == 2:
                try:
                    self._tick_v2(entry, now)
                except Exception as exc:  # noqa: BLE001
                    from dourmouse import obs

                    obs.log_error(source="scheduler", kind="automation_failed", what=str(entry.get("id")),
                                  detail=f"{type(exc).__name__}: {exc}")
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
                    pass  # a broken bus must never break the schedule loop

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
            # Finding #137: through the one choke point every other tool call
            # uses, so the pre-tool hooks, required-argument checks, the run
            # policy and the action ledger apply to an unattended run too, and
            # the result is scrubbed of credentials like any other tool result.
            from dourmouse.dispatch import _execute_tool
            from dourmouse.execution_policy import RunPolicy
            from dourmouse.governance import DlpFilter

            text = _execute_tool(spec, args, lambda _prompt: False, ledger=None, policy=RunPolicy(actor="scheduler"))
            return DlpFilter().redact(str(text))[0]
        except Exception as exc:  # honest failure, never a fabricated result
            return f"SCHEDULE RUN FAILED (reported honestly): {exc}"


def describe_next_run(entry: dict[str, Any], now_fn: Callable[[], datetime] = _now) -> str:
    """'next run' description for list output (used by the tools)."""
    try:
        spec = entry.get("spec") or {}
        if not spec.get("kind"):
            return ""  # an event trigger has no next time
        after = datetime.fromisoformat(str(entry.get("last_run"))) if entry.get("last_run") else (
            datetime.fromisoformat(str(entry.get("created_at"))) if entry.get("created_at") else now_fn()
        )
        nxt = next_run(spec, after)
        return "" if nxt.year >= 9999 else nxt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown"
