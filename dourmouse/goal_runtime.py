"""The persistent worker that actually executes a Goal's task graph in
the background, independent of any HTTP connection or open chat tab.

See docs/ARCHITECTURE.md's "What's missing for the new Goal/Task
runtime" section: every existing background mechanism in this codebase
(``schedules.SchedulerRunner``, ``live_runtime.LiveRuntime``) is either
a single recurring tool call or a hardcoded poll, and neither can hold
a multi-step plan, resume it after a crash, or tell an inspector which
step it is currently on. This module is that missing piece. It
deliberately mirrors ``SchedulerRunner``'s daemon-thread-with-tick shape
(``schedules.py``) rather than inventing a new background-execution
idiom, and reuses ``chat.ChatSession``/``dispatch.run_dispatch_messages``
for actually running a task exactly the way a normal chat turn already
does, rather than building a second, parallel tool-execution path.

Honest, stated v1 scope (the rest is tracked in docs/GODSPEED_ROADMAP.md
Phase 2, not silently assumed done):

- Verification (2026-09-18, see ``_verify_completion``) is a genuine
  second, independent reasoning pass over a fresh ChatSession with NO
  tools available -- the model that did the work does not get to grade
  its own homework. It is shown the real tool-call evidence from the
  actual run, not just the worker's own final_text. Deliberately NOT a
  deterministic check against per-task success criteria, since no such
  criteria field exists on a task yet (the founding spec's own suggested
  richer task schema) -- that remains real, separate follow-on work; this
  closes the more urgent half (self-reported vs. independently checked).
- A REQUIRES_CONFIRMATION tool inside an autonomous task either runs
  (``DOURMOUSE_AUTO_APPROVE=1``) or the task is marked
  WAITING_FOR_APPROVAL with an honest reason and a real notification
  goes out (auto-approve off). A resumable "approval ticket" that lets
  one specific blocked task continue after a later human approval
  (rather than requiring the global toggle to be flipped) is real,
  separate follow-on work.
- Cross-device (Tailscale) execution is not wired in this pass — tasks
  run against this host's own tool registry only.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from dourmouse.config import auto_approve_enabled, workspace_dir
from dourmouse.goals import GoalStore, TASK_TERMINAL_STATES

_DEFAULT_TICK_SECONDS = 5.0
#: How many ready tasks (across all active goals combined isn't tracked
#: here — this is per goal, per tick) run at once. Matches
#: delegate_parallel's own concurrency cap in spirit: bounded, not
#: unlimited, even though this loop is single-threaded per tick today.
_MAX_TASKS_PER_TICK = 3


def goal_runtime_enabled() -> bool:
    """Default ON since 2026-09-18 (opt-OUT, not opt-in) — a real, live
    bug this flag itself was causing, not a cautious choice worth keeping.
    ``goal_tools.build_goals_subagent`` (the ``create_goal``/``add_tasks``
    tool family) is registered by ``general_roster.py`` UNCONDITIONALLY,
    with no check of this flag anywhere in that path -- so with the flag
    off (the old default), the model could call ``create_goal``, get back
    a real goal id, and tell the user work was now happening in the
    background, while no worker thread ever existed to advance it. The
    goal just sat in CREATED/READY forever. That is exactly the founding
    spec's own named anti-pattern: "Do not create fake background
    execution in which the UI merely displays a spinner while no real
    worker is operating" -- except here it was worse than a spinner,
    it was a tool call that looked completely successful.

    This does not remove any real safety gate: a REQUIRES_CONFIRMATION
    tool inside an autonomous task still pauses at WAITING_FOR_APPROVAL
    (see ``_autonomous_confirmation_gate``/``_run_task`` below) whether
    this flag is on or off -- flipping the default makes the already-
    exposed, already-documented tool actually do what it already claimed
    to do, it does not grant any new capability the model didn't already
    have a tool for. Set ``DOURMOUSE_GOAL_RUNTIME=0`` to opt back out.
    """
    return os.environ.get("DOURMOUSE_GOAL_RUNTIME", "1").strip() != "0"


def _autonomous_confirmation_gate(_prompt_text: str) -> bool:
    """Autonomous tasks have no live human to ask synchronously.
    Auto-approve on: proceed, exactly like an interactive session after
    a yes. Auto-approve off: decline honestly (never hang, never
    silently approve) so the caller classifies this as needing
    approval rather than pretending the action happened."""
    return auto_approve_enabled()


class GoalRuntime:
    """Ticks over every active goal, executes ready tasks, retries or
    blocks on failure, and notifies on completion/blocking. One
    instance per process, started as a daemon thread from
    ``webui.run_server`` exactly like ``SchedulerRunner``."""

    def __init__(
        self,
        store: GoalStore,
        registry: Any,
        tick_seconds: float = _DEFAULT_TICK_SECONDS,
        bus: Any | None = None,
        state_store: Any | None = None,
        events_broadcast: Any | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._tick_seconds = max(1.0, float(tick_seconds))
        self._bus = bus
        self._state_store = state_store
        self._events_broadcast = events_broadcast
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._recover_orphaned_tasks()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="dourmouse-goal-runtime")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass  # a bug in one tick must never kill the runtime; the next tick tries again
            self._stop.wait(self._tick_seconds)

    def _recover_orphaned_tasks(self) -> None:
        """A task found RUNNING at startup means the process died mid
        execution — it is NOT still running. Never silently treat it as
        continuing; route it through the normal retry/fail path instead
        (spec: "never blindly repeat an external side effect")."""
        for goal in self._store.active_goals():
            for task in self._store.list_tasks(goal["id"]):
                if task["status"] != "RUNNING":
                    continue
                self._store.log_event(
                    goal["id"], "recovery_attempted",
                    {"task_id": task["id"], "reason": "found RUNNING at worker startup"},
                    task_id=task["id"],
                )
                if task["attempt_count"] < task["max_attempts"]:
                    self._store.update_task_status(
                        task["id"], "RETRYING", increment_attempt=True,
                        error="worker restarted mid-execution",
                    )
                else:
                    self._store.update_task_status(
                        task["id"], "FAILED",
                        error="worker restarted mid-execution, no attempts left",
                    )

    def tick(self) -> None:
        for goal in self._store.active_goals():
            try:
                self._advance_goal(goal)
            except Exception as exc:
                self._store.log_event(goal["id"], "note", {"error": f"tick failed: {exc}"})

    def _advance_goal(self, goal: dict[str, Any]) -> None:
        goal_id = goal["id"]
        tasks = self._store.list_tasks(goal_id)
        if not tasks:
            return  # still being planned by whatever created the goal (a create_goal call with tasks=[] mid-construction)
        if self._resolve_if_terminal(goal, tasks):
            return
        ready = [t for t in self._store.ready_tasks(goal_id) if t["status"] == "READY"]
        retrying = [t for t in tasks if t["status"] == "RETRYING"]
        runnable = (ready + retrying)[:_MAX_TASKS_PER_TICK]
        if not runnable:
            pending = [t for t in tasks if t["status"] == "PENDING"]
            in_flight = any(t["status"] in ("READY", "RUNNING", "RETRYING") for t in tasks)
            if pending and not in_flight:
                self._store.update_goal_status(
                    goal_id, "BLOCKED",
                    blocked_reason="remaining tasks depend on a task that will never complete",
                )
            return
        for task in runnable:
            current = self._store.get_goal(goal_id)
            if current["status"] not in ("EXECUTING", "READY"):
                return  # cancelled/paused since this tick started
            self._run_task(current, task)
        # A task finishing above can be the LAST one, or the one that
        # exhausts its retries — resolve it now rather than waiting a
        # full tick interval to notice either outcome.
        current = self._store.get_goal(goal_id)
        if current["status"] in ("EXECUTING", "READY"):
            self._resolve_if_terminal(current, self._store.list_tasks(goal_id))

    def _resolve_if_terminal(self, goal: dict[str, Any], tasks: list[dict[str, Any]]) -> bool:
        """Completes or blocks the goal if its task graph has already
        reached a terminal shape. Returns True when it did (caller
        should stop advancing this goal further this tick)."""
        goal_id = goal["id"]
        if all(t["status"] == "COMPLETED" for t in tasks):
            self._complete_goal(goal, tasks)
            return True
        permanently_failed = [t for t in tasks if t["status"] == "FAILED" and t["attempt_count"] >= t["max_attempts"]]
        if permanently_failed:
            culprit = permanently_failed[0]
            self._store.update_goal_status(
                goal_id, "BLOCKED",
                blocked_reason=f"task {culprit['id']!r} ({culprit['description']}) failed permanently: {culprit['last_error']}",
            )
            self._notify(goal_id, f"Blocked: {goal['objective']}", culprit["last_error"] or "a task failed")
            return True
        return False

    def _run_task(self, goal: dict[str, Any], task: dict[str, Any]) -> None:
        goal_id, task_id = goal["id"], task["id"]
        self._store.update_task_status(task_id, "RUNNING", increment_attempt=True)
        self._store.set_current_task(goal_id, task_id)
        try:
            report = self._execute_via_dispatch(goal, task)
        except Exception as exc:
            self._handle_task_failure(goal, task, f"{type(exc).__name__}: {exc}")
            return
        # A real, if narrow, race: cancel_goal() can run concurrently while
        # _execute_via_dispatch() above was still in flight (a real chat
        # turn can take many seconds). Re-check before writing any
        # completion/failure/approval status below, so a task already
        # marked CANCELLED in the meantime is never silently clobbered
        # back to COMPLETED once its now-moot dispatch call finally returns.
        current_task = self._store.get_task(task_id)
        if current_task is None or current_task["status"] in TASK_TERMINAL_STATES:
            return
        if report.get("blocked_reason"):
            reason = report["blocked_reason"]
            self._store.update_task_status(task_id, "WAITING_FOR_APPROVAL", error=reason)
            self._store.update_goal_status(goal_id, "WAITING_FOR_APPROVAL", blocked_reason=reason)
            self._notify(goal_id, f"Needs your approval: {goal['objective']}", reason)
            return
        final_text = (report.get("final_text") or "").strip()
        if not final_text:
            self._handle_task_failure(goal, task, "the model produced no final response for this task")
            return
        self._store.update_task_status(task_id, "VERIFYING")
        verdict = self._verify_completion(goal, task, final_text, report.get("tool_trace") or [])
        self._store.log_event(
            goal_id, "verification",
            {"verified": verdict["verified"], "reasoning": verdict["reasoning"][:2000], "error": verdict["error"]},
            task_id=task_id,
        )
        # Same real, narrow race as the dispatch call itself (finding #016):
        # the verification call above is a second real LLM round trip, so
        # cancel_goal() can just as easily land while THIS one is in flight.
        current_task = self._store.get_task(task_id)
        if current_task is None or current_task["status"] in TASK_TERMINAL_STATES:
            return
        if verdict["error"] is not None:
            # The verifier itself couldn't run -- the real work is not
            # thrown away and not blocked forever on a broken checker, but
            # the uncertainty is stated plainly, never silently upgraded.
            self._store.update_task_status(
                task_id, "COMPLETED",
                result={
                    "final_text": final_text, "verified": False,
                    "note": f"verification could not run: {verdict['error']}",
                },
            )
            return
        if not verdict["verified"]:
            self._handle_task_failure(
                goal, task,
                f"independent verification found this task NOT accomplished: {verdict['reasoning'][:500]}",
            )
            return
        self._store.update_task_status(
            task_id, "COMPLETED",
            result={"final_text": final_text, "verified": True, "note": "independently verified"},
        )

    def _execute_via_dispatch(self, goal: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        from dourmouse.chat import ChatSession  # lazy: keep this module importable without pulling in every backend

        session_file = workspace_dir() / "sessions" / f"goal_{goal['id']}_task_{task['id']}.jsonl"
        session = ChatSession(
            self._registry, session_file=session_file,
            confirmation_gate=_autonomous_confirmation_gate,
        )
        blocked_reason: list[str] = []
        #: Real evidence of what actually happened, handed to the
        #: independent verifier below -- NOT reconstructed from the
        #: model's own final_text, which is exactly the thing being
        #: checked. One line per real tool call/result pair, kept short.
        tool_trace: list[str] = []

        def sink(entry: dict[str, Any]) -> None:
            etype = entry.get("type")
            if etype in ("tool_use", "function"):
                name = entry.get("name")
                self._store.log_event(
                    goal["id"], "tool_call",
                    {"name": name, "arguments": entry.get("raw_arguments") or entry.get("arguments")},
                    task_id=task["id"],
                )
                tool_trace.append(f"CALLED: {name}")
            elif etype in ("tool_result", "function_result"):
                text = str(entry.get("text") or "")
                self._store.log_event(
                    goal["id"], "tool_result",
                    {"name": entry.get("name"), "text": text[:2000]},
                    task_id=task["id"],
                )
                if text.startswith("DECLINED BY USER:"):
                    blocked_reason.append(f"a gated action needs approval: {text}")
                tool_trace.append(f"RESULT ({entry.get('name')}): {text[:300]}")

        report = session.ask(
            task["description"],
            event_sink=sink,
            forced_agent=task.get("assigned_agent"),
            force_plain_dispatch=True,
        )
        report["blocked_reason"] = blocked_reason[0] if blocked_reason else None
        report["tool_trace"] = tool_trace
        return report

    def _verify_completion(
        self, goal: dict[str, Any], task: dict[str, Any], final_text: str, tool_trace: list[str],
    ) -> dict[str, Any]:
        """Acceptance test 11 (founding spec): "a task claims completion but
        the actual artifact/result is invalid; a real verifier catches it."
        Self-reporting was the honestly-tracked v1 gap (see this module's
        own former docstring) -- the model that did the work is not a
        credible judge of whether it actually worked.

        A second, genuinely independent reasoning pass: a FRESH ChatSession
        over an EMPTY DispatchRegistry (no tools at all, so this call
        cannot itself take any action, gated or not -- it can only judge),
        shown the real tool-call evidence collected during the real run
        (never the model's own final_text alone, which is exactly the
        thing being checked), asked for a strict, skeptical verdict.

        Honest on every failure mode: a genuine NOT_VERIFIED verdict is
        treated as a real task failure (routed through the normal retry
        path, same as any other failure) -- never silently downgraded to
        a warning. If the verifier itself cannot run (network error, no
        backend available), the real work is NOT thrown away and NOT
        blocked forever on a broken checker -- it completes, but with the
        uncertainty stated plainly in the result, never quietly upgraded
        to "verified".
        """
        from dourmouse.chat import ChatSession
        from dourmouse.dispatch import DispatchRegistry

        evidence = "\n".join(tool_trace) if tool_trace else "(no tool calls were made during this run)"
        prompt = (
            "You are a strict, skeptical verifier reviewing another agent's work. "
            "You did not do this work yourself -- judge only the evidence below.\n\n"
            f"TASK: {task['description']}\n\n"
            f"THE AGENT'S OWN FINAL REPORT:\n{final_text}\n\n"
            f"REAL TOOL-CALL EVIDENCE FROM THE ACTUAL RUN:\n{evidence}\n\n"
            "Did the agent's REAL actions (the tool-call evidence, not just its own claim) "
            "genuinely accomplish this task? If the task required an external action "
            "(sending, creating, saving, deleting, posting) and the evidence shows no real "
            "tool call that did that, say so explicitly -- a confident-sounding report with no "
            "supporting evidence is NOT verified. End your answer with exactly one line, "
            "verbatim: 'VERDICT: VERIFIED' or 'VERDICT: NOT_VERIFIED'."
        )
        try:
            session = ChatSession(DispatchRegistry(), session_file=None)
            result = session.ask(prompt, force_plain_dispatch=True)
            reasoning = (result.get("final_text") or "").strip()
        except Exception as exc:  # noqa: BLE001 - a broken verifier must never destroy real completed work
            return {"verified": False, "reasoning": "", "error": f"{type(exc).__name__}: {exc}"}
        # NOT_VERIFIED must win a substring collision against VERIFIED (a
        # reasoning paragraph can say "this was not verified" before the
        # instructed final line) -- check for the negative phrase first.
        # Fail-safe default: an unclear or missing verdict is NOT treated
        # as verified (Rule 2.2 -- honest by default, never optimistic).
        normalized = reasoning.upper()
        if "NOT_VERIFIED" in normalized or "NOT VERIFIED" in normalized:
            verified = False
        elif "VERDICT: VERIFIED" in normalized:
            verified = True
        else:
            verified = False
        return {"verified": verified, "reasoning": reasoning, "error": None}

    def _handle_task_failure(self, goal: dict[str, Any], task: dict[str, Any], error: str) -> None:
        goal_id, task_id = goal["id"], task["id"]
        current = self._store.get_task(task_id)
        if current is None:
            return
        if current["attempt_count"] < current["max_attempts"]:
            self._store.update_task_status(task_id, "RETRYING", error=error)
            self._store.log_event(
                goal_id, "recovery_attempted",
                {"task_id": task_id, "attempt": current["attempt_count"], "error": error},
                task_id=task_id,
            )
        else:
            self._store.update_task_status(task_id, "FAILED", error=error)

    def _complete_goal(self, goal: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
        goal_id = goal["id"]
        summary = "\n".join(
            f"- {t['description']}: {(t['result'] or {}).get('final_text', '')[:300]}" for t in tasks
        )
        self._store.update_goal_status(goal_id, "COMPLETED", result={"summary": summary})
        self._notify(goal_id, f"Done: {goal['objective']}", "every task completed")

    def _notify(self, goal_id: str, title: str, detail: str) -> None:
        """Real, established two-step notification, same as every other
        system-generated alert in this codebase (see webui.py's ATLAS-run
        alert): create the alert, then broadcast the section change so
        DesktopNotifier's existing SSE subscription picks it up and fires
        a real native notification. Never a new, parallel notification
        mechanism."""
        if self._bus is not None:
            try:
                self._bus.post(from_agent="dourmouse-goals", to_agent="*", subject=f"goal:{goal_id}", body=f"{title} — {detail}"[:600])
            except Exception:
                pass  # a broken bus must never break the goal runtime
        if self._state_store is not None:
            try:
                from dourmouse.state_store import SHARED_OWNER

                self._state_store.add_alert(kind="system", title=title[:160], detail=detail[:400], link=f"#/goal/{goal_id}")
                if self._events_broadcast is not None:
                    self._events_broadcast.broadcast({"type": "state_change", "section": "alerts", "owner": SHARED_OWNER})
            except Exception:
                pass  # a broken alert/broadcast must never break the goal runtime
