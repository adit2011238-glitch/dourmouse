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

    def test_delegate_target_uses_that_agents_model(self, registry, jobs):
        """v3.1: when a nested run is routed AT one subagent, it runs on
        THAT agent's configured NVIDIA model (DOURMOUSE_MODEL_<AGENT>); the
        parent keeps its own default. Deterministic (Rule 2.8)."""
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
