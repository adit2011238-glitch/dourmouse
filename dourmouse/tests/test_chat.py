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
        a 'tool' message (OpenAI-compatible APIs reject tool->user).

        v8.28: the old assertions (final_text=="", last transcript entry's
        "reason") encoded the bug this fix removed — a spent tool budget
        used to return a genuinely empty answer. It now forces one last
        tools-stripped call so the model must synthesize from whatever it
        already found; the fake client here keeps returning tool_calls even
        on that forced call, so this exercises the honest-fallback branch.
        The property this test actually exists to guard — a well-formed
        history that never ends on a bare 'tool' message — still holds and
        is still asserted below.
        """
        tool_call = _FakeToolCall("call_x", "echo", json.dumps({"text": "x"}))
        looping = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        client = FakeClient([looping])  # never stops calling tools
        session = ChatSession(_registry(), client=client, session_file=tmp_path / "s4.jsonl")

        report = session.ask("loop", max_turns=2)
        assert report["final_text"] != ""
        assert "tool budget" in report["final_text"]
        assert session.messages[-1]["role"] == "assistant"
        assert session.messages[-1]["content"] == report["final_text"]

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
        record = json.loads(session_file.read_text(encoding="utf-8").strip().splitlines()[0])
        assert record["user"] == "do the thing"
        assert record["final_text"] == ""


class TestForcedAgentThreadsThrough:
    """v13: a real bug fixed here, live-caught through an actual directive
    against the CODE screen's "docs" toolchain (a slideshow request with
    real commas in it) — ask() never threaded a REAL forced_agent through
    to dispatch.py's own run_dispatch_messages(forced_agent=...), the ONE
    mechanism dispatch.py already built specifically to bypass
    build_plan()'s comma-splitting fallback. Every focus_agent-pinned
    request from any screen's toolchain picker was relying purely on the
    webui.py-wrapped "[ROUTING DIRECTIVE]..." sentence being read
    correctly by the model instead of the dispatch-level mechanism meant
    for exactly this. Live-reproduced: a 3-sentence request with commas
    got split into 3 fragments routed to 'tasks'/'worldmonitor'/'docs'
    instead of running as one directive on 'docs'."""

    def test_forced_agent_reaches_run_dispatch_messages(self, monkeypatch, tmp_path):
        captured = {}

        def spy(messages, registry, **kwargs):
            captured.update(kwargs)
            return {"final_text": "ok", "transcript": [], "messages": messages}

        monkeypatch.setattr("dourmouse.chat.run_dispatch_messages", spy)
        session = ChatSession(
            _registry(), client=FakeClient([]), session_file=tmp_path / "s.jsonl",
        )
        session.ask("do the task, with commas, in it", forced_agent="docs")
        assert captured.get("forced_agent") == "docs"

    def test_no_forced_agent_passes_none_unchanged(self, monkeypatch, tmp_path):
        """An ordinary AUTO-routed turn (no focus_agent pinned) must keep
        build_plan()'s normal routing — forced_agent must default to None,
        not silently pin something."""
        captured = {}

        def spy(messages, registry, **kwargs):
            captured.update(kwargs)
            return {"final_text": "ok", "transcript": [], "messages": messages}

        monkeypatch.setattr("dourmouse.chat.run_dispatch_messages", spy)
        session = ChatSession(
            _registry(), client=FakeClient([]), session_file=tmp_path / "s2.jsonl",
        )
        session.ask("just a normal question")
        assert captured.get("forced_agent") is None


class TestPersistence:
    def test_audit_log_and_state_snapshot_written(self, tmp_path):
        client = FakeClient([_FakeResponse(_FakeMessage(content="Saved."))])
        session_file = tmp_path / "s" / "session_1.jsonl"
        session = ChatSession(_registry(), client=client, session_file=session_file)

        session.ask("remember this")

        assert session_file.exists()
        record = json.loads(session_file.read_text(encoding="utf-8").strip().splitlines()[0])
        assert record["user"] == "remember this"
        assert record["final_text"] == "Saved."
        assert session._state_file.exists()
        state = json.loads(session._state_file.read_text(encoding="utf-8"))
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

    def test_record_slash_does_not_raise_and_numbers_turn(self, tmp_path):
        # Regression: record_slash() used to raise NameError on every call
        # (dead tail code referenced undefined `report`/`elapsed_ms`), a
        # traceback silently swallowed at the webui.py call site.
        client = FakeClient([_FakeResponse(_FakeMessage(content="One."))])
        session_file = tmp_path / "slash.jsonl"
        session = ChatSession(_registry(), client=client, session_file=session_file)

        session.ask("first turn")
        assert session._turn_count == 1

        session.record_slash("/claude hello", "backend output", tools=["slash:claude"])
        assert session._turn_count == 2

        lines = session_file.read_text(encoding="utf-8").strip().splitlines()
        slash_record = json.loads(lines[-1])
        # "turn" numbers THIS turn (matches ask()'s pre-persist increment),
        # not the previous one.
        assert slash_record["turn"] == 2
        assert slash_record["user"] == "/claude hello"
        assert slash_record["final_text"] == "backend output"
        assert {"type": "tool_use", "name": "slash:claude", "raw_arguments": "{}"} in \
            slash_record["transcript"]

    def test_record_slash_as_first_turn(self, tmp_path):
        session_file = tmp_path / "slash_first.jsonl"
        session = ChatSession(_registry(), client=FakeClient([]), session_file=session_file)

        session.record_slash("/all status", "ok")
        assert session._turn_count == 1
        record = json.loads(session_file.read_text(encoding="utf-8").strip().splitlines()[0])
        assert record["turn"] == 1
        assert record["user"] == "/all status"

    def test_display_text_and_screen_persisted_separately_from_wrapped_prompt(self, tmp_path):
        """v13: a real bug fixed — webui.py wraps a focus_agent turn's
        prompt into a "[ROUTING DIRECTIVE] Complete this task using ONLY
        the '<agent>' subagent..." instruction before calling ask(), and
        that wrapped text used to be the ONLY thing persisted — a page
        reload's session-restore then showed the internal wrapper to the
        user verbatim instead of what they actually typed. `user` must
        still be the exact wrapped text the model saw (unchanged — the
        audit ledger's contract for "what was sent" doesn't change);
        `display_text`/`screen` are additive fields for restore only."""
        client = FakeClient([_FakeResponse(_FakeMessage(content="OK-CLAUDE"))])
        session_file = tmp_path / "wrapped.jsonl"
        session = ChatSession(_registry(), client=client, session_file=session_file)

        wrapped = (
            "[ROUTING DIRECTIVE] Complete this task using ONLY the "
            "'code_claude' subagent and its tools; do not use any other "
            "subagent's tools. TASK: reply with the exact text OK-CLAUDE"
        )
        session.ask(
            wrapped,
            display_text="reply with the exact text OK-CLAUDE",
            screen="CODE",
        )

        record = json.loads(session_file.read_text(encoding="utf-8").strip().splitlines()[0])
        assert record["user"] == wrapped
        assert record["display_text"] == "reply with the exact text OK-CLAUDE"
        assert record["screen"] == "CODE"
        # The wrapped text is still what the model actually saw.
        assert session.messages[1] == {"role": "user", "content": wrapped}

    def test_display_text_and_screen_default_when_not_given(self, tmp_path):
        """An ordinary AUTO-routed turn (no focus_agent, so webui.py never
        builds a wrapper) passes neither kwarg — must persist exactly as it
        always did, falling back to the plain prompt and HOME."""
        client = FakeClient([_FakeResponse(_FakeMessage(content="hi"))])
        session_file = tmp_path / "plain.jsonl"
        session = ChatSession(_registry(), client=client, session_file=session_file)

        session.ask("say hi")

        record = json.loads(session_file.read_text(encoding="utf-8").strip().splitlines()[0])
        assert record["user"] == "say hi"
        assert record["display_text"] == "say hi"
        assert record["screen"] == "HOME"

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
