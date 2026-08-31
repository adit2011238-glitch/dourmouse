"""Self-dispatch tests: the orchestrator can spawn NESTED agent runs.

Feature: the lead orchestrator gets its own ``delegate_task`` tool which
spawns a fresh run_dispatch_messages against the same registry (same client,
gate, sink), depth- and budget-bounded by deterministic guards (Rule 2.8),
and audit-logged as a JobTracker job. The UI surfaces jobs via /api/jobs in
the DELEGATED TASKS panel.

These tests use a fake OpenAI-shaped client (same discipline as
test_dispatch.py): the fake shapes the LLM side of the conversation, while
the delegate tool, guards, JobTracker, and HTTP surface are REAL code.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from typing import Any

import pytest

from dourmouse.dispatch import (
    JobTracker,
    Permission,
    Subagent,
    ToolSpec,
    run_dispatch_messages,
)
from dourmouse.general_roster import build_general_registry


# --- shared fake client (same shape as test_dispatch.py) ---

class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class FakeClient:
    def __init__(self, responses: list[_FakeResponse]):
        self.chat = _FakeChat(_FakeCompletions(responses))


def _echo_tool(name: str = "echo") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="echo the text back",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=lambda a: f"ECHOED: {a['text']}",
    )


@pytest.fixture
def registry():
    """The REAL general roster plus a no-network echo subagent to delegate to."""
    reg = build_general_registry()
    reg.register_subagent(
        Subagent(
            name="echo_agent",
            domain="Test",
            description="echoes text back",
            tools=(_echo_tool(),),
        )
    )
    return reg


@pytest.fixture
def jobs():
    return JobTracker()


# --------------------------------------------------------------------------- #
# JobTracker — the audit log behind /api/jobs
# --------------------------------------------------------------------------- #

class TestJobTracker:
    def test_spawn_creates_running_job(self, jobs):
        job_id = jobs.spawn(task="tidy the workspace", subagent="admin_ops", depth=1)
        assert job_id == "job-1"
        snap = jobs.snapshot()
        assert snap[0]["id"] == "job-1"
        assert snap[0]["status"] == "running"
        assert snap[0]["depth"] == 1
        assert snap[0]["subagent"] == "admin_ops"
        assert snap[0]["parent_id"] is None
        assert snap[0]["finished_at"] is None
        assert snap[0]["result"] == ""

    def test_finish_marks_done_with_result(self, jobs):
        job_id = jobs.spawn(task="x", subagent=None, depth=2, parent_id="job-0")
        jobs.finish(job_id, result="REAL final text")
        snap = jobs.snapshot()[0]
        assert snap["status"] == "done"
        assert snap["result"] == "REAL final text"
        assert snap["finished_at"] is not None

    def test_finish_with_error_marks_error(self, jobs):
        job_id = jobs.spawn(task="x", subagent=None, depth=1)
        jobs.finish(job_id, error="nested dispatch failed: boom")
        snap = jobs.snapshot()[0]
        assert snap["status"] == "error"
        assert snap["error"] == "nested dispatch failed: boom"

    def test_refuse_marks_refused(self, jobs):
        job_id = jobs.spawn(task="x", subagent=None, depth=1)
        jobs.refuse(job_id, reason="policy")
        snap = jobs.snapshot()[0]
        assert snap["status"] == "refused"
        assert snap["error"] == "policy"

    def test_snapshot_is_newest_first_and_bounded(self, jobs):
        for i in range(5):
            jobs.spawn(task=f"t{i}", subagent=None, depth=1)
        snaps = jobs.snapshot(limit=2)
        assert [s["id"] for s in snaps] == ["job-5", "job-4"]
        assert jobs.count() == 5

    def test_task_result_truncated(self, jobs):
        job_id = jobs.spawn(task="x" * 900, subagent=None, depth=1)
        snap = jobs.snapshot()[0]
        assert len(snap["task"]) == 400  # capped at spawn
        jobs.finish(job_id, result="y" * 1200)
        assert len(jobs.snapshot()[0]["result"]) == 800  # capped at finish

    def test_bounded_at_max_jobs(self, jobs):
        for _ in range(JobTracker._MAX_JOBS + 10):
            jobs.spawn(task="x", subagent=None, depth=1)
        assert jobs.count() == JobTracker._MAX_JOBS
        # Oldest jobs are the ones evicted; newest survive.
        assert jobs.snapshot(limit=1)[0]["id"] == f"job-{JobTracker._MAX_JOBS + 10}"


class TestJobTrackerChimeHook:
    """v13.5 (Vision OS checklist item 6, contextual chimes): JobTracker's
    optional chime_fn — called on a TOP-LEVEL (depth == 0) job's real
    finish()/refuse(), never for a nested sub-branch (no chime storm from
    one delegate_parallel fan-out), and never allowed to break job
    bookkeeping even if it raises."""

    def test_depth_zero_finish_fires_the_chime(self):
        seen = []
        jt = JobTracker(chime_fn=lambda job: seen.append(job))
        job_id = jt.spawn(task="run the tests", subagent="dev_coding", depth=0)
        jt.finish(job_id, result="all green")
        assert len(seen) == 1
        assert seen[0]["id"] == job_id
        assert seen[0]["status"] == "done"
        assert seen[0]["result"] == "all green"

    def test_depth_zero_error_fires_the_chime(self):
        seen = []
        jt = JobTracker(chime_fn=lambda job: seen.append(job))
        job_id = jt.spawn(task="x", subagent="dev_coding", depth=0)
        jt.finish(job_id, error="boom")
        assert len(seen) == 1
        assert seen[0]["status"] == "error"

    def test_depth_zero_refuse_fires_the_chime(self):
        seen = []
        jt = JobTracker(chime_fn=lambda job: seen.append(job))
        job_id = jt.spawn(task="x", subagent="dev_coding", depth=0)
        jt.refuse(job_id, reason="policy")
        assert len(seen) == 1
        assert seen[0]["status"] == "refused"

    def test_nested_job_never_fires_the_chime(self):
        seen = []
        jt = JobTracker(chime_fn=lambda job: seen.append(job))
        job_id = jt.spawn(task="x", subagent="dev_coding", depth=1)
        jt.finish(job_id, result="done")
        assert seen == []

    def test_no_chime_fn_is_a_true_noop(self):
        jt = JobTracker()  # default, matches every existing JobTracker() call
        job_id = jt.spawn(task="x", subagent="dev_coding", depth=0)
        jt.finish(job_id, result="done")  # must not raise with no chime_fn
        assert jt.snapshot()[0]["status"] == "done"

    def test_a_raising_chime_fn_never_breaks_finish(self):
        def boom(_job):
            raise RuntimeError("tts backend exploded")

        jt = JobTracker(chime_fn=boom)
        job_id = jt.spawn(task="x", subagent="dev_coding", depth=0)
        jt.finish(job_id, result="done")  # must not propagate boom
        assert jt.snapshot()[0]["status"] == "done"
        assert jt.snapshot()[0]["result"] == "done"

    def test_a_raising_chime_fn_never_breaks_refuse(self):
        def boom(_job):
            raise RuntimeError("tts backend exploded")

        jt = JobTracker(chime_fn=boom)
        job_id = jt.spawn(task="x", subagent="dev_coding", depth=0)
        jt.refuse(job_id, reason="policy")  # must not propagate boom
        assert jt.snapshot()[0]["status"] == "refused"


# --------------------------------------------------------------------------- #
# delegate_task — the tool itself (unit level)
# --------------------------------------------------------------------------- #

class TestDelegateTool:
    def test_requires_active_dispatch_context(self, registry):
        """Outside a run there is no context: the tool must say so, loudly."""
        spec = registry.get_subagent("orchestrator").tools[0]
        result = spec.handler({"task": "hello"})
        assert "requires an active dispatch context" in result

    def test_empty_task_errors(self, registry, jobs):
        tool_call = _FakeToolCall("c1", "delegate_task", json.dumps({"task": "   "}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "delegate something"}],
            registry,
            client=client,
            job_tracker=jobs,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "non-empty 'task'" in result["text"]
        assert jobs.count() == 0  # no job spawned for a rejected call

    def test_unknown_subagent_errors(self, registry, jobs):
        tool_call = _FakeToolCall("c1", "delegate_task", json.dumps({"task": "x", "subagent": "nope"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "delegate"}],
            registry,
            client=client,
            job_tracker=jobs,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "unknown subagent 'nope'" in result["text"]
        assert jobs.count() == 0

    def test_bad_max_turns_errors(self, registry, jobs):
        """Contract enforcement (v2.1): the ENGINE validates args against the
        declared schema BEFORE the handler, so a string max_turns is rejected
        there — the handler's own int guard stays for direct calls."""
        tool_call = _FakeToolCall("c1", "delegate_task", json.dumps({"task": "x", "max_turns": "many"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "delegate"}],
            registry,
            client=client,
            job_tracker=jobs,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "invalid arguments" in result["text"]
        assert "max_turns" in result["text"]
        assert "integer" in result["text"]

    def test_depth_cap_refuses_deeper_nesting(self, registry, jobs):
        """Run already at depth == max_depth: delegation is refused."""
        tool_call = _FakeToolCall("c1", "delegate_task", json.dumps({"task": "x"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="fine"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "delegate"}],
            registry,
            client=client,
            job_tracker=jobs,
            depth=2,
            max_depth=2,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "maximum delegate depth (2) reached" in result["text"]
        assert jobs.count() == 0

    def test_budget_cap_refuses_when_exhausted(self, registry, jobs):
        """max_delegates=0 means no nested runs may ever spawn."""
        tool_call = _FakeToolCall("c1", "delegate_task", json.dumps({"task": "x"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="fine"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "delegate"}],
            registry,
            client=client,
            job_tracker=jobs,
            max_delegates=0,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "delegate budget exhausted" in result["text"]
        assert jobs.count() == 0


# --------------------------------------------------------------------------- #
# Recursive dispatch — the real nested run through the loop
# --------------------------------------------------------------------------- #

class TestRecursiveDispatch:
    def test_delegate_spawns_nested_run_with_job_audit(self, registry, jobs):
        """Parent delegates to echo_agent; the nested run really executes the
        echo tool and the whole tree is audit-logged as one job. The nested
        run's events ride the parent's EVENT SINK (live UI streaming), while
        the parent's TRANSCRIPT holds only the delegate_task tool result."""
        responses = [
            # parent turn 1: call delegate_task
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[
                        _FakeToolCall(
                            "c1",
                            "delegate_task",
                            json.dumps({"task": "echo hello", "subagent": "echo_agent"}),
                        )
                    ],
                )
            ),
            # nested turn 1: call echo
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c2", "echo", json.dumps({"text": "hello"}))],
                )
            ),
            # nested turn 2: answer
            _FakeResponse(_FakeMessage(content="Nested said: ECHOED: hello")),
            # parent turn 2: answer
            _FakeResponse(_FakeMessage(content="Done. The nested agent echoed hello.")),
        ]
        client = FakeClient(responses)
        sink_events: list[dict] = []
        report = run_dispatch_messages(
            [{"role": "user", "content": "handle this"}],
            registry,
            client=client,
            job_tracker=jobs,
            event_sink=lambda e: sink_events.append(e),
        )

        # The parent saw the DELEGATED TASK result with the nested final text.
        delegate_results = [
            t for t in report["transcript"]
            if t["type"] == "tool_result" and "DELEGATED TASK job-1" in t["text"]
        ]
        assert delegate_results, "expected a delegated-task result in the transcript"
        assert "Nested said: ECHOED: hello" in delegate_results[0]["text"]

        # The parent's own transcript shows only its own delegate_task call...
        parent_names = [t["name"] for t in report["transcript"] if t["type"] == "tool_use"]
        assert parent_names == ["delegate_task"]
        # ...while the NESTED run's echo tool use streams through the sink.
        sink_names = [e["name"] for e in sink_events if e["type"] == "tool_use"]
        assert "echo" in sink_names, "nested run must stream through the parent sink"

        # The audit log has exactly one DONE job with the real result.
        snap = jobs.snapshot()
        assert len(snap) == 1
        job = snap[0]
        assert job["status"] == "done"
        assert job["depth"] == 1
        assert job["subagent"] == "echo_agent"
        assert "ECHOED: hello" in job["result"]
        assert job["parent_id"] is None  # top-level delegate has no parent job

    def test_delegate_without_subagent_runs_free_sub_orchestration(self, registry, jobs):
        """Omitting 'subagent' lets the nested run use any roster tool."""
        responses = [
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c1", "delegate_task", json.dumps({"task": "say hi"}))],
                )
            ),
            _FakeResponse(_FakeMessage(content="nested done")),
            _FakeResponse(_FakeMessage(content="parent done")),
        ]
        client = FakeClient(responses)
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            registry,
            client=client,
            job_tracker=jobs,
        )
        delegate_results = [
            t for t in report["transcript"]
            if t["type"] == "tool_result" and "DELEGATED TASK" in t["text"]
        ]
        assert delegate_results
        assert "nested done" in delegate_results[0]["text"]
        assert jobs.snapshot()[0]["subagent"] is None  # free orchestration

    def test_nested_run_uses_parent_confirmation_gate(self, registry, jobs):
        """The nested run MUST inherit the parent's gate: a gated tool called
        inside the nested run blocks on the SAME gate the parent was given."""
        gated = ToolSpec(
            name="gated_echo",
            description="echo, but gated",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=lambda a: f"ECHOED: {a['text']}",
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: f"Echo {a['text']!r}?",
        )
        registry.register_subagent(
            Subagent(name="gated_agent", domain="Test", description="x", tools=(gated,))
        )
        responses = [
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[
                        _FakeToolCall("c1", "delegate_task", json.dumps({"task": "g", "subagent": "gated_agent"}))
                    ],
                )
            ),
            # nested turn 1: call the gated tool
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c2", "gated_echo", json.dumps({"text": "go"}))],
                )
            ),
            _FakeResponse(_FakeMessage(content="nested ok")),
            _FakeResponse(_FakeMessage(content="parent ok")),
        ]
        client = FakeClient(responses)
        calls: list[str] = []
        sink_events: list[dict] = []
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            registry,
            client=client,
            job_tracker=jobs,
            confirmation_gate=lambda text: calls.append(text) or True,
            event_sink=lambda e: sink_events.append(e),
        )
        # The nested run really hit the gate with the tool's confirm prompt...
        assert calls == ["Echo 'go'?"]
        # ...and the gated tool executed inside the nested run (its output
        # streams through the sink; the job record holds only final_text).
        result_events = [
            e["text"] for e in sink_events if e["type"] == "tool_result"
        ]
        assert any("ECHOED: go" in t for t in result_events)
        assert jobs.snapshot()[0]["status"] == "done"
        assert report["final_text"] == "parent ok"

    def test_delegate_target_uses_that_agents_model(self, registry, jobs, monkeypatch):
        """v3.1: when a nested run is routed AT one subagent, it runs on
        THAT agent's configured NVIDIA model (DOURMOUSE_MODEL_<AGENT>); the
        parent keeps its own default. Deterministic (Rule 2.8). Fast lane
        pinned off — this test asserts config-default plumbing, not the
        simple-response speed lane."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.config import NvidiaConfig

        config = NvidiaConfig(
            api_key="k",
            base_url="u",
            model="nvidia/parent-120b",
            agent_models={"ECHO_AGENT": "nvidia/echo-agent-70b"},
        )
        responses = [
            # parent turn 1: call delegate_task (routed at echo_agent)
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c1", "delegate_task", json.dumps({"task": "echo hello", "subagent": "echo_agent"}))],
                )
            ),
            # nested turn 1: call echo
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c2", "echo", json.dumps({"text": "hello"}))],
                )
            ),
            # nested turn 2: answer
            _FakeResponse(_FakeMessage(content="Nested said: ECHOED: hello")),
            # parent turn 2: answer
            _FakeResponse(_FakeMessage(content="parent done")),
        ]
        client = FakeClient(responses)
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            registry,
            client=client,
            config=config,
            job_tracker=jobs,
        )
        assert report["final_text"] == "parent done"
        models = [c["model"] for c in client.chat.completions.calls]
        # Parent turns use the default; BOTH nested turns use the target's
        # model. Order: parent(120b), nested(70b), nested(70b), parent(120b).
        assert models == [
            "nvidia/parent-120b",
            "nvidia/echo-agent-70b",
            "nvidia/echo-agent-70b",
            "nvidia/parent-120b",
        ]
        # The nested job really used the target agent.
        assert jobs.snapshot()[0]["subagent"] == "echo_agent"

    def test_nested_run_failure_surfaces_honestly(self, registry, jobs):
        """If the nested dispatch raises, the job is marked error and the
        parent gets the real message — never a fabricated success."""
        responses = [
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c1", "delegate_task", json.dumps({"task": "boom"}))],
                )
            ),
            _FakeResponse(_FakeMessage(content="parent ok")),
        ]

        class _ExplodingCompletions(_FakeCompletions):
            """Call 1 = parent turn 1 (returns the delegate tool call); call 2
            = the NESTED run's first LLM call, which is the one that fails."""
            def __init__(self, responses):
                super().__init__(responses)
                self._calls = 0

            def create(self, **kwargs):
                self._calls += 1
                if self._calls == 2:
                    raise RuntimeError("NVIDIA endpoint down")
                return super().create(**kwargs)

        client = FakeClient(responses)
        client.chat.completions = _ExplodingCompletions(responses)
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            registry,
            client=client,
            job_tracker=jobs,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "nested dispatch failed" in result["text"]
        assert "NVIDIA endpoint down" in result["text"]
        assert jobs.snapshot()[0]["status"] == "error"

    def test_shared_budget_bounds_whole_tree(self, registry, jobs):
        """A pre-consumed shared budget blocks the delegate even at depth 0."""
        responses = [
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c1", "delegate_task", json.dumps({"task": "x"}))],
                )
            ),
            _FakeResponse(_FakeMessage(content="parent ok")),
        ]
        client = FakeClient(responses)
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            registry,
            client=client,
            job_tracker=jobs,
            budget=[1],  # one delegate already used by a sibling branch
            max_delegates=1,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "delegate budget exhausted" in result["text"]

    def test_nested_delegate_records_true_parent_chain(self, registry, jobs):
        """A delegate INSIDE a delegated run must record the outer job as its
        parent_id — the audit log is a real tree, not a flat list."""
        responses = [
            # parent turn 1: delegate task A (free orchestration)
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c1", "delegate_task", json.dumps({"task": "A"}))],
                )
            ),
            # nested run (depth 1) turn 1: delegate task B
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c2", "delegate_task", json.dumps({"task": "B"}))],
                )
            ),
            # sub-nested run (depth 2) turn 1: answer
            _FakeResponse(_FakeMessage(content="deep done")),
            # nested turn 2: answer
            _FakeResponse(_FakeMessage(content="mid done")),
            # parent turn 2: answer
            _FakeResponse(_FakeMessage(content="top done")),
        ]
        client = FakeClient(responses)
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            registry,
            client=client,
            job_tracker=jobs,
        )
        assert report["final_text"] == "top done"
        snaps = jobs.snapshot()
        assert [j["id"] for j in snaps] == ["job-2", "job-1"]  # newest first
        outer = snaps[1]
        inner = snaps[0]
        assert outer["depth"] == 1 and outer["parent_id"] is None
        assert inner["depth"] == 2 and inner["parent_id"] == "job-1"
        assert inner["result"] == "deep done"
        assert all(j["status"] == "done" for j in snaps)

    def test_delegate_without_job_tracker_reports_untracked(self, registry):
        """No job_tracker attached: the delegate still runs but honestly says
        the job is untracked (Rule 2.2 — no fabricated tracking)."""
        responses = [
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c1", "delegate_task", json.dumps({"task": "x"}))],
                )
            ),
            _FakeResponse(_FakeMessage(content="nested ok")),
            _FakeResponse(_FakeMessage(content="parent ok")),
        ]
        client = FakeClient(responses)
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            registry,
            client=client,
            job_tracker=None,  # explicit: no audit log attached
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "(untracked)" in result["text"]
        assert "nested ok" in result["text"]


# --------------------------------------------------------------------------- #
# v8.31 — delegate_parallel: genuinely concurrent multi-agent fan-out
# --------------------------------------------------------------------------- #

class _KeyedCompletions:
    """A thread-safe fake completions endpoint for REAL concurrency tests.

    Unlike _FakeCompletions (a single ordered response queue — fine for
    delegate_task's purely SEQUENTIAL nesting), delegate_parallel calls
    this from several threads at once, so responses are looked up by a
    keyword found in the call's own last-user-turn content rather than by
    call order, and every read/write of shared state is under one lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self._by_keyword: dict[str, list[Any]] = {}

    def add(self, keyword: str, response_factory) -> None:
        """``response_factory`` is a zero-arg callable returning a
        _FakeResponse — called fresh on each matching request so it can
        introduce a real, per-call delay (time.sleep) or side effect."""
        with self._lock:
            self._by_keyword.setdefault(keyword, []).append(response_factory)

    @staticmethod
    def _effective_content(content: str) -> str:
        """Strip the boilerplate delegate_task/delegate_parallel wraps
        (the '[ROUTING DIRECTIVE] ... TASK: ' prefix, and the
        '[PARENT CONTEXT]' block appended when the parent's own recent
        turn is threaded into a nested branch's prompt) down to just the
        real instructions text — otherwise a keyword like "fan out" that
        legitimately reappears inside a nested branch's PARENT CONTEXT
        block would shadow that branch's own, more specific keyword."""
        marker = "TASK: "
        idx = content.find(marker)
        if idx != -1:
            content = content[idx + len(marker):]
        boundary = content.find("\n\n[PARENT CONTEXT")
        if boundary != -1:
            content = content[:boundary]
        return content

    def create(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        messages = kwargs.get("messages") or []
        raw_content = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                raw_content = m.get("content") or ""
                break
        content = self._effective_content(raw_content)
        for keyword, factories in self._by_keyword.items():
            if keyword in content:
                with self._lock:
                    factory = factories.pop(0) if len(factories) > 1 else factories[0]
                return factory()
        raise AssertionError(f"_KeyedCompletions: no fake response registered matching: {raw_content!r}")


class _KeyedChat:
    def __init__(self, completions: _KeyedCompletions) -> None:
        self.completions = completions


class _KeyedClient:
    """Same shape as FakeClient, backed by _KeyedCompletions."""

    def __init__(self) -> None:
        self.chat = _KeyedChat(_KeyedCompletions())


def _delegate_parallel_call(call_id: str, branches: list[dict]) -> _FakeToolCall:
    return _FakeToolCall(call_id, "delegate_parallel", json.dumps({"branches": branches}))


class TestDelegateParallelTool:
    """Unit-level guards (mirrors TestDelegateTool for delegate_task)."""

    def test_requires_active_dispatch_context(self, registry):
        spec = next(t for t in registry.get_subagent("orchestrator").tools if t.name == "delegate_parallel")
        result = spec.handler({"branches": [{"instructions": "hi"}]})
        assert "requires an active dispatch context" in result

    def test_empty_branches_errors(self, registry, jobs):
        tool_call = _delegate_parallel_call("c1", [])
        client = FakeClient([
            _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call])),
            _FakeResponse(_FakeMessage(content="ok")),
        ])
        report = run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, job_tracker=jobs,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "non-empty 'branches'" in result["text"]
        assert jobs.count() == 0

    def test_branch_missing_instructions_errors(self, registry, jobs):
        tool_call = _delegate_parallel_call("c1", [{"agent_or_task": "echo_agent"}])
        client = FakeClient([
            _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call])),
            _FakeResponse(_FakeMessage(content="ok")),
        ])
        report = run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, job_tracker=jobs,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "non-empty 'instructions'" in result["text"]
        assert jobs.count() == 0

    def test_unknown_agent_in_branch_errors(self, registry, jobs):
        tool_call = _delegate_parallel_call("c1", [{"agent_or_task": "nope", "instructions": "x"}])
        client = FakeClient([
            _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call])),
            _FakeResponse(_FakeMessage(content="ok")),
        ])
        report = run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, job_tracker=jobs,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "unknown subagent 'nope'" in result["text"]
        assert jobs.count() == 0

    def test_depth_cap_refuses_the_whole_fan_out(self, registry, jobs):
        """SAME guard as delegate_task: at max depth, delegate_parallel
        refuses outright — no branch runs at all."""
        tool_call = _delegate_parallel_call(
            "c1",
            [
                {"agent_or_task": "echo_agent", "instructions": "one"},
                {"agent_or_task": "echo_agent", "instructions": "two"},
            ],
        )
        client = FakeClient([
            _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call])),
            _FakeResponse(_FakeMessage(content="ok")),
        ])
        report = run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, job_tracker=jobs,
            depth=2, max_depth=2,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "maximum delegate depth (2) reached" in result["text"]
        assert jobs.count() == 0

    def test_budget_cap_refuses_all_branches_when_exhausted(self, registry, jobs):
        tool_call = _delegate_parallel_call(
            "c1",
            [
                {"agent_or_task": "echo_agent", "instructions": "one"},
                {"agent_or_task": "echo_agent", "instructions": "two"},
            ],
        )
        client = FakeClient([
            _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call])),
            _FakeResponse(_FakeMessage(content="ok")),
        ])
        report = run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, job_tracker=jobs,
            max_delegates=0,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "delegate budget exhausted" in result["text"]
        assert "0 of 2" in result["text"]
        assert jobs.count() == 0

    def test_budget_partial_grant_runs_only_what_fits_and_reports_the_rest_refused(self, registry, jobs):
        """The SAME shared budget delegate_task uses, spent one unit per
        branch: with only 1 unit left, exactly 1 of 3 requested branches
        runs and the aggregate honestly reports the other 2 as refused —
        a real fan-out under a real cap holds, it doesn't silently drop or
        silently over-run the budget."""
        tool_call = _delegate_parallel_call(
            "c1",
            [
                {"agent_or_task": "echo_agent", "instructions": "one"},
                {"agent_or_task": "echo_agent", "instructions": "two"},
                {"agent_or_task": "echo_agent", "instructions": "three"},
            ],
        )
        client = FakeClient([
            _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call])),
            # the ONE branch that gets budget answers directly (no tool call)
            _FakeResponse(_FakeMessage(content="branch answered")),
            _FakeResponse(_FakeMessage(content="parent done")),
        ])
        report = run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, job_tracker=jobs,
            max_delegates=1,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "1 succeeded" in result["text"]
        assert "2 of 3 requested branches REFUSED" in result["text"]
        assert jobs.count() == 1  # only the granted branch spawned a job


class TestDelegateParallelConcurrency:
    """Real concurrency: several nested runs genuinely in flight at once,
    tagged with their own agent + model, aggregated coherently."""

    def test_branches_tagged_with_their_own_agent_and_model(self, registry, jobs, monkeypatch):
        """v3.1/v8.30 per-agent model resolution, reused per branch: each
        branch's result names the REAL agent and REAL configured model
        that answered it — never blurred into one anonymous blob."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.config import NvidiaConfig

        config = NvidiaConfig(
            api_key="k", base_url="u", model="nvidia/parent-120b",
            agent_models={"ECHO_AGENT": "nvidia/echo-agent-70b"},
        )
        client = _KeyedClient()
        client.chat.completions.add(
            "fan out",
            lambda: _FakeResponse(_FakeMessage(
                content=None,
                tool_calls=[_delegate_parallel_call(
                    "c1",
                    [
                        {"agent_or_task": "echo_agent", "instructions": "say alpha"},
                        {"instructions": "say beta"},  # free sub-orchestration, no target
                    ],
                )],
            )),
        )
        client.chat.completions.add("say alpha", lambda: _FakeResponse(_FakeMessage(content="ALPHA DONE")))
        client.chat.completions.add("say beta", lambda: _FakeResponse(_FakeMessage(content="BETA DONE")))
        client.chat.completions.add("fan out", lambda: _FakeResponse(_FakeMessage(content="parent wrap-up")))

        report = run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, config=config, job_tracker=jobs,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "agent=echo_agent model=nvidia/echo-agent-70b" in result["text"]
        assert "ALPHA DONE" in result["text"]
        # the free branch is tagged "any" and reports the parent's own
        # default model as its best-effort label (see the tool's docstring
        # for why a free branch's model can't be pinned up front)
        assert "agent=any model=nvidia/parent-120b" in result["text"]
        assert "BETA DONE" in result["text"]
        assert jobs.count() == 2

    def test_branch_events_carry_real_backend_identity(self, registry, jobs, monkeypatch):
        """world-monitor-expansion (UX pass item 1): every
        delegate_parallel_branch event (both start and result phases) now
        carries the SAME real backend/local classification the top-level
        "brain" event does — from config.backend_identity(), the config
        object's real TYPE, never guessed from the model string. All
        branches share ctx.config, so the backend is identical branch to
        branch even though the reported MODEL string can differ."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.config import OllamaConfig

        config = OllamaConfig(agent_models={"ECHO_AGENT": "qwen3:8b"})
        client = _KeyedClient()
        client.chat.completions.add(
            "fan out",
            lambda: _FakeResponse(_FakeMessage(
                content=None,
                tool_calls=[_delegate_parallel_call(
                    "c1", [{"agent_or_task": "echo_agent", "instructions": "say alpha"}],
                )],
            )),
        )
        client.chat.completions.add("say alpha", lambda: _FakeResponse(_FakeMessage(content="ALPHA DONE")))
        client.chat.completions.add("fan out", lambda: _FakeResponse(_FakeMessage(content="parent wrap-up")))

        events: list[dict[str, Any]] = []
        run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, config=config,
            job_tracker=jobs, event_sink=lambda e: events.append(e),
        )
        branch_events = [e for e in events if e.get("type") == "delegate_parallel_branch"]
        assert len(branch_events) == 2  # start + result
        for e in branch_events:
            assert e["backend"] == "ollama"
            assert e["local"] is True

    def test_slow_branch_does_not_block_a_fast_branch_real_concurrency(self, registry, jobs, monkeypatch):
        """The decisive proof this is REAL concurrency and not sequential
        work disguised as parallel: two branches each sleep INSIDE their
        own fake LLM call. If they ran one at a time the whole tool call
        would take >= the sum of both sleeps; run concurrently it takes
        roughly the SLOWER branch alone.

        DOURMOUSE_FAST_LANE pinned off (same reason
        test_delegate_target_uses_that_agents_model does it): otherwise the
        very first dispatch call in a process pays a real one-time
        probe/import cold-start cost unrelated to this feature, which would
        make the timing assertion flaky. A trivial warm-up call absorbs
        whatever cold-start cost remains before the timed run starts."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        warmup_client = FakeClient([_FakeResponse(_FakeMessage(content="warm"))])
        run_dispatch_messages(
            [{"role": "user", "content": "warm up"}], registry, client=warmup_client,
        )

        client = _KeyedClient()
        client.chat.completions.add(
            "fan out",
            lambda: _FakeResponse(_FakeMessage(
                content=None,
                tool_calls=[_delegate_parallel_call(
                    "c1",
                    [
                        {"agent_or_task": "echo_agent", "instructions": "slow branch"},
                        {"agent_or_task": "echo_agent", "instructions": "fast branch"},
                    ],
                )],
            )),
        )

        def _slow():
            time.sleep(0.35)
            return _FakeResponse(_FakeMessage(content="SLOW DONE"))

        client.chat.completions.add("slow branch", _slow)
        client.chat.completions.add(
            "fast branch", lambda: _FakeResponse(_FakeMessage(content="FAST DONE"))
        )
        client.chat.completions.add("fan out", lambda: _FakeResponse(_FakeMessage(content="parent wrap-up")))

        started = time.perf_counter()
        report = run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, job_tracker=jobs,
        )
        elapsed = time.perf_counter() - started
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "SLOW DONE" in result["text"] and "FAST DONE" in result["text"]
        # Sequential delegate_task-style nesting would cost >= 0.35s for the
        # slow branch PLUS a second nested run's own overhead for the fast
        # branch (empirically ~0.7-0.9s total, warm, on this machine).
        # Genuine concurrency keeps the whole call close to the SLOWER
        # branch's own delay alone (~0.35s + one run's overhead). 0.65s
        # sits with real margin on both sides of that gap.
        assert elapsed < 0.65, f"took {elapsed:.2f}s — branches do not look concurrent"

    def test_concurrency_cap_still_runs_every_granted_branch(self, registry, jobs):
        """More branches than _MAX_CONCURRENT_DELEGATES (6): the cap bounds
        how many run AT ONCE, not how many run in total — every granted
        branch still completes and is reported."""
        n = 8
        branches = [
            {"agent_or_task": "echo_agent", "instructions": f"branch {i}"}
            for i in range(n)
        ]
        client = _KeyedClient()
        client.chat.completions.add(
            "fan out",
            lambda: _FakeResponse(_FakeMessage(
                content=None,
                tool_calls=[_delegate_parallel_call("c1", branches)],
            )),
        )
        for i in range(n):
            client.chat.completions.add(
                f"branch {i}", (lambda i=i: _FakeResponse(_FakeMessage(content=f"DONE {i}")))
            )
        client.chat.completions.add("fan out", lambda: _FakeResponse(_FakeMessage(content="parent wrap-up")))

        report = run_dispatch_messages(
            [{"role": "user", "content": "fan out"}], registry, client=client, job_tracker=jobs,
            max_delegates=n,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert f"{n} branch(es) ran" in result["text"]
        assert f"{n} succeeded" in result["text"]
        for i in range(n):
            assert f"DONE {i}" in result["text"]
        assert jobs.count() == n


# --------------------------------------------------------------------------- #
# /api/jobs — the HTTP audit surface the UI panel polls
# --------------------------------------------------------------------------- #

@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    status = resp.status
    conn.close()
    return status, body


class TestJobsEndpoint:
    def test_empty_jobs_list(self, server):
        _, port = server
        status, body = _get(port, "/api/jobs")
        assert status == 200
        data = json.loads(body)
        assert data["jobs"] == []
        assert data["count"] == 0

    def test_jobs_round_trip_through_server_tracker(self, server):
        srv, port = server
        job_id = srv.jobs.spawn(task="nested research", subagent="research_info", depth=1)
        srv.jobs.finish(job_id, result="REAL findings")
        status, body = _get(port, "/api/jobs")
        assert status == 200
        data = json.loads(body)
        assert data["count"] == 1
        job = data["jobs"][0]
        assert job["id"] == "job-1"
        assert job["status"] == "done"
        assert job["subagent"] == "research_info"
        assert job["depth"] == 1
        assert job["result"] == "REAL findings"

    def test_jobs_snapshot_lists_spanning_jobs(self, server):
        srv, port = server
        a = srv.jobs.spawn(task="task A", subagent="comms", depth=1)
        b = srv.jobs.spawn(task="task B", subagent="dev_coding", depth=2, parent_id=a)
        srv.jobs.finish(a, result="a done")
        status, body = _get(port, "/api/jobs")
        data = json.loads(body)
        assert [j["id"] for j in data["jobs"]] == ["job-2", "job-1"]  # newest first
        running = data["jobs"][0]
        assert running["id"] == "job-2" and running["status"] == "running"
        assert running["parent_id"] == "job-1"
