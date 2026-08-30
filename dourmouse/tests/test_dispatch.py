"""General Dispatch engine tests (RUN:GENERAL).

Isolated per Integration Rule 7.3: a fake OpenAI-shaped client stands in for
NVIDIA NIM so we exercise OUR loop/registry/permission logic without a real
API key or network. The General-roster TOOLS themselves (real behavior) are
tested separately in test_general_roster.py; here the fake only shapes the
LLM side of the conversation, never the tools' output.
"""

from __future__ import annotations

import json
import time

import pytest

import dourmouse.dispatch as dispatch_module
from dourmouse.dispatch import (
    DispatchRegistry,
    Permission,
    Subagent,
    ToolSpec,
    run_dispatch,
)

# --- streaming fakes (v4.1) --------------------------------------------- #

class _FakeStreamDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeStreamChoice:
    def __init__(self, delta):
        self.delta = delta


class _FakeStreamChunk:
    def __init__(self, delta):
        self.choices = [_FakeStreamChoice(delta)]


class _FakeStreamFn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _FakeStreamToolCallDelta:
    def __init__(self, index=0, tc_id=None, name=None, arguments=None):
        self.index = index
        self.id = tc_id
        self.function = _FakeStreamFn(name, arguments)


class _FakeStreamingCompletions:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self._chunks)


class _FakeStreamingClient:
    def __init__(self, chunks):
        self.chat = type("C", (), {"completions": _FakeStreamingCompletions(chunks)})()


# --------------------------------------------------------------------------- #
# v4.1 streaming — token deltas to the UI, tool calls accumulated from chunks
# --------------------------------------------------------------------------- #

class TestStreaming:
    def test_stream_completion_emits_deltas_and_assembles_text(self):
        client = _FakeStreamingClient(
            [
                _FakeStreamChunk(_FakeStreamDelta(content="Hel")),
                _FakeStreamChunk(_FakeStreamDelta(content="lo, ")),
                _FakeStreamChunk(_FakeStreamDelta(content="world.")),
            ]
        )
        deltas: list[str] = []
        resp = dispatch_module._stream_completion(
            client, "qwen3:8b", [{"role": "user", "content": "hi"}], [], None, deltas.append
        )
        assert "".join(deltas) == "Hello, world."
        assert resp.choices[0].message.content == "Hello, world."
        assert resp.choices[0].message.tool_calls is None

    def test_stream_completion_accumulates_tool_calls(self):
        client = _FakeStreamingClient(
            [
                _FakeStreamChunk(_FakeStreamDelta(tool_calls=[_FakeStreamToolCallDelta(0, "call_9", "news_headlines", '{"max_')])),
                _FakeStreamChunk(_FakeStreamDelta(tool_calls=[_FakeStreamToolCallDelta(0, None, None, 'results": 3}')])),
            ]
        )
        resp = dispatch_module._stream_completion(
            client, "qwen3:8b", [{"role": "user", "content": "hi"}], [], None, lambda t: None
        )
        msg = resp.choices[0].message
        assert msg.content == ""
        assert msg.tool_calls is not None and len(msg.tool_calls) == 1
        tc = msg.tool_calls[0]
        assert tc.id == "call_9"
        assert tc.function.name == "news_headlines"
        assert tc.function.arguments == '{"max_results": 3}'


class TestOllamaNativeClient:
    """The native /api/chat adapter — fast, streamed, thinking disabled."""

    def _client(self, post, cfg=None):
        from dourmouse.config import OllamaConfig

        return dispatch_module.OllamaNativeClient(cfg or OllamaConfig(), _post=post)

    def test_complete_returns_openai_shaped_message(self):
        captured: dict = {}

        def fake_post(payload):
            captured.update(payload)
            return json.dumps({
                "message": {
                    "role": "assistant",
                    "content": "Hello there.",
                    "tool_calls": [{
                        "function": {"name": "news_headlines", "arguments": {"max_results": 3}},
                    }],
                },
                "done": True,
            })

        client = self._client(fake_post)
        resp = client.chat.completions.create(
            model="qwen3:8b", messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function"}], stream=False,
        )
        msg = resp.choices[0].message
        assert msg.content == "Hello there."
        assert msg.tool_calls is not None and len(msg.tool_calls) == 1
        assert msg.tool_calls[0].function.name == "news_headlines"
        # native arguments arrive as a dict; the adapter stringifies them
        assert json.loads(msg.tool_calls[0].function.arguments) == {"max_results": 3}
        # the body carries the speed fixes
        assert captured["think"] is False
        assert captured["keep_alive"] == dispatch_module._OLLAMA_KEEP_ALIVE
        assert captured["options"]["num_ctx"] == dispatch_module._OLLAMA_NUM_CTX
        assert captured["options"]["num_predict"] == dispatch_module._DEFAULT_MAX_TOKENS

    def _capture(self, model: str, content: str = "hi"):
        """Run one create() against ``model`` and return the sent payload."""
        captured: dict = {}

        def fake_post(payload):
            captured.update(payload)
            return json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True})

        self._client(fake_post).chat.completions.create(
            model=model, messages=[{"role": "user", "content": content}], stream=False,
        )
        return captured

    def test_think_flag_kept_for_models_that_honour_it(self):
        """qwen3:8b respects think:False (measured 4.4s median / 25 tokens), so
        the flag stays and the prompt is left alone."""
        sent = self._capture("qwen3:8b")
        assert sent["think"] is False
        assert sent["enable_thinking"] is False
        assert sent["messages"][-1]["content"] == "hi"

    def test_no_think_switch_used_for_models_that_ignore_the_flag(self):
        """qwen3:4b IGNORES think:False and emits its reasoning as the answer
        (measured 45.1s median / 360 tokens, vs 21.5s / 178 with /no_think).
        For those models the dead flags are dropped and the documented soft
        switch goes on the last user turn instead."""
        sent = self._capture("qwen3:4b")
        assert "think" not in sent
        assert "enable_thinking" not in sent
        assert sent["messages"][-1]["content"] == "hi /no_think"

    def test_no_think_targets_only_the_last_user_turn(self):
        captured: dict = {}

        def fake_post(payload):
            captured.update(payload)
            return json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True})

        self._client(fake_post).chat.completions.create(
            model="qwen3:4b",
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "second"},
            ],
            stream=False,
        )
        sent = captured["messages"]
        assert sent[0]["content"] == "sys"
        assert sent[1]["content"] == "first"  # earlier turns untouched
        assert sent[-1]["content"] == "second /no_think"

    def test_no_think_model_list_is_configurable(self, monkeypatch):
        """A future Ollama build may fix qwen3:4b or break another model —
        retuning must not need a code change."""
        monkeypatch.setenv("DOURMOUSE_NO_THINK_MODELS", "qwen3:8b")
        assert dispatch_module._ignores_think_flag("qwen3:8b")
        assert not dispatch_module._ignores_think_flag("qwen3:4b")

    def test_stream_yields_delta_chunks(self):
        lines = "\n".join(
            json.dumps({"message": {"role": "assistant", "content": c}}) for c in ("Hel", "lo", " world")
        )

        def fake_post(payload):
            assert payload["stream"] is True
            return lines

        client = self._client(fake_post)
        chunks = list(client.chat.completions.create(
            model="qwen3:8b", messages=[{"role": "user", "content": "hi"}], stream=True,
        ))
        text = "".join(c.choices[0].delta.content for c in chunks)
        assert text == "Hello world"

    def test_history_translated_to_native_format(self):
        """OpenAI-format tool_calls in history must become Ollama-native
        (arguments as an object, no id/type) or the native decoder 400s."""
        captured: dict = {}

        def fake_post(payload):
            captured["payload"] = payload
            return json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True})

        client = self._client(fake_post)
        client.chat.completions.create(
            model="qwen3:8b",
            messages=[
                {"role": "user", "content": "news"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1", "type": "function",
                        "function": {"name": "news_headlines", "arguments": '{"max_results": 3}'},
                    }],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "LIVE NEWS"},
            ],
            stream=False,
        )
        sent = captured["payload"]["messages"]
        ass = sent[1]
        assert "id" not in ass["tool_calls"][0]
        assert "type" not in ass["tool_calls"][0]
        assert ass["tool_calls"][0]["function"]["arguments"] == {"max_results": 3}
        assert sent[2] == {"role": "tool", "content": "LIVE NEWS"}


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


def _unique_tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda a: "ok",
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

    def test_extend_subagent_shares_same_tool_object(self):
        """v5.8: the SAME ToolSpec can ride multiple agents (one registry
        slot), and the schema is emitted once per name in the scoped set."""
        r = DispatchRegistry()
        tool = ToolSpec(
            name="shared_tool",
            description="shared across agents",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda a: "ok",
        )
        for name in ("one", "two", "three"):
            r.register_subagent(
                Subagent(name=name, domain="Test", description="x", tools=(tool,))
            )
        assert r.lookup("shared_tool") is tool
        assert len(r.tool_specs()) == 1
        # A DIFFERENT object claiming the name still raises (anti-shadowing).
        other = ToolSpec(
            name="shared_tool",
            description="different",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda a: "nope",
        )
        with pytest.raises(ValueError, match="collision"):
            r.register_subagent(
                Subagent(name="four", domain="Test", description="x", tools=(other,))
            )

    def test_extend_subagent_attaches_to_existing(self):
        r = _test_registry()
        r.register_subagent(
            Subagent(name="other", domain="Test", description="x", tools=())
        )
        tool = _unique_tool("new_tool")
        r.extend_subagent("other", tool)
        sub = r.get_subagent("other")
        assert sub is not None and tool in sub.tools
        # Idempotent — a repeat call does not duplicate the tool.
        r.extend_subagent("other", tool)
        assert sum(1 for t in sub.tools if t is tool) == 1

    def test_extend_subagent_rejects_different_object_same_name(self):
        """extending with a DIFFERENT object claiming an existing name still
        raises — the anti-shadowing invariant survives extend_subagent."""
        r = DispatchRegistry()
        r.register_subagent(
            Subagent(name="echo_agent", domain="Test", description="x", tools=(_echo_tool(),))
        )
        with pytest.raises(ValueError, match="collision"):
            r.extend_subagent("echo_agent", _unique_tool("echo"))

    def test_extend_subagent_unknown_agent_raises(self):
        r = _test_registry()
        with pytest.raises(ValueError, match="no subagent"):
            r.extend_subagent("nope", _unique_tool("brand_new"))

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
        """v4.1 scoping: plain chat sends NO tool schemas — the 80s-prefill
        fix (all 60 schemas cost ~5,457 tokens of cold prefill)."""
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("hi", _test_registry(), client=client)
        assert client.chat.completions.calls[0]["tools"] == []

    def test_planned_agents_tool_specs_are_passed(self):
        """An agentic prompt scopes the schemas to the plan's agents, so the
        model still sees exactly the tools it needs to execute."""
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("echo hello and then echo goodbye", _test_registry(), client=client)
        tools = client.chat.completions.calls[0]["tools"]
        names = {t["function"]["name"] for t in tools}
        assert names == {"echo"}

    def test_single_step_directive_scopes_target_agent_tools(self):
        """v5.2: a SINGLE-step agentic directive (no plan) must still scope
        the best-matching agent's tools — otherwise the model answers blind
        and can never actually check the inbox / fetch a quote."""
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("check my inbox", registry, client=client)
        tools = client.chat.completions.calls[0]["tools"]
        names = {t["function"]["name"] for t in tools}
        assert "read_inbox" in names, f"mail tools not scoped for inbox request: {names}"

    def test_pure_chat_still_sends_no_tools(self):
        """v5.2: a single-step prompt with NO agent match stays a pure chat
        question — zero schemas, the fastest path (no regression of the
        prefill fix)."""
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="42."))])
        run_dispatch("what is 2+2", registry, client=client)
        assert client.chat.completions.calls[0]["tools"] == []

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
        # v8.28: a model that never stops calling tools used to run out the
        # loop and return final_text="" — observed live on a real question
        # ("latest stable PyTorch version"): 8 real tool calls, then a bare
        # "No reply." with nothing to show for all that work. The fix forces
        # one last LLM call with NO tools offered once the budget is spent,
        # so the model is physically unable to keep looping and must answer
        # in text. Here the fake client keeps returning a tool-call response
        # even on that forced call (it doesn't simulate tools=[] narrowing
        # its own behavior), so this specifically exercises the honest
        # fallback path: still never an empty final_text.
        tool_call = _FakeToolCall("call_x", "echo", json.dumps({"text": "x"}))
        looping = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        client = FakeClient([looping])

        report = run_dispatch("loop", _test_registry(), client=client, max_turns=3)

        # Rule 2.2 in practice: a spent tool budget must never look like
        # silence. Real tool work happened (3 turns' worth) — the user gets
        # an honest account of that, not a blank box.
        assert report["final_text"] != ""
        assert "tool budget" in report["final_text"]
        # 3 turns to exhaust max_turns, +1 forced tools=[] synthesis call.
        assert client.chat.completions.calls.__len__() == 4
        assert client.chat.completions.calls[-1]["tools"] == []
        exhausted = next(
            t for t in report["transcript"]
            if t.get("type") == "budget_exhausted" and "max_turns" in t.get("reason", "")
        )
        assert "forcing a synthesis answer" in exhausted["reason"]
        assert report["transcript"][-1]["type"] == "assistant_text"
        assert report["transcript"][-1]["text"] == report["final_text"]

    def test_max_turns_forced_call_synthesizes_from_partial_research(self):
        # The primary case the fix targets: max_turns is spent on real tool
        # work (unlike the fallback-path test above), and the forced,
        # tools-stripped call actually comes back with real text — the model
        # summarizing what it already found instead of continuing to search.
        # This is what should happen for something like "latest stable
        # PyTorch version" after a few searches: a real, if imperfect,
        # answer — never a blank "No reply."
        tool_call = _FakeToolCall("call_x", "echo", json.dumps({"text": "x"}))
        looping = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        synthesized = _FakeResponse(
            _FakeMessage(content="Based on the search results, the answer is X.")
        )
        client = FakeClient([looping, looping, looping, synthesized])

        report = run_dispatch("loop", _test_registry(), client=client, max_turns=3)

        assert report["final_text"] == "Based on the search results, the answer is X."
        assert client.chat.completions.calls.__len__() == 4
        assert client.chat.completions.calls[-1]["tools"] == []


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

    def test_config_default_model_when_no_override(self, monkeypatch):
        # Override-plumbing test: pin the fast lane OFF so it asserts the
        # config default flows through, not the lane's fast model.
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.config import NvidiaConfig

        config = NvidiaConfig(api_key="k", base_url="u", model="nvidia/base-120b")
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("hi", _test_registry(), client=client, config=config)
        assert client.chat.completions.calls[0]["model"] == "nvidia/base-120b"

    def test_fast_lane_uses_small_model_for_pure_chat(self, monkeypatch):
        """v5.x: a PURE-CHAT turn (no plan, no agent match) answers on the
        small local fast model — the "simple response" speed lane."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_MODEL", "qwen3:4b")
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="4."))])
        run_dispatch("what is 2+2", registry, client=client)
        assert client.chat.completions.calls[0]["model"] == "qwen3:4b"

    def test_fast_lane_skips_agentic_and_explicit(self, monkeypatch):
        """An agentic directive (tools scoped) and an explicit model override
        must NOT hit the fast lane — deterministic override wins."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_MODEL", "qwen3:4b")
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("check my inbox", registry, client=client)
        assert client.chat.completions.calls[0]["model"] != "qwen3:4b"

    def test_auto_routed_single_agent_match_uses_that_agents_model(self, monkeypatch):
        # v8.30: the one gap in per-agent model routing — an explicit
        # focus_agent route and a delegate_task nested run both already
        # resolved model_for_agent(target) BEFORE this fix, since the
        # target agent is known up front in both cases. A plain, auto-
        # routed top-level directive with no focus_agent had no such
        # signal: the target agent isn't known until AFTER
        # find_agents_for_query runs, so it always fell back to the
        # single default model. "check my inbox" deterministically
        # resolves to exactly one agent (mail, per the test directly
        # above) with no explicit override and no fast-lane eligibility
        # (it's agentic, not pure chat) — exactly the case this closes.
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.config import NvidiaConfig
        from dourmouse.general_roster import build_general_registry

        config = NvidiaConfig(
            api_key="k", base_url="u", model="nvidia/base-120b",
            agent_models={"MAIL": "nvidia/mail-tuned-8b"},
        )
        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("check my inbox", registry, client=client, config=config)
        assert client.chat.completions.calls[0]["model"] == "nvidia/mail-tuned-8b"

    def test_multi_agent_plan_keeps_default_model_not_a_guess(self, monkeypatch):
        # Deliberately conservative: when the deterministic match resolves
        # to MORE than one agent, the run keeps the already-resolved
        # general-purpose model rather than guessing which step should own
        # the whole run's model choice.
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.config import NvidiaConfig
        from dourmouse.general_roster import build_general_registry

        config = NvidiaConfig(
            api_key="k", base_url="u", model="nvidia/base-120b",
            agent_models={"MAIL": "nvidia/mail-tuned-8b", "TASKS": "nvidia/tasks-tuned-8b"},
        )
        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        # A real multi-step directive: build_plan should split this into
        # more than one agent (mail + tasks), so no single per-agent model
        # is unambiguous.
        run_dispatch(
            "check my inbox and then add a task to follow up",
            registry, client=client, config=config,
        )
        assert client.chat.completions.calls[0]["model"] == "nvidia/base-120b"

        client2 = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("just say hi", registry, client=client2, model="nvidia/special-70b")
        assert client2.chat.completions.calls[0]["model"] == "nvidia/special-70b"

    def test_single_agent_routing_with_a_real_event_sink_does_not_crash(self, monkeypatch):
        # Regression guard: the per-agent routing refinement above used a
        # bare `depth` where only `ctx.depth` exists inside
        # _run_dispatch_loop — a real NameError. It was fully masked in
        # every other test here because none of them passed an
        # event_sink, so `event_sink is not None and depth == 0` never
        # evaluated its second operand (Python's `and` short-circuits).
        # This test exists specifically so a real event_sink is present,
        # which is what actually exercises the buggy line.
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.config import NvidiaConfig
        from dourmouse.general_roster import build_general_registry

        config = NvidiaConfig(
            api_key="k", base_url="u", model="nvidia/base-120b",
            agent_models={"MAIL": "nvidia/mail-tuned-8b"},
        )
        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        events = []
        from dourmouse.dispatch import run_dispatch_messages, system_message

        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "check my inbox"},
        ]
        run_dispatch_messages(
            messages, registry, client=client, config=config,
            event_sink=lambda e: events.append(e),
        )
        assert client.chat.completions.calls[0]["model"] == "nvidia/mail-tuned-8b"
        brain_events = [e for e in events if e.get("type") == "brain"]
        assert any(e["model"] == "nvidia/mail-tuned-8b" for e in brain_events)
        # UX pass item 1: the per-agent-routed refinement's own "brain"
        # event carries the SAME real backend identity as the initial one
        # — it's still ctx.config (NvidiaConfig here), only the MODEL
        # string changed.
        refined = next(e for e in brain_events if e["model"] == "nvidia/mail-tuned-8b")
        assert refined["backend"] == "nvidia"
        assert refined["local"] is False

    def test_initial_brain_event_reports_ollama_local(self, monkeypatch):
        """world-monitor-expansion (UX pass item 1): the plain top-level
        "brain" event (emitted once per top-level run, before any per-
        agent refinement) carries real backend identity too — an
        OllamaConfig reports local, not just NVIDIA reporting cloud
        (covered by the sibling test above)."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.config import OllamaConfig
        from dourmouse.general_roster import build_general_registry
        from dourmouse.dispatch import run_dispatch_messages, system_message

        config = OllamaConfig(model="qwen3:8b")
        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        events: list[dict] = []
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "just say hi"},
        ]
        run_dispatch_messages(
            messages, registry, client=client, config=config,
            event_sink=lambda e: events.append(e),
        )
        brain_events = [e for e in events if e.get("type") == "brain"]
        assert brain_events
        assert brain_events[0]["backend"] == "ollama"
        assert brain_events[0]["local"] is True

    def test_fast_lane_takes_knowledge_questions(self, monkeypatch):
        """A stable-fact question ("what is the tallest mountain...") answers
        on the fast model with NO tools even when a research agent nominally
        matches — web research adds seconds for facts the model already knows.
        Live-data questions (weather today, prices) stay agentic."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_MODEL", "qwen3:4b")
        from dourmouse.general_roster import build_general_registry
        from dourmouse.dispatch import _is_pure_chat

        registry = build_general_registry()
        assert _is_pure_chat("what is the tallest mountain on earth", registry)
        assert _is_pure_chat("who is the current pope", registry)
        assert _is_pure_chat("explain how dns works", registry)
        # live-data intent must still escalate even in knowledge phrasing
        assert not _is_pure_chat("what is the weather in London today", registry)
        assert not _is_pure_chat("what is the stock price of AAPL", registry)
        assert not _is_pure_chat("check my inbox", registry)

        # integration: a knowledge question routes to the fast model and
        # carries no tools at all
        client = FakeClient([_FakeResponse(_FakeMessage(content="Everest."))])
        run_dispatch("what is the tallest mountain on earth", registry, client=client)
        call = client.chat.completions.calls[0]
        assert call["model"] == "qwen3:4b"
        assert not call.get("tools")

    def test_fast_lane_server_routes_to_dell_when_online(self, monkeypatch):
        """v5.30: when the Dell is EXPLICITLY configured and a fresh cached
        probe says online, the fast lane's completion goes to the Dell
        (server:qwen3:1.7b) instead of the local fast model — the speed win."""
        import time

        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_MODEL", "qwen3:4b")
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://192.168.1.108:8000")
        monkeypatch.setenv("DOURMOUSE_FAST_LANE_SERVER", "1")
        from dourmouse import remote_server as rs

        with rs._health_lock:
            rs._health_cache["at"] = time.monotonic()
            rs._health_cache["online"] = True
        # the Dell answers successfully
        monkeypatch.setattr(
            rs.DourmouseServerClient, "chat",
            lambda self, messages, temperature=None: {
                "success": True, "response": "4", "model": "qwen3:1.7b",
                "node": "Node-01", "latency_ms": 300,
            },
        )
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="4"))])
        run_dispatch("what is 2+2", registry, client=client)
        # the Dell answered; the LOCAL fake client must NOT have been called
        assert client.chat.completions.calls == []

    def test_fast_lane_server_brain_event_reports_ollama_local(self, monkeypatch):
        """world-monitor-expansion (UX pass item 1): the Dell compute node
        literally runs Ollama (remote_server.py's own docstring: "MAIN
        DOURMOUSE -> ... -> Ollama") — backend_identity() can't see that
        from ``config`` alone (config is whatever the MAIN backend is), so
        the server_lane branch is special-cased to report ("ollama", True)
        exactly like any other Ollama call, never NVIDIA/cloud just
        because that happens to be the primary configured backend."""
        import time

        from dourmouse.dispatch import run_dispatch_messages, system_message

        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_MODEL", "qwen3:4b")
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://192.168.1.108:8000")
        monkeypatch.setenv("DOURMOUSE_FAST_LANE_SERVER", "1")
        from dourmouse import remote_server as rs

        with rs._health_lock:
            rs._health_cache["at"] = time.monotonic()
            rs._health_cache["online"] = True
        monkeypatch.setattr(
            rs.DourmouseServerClient, "chat",
            lambda self, messages, temperature=None: {
                "success": True, "response": "4", "model": "qwen3:1.7b",
                "node": "Node-01", "latency_ms": 300,
            },
        )
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        # No config passed — same as the sibling test above: the Dell
        # answers directly, so the (fake) client's own completions are
        # never called regardless of what config would have resolved to.
        client = FakeClient([_FakeResponse(_FakeMessage(content="4"))])
        events: list[dict] = []
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "what is 2+2"},
        ]
        run_dispatch_messages(
            messages, registry, client=client, event_sink=lambda e: events.append(e),
        )
        assert client.chat.completions.calls == []  # the Dell answered, not the local client
        brain = next(e for e in events if e.get("type") == "brain")
        assert brain["model"].startswith("server:")
        assert brain["backend"] == "ollama"
        assert brain["local"] is True

    def test_fast_lane_server_falls_back_to_local_on_failure(self, monkeypatch):
        """v5.30: if the Dell is configured+online per cache but the actual
        completion fails (node died mid-run, timeout, 500), the reply is
        served by the LOCAL fast model — never a crash, never silence."""
        import time

        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_MODEL", "qwen3:4b")
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://192.168.1.108:8000")
        monkeypatch.setenv("DOURMOUSE_FAST_LANE_SERVER", "1")
        from dourmouse import remote_server as rs

        with rs._health_lock:
            rs._health_cache["at"] = time.monotonic()
            rs._health_cache["online"] = True
        # the Dell client's chat path now fails (node went down)
        monkeypatch.setattr(
            rs.DourmouseServerClient, "chat",
            lambda self, messages, temperature=None: {
                "success": False, "error": "server unreachable: ConnectionRefusedError"
            },
        )
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="4"))])
        run_dispatch("what is 2+2", registry, client=client)
        # the LOCAL fake client answered, with the local fast model
        assert client.chat.completions.calls and client.chat.completions.calls[0]["model"] == "qwen3:4b"

    def test_fast_lane_server_skips_unconfigured_node(self, monkeypatch):
        """v5.30: with no explicit DOURMOUSE_SERVER_URL the lane must stay
        local — a silent 2s probe on every reply would be a regression."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_MODEL", "qwen3:4b")
        monkeypatch.delenv("DOURMOUSE_SERVER_URL", raising=False)
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="4"))])
        run_dispatch("what is 2+2", registry, client=client)
        assert client.chat.completions.calls[0]["model"] == "qwen3:4b"

    def test_fast_lane_sends_compact_system_prompt(self, monkeypatch):
        """The fast lane swaps the 2.2k-token roster prompt for the compact
        style-only prompt AT THE API BOUNDARY (prefill is the dominant
        latency on a fanless M3). Agentic turns must keep the full prompt,
        and the persisted authoritative messages stay untouched."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_MODEL", "qwen3:4b")
        from dourmouse.dispatch import _FAST_LANE_SYSTEM
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()

        # pure chat: compact system at the API boundary
        client = FakeClient([_FakeResponse(_FakeMessage(content="4"))])
        run_dispatch("what is 2+2", registry, client=client)
        sent = client.chat.completions.calls[0]["messages"]
        assert sent[0]["role"] == "system"
        assert sent[0]["content"] == _FAST_LANE_SYSTEM
        assert "Lead Orchestrator" not in sent[0]["content"]

        # agentic turn: full system prompt intact
        client2 = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("check my inbox", registry, client=client2)
        sent2 = client2.chat.completions.calls[0]["messages"]
        assert "Lead Orchestrator" in sent2[0]["content"]

    def test_override_beats_config_default(self):
        from dourmouse.config import NvidiaConfig

        config = NvidiaConfig(api_key="k", base_url="u", model="nvidia/base-120b")
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch(
            "hi", _test_registry(), client=client, config=config,
            model="nvidia/forced-70b",
        )
        assert client.chat.completions.calls[0]["model"] == "nvidia/forced-70b"


class TestBespokeAgentPrompts:
    """v8.31: dourmouse/agent_prompts.py's AGENT_SYSTEM_PROMPTS wired into
    the SAME single-agent detection v8.30's per-agent model routing uses.
    A single, unambiguous plan_agents match with a covered agent gets its
    bespoke prompt spliced onto the base orchestrator rules; an uncovered
    agent or a multi-agent turn keeps the existing generic roster prompt."""

    def test_single_covered_agent_gets_bespoke_prompt(self, monkeypatch):
        # "check my inbox" deterministically resolves to exactly one agent
        # (mail), which IS covered by AGENT_SYSTEM_PROMPTS.
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.agent_prompts import AGENT_SYSTEM_PROMPTS
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("check my inbox", registry, client=client)
        sent = client.chat.completions.calls[0]["messages"]
        system_content = sent[0]["content"]
        # base orchestrator governance rules must still be present
        assert "Lead Orchestrator" in system_content
        assert "CONFIRMATION REQUIRED" in system_content
        # AND the bespoke mail prompt was spliced in
        assert AGENT_SYSTEM_PROMPTS["mail"] in system_content

    def test_uncovered_single_agent_keeps_generic_roster_prompt(self, monkeypatch):
        # "add a task to buy milk" deterministically resolves to exactly
        # one agent (tasks), which is NOT covered by AGENT_SYSTEM_PROMPTS
        # (see the coverage-gap list in agent_prompts.py's own docstring).
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.agent_prompts import AGENT_SYSTEM_PROMPTS
        from dourmouse.general_roster import build_general_registry

        assert "tasks" not in AGENT_SYSTEM_PROMPTS
        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("add a task to buy milk", registry, client=client)
        sent = client.chat.completions.calls[0]["messages"]
        system_content = sent[0]["content"]
        assert "Lead Orchestrator" in system_content
        assert "AGENT-SPECIFIC INSTRUCTIONS" not in system_content
        for bespoke in AGENT_SYSTEM_PROMPTS.values():
            assert bespoke not in system_content

    def test_multi_agent_turn_keeps_generic_roster_prompt(self, monkeypatch):
        # A real multi-step directive resolves to more than one agent
        # (mail + tasks) — no single owning prompt, same conservatism as
        # v8.30's per-agent model routing.
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.agent_prompts import AGENT_SYSTEM_PROMPTS
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch(
            "check my inbox and then add a task to follow up",
            registry, client=client,
        )
        sent = client.chat.completions.calls[0]["messages"]
        system_content = sent[0]["content"]
        assert "Lead Orchestrator" in system_content
        assert "AGENT-SPECIFIC INSTRUCTIONS" not in system_content
        assert AGENT_SYSTEM_PROMPTS["mail"] not in system_content

    def test_forced_agent_single_covered_agent_also_gets_bespoke_prompt(self):
        # forced_agent (delegate_task's nested ROUTING DIRECTIVE runs) also
        # collapses plan_agents to exactly one agent — same detection, same
        # splice, exercised directly rather than through the planner.
        from dourmouse.agent_prompts import AGENT_SYSTEM_PROMPTS
        from dourmouse.dispatch import run_dispatch_messages, system_message
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "do the research task"},
        ]
        run_dispatch_messages(
            messages, registry, client=client, forced_agent="research_info",
        )
        sent = client.chat.completions.calls[0]["messages"]
        assert AGENT_SYSTEM_PROMPTS["research_info"] in sent[0]["content"]

    def test_forced_agent_never_exposes_delegate_task(self):
        """v13 (live-reproduced, real bug): a forced_agent run scoped to
        code_claude — a subagent whose only real tool happens to share its
        name — still exposed delegate_task alongside it. A weak local
        orchestrator model, given a choice between calling code_claude
        directly and delegating to "the code_claude subagent" by name,
        picked delegate_task, which opened a NESTED forced_agent run
        scoped to code_claude again — recursing to the hard depth cap
        without ever calling the real tool once (reproduced live through
        the actual /api/chat endpoint: 4 delegate_task calls, then
        "REFUSED: maximum delegate depth (3) reached", zero real work
        done). forced_agent's own docstring promises a run "hard-scoped to
        exactly one subagent's tools" — delegate_task reaching a second
        subagent (even itself) breaks that promise."""
        from dourmouse.dispatch import run_dispatch_messages, system_message
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "list my tasks"},
        ]
        run_dispatch_messages(
            messages, registry, client=client, forced_agent="code_claude",
        )
        tools = client.chat.completions.calls[0]["tools"]
        names = {t["function"]["name"] for t in tools}
        assert "code_claude" in names
        assert "delegate_task" not in names
        assert "delegate_parallel" not in names

    def test_non_forced_run_still_exposes_delegate_task(self):
        """The fix above must not remove delegate_task from the NORMAL
        (non-forced) top-level path — mid-task delegation is a real,
        intentional capability there (see _scoped_tool_specs's own
        docstring)."""
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("check my inbox and then add a task to follow up", registry, client=client)
        tools = client.chat.completions.calls[0]["tools"]
        names = {t["function"]["name"] for t in tools}
        assert "delegate_task" in names


class TestGroundedMode:
    """v13: user-controllable Grounded Mode (config.grounded_mode_enabled()).
    Live bug this exists to catch: asked through RESEARCH with no
    forced_agent, the orchestrator answered a factual question wrong and
    stale after 56.6s with ZERO tool calls, despite the screen's own UI
    promising "searches the live web and cites sources" — nothing verified
    a "research" answer actually used a real tool. Off by default (these
    tests always monkeypatch the setting explicitly, never relying on
    whatever the real dev machine's .env happens to contain)."""

    def test_off_by_default_zero_tools_no_nudge_no_caveat(self, monkeypatch):
        from dourmouse.dispatch import run_dispatch_messages, system_message

        monkeypatch.setattr("dourmouse.config.grounded_mode_enabled", lambda: False)
        registry = _test_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="just an answer"))])
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "x"},
        ]
        report = run_dispatch_messages(
            messages, registry, client=client, forced_agent="echo_agent",
        )
        assert report["final_text"] == "just an answer"
        assert len(client.chat.completions.calls) == 1  # no nudge round-trip

    def test_on_zero_tools_nudges_once_then_caveats(self, monkeypatch):
        from dourmouse.dispatch import run_dispatch_messages, system_message

        monkeypatch.setattr("dourmouse.config.grounded_mode_enabled", lambda: True)
        registry = _test_registry()
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="first try, no tool")),
                _FakeResponse(_FakeMessage(content="second try, still no tool")),
            ]
        )
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "x"},
        ]
        report = run_dispatch_messages(
            messages, registry, client=client, forced_agent="echo_agent",
        )
        # Exactly one nudge round-trip (the _MAX_GROUNDED_NUDGES=1 budget).
        assert len(client.chat.completions.calls) == 2
        second_call_messages = client.chat.completions.calls[1]["messages"]
        assert any(
            m["role"] == "system" and "[GROUNDED MODE]" in m["content"]
            for m in second_call_messages
        )
        # Budget spent, still zero tools -> the final answer carries an
        # honest caveat rather than being presented as grounded.
        assert "second try, still no tool" in report["final_text"]
        assert "Grounded Mode was on" in report["final_text"]
        assert "zero tool calls" in report["final_text"]

    def test_on_but_a_real_tool_was_used_no_caveat(self, monkeypatch):
        from dourmouse.dispatch import run_dispatch_messages, system_message

        monkeypatch.setattr("dourmouse.config.grounded_mode_enabled", lambda: True)
        registry = _test_registry()
        client = FakeClient(
            [
                _FakeResponse(
                    _FakeMessage(
                        tool_calls=[
                            _FakeToolCall("1", "echo", json.dumps({"text": "hi"}))
                        ]
                    )
                ),
                _FakeResponse(_FakeMessage(content="answered using the tool")),
            ]
        )
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "x"},
        ]
        report = run_dispatch_messages(
            messages, registry, client=client, forced_agent="echo_agent",
        )
        assert report["final_text"] == "answered using the tool"
        assert "GROUNDED MODE" not in report["final_text"]
        assert "Grounded Mode" not in report["final_text"]

    def test_on_but_the_forced_agent_has_no_tools_never_nudges(self, monkeypatch):
        # Nothing was ever offered to call -> a zero-tool answer is the only
        # possible outcome and must not be treated as a violation.
        from dourmouse.dispatch import run_dispatch_messages, system_message

        monkeypatch.setattr("dourmouse.config.grounded_mode_enabled", lambda: True)
        registry = DispatchRegistry()
        registry.register_subagent(
            Subagent(name="toolless", domain="Test", description="no tools", tools=())
        )
        client = FakeClient([_FakeResponse(_FakeMessage(content="a plain reply"))])
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "x"},
        ]
        report = run_dispatch_messages(
            messages, registry, client=client, forced_agent="toolless",
        )
        assert report["final_text"] == "a plain reply"
        assert len(client.chat.completions.calls) == 1


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

    def test_general_roster_registers_all_subagents(self):
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
            "code_codex",  # v5.0: OpenAI Codex API backend
            "code_claude",
            "messenger",  # v3.0: inter-agent messaging
            "atlas",  # v4.0: ATLAS command-centre telemetry
            "freebuff",  # v5.5: Freebuff Desktop reads
            "music",  # v5.7: Spotify playback + discovery
            "worldmonitor",  # v5.12: global intelligence
            "forex",  # v6.0: forex-data pipeline telemetry
            "atlas_ui",  # v8.0: ATLAS Terminal status
            "atlas_cmd",  # v8.1: ATLAS Command Center
            "t212",  # v8.2: Trading 212 broker (demo/live, paper-first)
            "mt5",  # v8.3: MetaTrader 5 paper broker (demo, no subscriptions)
            "docs",  # v5.x: Google Sheets/Drive link-shared access
            "browser",  # v5.25: real headless-Chrome agent (signup/login)
            "compute",  # v5.26: the Dell compute node (LAN inference + failover)
            "design_3d",  # 3D & UI Design — spec generation + manifest cataloguing
            "companion",  # world-monitor-expansion: friendly-persona counterpart
                          # to orchestrator, for the Vision workspace chat panel
            "globe",  # v13: God's Eye View 3D globe control
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



class TestTextOnlyNudge:
    """Regression: a short text-only model message right after a tool result,
    while the plan still has unexecuted steps, is a transitional note — the
    loop must keep going (bounded) instead of ending the run mid-plan.

    Live failure: after two web_searches, qwen3 emitted "let me try a more
    targeted search" with no tool call, and the loop returned it as the final
    answer, silently dropping the remaining write_file step."""

    def _tool(self, text: str) -> _FakeResponse:
        tc = _FakeToolCall("call_" + text, "echo", json.dumps({"text": text}))
        return _FakeResponse(_FakeMessage(content=None, tool_calls=[tc]))

    def test_note_mid_plan_continues_and_completes(self):
        # tool -> short note -> tool -> final. The note must NOT end the run.
        client = FakeClient([
            self._tool("hello"),
            _FakeResponse(_FakeMessage(content="let me try the second part")),
            self._tool("goodbye"),
            _FakeResponse(_FakeMessage(content="done: hello then goodbye")),
        ])
        report = run_dispatch("echo hello then echo goodbye", _test_registry(), client=client)
        uses = [t for t in report["transcript"] if t["type"] == "tool_use"]
        assert len(uses) == 2, f"second step dropped; transcript={report['transcript']}"
        assert report["final_text"] == "done: hello then goodbye"

    def test_nudge_budget_is_bounded_and_ends_honestly(self):
        # The model keeps talking instead of calling the next tool: after the
        # bounded nudges, the last note IS the final answer (honest end).
        client = FakeClient([
            self._tool("hello"),
            _FakeResponse(_FakeMessage(content="note one")),
            _FakeResponse(_FakeMessage(content="note two")),
            _FakeResponse(_FakeMessage(content="note three")),
        ])
        report = run_dispatch("echo hello then echo goodbye", _test_registry(), client=client)
        uses = [t for t in report["transcript"] if t["type"] == "tool_use"]
        assert len(uses) == 1
        assert report["final_text"] == "note three"
        # All notes are preserved in the transcript as assistant context.
        notes = [t for t in report["transcript"] if t["type"] == "assistant_text"]
        assert [n["text"] for n in notes] == ["note one", "note two", "note three"]

    def test_long_text_after_tool_result_is_final_not_nudged(self):
        # A real (long) answer after a tool result ends the run immediately.
        client = FakeClient([
            self._tool("hello"),
            _FakeResponse(_FakeMessage(content="Here is the complete answer. " * 40)),
        ])
        report = run_dispatch("echo hello then echo goodbye", _test_registry(), client=client)
        assert report["final_text"].startswith("Here is the complete answer.")

    def _tool_named(self, name: str, text: str) -> _FakeResponse:
        tc = _FakeToolCall("call_" + name, name, json.dumps({"text": text}))
        return _FakeResponse(_FakeMessage(content=None, tool_calls=[tc]))

    def test_note_after_budget_burned_on_one_step_is_nudged(self):
        """Live bug: the model burned all its tool budget re-running step 1's
        tools (three atlas_* calls) while steps 2-3 never ran, then emitted a
        short transitional note ("Let me pull the latest economic news…").
        The nudge counted RAW tool calls (3 < 3 = False) and ended the run;
        it must count UNTOUCHED plan steps instead, so the note keeps the
        loop alive and the chain can still complete."""
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="agent_a", domain="Test", description="owns ping_a", tools=(_echo_tool(name="ping_a"),)))
        r.register_subagent(Subagent(name="agent_b", domain="Test", description="owns ping_b", tools=(_echo_tool(name="ping_b"),)))
        client = FakeClient([
            self._tool_named("ping_a", "x"),
            self._tool_named("ping_a", "y"),
            _FakeResponse(_FakeMessage(content="let me handle the second part now")),
            self._tool_named("ping_b", "w"),
            _FakeResponse(_FakeMessage(content="both parts done")),
        ])
        report = run_dispatch("ping_a now then ping_b later", r, client=client)
        uses = [t for t in report["transcript"] if t["type"] == "tool_use"]
        assert [u["name"] for u in uses] == ["ping_a", "ping_a", "ping_b"], f"got {uses}"
        assert report["final_text"] == "both parts done"



class TestPlanCheckpoint:
    """Regression: when the model fixates on one plan step (re-searching) and
    spends a plan's worth of tool calls without touching every step, the loop
    injects ONE deterministic checkpoint reminder naming the unexecuted steps.
    Without it, multi-step chains silently end half-finished (live failure:
    six web_searches, never the write_file step)."""

    def _two_agent_registry(self) -> DispatchRegistry:
        r = DispatchRegistry()
        r.register_subagent(
            Subagent(
                name="agent_a",
                domain="Test",
                description="owns ping_a",
                tools=(_echo_tool(name="ping_a"),),
            )
        )
        r.register_subagent(
            Subagent(
                name="agent_b",
                domain="Test",
                description="owns ping_b",
                tools=(_echo_tool(name="ping_b"),),
            )
        )
        return r

    def _tool(self, name: str, text: str) -> _FakeResponse:
        tc = _FakeToolCall("call_" + name, name, json.dumps({"text": text}))
        return _FakeResponse(_FakeMessage(content=None, tool_calls=[tc]))

    def test_fixation_on_one_step_triggers_reminder_and_recovers(self):
        # Model re-runs agent_a's tool three times, never touching step 2.
        # After the checkpoint reminder it must execute ping_b and finish.
        client = FakeClient([
            self._tool("ping_a", "x"),
            self._tool("ping_a", "y"),
            self._tool("ping_a", "z"),
            self._tool("ping_b", "w"),
            _FakeResponse(_FakeMessage(content="both done")),
        ])
        report = run_dispatch("ping_a now then ping_b later", self._two_agent_registry(), client=client)
        reminders = [e for e in report["transcript"] if e["type"] == "plan_reminder"]
        assert reminders, "expected a plan_reminder"
        assert reminders[0]["steps"] == [2]
        uses = {t["name"] for t in report["transcript"] if t["type"] == "tool_use"}
        assert uses == {"ping_a", "ping_b"}, f"step 2 never executed: {uses}"
        assert report["final_text"] == "both done"

    def test_reminder_is_bounded_and_ends_honestly(self):
        # Model keeps fixating; after the single reminder it still gives a
        # final answer — the run ends honestly with the reminder recorded
        # AND a caveat naming the unexecuted step.
        client = FakeClient([
            self._tool("ping_a", "x"),
            self._tool("ping_a", "y"),
            _FakeResponse(_FakeMessage(content="giving up now")),
        ])
        report = run_dispatch("ping_a now then ping_b later", self._two_agent_registry(), client=client)
        reminders = [e for e in report["transcript"] if e["type"] == "plan_reminder"]
        assert len(reminders) == 1
        assert "giving up now" in report["final_text"]
        assert "not executed" in report["final_text"].lower()
        assert "STEP 2/2" in report["final_text"]

    def test_ignored_checkpoint_caveats_claimed_success(self):
        """Live bug: the model CLAIMED "saved to outlook_brief.txt" with zero
        tool calls, ignored the one checkpoint reminder, and the false claim
        became the final answer. With the reminder budget spent and steps
        still unexecuted, the final text MUST carry an honest caveat — a
        fabricated completion can never pass silently."""
        r = self._two_agent_registry()
        client = FakeClient([
            # Long text-only "done" claiming success, no tools.
            _FakeResponse(_FakeMessage(content="Saved to outlook_brief.txt. " * 8)),
            # After the reminder it STILL answers without touching ping_b.
            _FakeResponse(_FakeMessage(content="Saved to outlook_brief.txt. " * 8)),
        ])
        report = run_dispatch(
            "ping_a now then ping_b later", r, client=client
        )
        reminders = [e for e in report["transcript"] if e["type"] == "plan_reminder"]
        assert len(reminders) == 1
        final = report["final_text"]
        assert "not executed" in final.lower()
        assert "STEP 2/2" in final
        # The transcript's final assistant_text entry carries the same caveat
        # (UI feed honest), not just the returned final_text.
        texts = [e for e in report["transcript"] if e["type"] == "assistant_text"]
        assert "not executed" in texts[-1]["text"].lower()

    def test_completed_plan_gets_no_reminder(self):
        client = FakeClient([
            self._tool("ping_a", "x"),
            self._tool("ping_b", "w"),
            _FakeResponse(_FakeMessage(content="all steps done")),
        ])
        report = run_dispatch("ping_a now then ping_b later", self._two_agent_registry(), client=client)
        reminders = [e for e in report["transcript"] if e["type"] == "plan_reminder"]
        assert reminders == []

    def test_zero_tool_fabricated_completion_gets_checkpoint_not_final(self):
        """Live bug (end-to-end): the model answered "saved to
        .../outlook_brief.txt" with NO tool calls, and the run accepted it
        as the final answer — the file was never written. A long text-only
        message with unexecuted plan steps must fire the exit-path
        checkpoint (bounded), then let the model actually do the work."""
        client = FakeClient([
            # Long fabricated "done" — the exact live failure mode.
            _FakeResponse(_FakeMessage(content="Saved to /Users/me/outlook_brief.txt." * 6)),
            self._tool("ping_b", "w"),  # model complies after the reminder
            # Long real report (over the nudge threshold -> final, not nudged).
            _FakeResponse(_FakeMessage(content="The file is written and verified. " * 15)),
        ])
        report = run_dispatch(
            "ping_a now then ping_b later", self._two_agent_registry(), client=client
        )
        reminders = [e for e in report["transcript"] if e["type"] == "plan_reminder"]
        assert len(reminders) == 1, f"expected one reminder; transcript={report['transcript']}"
        uses = {t["name"] for t in report["transcript"] if t["type"] == "tool_use"}
        assert uses == {"ping_b"}, f"fabrication not corrected: {uses}"
        assert "Saved to /Users/me" not in report["final_text"]
        assert report["final_text"].startswith("The file is written and verified.")


# --------------------------------------------------------------------------- #
# v4.2 LLM context bounding — bounded rolling window at the API boundary
# --------------------------------------------------------------------------- #

class TestBoundedContext:
    """_bounded_context keeps system + in-flight exchange, drops old history
    at clean user boundaries, and truncates OLD tool results only."""

    def _long_history(self, n_turns: int) -> list[dict]:
        msgs = [{"role": "system", "content": "SYSTEM" * 20}]
        for i in range(n_turns):
            msgs.append({"role": "user", "content": f"turn {i}: " + "x" * 250})
            msgs.append({"role": "assistant", "content": "y" * 150})
        return msgs

    def test_keeps_system_and_inflight_exchange(self):
        msgs = self._long_history(100)
        msgs.append({"role": "user", "content": "LATEST DIRECTIVE"})
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": "c1", "content": "R" * 10000})
        out = dispatch_module._bounded_context(msgs)
        assert out[0]["role"] == "system"
        assert out[-1]["content"] == "R" * 10000  # in-flight result kept in full
        assert out[-2]["role"] == "assistant" and out[-2]["tool_calls"]
        assert out[-3]["content"] == "LATEST DIRECTIVE"
        assert len(out) < len(msgs)  # old turns dropped, not the exchange

    def test_drops_history_at_clean_user_boundary(self):
        msgs = self._long_history(100)
        msgs.append({"role": "user", "content": "final question"})
        out = dispatch_module._bounded_context(msgs)
        assert out[0]["role"] == "system"
        assert out[-1]["content"] == "final question"
        assert out[1]["role"] == "user"  # window starts at a user boundary
        assert len(out) < len(msgs)
        total = sum(dispatch_module._est_tokens(m) for m in out)
        assert total <= dispatch_module._MAX_LLM_TOKENS + 200

    def test_truncates_old_tool_results_only(self):
        msgs = [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "OLD" * 5000},
            {"role": "user", "content": "latest"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c2", "content": "NEW" * 5000},
        ]
        out = dispatch_module._bounded_context(msgs, max_tokens=100000)
        old_tool = out[3]
        assert old_tool["role"] == "tool"
        assert old_tool["content"].endswith("...[truncated]")
        assert len(old_tool["content"]) < len(msgs[3]["content"])
        assert out[-1]["content"] == msgs[-1]["content"]  # in-flight kept full


class TestLoopBounding:
    """The dispatch loop sends a bounded copy to the LLM, never the whole
    conversation (v4.2 speed: unbounded re-prefill made long sessions crawl
    on the local GPU)."""

    def test_loop_sends_bounded_history_to_client(self):
        full = [{"role": "system", "content": "SYSTEM" * 20}]
        for i in range(100):
            full.append({"role": "user", "content": f"old turn {i}: " + "x" * 250})
            full.append({"role": "assistant", "content": "y" * 150})
        full.append({"role": "user", "content": "hello"})
        full_len = len(full)
        client = FakeClient([_FakeResponse(_FakeMessage(content="Done."))])
        dispatch_module.run_dispatch_messages(full, _test_registry(), client=client)
        sent = client.chat.completions.calls[0]["messages"]
        assert sent[0]["role"] == "system"
        assert sent[-1]["role"] == "user" and sent[-1]["content"] == "hello"
        assert len(sent) < full_len
        total = sum(dispatch_module._est_tokens(m) for m in sent)
        assert total <= dispatch_module._MAX_LLM_TOKENS + 200

    def test_loop_bounds_every_llm_call_in_a_tool_chain(self):
        """Mid-chain, after a tool result, the NEXT call is bounded too."""
        full = [{"role": "system", "content": "SYSTEM" * 20}]
        for i in range(100):
            full.append({"role": "user", "content": f"old turn {i}: " + "x" * 250})
            full.append({"role": "assistant", "content": "y" * 150})
        full.append({"role": "user", "content": "first"})
        client = FakeClient(
            [
                _FakeResponse(
                    _FakeMessage(
                        tool_calls=[
                            _FakeToolCall("c1", "echo", '{"text": "hi"}')
                        ]
                    )
                ),
                _FakeResponse(_FakeMessage(content="Final answer.")),
            ]
        )
        dispatch_module.run_dispatch_messages(full, _test_registry(), client=client)
        assert len(client.chat.completions.calls) == 2
        for call in client.chat.completions.calls:
            sent = call["messages"]
            total = sum(dispatch_module._est_tokens(m) for m in sent)
            assert total <= dispatch_module._MAX_LLM_TOKENS + 200
            assert sent[0]["role"] == "system"


class TestBrainEscalation:
    """v5.5: multi-step prompts escalate to the full default brain; simple
    chat stays on the fast orchestrator brain. Deterministic (Rule 2.8)."""

    def test_simple_prompt_stays_fast(self):
        model, escalated = dispatch_module._resolve_brain_model(
            fast="fast-model", default="full-model", prompt="what is 2+2", explicit=None
        )
        assert model == "fast-model"
        assert escalated is False

    def test_multi_step_prompt_escalates(self):
        model, escalated = dispatch_module._resolve_brain_model(
            fast="fast-model",
            default="full-model",
            prompt="write a script then run it and report the output",
            explicit=None,
        )
        assert model == "full-model"
        assert escalated is True

    def test_explicit_override_wins(self):
        model, escalated = dispatch_module._resolve_brain_model(
            fast="fast-model",
            default="full-model",
            prompt="write a script then run it and report the output",
            explicit="focus-agent-model",
        )
        assert model == "focus-agent-model"
        assert escalated is False

    def test_same_model_no_false_escalation_flag(self):
        model, escalated = dispatch_module._resolve_brain_model(
            fast="full-model", default="full-model", prompt="do a then b", explicit=None
        )
        assert model == "full-model"
        assert escalated is False

    def test_music_prompt_escalates_tool_critical(self):
        """v5.22.5: a single-step Spotify/music directive is tool-critical —
        the fast brain fabricated playlist URIs and hallucinated an empty
        playlist, so the stronger brain must handle it."""
        for prompt in (
            "play my jazz bar in nyc playlist on spotify",
            "what's currently playing on Spotify",
            "show me my top tracks",
        ):
            model, escalated = dispatch_module._resolve_brain_model(
                fast="fast-model",
                default="full-model",
                prompt=prompt,
                explicit=None,
            )
            assert model == "full-model", prompt
            assert escalated is True, prompt

    def test_non_music_simple_prompt_stays_fast(self):
        model, escalated = dispatch_module._resolve_brain_model(
            fast="fast-model", default="full-model",
            prompt="what is the weather", explicit=None,
        )
        assert model == "fast-model"
        assert escalated is False


class TestGlobalMemoryWiring:
    """v8.30: real retrieval auto-injected into the prompt, real ingestion
    of the completed turn — both OFF unless DOURMOUSE_GLOBAL_MEMORY=1, and
    both must never break a turn if memory itself fails."""

    def test_memory_context_is_injected_when_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_GLOBAL_MEMORY", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")

        class _FakeMemory:
            def retrieve_context_for_prompt(self, prompt):
                return "RELEVANT PAST CONTEXT (from earlier conversations...):\nsomething real"

            def add(self, *a, **k):
                pass

        monkeypatch.setattr("dourmouse.global_memory.get_default_memory", lambda: _FakeMemory())

        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("check my inbox", _test_registry(), client=client)
        sent = client.chat.completions.calls[0]["messages"]
        user_msg = next(m for m in reversed(sent) if m["role"] == "user")
        assert "RELEVANT PAST CONTEXT" in user_msg["content"]
        assert "check my inbox" in user_msg["content"]

    def test_memory_context_absent_when_disabled(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_GLOBAL_MEMORY", raising=False)
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")

        calls = []

        class _FakeMemory:
            def retrieve_context_for_prompt(self, prompt):
                calls.append(prompt)
                return "should never be reached"

        monkeypatch.setattr("dourmouse.global_memory.get_default_memory", lambda: _FakeMemory())

        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch("check my inbox", _test_registry(), client=client)
        assert calls == []
        sent = client.chat.completions.calls[0]["messages"]
        user_msg = next(m for m in reversed(sent) if m["role"] == "user")
        assert "RELEVANT PAST CONTEXT" not in user_msg["content"]

    def test_completed_turn_is_ingested_when_enabled(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GLOBAL_MEMORY", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")

        added = []

        class _FakeMemory:
            def retrieve_context_for_prompt(self, prompt):
                return ""

            def add(self, text, screen=""):
                added.append((text, screen))

        monkeypatch.setattr("dourmouse.global_memory.get_default_memory", lambda: _FakeMemory())

        client = FakeClient([_FakeResponse(_FakeMessage(content="the inbox is empty"))])
        run_dispatch("check my inbox", _test_registry(), client=client)
        assert len(added) == 1
        text, screen = added[0]
        assert "check my inbox" in text
        assert "the inbox is empty" in text

    def test_memory_failure_never_breaks_the_turn(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GLOBAL_MEMORY", "1")
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")

        class _BoomMemory:
            def retrieve_context_for_prompt(self, prompt):
                raise RuntimeError("ollama down")

            def add(self, *a, **k):
                raise RuntimeError("sqlite locked")

        monkeypatch.setattr("dourmouse.global_memory.get_default_memory", lambda: _BoomMemory())

        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        report = run_dispatch("check my inbox", _test_registry(), client=client)
        assert report["final_text"] == "ok"


class TestRepeatToolCallGuard:
    """v13: a weak orchestrator model regularly can't tell a completed
    single-shot task apart from one still pending and re-issues the exact
    same claude_code/codex_code call a second time — live-observed:
    'reply with the exact text OK-CLAUDE' came back as 'OK-CLAUDEOK-CLAUDE'
    because the tool genuinely ran twice and the model glued both real
    results together. Only claude_code/codex_code are guarded (see the
    guard's own comment in dispatch.py for why)."""

    def _registry_with(self, name: str) -> tuple[DispatchRegistry, list[int]]:
        calls: list[int] = []

        def handler(args):
            calls.append(1)
            return f"CALL-{len(calls)}: {args.get('task')}"

        r = DispatchRegistry()
        r.register_subagent(
            Subagent(
                name="dev_coding",
                domain="Test",
                description="coding delegate",
                tools=(
                    ToolSpec(
                        name=name,
                        description=f"{name} delegate",
                        parameters={
                            "type": "object",
                            "properties": {"task": {"type": "string"}},
                            "required": ["task"],
                        },
                        handler=handler,
                    ),
                ),
            )
        )
        return r, calls

    def test_identical_repeat_call_is_not_re_executed(self):
        registry, calls = self._registry_with("claude_code")
        args = json.dumps({"task": "reply with the exact text OK-CLAUDE"})
        client = FakeClient(
            [
                _FakeResponse(
                    _FakeMessage(
                        tool_calls=[_FakeToolCall("1", "claude_code", args)]
                    )
                ),
                _FakeResponse(
                    _FakeMessage(
                        tool_calls=[_FakeToolCall("2", "claude_code", args)]
                    )
                ),
                _FakeResponse(_FakeMessage(content="OK-CLAUDE")),
            ]
        )
        report = run_dispatch("say ok", registry, client=client, max_turns=5)
        # The real handler ran exactly once, not twice.
        assert len(calls) == 1
        # The second (duplicate) call's tool result told the model it was
        # reused rather than replaying a real CLI call.
        tool_results = [
            e["text"] for e in report["transcript"] if e.get("type") == "tool_result"
        ]
        assert tool_results[0] == "CALL-1: reply with the exact text OK-CLAUDE"
        assert "identical call already made this turn" in tool_results[1]
        assert "CALL-1: reply with the exact text OK-CLAUDE" in tool_results[1]

    def test_different_arguments_are_not_deduped(self):
        registry, calls = self._registry_with("codex_code")
        client = FakeClient(
            [
                _FakeResponse(
                    _FakeMessage(
                        tool_calls=[
                            _FakeToolCall(
                                "1", "codex_code", json.dumps({"task": "task A"})
                            )
                        ]
                    )
                ),
                _FakeResponse(
                    _FakeMessage(
                        tool_calls=[
                            _FakeToolCall(
                                "2", "codex_code", json.dumps({"task": "task B"})
                            )
                        ]
                    )
                ),
                _FakeResponse(_FakeMessage(content="done")),
            ]
        )
        run_dispatch("do two things", registry, client=client, max_turns=5)
        # Different arguments -> both real calls run, guard never fires.
        assert len(calls) == 2

    def test_unguarded_tools_still_repeat_freely(self):
        """Only claude_code/codex_code are guarded — an ordinary tool (e.g.
        checking email twice) must keep re-running every time, since a
        second real call CAN legitimately return something new."""
        registry, calls = self._registry_with("echo")
        args = json.dumps({"task": "same text"})
        client = FakeClient(
            [
                _FakeResponse(
                    _FakeMessage(tool_calls=[_FakeToolCall("1", "echo", args)])
                ),
                _FakeResponse(
                    _FakeMessage(tool_calls=[_FakeToolCall("2", "echo", args)])
                ),
                _FakeResponse(_FakeMessage(content="done")),
            ]
        )
        run_dispatch("echo twice", registry, client=client, max_turns=5)
        assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Heartbeat during a slow model call (v13) — live bug this fixes: a slow
# local model gives ZERO signal for its entire prefill phase (no tokens
# exist yet to stream), measured live at 60+ seconds of nothing visible —
# indistinguishable from a hung request, reported as "no reply".
# --------------------------------------------------------------------------- #

class _SlowFakeCompletions:
    """create() blocks for `delay` seconds before returning — real enough
    to let a background heartbeat thread actually fire during the call,
    without a real model or real sleep-heavy test."""

    def __init__(self, message, delay: float):
        self._message = message
        self._delay = delay
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        time.sleep(self._delay)
        return _FakeResponse(self._message)


class _SlowFakeClient:
    def __init__(self, message, delay: float):
        self.chat = _FakeChat(_SlowFakeCompletions(message, delay))


class TestCallWithRetryHeartbeat:
    def test_no_event_sink_means_no_heartbeat_thread(self, monkeypatch):
        """Default behavior (no event_sink) must be byte-identical to
        before this feature existed — no thread spawned, no event ever
        emitted, zero risk to every caller that doesn't opt in."""
        monkeypatch.setattr(dispatch_module, "_HEARTBEAT_INTERVAL_S", 0.02)
        client = _SlowFakeClient(_FakeMessage(content="done"), delay=0.08)
        response = dispatch_module._call_with_retry(
            client, model="m", messages=[], tools=[], config=None,
        )
        assert response.choices[0].message.content == "done"

    def test_heartbeat_fires_while_the_call_is_blocked(self, monkeypatch):
        monkeypatch.setattr(dispatch_module, "_HEARTBEAT_INTERVAL_S", 0.02)
        client = _SlowFakeClient(_FakeMessage(content="done"), delay=0.12)
        seen: list[dict] = []
        response = dispatch_module._call_with_retry(
            client, model="qwen2.5:7b", messages=[], tools=[], config=None,
            event_sink=seen.append,
        )
        assert response.choices[0].message.content == "done"
        beats = [e for e in seen if e["type"] == "brain_thinking"]
        assert beats, "expected at least one heartbeat during a 0.12s call with a 0.02s interval"
        assert all(b["model"] == "qwen2.5:7b" for b in beats)
        assert all(isinstance(b["elapsed_s"], (int, float)) for b in beats)

    def test_heartbeat_never_fires_after_the_call_returns(self, monkeypatch):
        """The background thread must be stopped and joined before
        _call_with_retry returns — a heartbeat arriving AFTER the real
        answer would be a confusing, meaningless event."""
        monkeypatch.setattr(dispatch_module, "_HEARTBEAT_INTERVAL_S", 0.02)
        client = _SlowFakeClient(_FakeMessage(content="done"), delay=0.05)
        seen: list[dict] = []
        dispatch_module._call_with_retry(
            client, model="m", messages=[], tools=[], config=None,
            event_sink=seen.append,
        )
        count_immediately_after = len(seen)
        time.sleep(0.08)  # well past another heartbeat interval
        assert len(seen) == count_immediately_after

    def test_a_fast_call_never_gets_a_heartbeat(self, monkeypatch):
        """A call that finishes before the first interval elapses must
        produce zero heartbeat noise — this is a slow-call safety net,
        not a per-call status ping."""
        monkeypatch.setattr(dispatch_module, "_HEARTBEAT_INTERVAL_S", 5.0)
        client = _SlowFakeClient(_FakeMessage(content="done"), delay=0.01)
        seen: list[dict] = []
        dispatch_module._call_with_retry(
            client, model="m", messages=[], tools=[], config=None,
            event_sink=seen.append,
        )
        assert seen == []

    def test_a_raising_sink_never_breaks_the_real_call(self, monkeypatch):
        """Same discipline as every other event_sink in this codebase
        (Rule: a UI-streaming observer must never break the dispatch it's
        observing)."""
        monkeypatch.setattr(dispatch_module, "_HEARTBEAT_INTERVAL_S", 0.02)
        client = _SlowFakeClient(_FakeMessage(content="done"), delay=0.08)

        def _boom(entry):
            raise RuntimeError("sink exploded")

        response = dispatch_module._call_with_retry(
            client, model="m", messages=[], tools=[], config=None,
            event_sink=_boom,
        )
        assert response.choices[0].message.content == "done"

    def test_heartbeat_survives_a_raising_call(self, monkeypatch):
        """The heartbeat thread must still be stopped/joined cleanly even
        when the underlying model call raises — no leaked thread, no
        hang on the way out."""
        monkeypatch.setattr(dispatch_module, "_HEARTBEAT_INTERVAL_S", 0.02)

        class _BoomCompletions:
            def create(self, **kwargs):
                time.sleep(0.08)
                raise RuntimeError("model call failed")

        class _BoomClient:
            def __init__(self):
                self.chat = _FakeChat(_BoomCompletions())

        seen: list[dict] = []
        with pytest.raises(RuntimeError, match="model call failed"):
            dispatch_module._call_with_retry(
                _BoomClient(), model="m", messages=[], tools=[], config=None,
                event_sink=seen.append,
            )
        assert any(e["type"] == "brain_thinking" for e in seen)
