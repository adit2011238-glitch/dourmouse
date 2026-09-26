"""Tests for the Claude-as-orchestrator delegation layer."""

from __future__ import annotations

import time

import pytest

from dourmouse import model_delegation as md
from dourmouse.model_delegation import DelegationTask, delegate, format_results, route_for


class TestRoutingPolicy:
    """The routing split is privacy-first. These tests exist so a future edit
    cannot quietly move a private-data agent onto a cloud backend."""

    def test_every_real_agent_has_an_explicit_policy(self):
        """A heuristic would eventually guess wrong on a new agent and fail
        silently, so every agent in the real registry must be named."""
        from dourmouse.general_roster import build_general_registry

        real = {sa.name for sa in build_general_registry().all_subagents()}
        classified = md._LOCAL_ONLY_AGENTS | md._CLOUD_OK_AGENTS
        assert real - classified == set(), (
            "these real agents have no explicit routing policy: "
            f"{sorted(real - classified)}"
        )
        assert classified - real == set(), (
            f"policy names agents that do not exist: {sorted(classified - real)}"
        )

    def test_the_two_sets_never_overlap(self):
        assert set() == md._LOCAL_ONLY_AGENTS & md._CLOUD_OK_AGENTS

    @pytest.mark.parametrize(
        "agent", ["mail", "memory", "markets", "dev_coding", "atlas_cmd", "admin_ops"]
    )
    def test_private_data_agents_never_leave_the_machine(self, agent, monkeypatch):
        """Even with the cloud backend fully available and allowed."""
        monkeypatch.setattr(md, "gemini_available", lambda: True)
        assert route_for(agent) == md.LOCAL

    def test_unknown_agents_default_to_local(self, monkeypatch):
        monkeypatch.setattr(md, "gemini_available", lambda: True)
        assert route_for("some_future_agent") == md.LOCAL
        assert route_for(None) == md.LOCAL

    def test_public_input_agents_may_use_the_cloud(self, monkeypatch):
        monkeypatch.setattr(md, "gemini_available", lambda: True)
        assert route_for("research_info") == md.CLOUD
        assert route_for("news") == md.CLOUD

    def test_everything_is_local_when_the_cloud_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(md, "gemini_available", lambda: False)
        assert route_for("research_info") == md.LOCAL

    def test_allow_cloud_false_forces_everything_local(self, monkeypatch):
        monkeypatch.setattr(md, "gemini_available", lambda: True)
        assert route_for("news", allow_cloud=False) == md.LOCAL


class TestRunCloud:
    """`_run_cloud` (the Gemini delegation path) had no direct test at all
    before this: its on_usage wiring silently relied on gemini_backend.
    call_gemini accepting a keyword it never actually implemented, always
    raised TypeError, and was defensively swallowed by a fallback branch
    that always fired. See docs/ENGINEERING_AUDIT.md finding #019."""

    def test_a_successful_call_populates_usage_from_the_real_backend(self, monkeypatch):
        def fake_call_gemini(prompt, *, timeout, on_usage=None):
            if on_usage:
                on_usage({"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
            return "a real answer"

        monkeypatch.setattr(
            "dourmouse.gemini_backend.call_gemini", fake_call_gemini, raising=False
        )
        result = md._run_cloud(DelegationTask(agent="research_info", prompt="hi"), timeout=5.0)
        assert result.ok is True
        assert result.text == "a real answer"
        assert result.usage == {
            "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
        }

    def test_a_backend_failure_is_reported_not_swallowed(self, monkeypatch):
        def fake_call_gemini(prompt, *, timeout, on_usage=None):
            raise RuntimeError("HTTP 503: overloaded")

        monkeypatch.setattr(
            "dourmouse.gemini_backend.call_gemini", fake_call_gemini, raising=False
        )
        result = md._run_cloud(DelegationTask(agent="research_info", prompt="hi"), timeout=5.0)
        assert result.ok is False
        assert "HTTP 503" in result.error


class TestFanOut:
    def test_tasks_run_concurrently_not_sequentially(self, monkeypatch):
        """The whole point of the fan-out. Four 0.4s tasks must finish in
        well under the 1.6s they would take one at a time."""

        def slow(task, timeout):
            time.sleep(0.4)
            return md.DelegationResult(task=task, ok=True, text="ok", model_used=md.LOCAL)

        monkeypatch.setattr(md, "_run_local", slow)
        tasks = [DelegationTask(prompt=f"q{i}") for i in range(4)]
        started = time.monotonic()
        results = delegate(tasks, max_workers=4, timeout=10)
        elapsed = time.monotonic() - started

        assert len(results) == 4
        assert all(r.ok for r in results)
        assert elapsed < 1.2, f"ran sequentially: {elapsed:.2f}s for 4x0.4s tasks"

    def test_results_come_back_in_the_order_asked(self, monkeypatch):
        """Completion order is meaningless to a caller matching N answers to
        N questions, so the original order must be restored."""

        def variable(task, timeout):
            # Later tasks finish sooner, so completion order is reversed.
            time.sleep(0.05 * (3 - int(task.label)))
            return md.DelegationResult(task=task, ok=True, text=task.label, model_used=md.LOCAL)

        monkeypatch.setattr(md, "_run_local", variable)
        tasks = [DelegationTask(prompt="q", label=str(i)) for i in range(4)]
        results = delegate(tasks, max_workers=4, timeout=10)
        assert [r.text for r in results] == ["0", "1", "2", "3"]

    def test_one_failure_never_discards_the_other_answers(self, monkeypatch):
        def flaky(task, timeout):
            if task.label == "bad":
                raise RuntimeError("backend exploded")
            return md.DelegationResult(task=task, ok=True, text="fine", model_used=md.LOCAL)

        monkeypatch.setattr(md, "_run_local", flaky)
        results = delegate(
            [
                DelegationTask(prompt="a", label="good1"),
                DelegationTask(prompt="b", label="bad"),
                DelegationTask(prompt="c", label="good2"),
            ],
            timeout=10,
        )
        assert [r.ok for r in results] == [True, False, True]
        assert "backend exploded" in results[1].error

    def test_empty_task_list_is_a_no_op(self):
        assert delegate([]) == []


class TestFormatting:
    def test_a_backend_failure_reply_is_a_failure_not_a_success(self, monkeypatch):
        """Finding #134: a backend that cannot answer returns its own failure
        as the reply. The fan-out used to count that as a success."""
        import dourmouse.dispatch as dispatch

        monkeypatch.setattr(
            dispatch, "run_dispatch_messages",
            lambda *a, **k: {"final_text": "CLAUDE ORCHESTRATOR (reported honestly): claude exited 1: boom"},
        )
        result = md._run_local(DelegationTask(prompt="x"), timeout=5.0)
        assert result.ok is False and "boom" in result.error
        assert "0 succeeded, 1 failed" in md.format_results([result])

    def test_delegated_turns_inherit_the_calling_run(self, monkeypatch):
        import types

        import dourmouse.dispatch as dispatch
        from dourmouse.dispatch import JobTracker

        seen: dict = {}

        def fake_run(messages, registry, **kwargs):
            seen.update(kwargs)
            return {"final_text": "fine"}

        monkeypatch.setattr(dispatch, "run_dispatch_messages", fake_run)
        jobs = JobTracker()
        budget = [1]
        caller = types.SimpleNamespace(
            client="C", config=None, confirmation_gate="G", event_sink="S", jobs=jobs, depth=1, max_depth=3,
            budget=budget, max_delegates=9, current_job_id=None, cost_budget="B", dlp="D", rbac="R",
            policy="P",
        )
        with md.inherit_caller(caller):
            result = md._run_local(DelegationTask(prompt="x"), timeout=5.0)
        assert result.ok and result.text == "fine"
        assert (seen["client"], seen["confirmation_gate"], seen["dlp"], seen["rbac"]) == ("C", "G", "D", "R")
        assert seen["depth"] == 2 and seen["budget"] is budget and seen["fanout_branch"] is True
        assert seen["policy"] == "P"  # finding #140: the parent's run policy, shared
        assert jobs.count() == 1

    def test_worker_threads_see_the_calling_run(self, monkeypatch):
        seen = []

        def peek(task, timeout):
            seen.append(md._caller_ctx.get())
            return md.DelegationResult(task=task, ok=True, text="x")

        monkeypatch.setattr(md, "_run_local", peek)
        with md.inherit_caller("the-caller"):
            md.delegate([DelegationTask(prompt="a"), DelegationTask(prompt="b")])
        assert seen == ["the-caller", "the-caller"]
        assert md._caller_ctx.get() is None  # and it is released afterwards

    def test_failures_are_never_rendered_as_answers(self):
        out = format_results(
            [
                md.DelegationResult(
                    task=DelegationTask(prompt="a", label="one"), ok=True,
                    text="real answer", model_used="ollama",
                ),
                md.DelegationResult(
                    task=DelegationTask(prompt="b", label="two"), ok=False,
                    error="it broke", model_used="gemini",
                ),
            ]
        )
        assert "1 succeeded" in out and "1 failed" in out
        assert "FAILED: it broke" in out
        assert "real answer" in out
