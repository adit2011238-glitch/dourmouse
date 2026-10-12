"""Tests for the Claude-as-orchestrator delegation layer."""

from __future__ import annotations

import time

import pytest

from dourmouse import model_delegation as md
from dourmouse.model_delegation import DelegationTask, delegate, format_results, route_for


class TestRoutingPolicy:
    """PRIV (2026-10-12): every model is a cloud model, so routing no longer
    promises to keep anything on this machine (the old "local only" list was
    false once a key was set, A-13). It decides whether a task gets real
    tools: the roster (behind the privacy gate) or one tool-less Gemini call
    for public work. These tests keep a future edit from sending a sensitive
    agent's task to tool-less Gemini."""

    def test_every_real_agent_is_sensitive_public_or_roster(self):
        from dourmouse.general_roster import build_general_registry
        from dourmouse.privacy import SENSITIVE_AGENTS

        real = {sa.name for sa in build_general_registry().all_subagents()}
        assert set() == SENSITIVE_AGENTS & md._PUBLIC_INPUT_AGENTS
        assert md._PUBLIC_INPUT_AGENTS - real == set(), sorted(md._PUBLIC_INPUT_AGENTS - real)

    @pytest.mark.parametrize("agent", ["mail", "memory", "markets", "admin_ops", "docs"])
    def test_sensitive_agents_never_go_to_tool_less_gemini(self, agent, monkeypatch):
        monkeypatch.setattr(md, "gemini_available", lambda: True)
        assert route_for(agent) == md.ROSTER

    def test_unknown_agents_run_through_the_roster(self, monkeypatch):
        monkeypatch.setattr(md, "gemini_available", lambda: True)
        assert route_for("some_future_agent") == md.ROSTER
        assert route_for(None) == md.ROSTER

    def test_public_input_agents_may_use_gemini(self, monkeypatch):
        monkeypatch.setattr(md, "gemini_available", lambda: True)
        assert route_for("research_info") == md.GEMINI
        assert route_for("news") == md.GEMINI

    def test_everything_runs_on_the_roster_when_gemini_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(md, "gemini_available", lambda: False)
        assert route_for("research_info") == md.ROSTER

    def test_allow_gemini_false_keeps_everything_on_the_roster(self, monkeypatch):
        monkeypatch.setattr(md, "gemini_available", lambda: True)
        assert route_for("news", allow_gemini=False) == md.ROSTER


class TestRunCloud:
    """`_run_gemini` (was _run_cloud, the Gemini delegation path) had no direct test at all
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
        result = md._run_gemini(DelegationTask(agent="research_info", prompt="hi"), timeout=5.0)
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
        result = md._run_gemini(DelegationTask(agent="research_info", prompt="hi"), timeout=5.0)
        assert result.ok is False
        assert "HTTP 503" in result.error


class TestFanOut:
    def test_tasks_run_concurrently_not_sequentially(self, monkeypatch):
        """The whole point of the fan-out. Four 0.4s tasks must finish in
        well under the 1.6s they would take one at a time."""

        def slow(task, timeout):
            time.sleep(0.4)
            return md.DelegationResult(task=task, ok=True, text="ok", model_used=md.ROSTER)

        monkeypatch.setattr(md, "_run_roster", slow)
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
            return md.DelegationResult(task=task, ok=True, text=task.label, model_used=md.ROSTER)

        monkeypatch.setattr(md, "_run_roster", variable)
        tasks = [DelegationTask(prompt="q", label=str(i)) for i in range(4)]
        results = delegate(tasks, max_workers=4, timeout=10)
        assert [r.text for r in results] == ["0", "1", "2", "3"]

    def test_one_failure_never_discards_the_other_answers(self, monkeypatch):
        def flaky(task, timeout):
            if task.label == "bad":
                raise RuntimeError("backend exploded")
            return md.DelegationResult(task=task, ok=True, text="fine", model_used=md.ROSTER)

        monkeypatch.setattr(md, "_run_roster", flaky)
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
        result = md._run_roster(DelegationTask(prompt="x"), timeout=5.0)
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
            result = md._run_roster(DelegationTask(prompt="x"), timeout=5.0)
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

        monkeypatch.setattr(md, "_run_roster", peek)
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
