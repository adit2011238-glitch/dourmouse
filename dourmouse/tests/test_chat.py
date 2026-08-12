"""ChatSession (Cowork-style conversational front end) tests.

Same isolation discipline as the rest of the suite: a fake OpenAI-shaped
client stands in for NVIDIA NIM so we exercise OUR conversation/persistence
logic without a real API key or network (Integration Rule 7.3).
"""

from __future__ import annotations

import json
import time

import pytest

from dourmouse.chat import ChatSession
from dourmouse.dispatch import DispatchRegistry, Permission, Subagent, ToolSpec


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
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        handler=lambda a: f"ECHOED: {a['text']}",
    )


def _registry() -> DispatchRegistry:
    r = DispatchRegistry()
    r.register_subagent(Subagent(name="echo_agent", domain="Test", description="x", tools=(_echo_tool(),)))
    return r


class TestMultiTurnMemory:
    def test_history_grows_across_turns(self, tmp_path):
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="First answer.")),
                _FakeResponse(_FakeMessage(content="Second answer.")),
            ]
        )
        session = ChatSession(_registry(), client=client, session_file=tmp_path / "s1.jsonl")
        assert len(session.messages) == 1  # system only

        r1 = session.ask("hi")
        assert r1["final_text"] == "First answer."
        roles1 = [m["role"] for m in session.messages]
        assert roles1 == ["system", "user", "assistant"]

        r2 = session.ask("again")
        assert r2["final_text"] == "Second answer."
        roles2 = [m["role"] for m in session.messages]
        assert roles2 == ["system", "user", "assistant", "user", "assistant"]

    def test_full_history_is_sent_to_model(self, tmp_path, monkeypatch):
        # The fast lane (v5.x) swaps the roster prompt for a compact style-
        # only prompt on pure-chat turns; this test asserts the FULL roster
        # is carried every turn, so pin the fast lane off (same pattern as
        # test_learn.py).
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="First.")),
                _FakeResponse(_FakeMessage(content="Second.")),
            ]
        )
        session = ChatSession(_registry(), client=client, session_file=tmp_path / "s2.jsonl")
        session.ask("hello")
        session.ask("world")

        sent = client.chat.completions.calls[1]["messages"]
        contents = [m.get("content") for m in sent]
        assert "hello" in contents
        assert "First." in contents
        assert "world" in contents
        # System prompt carried every turn.
        assert sent[0]["role"] == "system"
        assert "ROSTER" in sent[0]["content"]

    def test_tool_call_flow_appends_to_history(self, tmp_path):
        tool_call = _FakeToolCall("call_1", "echo", json.dumps({"text": "hi"}))
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call])),
                _FakeResponse(_FakeMessage(content="It said hi.")),
            ]
        )
        session = ChatSession(_registry(), client=client, session_file=tmp_path / "s3.jsonl")
        report = session.ask("echo hi")

        assert report["final_text"] == "It said hi."
        roles = [m["role"] for m in session.messages]
        assert "tool" in roles
        assert "assistant" in roles

    def test_max_turns_exhaustion_keeps_history_well_formed(self, tmp_path):
        """After max_turns is hit mid tool-loop, the history must not end on
        a 'tool' message (OpenAI-compatible APIs reject tool->user)."""
        tool_call = _FakeToolCall("call_x", "echo", json.dumps({"text": "x"}))
        looping = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        client = FakeClient([looping])  # never stops calling tools
        session = ChatSession(_registry(), client=client, session_file=tmp_path / "s4.jsonl")

        report = session.ask("loop", max_turns=2)
        assert report["final_text"] == ""
        assert report["transcript"][-1]["reason"] == "max_turns exceeded"
        assert session.messages[-1]["role"] == "assistant"

    def test_resume_reinjects_current_system_prompt(self, tmp_path):
        from dourmouse.dispatch import system_message

        client = FakeClient([_FakeResponse(_FakeMessage(content="One."))])
        session_file = tmp_path / "s5.jsonl"
        session = ChatSession(_registry(), client=client, session_file=session_file)
        session.ask("first")

        # Simulate the registry gaining a new tool before resume.
        r2 = _registry()
        r2.register_subagent(Subagent(name="new", domain="Test", description="y", tools=(_echo_tool(name="new_tool"),)))
        resumed = ChatSession(r2, client=client, session_file=session_file)
        assert "new_tool" in resumed.messages[0]["content"]
        assert "ROSTER" in resumed.messages[0]["content"]
        assert system_message(_registry()) != resumed.messages[0]["content"]

    def test_long_lived_session_turn_not_budget_exhausted(self, tmp_path):
        """A session alive past max_wall_seconds must still answer.

        The web UI reuses ONE ChatSession for the whole life of the server, so
        its BudgetTracker is born at startup. The 600s wall cap must apply per
        request tree, not per session — otherwise every directive after ten
        minutes of uptime dies instantly with BUDGET EXHAUSTED.
        """
        client = FakeClient([_FakeResponse(_FakeMessage(content="Latest headlines."))])
        session = ChatSession(_registry(), client=client, session_file=tmp_path / "s6.jsonl")
        session.cost_budget._started = time.monotonic() - 700.0  # 100s past the cap
        assert "BUDGET EXHAUSTED" in session.cost_budget.check()
        report = session.ask("send an agent to fetch the latest news")
        # The answer comes through (no BUDGET EXHAUSTED); the plan-step
        # caveat is expected because the model answered without calling the
        # fetch tool — that is the honesty contract, not a budget failure.
        assert report["final_text"].startswith("Latest headlines.")
        assert not any(t["type"] == "budget_exhausted" for t in report["transcript"])


class TestFailurePaths:
    def test_api_failure_keeps_history_well_formed_and_persists(self, tmp_path):
        class _RaisingCompletions:
            def create(self, **kwargs):
                raise RuntimeError("nvidia api down")

        class _RaisingChat:
            def __init__(self):
                self.completions = _RaisingCompletions()

        class _RaisingClient:
            def __init__(self):
                self.chat = _RaisingChat()

        session_file = tmp_path / "fail.jsonl"
        session = ChatSession(_registry(), client=_RaisingClient(), session_file=session_file)

        with pytest.raises(RuntimeError, match="nvidia api down"):
            session.ask("do the thing")

        # History stays well-formed: ends on assistant, not a bare user.
        assert session.messages[-1]["role"] == "assistant"
        assert session._turn_count == 1
        # The failed turn was still persisted honestly (empty final_text).
        assert session_file.exists()
        record = json.loads(session_file.read_text().strip().splitlines()[0])
        assert record["user"] == "do the thing"
        assert record["final_text"] == ""


class TestPersistence:
    def test_audit_log_and_state_snapshot_written(self, tmp_path):
        client = FakeClient([_FakeResponse(_FakeMessage(content="Saved."))])
        session_file = tmp_path / "s" / "session_1.jsonl"
        session = ChatSession(_registry(), client=client, session_file=session_file)

        session.ask("remember this")

        assert session_file.exists()
        record = json.loads(session_file.read_text().strip().splitlines()[0])
        assert record["user"] == "remember this"
        assert record["final_text"] == "Saved."
        assert session._state_file.exists()
        state = json.loads(session._state_file.read_text())
        assert [m["role"] for m in state] == ["system", "user", "assistant"]

    def test_resume_rebuilds_history_from_state(self, tmp_path):
        client = FakeClient([_FakeResponse(_FakeMessage(content="One."))])
        session_file = tmp_path / "session.jsonl"
        session = ChatSession(_registry(), client=client, session_file=session_file)
        session.ask("first turn")
        assert session._turn_count == 1

        # New session on the same file resumes prior state.
        resumed = ChatSession(_registry(), client=client, session_file=session_file)
        assert [m["role"] for m in resumed.messages] == ["system", "user", "assistant"]
        assert resumed._turn_count == 1

        # Continuing appends after the resumed history.
        client.chat.completions._responses = [_FakeResponse(_FakeMessage(content="Two."))]
        r = resumed.ask("second turn")
        assert r["final_text"] == "Two."
        assert resumed._turn_count == 2

    def test_corrupt_state_raises_not_silent(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        (tmp_path / "session.messages.json").write_text("{not json")
        with pytest.raises(RuntimeError, match="cannot resume"):
            ChatSession(_registry(), client=FakeClient([]), session_file=session_file)


class TestGatedToolThroughChat:
    def test_confirmation_gate_wired_to_session(self, tmp_path):
        gated = ToolSpec(
            name="gated_echo",
            description="gated",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            handler=lambda a: f"ECHOED: {a['text']}",
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: "proceed?",
        )
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="g", domain="Test", description="x", tools=(gated,)))

        tool_call = _FakeToolCall("call_1", "gated_echo", json.dumps({"text": "x"}))
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call])),
                _FakeResponse(_FakeMessage(content="Blocked.")),
            ]
        )
        # No gate attached => tool must NOT execute.
        session = ChatSession(r, client=client, session_file=tmp_path / "g1.jsonl", confirmation_gate=None)
        report = session.ask("do it")
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "CONFIRMATION REQUIRED" in result["text"]
        assert "ECHOED" not in result["text"]

        # With an approving gate => executes.
        tool_call2 = _FakeToolCall("call_2", "gated_echo", json.dumps({"text": "y"}))
        client2 = FakeClient(
            [
                _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call2])),
                _FakeResponse(_FakeMessage(content="Done.")),
            ]
        )
        session2 = ChatSession(r, client=client2, session_file=tmp_path / "g2.jsonl", confirmation_gate=lambda t: True)
        report2 = session2.ask("do it")
        result2 = next(t for t in report2["transcript"] if t["type"] == "tool_result")
        assert result2["text"] == "ECHOED: y"


class TestChatImportLaziness:
    def test_chat_module_importable_without_general_roster(self):
        # chat.py must import without pulling tool backends (no cycle).
        import dourmouse.chat as chat_module  # noqa: F401

        assert callable(chat_module.ChatSession)
