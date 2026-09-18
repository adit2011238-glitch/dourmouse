"""dourmouse/goal_runtime.py — the persistent background worker that
executes a Goal's task graph. Hermetic: ``chat.ChatSession`` is replaced
with a scripted fake keyed by task description, so no real model/tool
call ever happens. The store is always an in-memory ``GoalStore(None)``.
"""

from __future__ import annotations

import dourmouse.chat as chat_module
from dourmouse.goal_runtime import GoalRuntime, goal_runtime_enabled
from dourmouse.goals import GoalStore


#: _verify_completion (goal_runtime.py) constructs its own ChatSession
#: over the exact same chat_module.ChatSession reference this fake
#: replaces -- so EVERY test using this fake also exercises the fake
#: verifier call, whether or not the test itself cares about
#: verification. Its prompt always starts with this exact header (see
#: _verify_completion's own prompt text); _FakeSession recognizes it and
#: answers separately from the task-description-keyed responses below,
#: defaulting to a clean VERIFIED so every test written before
#: verification existed keeps its original, unrelated meaning.
_VERIFIER_PROMPT_MARKER = "You are a strict, skeptical verifier"

#: _verify_goal_criteria (finding, success-criteria check) constructs its
#: OWN fresh ChatSession too, over the same chat_module.ChatSession
#: reference -- a real, deliberately DIFFERENT opening phrase from
#: _VERIFIER_PROMPT_MARKER above (the two prompts used to collide on the
#: same opening words, caught before it ever caused a real test
#: collision) so the fake can tell them apart.
_GOAL_CRITERIA_PROMPT_MARKER = "You are reviewing a completed multi-task goal"


class _FakeSession:
    """Records every ``ask()`` call and returns a scripted response keyed
    by the exact prompt text (== the task description). A response is
    either a plain dict (returned as-is) or a callable producing one, so
    a test can simulate raising on the first attempt and succeeding on a
    retry."""

    calls: list[tuple[str, str | None]] = []
    responses: dict[str, object] = {}
    #: Default: every task's independent verification passes cleanly, so
    #: existing tests (written before verification existed) keep reaching
    #: COMPLETED exactly as before. Tests exercising verification itself
    #: override this directly, same shape as `responses`' own values.
    verification_response: object = {"final_text": "VERDICT: VERIFIED"}
    #: Default: a goal with declared success_criteria passes cleanly, same
    #: reasoning as verification_response above.
    goal_criteria_response: object = {"final_text": "VERDICT: SATISFIED"}

    def __init__(self, registry, session_file=None, confirmation_gate=None):
        self.registry = registry
        self.confirmation_gate = confirmation_gate

    def ask(self, prompt, event_sink=None, forced_agent=None, force_plain_dispatch=False):
        type(self).calls.append((prompt, forced_agent))
        if prompt.startswith(_GOAL_CRITERIA_PROMPT_MARKER):
            scripted = type(self).goal_criteria_response
        elif prompt.startswith(_VERIFIER_PROMPT_MARKER):
            scripted = type(self).verification_response
        else:
            scripted = type(self).responses.get(prompt, {"final_text": "done"})
        if callable(scripted):
            scripted = scripted()
        if event_sink is not None:
            for event in scripted.get("events", []):
                event_sink(event)
        if scripted.get("raises"):
            raise RuntimeError(scripted["raises"])
        return {"final_text": scripted.get("final_text", ""), "transcript": [], "messages": []}


def _install_fake(monkeypatch, responses: dict[str, object]):
    _FakeSession.calls = []
    _FakeSession.responses = responses
    _FakeSession.verification_response = {"final_text": "VERDICT: VERIFIED"}
    _FakeSession.goal_criteria_response = {"final_text": "VERDICT: SATISFIED"}
    monkeypatch.setattr(chat_module, "ChatSession", _FakeSession)


def _task_calls() -> list[tuple[str, str | None]]:
    """``_FakeSession.calls`` minus the independent-verification call every
    completed task now also makes, and minus any goal-criteria check --
    for tests whose own point is task execution/ordering/routing, not
    verification."""
    return [
        c for c in _FakeSession.calls
        if not c[0].startswith(_VERIFIER_PROMPT_MARKER) and not c[0].startswith(_GOAL_CRITERIA_PROMPT_MARKER)
    ]


def _runtime(store: GoalStore) -> GoalRuntime:
    return GoalRuntime(store, registry=object(), tick_seconds=1000.0)


class TestSingleTaskGoal:
    def test_a_successful_task_completes_its_goal(self, monkeypatch):
        _install_fake(monkeypatch, {"do the one thing": {"final_text": "it is done"}})
        store = GoalStore(None)
        goal = store.create_goal("A simple goal")
        store.create_task(goal["id"], "do the one thing")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        updated = store.get_goal(goal["id"])
        assert updated["status"] == "COMPLETED"
        assert "it is done" in updated["result"]["summary"]

    def test_an_empty_final_response_is_treated_as_a_failure_not_a_success(self, monkeypatch):
        _install_fake(monkeypatch, {"say nothing": {"final_text": "   "}})
        store = GoalStore(None)
        goal = store.create_goal("A goal")
        task = store.create_task(goal["id"], "say nothing", max_attempts=1)
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "FAILED"
        assert store.get_goal(goal["id"])["status"] == "BLOCKED"


class TestGoalSuccessCriteria:
    """success_criteria has existed on every goal since this module's
    first version but nothing ever read it back until now -- finding
    #025's own docstring named this precisely as real, separate,
    not-yet-done follow-on work. See the module's own _verify_goal_criteria."""

    def test_a_goal_with_no_criteria_completes_exactly_as_before(self, monkeypatch):
        _install_fake(monkeypatch, {"do the one thing": {"final_text": "it is done"}})
        store = GoalStore(None)
        goal = store.create_goal("A simple goal")  # no success_criteria at all
        store.create_task(goal["id"], "do the one thing")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        updated = store.get_goal(goal["id"])
        assert updated["status"] == "COMPLETED"
        assert "criteria_checked" not in updated["result"]
        # no extra real LLM round trip for a goal that never asked for one
        assert not any(c[0].startswith(_GOAL_CRITERIA_PROMPT_MARKER) for c in _FakeSession.calls)

    def test_satisfied_criteria_complete_the_goal_with_the_real_reasoning_kept(self, monkeypatch):
        _install_fake(monkeypatch, {"write the report": {"final_text": "report written, cites 3 sources"}})
        _FakeSession.goal_criteria_response = {"final_text": "All good.\n\nVERDICT: SATISFIED"}
        store = GoalStore(None)
        goal = store.create_goal("Write a cited report", success_criteria=["cites at least 3 real sources"])
        store.create_task(goal["id"], "write the report")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        updated = store.get_goal(goal["id"])
        assert updated["status"] == "COMPLETED"
        assert updated["result"]["criteria_checked"] is True
        assert "SATISFIED" in updated["result"]["reasoning"]

    def test_unsatisfied_criteria_block_the_goal_instead_of_completing_it(self, monkeypatch):
        """The real point of this whole feature: a goal whose tasks all
        reported success must NOT silently complete if its own declared
        bar was not actually met."""
        _install_fake(monkeypatch, {"write the report": {"final_text": "report written, no sources cited"}})
        _FakeSession.goal_criteria_response = {
            "final_text": "The report cites zero sources, not 3.\n\nVERDICT: NOT_SATISFIED"
        }
        store = GoalStore(None)
        goal = store.create_goal("Write a cited report", success_criteria=["cites at least 3 real sources"])
        store.create_task(goal["id"], "write the report")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        updated = store.get_goal(goal["id"])
        assert updated["status"] == "BLOCKED"
        assert "cites zero sources" in updated["blocked_reason"]
        # the real, completed task work is never thrown away just because
        # the GOAL's own bar wasn't met
        task = store.list_tasks(goal["id"])[0]
        assert task["status"] == "COMPLETED"

    def test_a_broken_criteria_checker_completes_the_real_work_honestly_uncertain(self, monkeypatch):
        _install_fake(monkeypatch, {"write the report": {"final_text": "report written"}})
        _FakeSession.goal_criteria_response = {"raises": "verifier backend unavailable"}
        store = GoalStore(None)
        goal = store.create_goal("Write a report", success_criteria=["is at least 100 words"])
        store.create_task(goal["id"], "write the report")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        updated = store.get_goal(goal["id"])
        assert updated["status"] == "COMPLETED"
        assert updated["result"]["criteria_checked"] is False
        assert "could not run" in updated["result"]["note"]

    def test_a_real_verification_event_is_logged_to_the_audit_trail(self, monkeypatch):
        _install_fake(monkeypatch, {"write the report": {"final_text": "report written"}})
        store = GoalStore(None)
        goal = store.create_goal("Write a report", success_criteria=["is real"])
        store.create_task(goal["id"], "write the report")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        events = store.goal_events(goal["id"])
        goal_verifications = [e for e in events if e["type"] == "verification" and e["detail"].get("scope") == "goal"]
        assert len(goal_verifications) == 1
        assert goal_verifications[0]["detail"]["satisfied"] is True


class TestIndependentVerification:
    """Acceptance test 11 (founding spec): "a task claims completion but
    the actual artifact/result is invalid; a real verifier catches it."
    _FakeSession.verification_response defaults to VERIFIED (see its own
    docstring) so every test in this file NOT about verification keeps its
    original meaning; these tests override it directly."""

    def test_a_genuinely_verified_task_completes_marked_as_such(self, monkeypatch):
        _install_fake(monkeypatch, {"do the real thing": {"final_text": "it is done"}})
        store = GoalStore(None)
        goal = store.create_goal("A goal")
        task = store.create_task(goal["id"], "do the real thing")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        result = store.get_task(task["id"])["result"]
        assert store.get_task(task["id"])["status"] == "COMPLETED"
        assert result["verified"] is True
        assert result["note"] == "independently verified"

    def test_a_not_verified_verdict_is_a_real_failure_not_a_silent_success(self, monkeypatch):
        """The whole point of this feature: a confident-sounding final_text
        must not reach COMPLETED just because the worker claims success."""
        _install_fake(monkeypatch, {"claim success with no evidence": {"final_text": "Done! Email sent."}})
        _FakeSession.verification_response = {
            "final_text": "The report claims an email was sent, but no tool call shows this "
                           "happening.\nVERDICT: NOT_VERIFIED",
        }
        store = GoalStore(None)
        goal = store.create_goal("A goal")
        task = store.create_task(goal["id"], "claim success with no evidence", max_attempts=1)
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "FAILED"
        assert "NOT accomplished" in store.get_task(task["id"])["last_error"]
        assert store.get_goal(goal["id"])["status"] == "BLOCKED"

    def test_a_not_verified_task_retries_before_giving_up_same_as_any_other_failure(self, monkeypatch):
        _install_fake(monkeypatch, {"retry me": {"final_text": "claimed done"}})
        _FakeSession.verification_response = {"final_text": "VERDICT: NOT_VERIFIED"}
        store = GoalStore(None)
        goal = store.create_goal("A goal")
        task = store.create_task(goal["id"], "retry me", max_attempts=3)
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "RETRYING"
        assert store.get_task(task["id"])["attempt_count"] == 1

    def test_a_broken_verifier_completes_the_real_work_honestly_uncertain(self, monkeypatch):
        """The verifier failing to run must never destroy or block real,
        otherwise-successful work -- but the uncertainty is stated
        plainly, never silently upgraded to "verified"."""
        _install_fake(monkeypatch, {"do real work": {"final_text": "the real work is done"}})
        _FakeSession.verification_response = {"raises": "verifier backend unreachable"}
        store = GoalStore(None)
        goal = store.create_goal("A goal")
        task = store.create_task(goal["id"], "do real work")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        result = store.get_task(task["id"])["result"]
        assert store.get_task(task["id"])["status"] == "COMPLETED"
        assert result["final_text"] == "the real work is done"
        assert result["verified"] is False
        assert "verification could not run" in result["note"]

    def test_verifier_sees_real_tool_evidence_not_just_the_final_claim(self, monkeypatch):
        """The verifier prompt must carry the real tool-call trace, not
        just the worker's own final_text -- otherwise it is just asking
        the same unreliable source to confirm itself in different words."""
        _install_fake(monkeypatch, {
            "use a tool": {
                "final_text": "saved the file",
                "events": [
                    {"type": "tool_use", "name": "write_file", "raw_arguments": {"path": "x.txt"}},
                    {"type": "tool_result", "name": "write_file", "text": "SAVED: x.txt"},
                ],
            },
        })
        store = GoalStore(None)
        goal = store.create_goal("A goal")
        store.create_task(goal["id"], "use a tool")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        verifier_calls = [c for c in _FakeSession.calls if c[0].startswith(_VERIFIER_PROMPT_MARKER)]
        assert len(verifier_calls) == 1
        assert "CALLED: write_file" in verifier_calls[0][0]
        assert "SAVED: x.txt" in verifier_calls[0][0]

    def test_verification_event_is_logged_to_the_real_audit_trail(self, monkeypatch):
        _install_fake(monkeypatch, {"do it": {"final_text": "done"}})
        store = GoalStore(None)
        goal = store.create_goal("A goal")
        store.create_task(goal["id"], "do it")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        events = store.goal_events(goal["id"])
        verification_events = [e for e in events if e["type"] == "verification"]
        assert len(verification_events) == 1
        assert verification_events[0]["detail"]["verified"] is True


class TestDependencyOrdering:
    def test_a_dependent_task_only_runs_after_its_dependency_completes(self, monkeypatch):
        _install_fake(monkeypatch, {
            "first step": {"final_text": "first done"},
            "second step": {"final_text": "second done"},
        })
        store = GoalStore(None)
        goal = store.create_goal("Two steps")
        first = store.create_task(goal["id"], "first step")
        store.create_task(goal["id"], "second step", depends_on=[first["id"]])
        store.update_goal_status(goal["id"], "EXECUTING")
        runtime = _runtime(store)

        runtime.tick()
        assert [c[0] for c in _task_calls()] == ["first step"]
        assert store.get_goal(goal["id"])["status"] == "EXECUTING"

        runtime.tick()
        assert [c[0] for c in _task_calls()] == ["first step", "second step"]
        assert store.get_goal(goal["id"])["status"] == "COMPLETED"

    def test_independent_branches_both_run_in_the_same_tick(self, monkeypatch):
        _install_fake(monkeypatch, {"branch a": {"final_text": "a"}, "branch b": {"final_text": "b"}})
        store = GoalStore(None)
        goal = store.create_goal("Two branches")
        store.create_task(goal["id"], "branch a")
        store.create_task(goal["id"], "branch b")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert {c[0] for c in _task_calls()} == {"branch a", "branch b"}
        assert store.get_goal(goal["id"])["status"] == "COMPLETED"

    def test_forced_agent_is_threaded_through_to_the_session(self, monkeypatch):
        _install_fake(monkeypatch, {"pick an agent": {"final_text": "ok"}})
        store = GoalStore(None)
        goal = store.create_goal("Assigned")
        store.create_task(goal["id"], "pick an agent", assigned_agent="dev_coding")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert _task_calls() == [("pick an agent", "dev_coding")]


class TestFailureAndRecovery:
    def test_a_failing_task_retries_before_giving_up(self, monkeypatch):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                return {"raises": "temporary failure"}
            return {"final_text": "recovered"}

        _install_fake(monkeypatch, {"flaky step": flaky})
        store = GoalStore(None)
        goal = store.create_goal("Retry me")
        task = store.create_task(goal["id"], "flaky step", max_attempts=3)
        store.update_goal_status(goal["id"], "EXECUTING")
        runtime = _runtime(store)

        runtime.tick()
        assert store.get_task(task["id"])["status"] == "RETRYING"
        assert store.get_goal(goal["id"])["status"] == "EXECUTING"

        runtime.tick()
        assert store.get_task(task["id"])["status"] == "COMPLETED"
        assert store.get_goal(goal["id"])["status"] == "COMPLETED"

    def test_exhausting_attempts_blocks_the_goal_with_an_honest_reason(self, monkeypatch):
        _install_fake(monkeypatch, {"always fails": {"raises": "boom"}})
        store = GoalStore(None)
        goal = store.create_goal("Doomed")
        task = store.create_task(goal["id"], "always fails", max_attempts=2)
        store.update_goal_status(goal["id"], "EXECUTING")
        runtime = _runtime(store)

        runtime.tick()
        assert store.get_task(task["id"])["status"] == "RETRYING"
        runtime.tick()
        assert store.get_task(task["id"])["status"] == "FAILED"
        runtime.tick()  # the tick that notices the permanent failure and blocks the goal

        updated = store.get_goal(goal["id"])
        assert updated["status"] == "BLOCKED"
        assert "boom" in updated["blocked_reason"]

    def test_a_task_found_running_at_startup_is_never_assumed_still_running(self, monkeypatch):
        _install_fake(monkeypatch, {})
        store = GoalStore(None)
        goal = store.create_goal("Interrupted")
        task = store.create_task(goal["id"], "was mid-flight", max_attempts=3)
        store.update_goal_status(goal["id"], "EXECUTING")
        store.update_task_status(task["id"], "RUNNING")

        runtime = _runtime(store)
        runtime._recover_orphaned_tasks()

        updated = store.get_task(task["id"])
        assert updated["status"] == "RETRYING"
        events = [e["type"] for e in store.goal_events(goal["id"])]
        assert "recovery_attempted" in events

    def test_orphan_recovery_fails_the_task_when_no_attempts_remain(self, monkeypatch):
        _install_fake(monkeypatch, {})
        store = GoalStore(None)
        goal = store.create_goal("Interrupted, out of retries")
        task = store.create_task(goal["id"], "was mid-flight", max_attempts=1)
        store.update_goal_status(goal["id"], "EXECUTING")
        store.update_task_status(task["id"], "RUNNING", increment_attempt=True)

        _runtime(store)._recover_orphaned_tasks()

        assert store.get_task(task["id"])["status"] == "FAILED"


class TestApprovalGating:
    def test_a_declined_gated_tool_call_waits_for_approval_instead_of_completing(self, monkeypatch):
        _install_fake(monkeypatch, {
            "delete everything": {
                "final_text": "I tried but was declined",
                "events": [{"type": "tool_result", "name": "delete_path", "text": "DECLINED BY USER: delete /important"}],
            }
        })
        store = GoalStore(None)
        goal = store.create_goal("Risky goal")
        task = store.create_task(goal["id"], "delete everything")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "WAITING_FOR_APPROVAL"
        assert store.get_goal(goal["id"])["status"] == "WAITING_FOR_APPROVAL"


class TestConfirmationGateFor:
    """The gate-builder itself (finding #029, acceptance test 7), tested
    directly and in isolation rather than only through the harder-to-
    probe full dispatch path -- _FakeSession replaces ChatSession
    entirely, so it never actually calls a real confirmation_gate."""

    def test_a_fresh_approval_ticket_approves_regardless_of_global_auto_approve(self, monkeypatch):
        import dourmouse.goal_runtime as gr

        monkeypatch.setattr(gr, "auto_approve_enabled", lambda: False)
        gate = gr._confirmation_gate_for(approved_this_run=True)
        assert gate("do the risky thing") is True

    def test_no_ticket_and_no_global_auto_approve_declines(self, monkeypatch):
        import dourmouse.goal_runtime as gr

        monkeypatch.setattr(gr, "auto_approve_enabled", lambda: False)
        gate = gr._confirmation_gate_for(approved_this_run=False)
        assert gate("do the risky thing") is False

    def test_global_auto_approve_still_works_without_a_per_task_ticket(self, monkeypatch):
        import dourmouse.goal_runtime as gr

        monkeypatch.setattr(gr, "auto_approve_enabled", lambda: True)
        gate = gr._confirmation_gate_for(approved_this_run=False)
        assert gate("do the risky thing") is True


class TestResumableApprovalTicket:
    """The end-to-end resume path (finding #029): a human approves one
    specific WAITING_FOR_APPROVAL task through GoalStore.resolve_task_approval
    (the GOALS screen's own APPROVE/DECLINE action), and the worker's own
    next tick genuinely picks it back up -- no global toggle involved."""

    def test_an_approved_task_is_picked_up_and_completes_on_the_next_tick(self, monkeypatch):
        _install_fake(monkeypatch, {"delete an old file": {"final_text": "deleted, all clear"}})
        store = GoalStore(None)
        goal = store.create_goal("Risky goal")
        task = store.create_task(goal["id"], "delete an old file")
        store.update_task_status(task["id"], "WAITING_FOR_APPROVAL", error="needs approval")
        store.update_goal_status(goal["id"], "WAITING_FOR_APPROVAL", blocked_reason="needs approval")

        assert store.resolve_task_approval(task["id"], True) is True
        assert store.get_task(task["id"])["status"] == "READY"

        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "COMPLETED"
        assert store.get_goal(goal["id"])["status"] == "COMPLETED"

    def test_the_ticket_is_consumed_by_the_run_it_authorized_not_left_dangling(self, monkeypatch):
        """A real bug this guards against: if update_task_status's own
        result=None default silently kept the stale ticket around,
        an UNRELATED later retry of the same task would inherit a
        blanket approval nobody actually granted it."""
        _install_fake(monkeypatch, {"delete an old file": {"final_text": "deleted, all clear"}})
        store = GoalStore(None)
        goal = store.create_goal("Risky goal")
        task = store.create_task(goal["id"], "delete an old file")
        store.update_task_status(task["id"], "WAITING_FOR_APPROVAL", error="needs approval")
        store.update_goal_status(goal["id"], "WAITING_FOR_APPROVAL", blocked_reason="needs approval")
        store.resolve_task_approval(task["id"], True)

        _runtime(store).tick()

        result = store.get_task(task["id"])["result"]
        assert result is not None and "approved_for_next_run" not in result

    def test_declining_fails_the_task_for_good_the_worker_never_touches_it_again(self, monkeypatch):
        _install_fake(monkeypatch, {"delete an old file": {"final_text": "should never run"}})
        store = GoalStore(None)
        goal = store.create_goal("Risky goal")
        task = store.create_task(goal["id"], "delete an old file")
        store.update_task_status(task["id"], "WAITING_FOR_APPROVAL", error="needs approval")
        store.update_goal_status(goal["id"], "WAITING_FOR_APPROVAL", blocked_reason="needs approval")

        store.resolve_task_approval(task["id"], False, reason="not worth the risk")
        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "FAILED"
        assert store.get_goal(goal["id"])["status"] == "BLOCKED"
        assert _task_calls() == []  # the worker never ran the declined task at all


class TestGoalControl:
    def test_a_cancelled_goal_is_never_advanced(self, monkeypatch):
        _install_fake(monkeypatch, {"should not run": {"final_text": "ran anyway"}})
        store = GoalStore(None)
        goal = store.create_goal("Cancel before it starts")
        store.create_task(goal["id"], "should not run")
        store.cancel_goal(goal["id"])

        _runtime(store).tick()

        assert _FakeSession.calls == []
        assert store.get_goal(goal["id"])["status"] == "CANCELLED"

    def test_cancelling_mid_flight_is_never_clobbered_back_to_completed(self, monkeypatch):
        """Real, narrow race: cancel_goal() can run on another thread
        while a task's own dispatch call is still in flight (a real chat
        turn can take many seconds). Once that call finally returns, the
        task must stay CANCELLED, not get silently overwritten back to
        COMPLETED just because its now-moot result showed up late."""
        store = GoalStore(None)
        goal = store.create_goal("Cancel while running")
        task = store.create_task(goal["id"], "slow task")
        store.update_goal_status(goal["id"], "EXECUTING")

        def slow_response():
            # Simulates another thread calling cancel_goal() while this
            # task's own "dispatch call" is still in flight.
            store.cancel_goal(goal["id"])
            return {"final_text": "finished after being cancelled"}

        _install_fake(monkeypatch, {"slow task": slow_response})

        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "CANCELLED"
        assert store.get_goal(goal["id"])["status"] == "CANCELLED"

    def test_a_goal_with_no_tasks_yet_is_left_alone(self, monkeypatch):
        _install_fake(monkeypatch, {})
        store = GoalStore(None)
        goal = store.create_goal("Not planned yet")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_goal(goal["id"])["status"] == "EXECUTING"

    def test_a_goal_blocked_on_an_unreachable_dependency_is_marked_blocked(self, monkeypatch):
        _install_fake(monkeypatch, {})
        store = GoalStore(None)
        goal = store.create_goal("Bad graph")
        stuck = store.create_task(goal["id"], "never satisfied", depends_on=["task_doesnotexist"])
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_task(stuck["id"])["status"] == "PENDING"
        assert store.get_goal(goal["id"])["status"] == "BLOCKED"


class TestNotifications:
    def test_completion_posts_to_the_bus_and_raises_a_real_alert(self, monkeypatch):
        _install_fake(monkeypatch, {"one step": {"final_text": "done"}})
        store = GoalStore(None)
        goal = store.create_goal("Notify me")
        store.create_task(goal["id"], "one step")
        store.update_goal_status(goal["id"], "EXECUTING")

        bus_calls = []
        alert_calls = []
        broadcast_calls = []

        class FakeBus:
            def post(self, **kwargs):
                bus_calls.append(kwargs)

        class FakeStateStore:
            def add_alert(self, **kwargs):
                alert_calls.append(kwargs)

        class FakeBroadcast:
            def broadcast(self, payload):
                broadcast_calls.append(payload)

        runtime = GoalRuntime(
            store, registry=object(), tick_seconds=1000.0,
            bus=FakeBus(), state_store=FakeStateStore(), events_broadcast=FakeBroadcast(),
        )
        runtime.tick()

        assert len(bus_calls) == 1
        assert "Notify me" in bus_calls[0]["body"]
        assert len(alert_calls) == 1
        assert broadcast_calls == [{"type": "state_change", "section": "alerts", "owner": "*"}]

    def test_a_broken_notifier_never_breaks_the_runtime(self, monkeypatch):
        _install_fake(monkeypatch, {"one step": {"final_text": "done"}})
        store = GoalStore(None)
        goal = store.create_goal("Notify me")
        store.create_task(goal["id"], "one step")
        store.update_goal_status(goal["id"], "EXECUTING")

        class BrokenBus:
            def post(self, **kwargs):
                raise RuntimeError("bus is down")

        runtime = GoalRuntime(store, registry=object(), tick_seconds=1000.0, bus=BrokenBus())
        runtime.tick()  # must not raise

        assert store.get_goal(goal["id"])["status"] == "COMPLETED"


class TestEnvGate:
    def test_enabled_by_default(self, monkeypatch):
        """2026-09-18: flipped from opt-in to opt-out -- see
        goal_runtime_enabled's own docstring for the real bug this
        closes (create_goal was unconditionally registered and callable
        with the old off-by-default flag, so a "successful" tool call
        could silently do nothing forever)."""
        monkeypatch.delenv("DOURMOUSE_GOAL_RUNTIME", raising=False)
        assert goal_runtime_enabled() is True

    def test_only_the_literal_value_zero_opts_out(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GOAL_RUNTIME", "0")
        assert goal_runtime_enabled() is False
        monkeypatch.setenv("DOURMOUSE_GOAL_RUNTIME", "1")
        assert goal_runtime_enabled() is True
        monkeypatch.setenv("DOURMOUSE_GOAL_RUNTIME", "false")
        assert goal_runtime_enabled() is True  # only "0" opts out, not any falsy-looking string
