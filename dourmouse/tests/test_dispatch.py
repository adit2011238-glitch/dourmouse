"""General Dispatch engine tests (RUN:GENERAL).

Isolated per Integration Rule 7.3: a fake OpenAI-shaped client stands in for
NVIDIA NIM so we exercise OUR loop/registry/permission logic without a real
API key or network. The General-roster TOOLS themselves (real behavior) are
tested separately in test_general_roster.py; here the fake only shapes the
LLM side of the conversation, never the tools' output.
"""

from __future__ import annotations

import json

import pytest

import dourmouse.dispatch as dispatch_module
from dourmouse.dispatch import (
    DispatchRegistry,
    Permission,
    Subagent,
    ToolSpec,
    run_dispatch,
)


# --- shared fake client (same shape as test_orchestrator.py) ---

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


# --- minimal test subagent ---

def _echo_tool(name: str = "echo", permission: Permission = Permission.REGULAR) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="echo the text back",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=lambda a: f"ECHOED: {a['text']}",
        permission=permission,
        confirm_prompt=lambda a: f"Echo {a['text']!r}?",
    )


def _test_registry() -> DispatchRegistry:
    r = DispatchRegistry()
    r.register_subagent(
        Subagent(
            name="echo_agent",
            domain="Test",
            description="echoes text",
            tools=(_echo_tool(),),
        )
    )
    return r


class TestRegistry:
    def test_register_and_lookup(self):
        r = _test_registry()
        spec = r.lookup("echo")
        assert spec is not None
        assert spec.name == "echo"
        assert len(r.tool_specs()) == 1
        assert "echo" in r.describe_roster()

    def test_duplicate_tool_name_raises(self):
        r = _test_registry()
        with pytest.raises(ValueError, match="collision"):
            r.register_subagent(
                Subagent(
                    name="other",
                    domain="Test",
                    description="collides",
                    tools=(_echo_tool(),),
                )
            )

    def test_duplicate_subagent_raises(self):
        r = _test_registry()
        with pytest.raises(ValueError, match="already registered"):
            r.register_subagent(
                Subagent(name="echo_agent", domain="Test", description="x", tools=())
            )

    def test_open_ended_extension_registers_any_new_subagent(self):
        """The registry is the single extension point: adding a future
        Trading subagent requires NO engine changes and no collision."""
        r = _test_registry()
        r.register_subagent(
            Subagent(
                name="monitoring",
                domain="Trading",
                description="future trading monitoring agent",
                tools=(_echo_tool(name="poll_positions"),),
            )
        )
        assert r.lookup("poll_positions") is not None
        assert "Trading" in r.describe_roster()


class TestLoop:
    def test_direct_answer_no_tools(self):
        client = FakeClient([_FakeResponse(_FakeMessage(content="Hello."))])
        report = run_dispatch("Say hi", _test_registry(), client=client)
        assert report["final_text"] == "Hello."
        assert report["transcript"] == [{"type": "assistant_text", "text": "Hello."}]

    def test_regular_tool_executes(self):
        tool_call = _FakeToolCall("call_1", "echo", json.dumps({"text": "hi"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="It said hi."))
        client = FakeClient([first, second])

        report = run_dispatch("Echo hi", _test_registry(), client=client)

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert result["text"] == "ECHOED: hi"
        assert report["final_text"] == "It said hi."

    def test_tool_specs_are_passed_to_the_model(self):
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("hi", _test_registry(), client=client)
        tools = client.chat.completions.calls[0]["tools"]
        names = {t["function"]["name"] for t in tools}
        assert names == {"echo"}

    def test_unknown_tool_reports_error_not_crash(self):
        tool_call = _FakeToolCall("call_1", "not_a_tool", "{}")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])

        report = run_dispatch("x", _test_registry(), client=client)

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "unknown tool" in result["text"]

    def test_malformed_arguments_reported_not_crash(self):
        tool_call = _FakeToolCall("call_1", "echo", "{nope")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])

        report = run_dispatch("x", _test_registry(), client=client)

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "invalid JSON" in result["text"]

    def test_handler_exception_is_surfaced_not_crashed(self):
        spec = ToolSpec(
            name="boom",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: (_ for _ in ()).throw(RuntimeError("kaboom")),
        )
        r = DispatchRegistry()
        r.register_subagent(
            Subagent(name="bad", domain="Test", description="x", tools=(spec,))
        )
        tool_call = _FakeToolCall("call_1", "boom", "{}")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])

        report = run_dispatch("x", r, client=client)

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "failed" in result["text"]
        assert "kaboom" in result["text"]

    def test_max_turns_bounds_looping_model(self):
        tool_call = _FakeToolCall("call_x", "echo", json.dumps({"text": "x"}))
        looping = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        client = FakeClient([looping])

        report = run_dispatch("loop", _test_registry(), client=client, max_turns=3)

        assert report["final_text"] == ""
        assert client.chat.completions.calls.__len__() == 3
        assert report["transcript"][-1]["reason"] == "max_turns exceeded"


class TestPermissions:
    """Deterministic permission enforcement (Rule 2.8 / Section 2.9)."""

    def test_confirmation_gated_without_gate_not_executed(self):
        r = DispatchRegistry()
        r.register_subagent(
            Subagent(
                name="gated",
                domain="Test",
                description="x",
                tools=(_echo_tool(name="gated_echo", permission=Permission.REQUIRES_CONFIRMATION),),
            )
        )
        tool_call = _FakeToolCall("call_1", "gated_echo", json.dumps({"text": "secret"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="Cannot do that."))
        client = FakeClient([first, second])

        report = run_dispatch("do it", r, client=client, confirmation_gate=None)

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "CONFIRMATION REQUIRED" in result["text"]
        assert "ECHOED" not in result["text"]
        assert "NOT executed" in result["text"]

    def test_confirmation_gate_approval_executes(self):
        r = DispatchRegistry()
        r.register_subagent(
            Subagent(
                name="gated",
                domain="Test",
                description="x",
                tools=(_echo_tool(name="gated_echo", permission=Permission.REQUIRES_CONFIRMATION),),
            )
        )
        tool_call = _FakeToolCall("call_1", "gated_echo", json.dumps({"text": "go"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="done"))
        client = FakeClient([first, second])

        gate_prompts: list[str] = []
        report = run_dispatch(
            "do it", r, client=client,
            confirmation_gate=lambda text: gate_prompts.append(text) or True,
        )

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert result["text"] == "ECHOED: go"
        assert gate_prompts == ["Echo 'go'?"]

    def test_confirmation_gate_decline_not_executed(self):
        r = DispatchRegistry()
        r.register_subagent(
            Subagent(
                name="gated",
                domain="Test",
                description="x",
                tools=(_echo_tool(name="gated_echo", permission=Permission.REQUIRES_CONFIRMATION),),
            )
        )
        tool_call = _FakeToolCall("call_1", "gated_echo", json.dumps({"text": "no"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="Skipped."))
        client = FakeClient([first, second])

        report = run_dispatch("do it", r, client=client, confirmation_gate=lambda text: False)

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "DECLINED BY USER" in result["text"]
        assert "ECHOED" not in result["text"]

    def test_prohibited_never_executes(self):
        r = DispatchRegistry()
        r.register_subagent(
            Subagent(
                name="bad",
                domain="Test",
                description="x",
                tools=(_echo_tool(name="forbidden", permission=Permission.PROHIBITED),),
            )
        )
        tool_call = _FakeToolCall("call_1", "forbidden", json.dumps({"text": "tempting"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])

        report = run_dispatch("x", r, client=client, confirmation_gate=lambda t: True)

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "REFUSED" in result["text"]
        assert "prohibited" in result["text"]
        assert "ECHOED" not in result["text"]


class TestModelOverride:
    """v3.1 per-agent models: an explicit model override flows to the LLM
    call, and the default config model is used otherwise."""

    def test_model_override_is_used_for_calls(self):
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch(
            "hi", _test_registry(), client=client, model="nvidia/special-70b"
        )
        assert client.chat.completions.calls[0]["model"] == "nvidia/special-70b"

    def test_config_default_model_when_no_override(self):
        from dourmouse.config import NvidiaConfig

        config = NvidiaConfig(api_key="k", base_url="u", model="nvidia/base-120b")
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("hi", _test_registry(), client=client, config=config)
        assert client.chat.completions.calls[0]["model"] == "nvidia/base-120b"

    def test_override_beats_config_default(self):
        from dourmouse.config import NvidiaConfig

        config = NvidiaConfig(api_key="k", base_url="u", model="nvidia/base-120b")
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch(
            "hi", _test_registry(), client=client, config=config,
            model="nvidia/forced-70b",
        )
        assert client.chat.completions.calls[0]["model"] == "nvidia/forced-70b"


class TestRealClientConstruction:
    def test_builds_client_from_env_config_when_none_injected(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")  # v4.0: explicit backend
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("NVIDIA_BASE_URL", "https://example.test/v1")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")
        captured = {}

        class _FakeOpenAI:
            def __init__(self, api_key, base_url):
                captured["api_key"] = api_key
                captured["base_url"] = base_url
                self.chat = _FakeChat(
                    _FakeCompletions([_FakeResponse(_FakeMessage(content="ok"))])
                )

        monkeypatch.setattr(dispatch_module, "OpenAI", _FakeOpenAI)
        report = run_dispatch("hi", _test_registry())

        assert captured["api_key"] == "nvapi-fake-test-key"
        assert captured["base_url"] == "https://example.test/v1"
        assert report["final_text"] == "ok"

    def test_missing_api_key_raises_before_network(self, monkeypatch):
        # v4.0: force NVIDIA so the no-key path is deterministic (auto would
        # pick Ollama when a local server answers).
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        with pytest.raises(ValueError, match="NVIDIA_API_KEY is not set"):
            run_dispatch("hi", _test_registry())


class TestEndToEndThroughGeneralRoster:
    """Proves the REAL general roster works through the loop with a fake
    LLM side, including the extension case (trading subagent added later)."""

    def test_general_roster_registers_nineteen_subagents(self):
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        assert registry.subagent_names == {
            "orchestrator",
            "research_info",
            "comms",
            "scheduling",
            "dev_coding",
            "admin_ops",
            "memory",
            "system",
            "news",
            "markets",
            "rnd",
            "mail",
            "tasks",
            "code_ollama",  # v4.0: local Ollama coding backend
            "code_nvidia",
            "code_deepseek",
            "code_claude",
            "messenger",  # v3.0: inter-agent messaging
            "atlas",  # v4.0: ATLAS command-centre telemetry
        }

    def test_trading_subagent_added_later_dispatchable(self):
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        registry.register_subagent(
            Subagent(
                name="monitoring",
                domain="Trading",
                description="polls Alpaca positions",
                tools=(
                    ToolSpec(
                        name="poll_positions",
                        description="poll positions (real Alpaca call later)",
                        parameters={"type": "object", "properties": {}},
                        handler=lambda a: "POSITIONS: [] (no Alpaca configured yet)",
                    ),
                ),
            )
        )
        tool_call = _FakeToolCall("call_1", "poll_positions", "{}")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="No positions."))
        client = FakeClient([first, second])

        report = run_dispatch("check positions", registry, client=client)

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "POSITIONS" in result["text"]

    def test_real_gated_roster_tool_requires_confirmation_through_loop(self):
        """Drive the REAL send_draft tool (REQUIRES_CONFIRMATION) through the
        loop with NO gate: it must not execute and must report so honestly."""
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        tool_call = _FakeToolCall(
            "call_1",
            "send_draft",
            json.dumps({"channel": "email", "recipient": "x@y.com", "body": "hello"}),
        )
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="I will not send that."))
        client = FakeClient([first, second])

        report = run_dispatch("send it", registry, client=client, confirmation_gate=None)

        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "CONFIRMATION REQUIRED" in result["text"]
        assert "NOT executed" in result["text"]
        assert "NOT CONFIGURED" not in result["text"]  # gating fires before backend
