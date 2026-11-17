"""Automations: run history, caps, parking, templates and the engine that runs one (plan 2.2, finding 207).

An automation is a ``schedules.py`` v2 row: a trigger (a clock, a new file, a security finding), an action and
delivery. This module owns what happens when it fires:

* **Actions.** ``reminder`` shows a line and needs no model. ``tool`` runs one tool with fixed arguments. ``prompt``
  starts an agent job (``jobs.py``) for one prompt, so the job board, its step budget and its approval cards are the
  ones that already exist. Nothing here calls a model itself.
* **Caps count runs and steps, not money** (models run on the Ollama Cloud key and the Claude CLI login).
  ``max_runs_per_day`` stops automatic runs for the rest of the day; ``max_steps`` is the step and tool-call budget
  of a prompt job. A capped run is recorded and the owner is told once a day. Run now is the owner's own click and
  is counted but not refused.
* **Parking.** A tool that needs confirmation never runs unattended. The run is recorded as parked, one needs-you
  item goes to the feed, and Review in the screen runs it once on the owner's explicit approval.
* **Three failures in a row** switch the automation off and say so. A success clears the count.
* **Outcomes** reach the owner through the attention feed (never with the run's text in a native notification: the
  feed's own rules apply) and the run history, kept 90 days.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dourmouse import attention

SOURCE = "automations"
SCREEN = "timetable"
MAX_FAILURES = 3
HISTORY_DAYS = 90
MAX_ROWS = 5000
MAX_NAME = 60
MAX_PROMPT = 600
MAX_REMINDER = 200
DEFAULT_CAPS = {"max_runs_per_day": 6, "max_steps": 8}
CAP_CEILING = {"max_runs_per_day": 48, "max_steps": 24}
_HISTORY_FILE = "automation_runs.jsonl"
#: What a tool returns when it did not do the work. Anything else is a result.
_FAIL_PREFIXES = ("ERROR", "REFUSED", "BLOCKED", "CONFIRMATION REQUIRED", "SCHEDULE RUN FAILED", "TOOL ERROR")
#: Words for the screen's one-word result.
RESULT_WORDS = {"ok": "Done", "failed": "Failed", "parked": "Needs you", "running": "Running", "capped": "Stopped at limit",
                "skipped": "Skipped"}

#: The five starting points. ``when`` is parsed by the same schedule parser the create sheet uses.
TEMPLATES: list[dict[str, Any]] = [
    {"id": "morning_brief", "name": "Morning brief", "when": "every weekday at 7:30",
     "trigger": {"type": "time"}, "action": {"type": "tool", "tool": "daily_digest", "arguments": {}}},
    {"id": "daily_security", "name": "Daily security check", "when": "daily at 18:00",
     "trigger": {"type": "time"},
     "action": {"type": "prompt", "owner": "security",
                "prompt": "Check this Mac's security status and tell me about anything new since yesterday. Do not change anything."}},
    {"id": "downloads_tidy", "name": "Downloads tidy-up", "when": "",
     "trigger": {"type": "file_appears", "folder": "~/Downloads", "pattern": "*"},
     "action": {"type": "prompt", "owner": "mac_operator",
                "prompt": "A new file arrived in Downloads. Say where it should be filed. Do not move or delete anything."}},
    {"id": "weekly_research", "name": "Weekly research digest", "when": "every Monday at 8:00",
     "trigger": {"type": "time"},
     "action": {"type": "prompt", "owner": "researcher",
                "prompt": "Summarise what is new this week in the topics of my active research. Cite sources."}},
    {"id": "bills_deadlines", "name": "Bill and deadline reminders", "when": "every weekday at 9:00",
     "trigger": {"type": "time"},
     "action": {"type": "prompt", "owner": "mail_calendar",
                "prompt": "List bills and deadlines due in the next 7 days from my calendar and tasks. Do not send anything."}},
]


# ---- validation ----------------------------------------------------------------------------------------------


def clean_caps(raw: Any, base: dict[str, int] | None = None) -> dict[str, int]:
    out = dict(base or DEFAULT_CAPS)
    if raw in (None, {}):
        return out
    if not isinstance(raw, dict):
        raise ValueError("limits must be an object")
    for key, value in raw.items():
        if key not in DEFAULT_CAPS:
            raise ValueError(f"unknown limit {key!r} (allowed: {', '.join(DEFAULT_CAPS)})")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value < 1:
            raise ValueError(f"{key} must be a whole number of at least 1")
        if value > CAP_CEILING[key]:
            raise ValueError(f"{key} is at most {CAP_CEILING[key]}")
        out[key] = int(value)
    return out


def clean_action(raw: Any, registry: Any = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("an action is required")
    kind = str(raw.get("type") or "").strip()
    if kind == "reminder":
        text = " ".join(str(raw.get("text") or "").split())
        if not text:
            raise ValueError("a reminder needs its words")
        if len(text) > MAX_REMINDER:
            raise ValueError(f"a reminder is at most {MAX_REMINDER} characters")
        return {"type": "reminder", "text": text}
    if kind == "tool":
        tool = str(raw.get("tool") or "").strip()
        args = raw.get("arguments") or {}
        if not tool:
            raise ValueError("choose the tool to run")
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
        if registry is not None:
            spec = registry.lookup(tool)
            if spec is None:
                raise ValueError(f"no tool called {tool!r}")
            if spec.permission.value == "prohibited":
                raise ValueError(f"{tool} is never allowed to run unattended")
        return {"type": "tool", "tool": tool, "arguments": args}
    if kind == "prompt":
        from dourmouse.agent_profiles import resolve

        text = " ".join(str(raw.get("prompt") or "").split())
        if not text:
            raise ValueError("write what the automation should do")
        if len(text) > MAX_PROMPT:
            raise ValueError(f"the prompt is at most {MAX_PROMPT} characters")
        owner = str(raw.get("owner") or "orchestrator").strip()
        resolved = resolve(owner)
        if resolved is None:
            raise ValueError(f"no agent called {owner!r}")
        return {"type": "prompt", "prompt": text, "owner": resolved.agent_id}
    raise ValueError("an action is a reminder, a tool or a prompt")


def clean_name(raw: Any, fallback: str) -> str:
    name = " ".join(str(raw or "").split()) or fallback
    if len(name) > MAX_NAME:
        raise ValueError(f"the name is at most {MAX_NAME} characters")
    return name


def action_words(action: dict[str, Any]) -> str:
    kind = action.get("type")
    if kind == "reminder":
        return f"Remind: {action.get('text', '')}"
    if kind == "tool":
        return str(action.get("tool") or "").replace("_", " ").capitalize()
    return str(action.get("prompt") or "")[:140]


def tools_it_may_use(action: dict[str, Any]) -> str:
    """The plain answer to "what can this touch". A prompt run keeps its agent's own tool list and every gated step
    still parks for approval."""
    kind = action.get("type")
    if kind == "reminder":
        return "Nothing; it only shows a line."
    if kind == "tool":
        return f"Only {str(action.get('tool') or '').replace('_', ' ')}."
    return "Its agent's tools. Anything that needs confirmation waits for you."


# ---- history ------------------------------------------------------------------------------------------------


class RunHistory:
    """Append-mostly JSONL of runs under the workspace, trimmed to 90 days and 5000 rows."""

    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        from dourmouse.config import workspace_dir

        self._path = Path(path) if path is not None else workspace_dir() / _HISTORY_FILE
        self._clock = clock
        self._lock = threading.RLock()
        self._last_trim = 0.0

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        rows = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # one damaged line never loses the rest
        return rows

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def add(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rows = self._load()
            rows.append(row)
            now = self._clock()
            if now - self._last_trim > 3600 or len(rows) > MAX_ROWS:
                cutoff = now - HISTORY_DAYS * 86400
                rows = [r for r in rows if float(r.get("ts") or now) >= cutoff][-MAX_ROWS:]
                self._last_trim = now
            self._save(rows)
            return row

    def update(self, run_id: str, **changes: Any) -> dict[str, Any] | None:
        with self._lock:
            rows = self._load()
            hit = None
            for r in rows:
                if r.get("id") == run_id:
                    r.update(changes)
                    hit = r
                    break
            if hit is not None:
                self._save(rows)
            return hit

    def get(self, run_id: str) -> dict[str, Any] | None:
        return next((r for r in self._load() if r.get("id") == run_id), None)

    def for_automation(self, automation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = [r for r in self._load() if r.get("automation_id") == automation_id]
        return list(reversed(rows))[: max(1, min(int(limit), 200))]

    def last(self, automation_id: str) -> dict[str, Any] | None:
        rows = self.for_automation(automation_id, 1)
        return rows[0] if rows else None

    def running(self) -> list[dict[str, Any]]:
        return [r for r in self._load() if r.get("status") in ("running", "parked") and r.get("job_id")]

    def forget(self, automation_id: str) -> int:
        with self._lock:
            rows = self._load()
            kept = [r for r in rows if r.get("automation_id") != automation_id]
            if len(kept) != len(rows):
                self._save(kept)
            return len(rows) - len(kept)


# ---- the engine ---------------------------------------------------------------------------------------------


def _default_tool_runner(registry: Any) -> Callable[[Any, dict[str, Any], bool], str]:
    def run(spec: Any, args: dict[str, Any], approved: bool) -> str:
        from dourmouse.dispatch import _execute_tool
        from dourmouse.execution_policy import RunPolicy
        from dourmouse.governance import DlpFilter

        text = _execute_tool(spec, args, lambda _prompt: approved, ledger=None, policy=RunPolicy(actor="automation"))
        return DlpFilter().redact(str(text))[0]

    return run


def _default_job_starter(prompt: str, owner: str, name: str, max_steps: int) -> str:
    from dourmouse import jobs

    goal = jobs.create_job(
        name, [prompt], owner=owner,
        budget={"max_runs": max_steps, "max_tool_calls": min(max_steps * 4, jobs.BUDGET_CEILING["max_tool_calls"])},
    )
    return goal["id"]


def _default_job_status(job_id: str) -> tuple[str, str]:
    from dourmouse.goals import get_goal_store

    goal = get_goal_store().get_goal(job_id)
    if goal is None:
        return "MISSING", ""
    return str(goal["status"]), str(goal.get("blocked_reason") or "")


class Engine:
    """Runs one automation and records what happened. Every dependency can be replaced, which is how the tests drive
    it with stubs and no model."""

    def __init__(
        self, store: Any, registry: Any = None, history: RunHistory | None = None, *,
        tool_runner: Callable[[Any, dict[str, Any], bool], str] | None = None,
        job_starter: Callable[[str, str, str, int], str] = _default_job_starter,
        job_status: Callable[[str], tuple[str, str]] = _default_job_status,
        notify: Callable[..., Any] = attention.notify, settle: Callable[[str], None] = attention.settle,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.store = store
        self.registry = registry
        self.history = history or RunHistory()
        self._tool_runner = tool_runner or _default_tool_runner(registry)
        self._job_starter = job_starter
        self._job_status = job_status
        self._notify = notify
        self._settle = settle
        self._now = now_fn

    # -- helpers
    def _row(self, entry: dict[str, Any], trigger: str, manual: bool) -> dict[str, Any]:
        now = self._now()
        return {
            "id": "run-" + uuid.uuid4().hex[:10], "automation_id": entry["id"], "name": entry.get("name") or entry["id"],
            "at": now.isoformat(timespec="seconds"), "ts": now.timestamp(), "trigger": trigger, "manual": manual,
            "status": "running", "result": "", "tools": [], "duration_ms": 0, "job_id": "", "steps": 0,
            "parked": None, "notified": "",
        }

    def _tell(self, entry: dict[str, Any], row: dict[str, Any], kind: str, title: str, *, detail: str = "",
              key: str = "", action: bool = False, params: dict[str, str] | None = None, screen: str = SCREEN) -> None:
        if not (entry.get("delivery") or {}).get("notify", True) and kind != "needs_you":
            return  # a switched-off delivery never hides something that needs the owner
        self._notify(
            kind, title, detail=detail, screen=screen, params=params or {"id": entry["id"]}, source=SOURCE,
            dedupe_key=key, action={"label": "Review"} if action else None,
        )

    def _settle_row(self, row: dict[str, Any], status: str, result: str, started: float) -> dict[str, Any]:
        row["status"], row["result"] = status, result[:500]
        row["duration_ms"] = int((time.time() - started) * 1000)
        return row

    def _outcome(self, entry: dict[str, Any], row: dict[str, Any]) -> None:
        """Fold a finished run into the entry and tell the owner."""
        status = row["status"]
        if status not in ("ok", "failed"):
            return
        updated = self.store.record_outcome(entry["id"], status == "ok", status, max_failures=MAX_FAILURES) or entry
        name = row["name"]
        if status == "ok":
            self._tell(entry, row, "finished", f"{name} finished", detail=row["result"][:160])
        else:
            self._tell(entry, row, "heads_up", f"{name} failed", detail=row["result"][:160])
        if (updated.get("state") or {}).get("auto_disabled") and entry.get("enabled"):
            self._tell(updated, row, "needs_you",
                       f"{name} was switched off after {MAX_FAILURES} failures",
                       detail="Open it to see why, then turn it back on.", key=f"automation-off-{entry['id']}")
        self.store.patch_state(entry["id"], last_run_id=row["id"])

    # -- the one entry point
    def run(self, entry: dict[str, Any], trigger: str = "", *, manual: bool = False) -> dict[str, Any]:
        started = time.time()
        row = self._row(entry, trigger, manual)
        caps = clean_caps(entry.get("caps"))
        today = self._now().date().isoformat()
        if not manual and self.store.runs_today(entry, self._now()) >= caps["max_runs_per_day"]:
            self._settle_row(row, "capped", f"The daily limit of {caps['max_runs_per_day']} runs is used up. It runs again tomorrow.", started)
            self.history.add(row)
            self._tell(entry, row, "heads_up", f"{row['name']} hit its daily limit",
                       detail=row["result"], key=f"automation-cap-{entry['id']}-{today}")
            return row
        self.store.count_run(entry["id"], self._now())
        action = entry.get("action") or {"type": "tool", "tool": entry.get("tool"), "arguments": entry.get("arguments") or {}}
        try:
            kind = action.get("type")
            if kind == "reminder":
                self._settle_row(row, "ok", str(action.get("text") or ""), started)
            elif kind == "prompt":
                self._start_prompt(entry, action, caps, row, started)
            else:
                self._run_tool(entry, action, row, started)
        except Exception as exc:  # noqa: BLE001 -- a failed run is a recorded run, never a dead scheduler
            self._settle_row(row, "failed", f"{type(exc).__name__}: {exc}", started)
        self.history.add(row)
        if row["status"] == "parked":
            self._tell(entry, row, "needs_you", f"{row['name']} is waiting for you",
                       detail="It wants to run a step that needs your approval.", key=f"automation-park-{row['id']}",
                       action=True, params={"id": entry["id"], "run": row["id"]})
        self._outcome(entry, row)
        return row

    def _run_tool(self, entry: dict[str, Any], action: dict[str, Any], row: dict[str, Any], started: float) -> None:
        tool = str(action.get("tool") or "")
        args = action.get("arguments") or {}
        row["tools"] = [tool]
        spec = self.registry.lookup(tool) if self.registry is not None else None
        if spec is None:
            self._settle_row(row, "failed", f"There is no tool called {tool}. Was it removed?", started)
            return
        tier = spec.permission.value
        if tier == "prohibited":
            self._settle_row(row, "failed", f"{tool} is never allowed to run unattended.", started)
        elif tier != "regular":
            row["parked"] = {"tool": tool, "arguments": json.dumps(args)[:200]}
            self._settle_row(row, "parked", f"Waiting for you to approve {tool}.", started)
        else:
            text = self._tool_runner(spec, args, False)
            bad = str(text).lstrip().upper().startswith(tuple(p.upper() for p in _FAIL_PREFIXES))
            self._settle_row(row, "failed" if bad else "ok", str(text), started)

    def _start_prompt(self, entry: dict[str, Any], action: dict[str, Any], caps: dict[str, int], row: dict[str, Any],
                      started: float) -> None:
        job_id = self._job_starter(str(action.get("prompt") or ""), str(action.get("owner") or "orchestrator"),
                                   str(row["name"]), caps["max_steps"])
        row["job_id"] = job_id
        self._settle_row(row, "running", "Started. Its steps are on the Jobs board.", started)

    # -- parked tool runs the owner approves
    def approve_parked(self, run_id: str) -> dict[str, Any]:
        row = self.history.get(run_id)
        if row is None or row.get("status") != "parked" or row.get("job_id"):
            raise LookupError("nothing is waiting under that run")
        entry = self.store.get(row["automation_id"])
        if entry is None:
            raise LookupError("that automation was removed")
        tool = str((entry.get("action") or {}).get("tool") or entry.get("tool") or "")
        spec = self.registry.lookup(tool) if self.registry is not None else None
        if spec is None or spec.permission.value != "requires_confirmation":
            raise ValueError(f"{tool} cannot be approved from here")
        started = time.time()
        text = self._tool_runner(spec, (entry.get("action") or {}).get("arguments") or entry.get("arguments") or {}, True)
        bad = str(text).lstrip().upper().startswith(tuple(p.upper() for p in _FAIL_PREFIXES))
        self._settle_row(row, "failed" if bad else "ok", str(text), started)
        row["parked"] = None
        row["approved"] = True
        self.history.update(run_id, **row)
        self._settle(f"automation-park-{run_id}")
        self._outcome(entry, row)
        return row

    def decline_parked(self, run_id: str) -> dict[str, Any]:
        row = self.history.get(run_id)
        if row is None or row.get("status") != "parked" or row.get("job_id"):
            raise LookupError("nothing is waiting under that run")
        row.update(status="skipped", result="You declined it. It did not run.", parked=None)
        self.history.update(run_id, **row)
        self._settle(f"automation-park-{run_id}")
        return row

    # -- prompt runs finish later: read the job, record the end once
    def refresh_running(self) -> int:
        changed = 0
        for row in self.history.running():
            state, reason = self._job_status(row["job_id"])
            status, result = _map_job(state, reason)
            if status == row["status"]:
                continue
            changed += 1
            row.update(status=status, result=result)
            self.history.update(row["id"], status=status, result=result)
            entry = self.store.get(row["automation_id"]) or {"id": row["automation_id"], "name": row["name"]}
            if status == "parked":
                self._tell(entry, row, "needs_you", f"{row['name']} is waiting for you", detail=result,
                           key=f"automation-park-{row['id']}", action=True, screen="goals", params={"id": row["job_id"]})
            elif status == "capped":
                self._tell(entry, row, "heads_up", f"{row['name']} stopped at its step limit", detail=result,
                           key=f"automation-cap-{row['id']}")
            elif status in ("ok", "failed") and entry.get("enabled") is not None:
                self._settle(f"automation-park-{row['id']}")
                self._outcome(entry, row)
        return changed


def _map_job(state: str, reason: str) -> tuple[str, str]:
    if state == "COMPLETED":
        return "ok", "The job finished."
    if state in ("FAILED", "EXPIRED", "CANCELLED", "MISSING"):
        return "failed", reason or {"CANCELLED": "The job was cancelled.", "MISSING": "The job is gone."}.get(state, "The job failed.")
    if state in ("BLOCKED", "WAITING_FOR_APPROVAL", "WAITING_FOR_AUTHENTICATION"):
        if "budget is used up" in reason:
            return "capped", reason
        return "parked", reason or "A step is waiting for your approval."
    return "running", "Running."


# ---- the create sentence ------------------------------------------------------------------------------------


def parse_sentence(text: str, now: datetime | None = None) -> dict[str, Any]:
    """Split "every weekday at 8 check my security" into the clock and what is left, with no model.

    Returns {spec, schedule_text, rest, reminder}. ``rest`` is whatever follows the longest leading part the
    schedule parser accepts. "remind me <when> to <words>" is a reminder. A sentence with no readable time is
    refused with the shapes it does accept; nothing is guessed."""
    from dourmouse.schedules import parse_schedule

    raw = " ".join(str(text or "").split())
    if not raw:
        raise ValueError("say when, for example: every weekday at 8 check my security")
    reminder = False
    body = raw
    low = body.lower()
    if low.startswith("remind me"):
        reminder, body = True, body[len("remind me"):].strip()
    words = body.split(" ")
    for cut in range(len(words), 0, -1):
        head = " ".join(words[:cut]).rstrip(",")
        try:
            spec = parse_schedule(head, now=now)
        except ValueError:
            continue
        rest = " ".join(words[cut:]).strip(" ,")
        for lead in ("to ", "then ", "and ", "about "):
            if rest.lower().startswith(lead):
                rest = rest[len(lead):].strip()
                break
        return {"spec": spec, "schedule_text": head, "rest": rest, "reminder": reminder}
    # a reminder may put the words first: "remind me to call the bank tomorrow at 5pm"
    if reminder:
        for cut in range(1, len(words)):
            tail = " ".join(words[cut:])
            try:
                spec = parse_schedule(tail, now=now)
            except ValueError:
                continue
            what = " ".join(words[:cut]).strip(" ,")
            for lead in ("to ", "about "):
                if what.lower().startswith(lead):
                    what = what[len(lead):].strip()
                    break
            return {"spec": spec, "schedule_text": tail, "rest": what, "reminder": True}
    parse_schedule(raw, now=now)  # raises ValueError listing the accepted shapes
    raise ValueError("schedule not understood")
