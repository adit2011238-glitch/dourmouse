"""Agents talking to each other: ask_agent and list_agents (finding #189).

Before this only the orchestrator and the companion could reach another agent.
Every chat screen pins one agent's tools and drops delegate_task, so the
browser (and every other specialist) could not hand a job to a peer at all.
These tests pin: every agent is offered the peer tools, the asking works end to
end through the real nested run, the peer rules hold (never yourself, never a
loop, the depth limit), and the owner is asked first when a conversation that
reads text written by other people tries to hand a job to an agent that can act.
"""

from __future__ import annotations

import json

import pytest

from dourmouse.dispatch import (
    PEER_ASK_OPEN_TARGETS,
    PEER_TOOLS,
    UNTRUSTED_CONTENT_AGENTS,
    DispatchRegistry,
    Subagent,
    ToolSpec,
    _argument_gate,
    _scoped_tool_specs,
    current_dispatch_context,
    run_dispatch,
    run_dispatch_messages,
    system_message,
)
from dourmouse.general_roster import _build_delegate_tool, build_general_registry
from dourmouse.peer_tools import build_peer_tools
from dourmouse.tests.test_dispatch import FakeClient, _FakeMessage, _FakeResponse, _FakeToolCall


@pytest.fixture(scope="module")
def real_registry() -> DispatchRegistry:
    return build_general_registry()


@pytest.fixture(autouse=True)
def _no_fast_lane(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")


def _tool(name: str, handler=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler or (lambda a, n=name: f"{n} ran"),
    )


def _fake_registry(*agents: str, extra: dict[str, list[ToolSpec]] | None = None) -> DispatchRegistry:
    """Small registry: each named agent owns one ping tool, plus a messenger
    that owns the peer tools exactly as the real roster does."""
    extra = extra or {}
    registry = DispatchRegistry()
    for name in agents:
        registry.register_subagent(Subagent(
            name=name, domain="Test", description=f"{name} agent",
            tools=(_tool(f"ping_{name}"), *extra.get(name, ())),
        ))
    registry.register_subagent(Subagent(name="messenger", domain="Test", description="agents talk", tools=()))
    delegate = _build_delegate_tool(registry)
    for tool in build_peer_tools(registry, delegate):
        registry.extend_subagent("messenger", tool)
    return registry


def _call(name: str, **arguments) -> _FakeResponse:
    return _FakeResponse(_FakeMessage(tool_calls=[_FakeToolCall("c-" + name, name, json.dumps(arguments))]))


def _text(text: str) -> _FakeResponse:
    return _FakeResponse(_FakeMessage(content=text))


def _run(registry, client, agent, prompt="hello", **kwargs):
    messages = [
        {"role": "system", "content": system_message(registry)},
        {"role": "user", "content": prompt},
    ]
    return run_dispatch_messages(messages, registry, client=client, forced_agent=agent, **kwargs)


def _tool_results(report, name):
    return [e["text"] for e in report["transcript"] if e["type"] == "tool_result" and e["name"] == name]


# ------------------------------------------------------------ every agent can talk


class TestEveryAgentIsOfferedThePeerTools:
    def test_the_real_roster_has_both_tools_under_the_messenger(self, real_registry):
        messenger = {t.name for t in real_registry.get_subagent("messenger").tools}
        assert set(PEER_TOOLS) <= messenger

    def test_every_real_agent_pinned_run_sends_ask_agent_and_list_agents(self, real_registry):
        """Through the real call site, not the helper: 44 pinned runs, one per agent."""
        missing: dict[str, set[str]] = {}
        for agent in sorted(real_registry.subagent_names):
            client = FakeClient([_text("ok")])
            _run(real_registry, client, agent)
            sent = {t["function"]["name"] for t in client.chat.completions.calls[0]["tools"]}
            gone = set(PEER_TOOLS) - sent
            if gone:
                missing[agent] = gone
        assert not missing, f"agents that cannot reach the others: {missing}"

    def test_the_browser_in_particular(self, real_registry):
        client = FakeClient([_text("ok")])
        _run(real_registry, client, "browser")
        sent = {t["function"]["name"] for t in client.chat.completions.calls[0]["tools"]}
        assert {"ask_agent", "list_agents", "browser_open", "browser_snapshot"} <= sent
        assert "delegate_task" not in sent  # the pin is intact: only the peer tools were added

    def test_an_ordinary_routed_turn_gets_them_too(self, real_registry):
        client = FakeClient([_text("ok")])
        run_dispatch("check my inbox", real_registry, client=client)
        sent = {t["function"]["name"] for t in client.chat.completions.calls[0]["tools"]}
        assert set(PEER_TOOLS) <= sent

    def test_the_helper_adds_them_only_when_asked_to(self, real_registry):
        """Opt in, like the desk: any other caller of the scoping helper is unchanged."""
        plain = {t["function"]["name"] for t in _scoped_tool_specs(real_registry, {"browser"}, include_delegate=False)}
        assert not (set(PEER_TOOLS) & plain)
        with_peers = {
            t["function"]["name"]
            for t in _scoped_tool_specs(real_registry, {"browser"}, include_delegate=False, include_peer_tools=True)
        }
        assert set(PEER_TOOLS) <= with_peers

    def test_the_planner_does_not_score_the_peer_tools(self):
        from dourmouse.planner import _AMBIENT_SHARED_TOOLS

        assert set(PEER_TOOLS) <= _AMBIENT_SHARED_TOOLS


# ------------------------------------------------------------ the asking itself


class TestAskAgentEndToEnd:
    def test_one_agent_asks_another_and_gets_a_real_answer_back(self):
        registry = _fake_registry("alpha", "beta")
        client = FakeClient([
            _call("ask_agent", agent="beta", question="what is the weather"),
            _text("beta says: sunny"),
            _text("alpha relays: sunny"),
        ])
        report = _run(registry, client, "alpha")
        assert report["final_text"] == "alpha relays: sunny"
        (result,) = _tool_results(report, "ask_agent")
        assert result.startswith("ANSWER FROM beta (asked by alpha):")
        assert "beta says: sunny" in result
        nested_user = [m for m in client.chat.completions.calls[1]["messages"] if m["role"] == "user"][0]["content"]
        assert "what is the weather" in nested_user and "'beta'" in nested_user

    def test_the_asked_agent_runs_as_itself_with_its_own_tools_and_the_chain(self):
        seen = {}

        def probe(arguments):
            ctx = current_dispatch_context(registry)
            seen["agent"] = ctx.forced_agent
            seen["chain"] = ctx.agent_chain
            seen["depth"] = ctx.depth
            return "probe ok"

        registry = _fake_registry("alpha", "beta", extra={"beta": [_tool("probe", probe)]})
        client = FakeClient([
            _call("ask_agent", agent="beta", question="probe it"),
            _call("probe"),
            _text("beta done"),
            _text("alpha done"),
        ])
        report = _run(registry, client, "alpha")
        assert seen == {"agent": "beta", "chain": ("alpha",), "depth": 1}
        nested_tools = {t["function"]["name"] for t in client.chat.completions.calls[1]["tools"]}
        assert {"probe", "ping_beta", "ask_agent", "list_agents"} <= nested_tools
        assert "ping_alpha" not in nested_tools
        assert report["final_text"] == "alpha done"

    def test_the_browser_asks_the_research_agent_with_the_real_names(self):
        registry = _fake_registry("browser", "research_info")
        client = FakeClient([
            _call("ask_agent", agent="research_info", question="summarise this page"),
            _text("research_info: a summary"),
            _text("here is the summary"),
        ])
        confirmations = []
        report = _run(registry, client, "browser", confirmation_gate=lambda p: confirmations.append(p) or True)
        assert confirmations == []  # a read-only target needs no owner prompt
        assert "research_info: a summary" in _tool_results(report, "ask_agent")[0]

    def test_asking_yourself_is_refused_and_nothing_is_spawned(self):
        registry = _fake_registry("alpha", "beta")
        client = FakeClient([_call("ask_agent", agent="alpha", question="do it"), _text("ok")])
        report = _run(registry, client, "alpha")
        (result,) = _tool_results(report, "ask_agent")
        assert result.startswith("REFUSED: you are already working as 'alpha'")
        assert len(client.chat.completions.calls) == 2  # no nested run happened

    def test_an_unknown_agent_is_an_error_naming_the_real_ones(self):
        registry = _fake_registry("alpha", "beta")
        client = FakeClient([_call("ask_agent", agent="ghost", question="hi"), _text("ok")])
        (result,) = _tool_results(_run(registry, client, "alpha"), "ask_agent")
        assert result.startswith("ERROR: unknown agent 'ghost'") and "beta" in result

    @pytest.mark.parametrize("arguments,fragment", [
        ({"question": "hi"}, "missing required argument 'agent'"),
        ({"agent": "beta"}, "missing required argument 'question'"),
    ])
    def test_missing_arguments_are_honest_errors(self, arguments, fragment):
        registry = _fake_registry("alpha", "beta")
        client = FakeClient([_call("ask_agent", **arguments), _text("ok")])
        (result,) = _tool_results(_run(registry, client, "alpha"), "ask_agent")
        assert result.startswith("ERROR") and fragment in result
        assert len(client.chat.completions.calls) == 2  # nothing was spawned

    def test_a_blank_question_is_refused_by_the_handler_itself(self):
        registry = _fake_registry("alpha", "beta")
        client = FakeClient([_call("ask_agent", agent="beta", question="   "), _text("ok")])
        (result,) = _tool_results(_run(registry, client, "alpha"), "ask_agent")
        assert result.startswith("ERROR") and "non-empty 'question'" in result

    def test_other_argument_spellings_get_a_message_naming_the_real_ones(self):
        """The schema is a contract (validated before any handler runs), so a model that
        writes subagent/task is told which two arguments exist and can retry."""
        registry = _fake_registry("alpha", "beta")
        client = FakeClient([_call("ask_agent", subagent="beta", task="do the thing"), _text("ok")])
        (result,) = _tool_results(_run(registry, client, "alpha"), "ask_agent")
        assert result.startswith("ERROR: invalid arguments for 'ask_agent'") and "'agent'" in result

    def test_a_loop_back_to_an_agent_already_in_the_chain_is_refused(self):
        registry = _fake_registry("alpha", "beta")
        client = FakeClient([
            _call("ask_agent", agent="beta", question="help"),
            _call("ask_agent", agent="alpha", question="and you, help me back"),
            _text("beta finished without alpha"),
            _text("alpha finished"),
        ])
        events: list[dict] = []
        _run(registry, client, "alpha", event_sink=events.append)
        inner = [e["text"] for e in events if e.get("type") == "tool_result" and e.get("name") == "ask_agent"]
        assert any(t.startswith("REFUSED: 'alpha' is already part of this conversation (alpha -> beta)") for t in inner), inner

    def test_the_depth_limit_still_applies(self):
        registry = _fake_registry("alpha", "beta", "gamma")
        client = FakeClient([
            _call("ask_agent", agent="beta", question="one"),
            _call("ask_agent", agent="gamma", question="two"),
            _text("beta done"),
            _text("alpha done"),
        ])
        events: list[dict] = []
        _run(registry, client, "alpha", max_depth=1, event_sink=events.append)
        inner = [e["text"] for e in events if e.get("type") == "tool_result" and e.get("name") == "ask_agent"]
        assert any("maximum delegate depth (1) reached" in t for t in inner), inner

    def test_a_nested_ask_keeps_the_model_it_was_given_not_the_targets_backend_label(self, monkeypatch):
        """Live, finding #189: a pinned markets run asking research_info failed with "HTTP Error 404".
        research_info splits to Claude, and the nested run swapped the model it was handed for the
        label "claude-sonnet-5 (CLI)" while still talking through its parent's Ollama client."""
        monkeypatch.setattr("dourmouse.dispatch._orchestrator_backend_mode", lambda: "split")
        registry = _fake_registry("markets", "research_info")
        client = FakeClient([
            _call("ask_agent", agent="research_info", question="what is the capital of Australia"),
            _text("Canberra"),
            _text("Canberra, per research_info"),
        ])
        _run(registry, client, "markets")
        models = [c["model"] for c in client.chat.completions.calls]
        assert len(models) == 3
        assert "claude-sonnet-5 (CLI)" not in models, models
        assert models[1] == models[0], "the nested call must use the model the run was given"

    def test_the_chain_survives_delegate_task_nesting_too(self):
        seen = {}

        def probe(arguments):
            seen["chain"] = current_dispatch_context(registry).agent_chain
            return "probed"

        def launch(arguments):
            return delegate.handler({"task": "probe it", "subagent": "beta"})

        registry = _fake_registry("alpha", "beta", extra={"alpha": [_tool("launch", launch)], "beta": [_tool("probe", probe)]})
        delegate = _build_delegate_tool(registry)
        client = FakeClient([_call("launch"), _call("probe"), _text("beta done"), _text("alpha done")])
        _run(registry, client, "alpha")
        assert seen["chain"] == ("alpha",)


# ------------------------------------------------------------ the owner is asked first


class TestWhenTheOwnerIsAsked:
    def _spec(self, real_registry):
        return real_registry.lookup("ask_agent")

    @pytest.mark.parametrize("target", ["mail", "system", "dev_coding", "t212", "memory", "messenger", "app_driver", "tasks"])
    def test_a_page_reading_agent_asking_an_agent_that_can_act_asks_first(self, real_registry, target):
        decision = _argument_gate(self._spec(real_registry), {"agent": target, "question": "do it"}, "browser")
        assert decision is not None and decision[0] == "confirm"
        assert target in decision[1] and "do it" in decision[1]

    @pytest.mark.parametrize("target", sorted(PEER_ASK_OPEN_TARGETS))
    def test_asking_an_agent_that_only_reads_never_asks(self, real_registry, target):
        assert _argument_gate(self._spec(real_registry), {"agent": target, "question": "q"}, "browser") is None

    def test_a_brand_new_agent_starts_out_asking(self, real_registry):
        decision = _argument_gate(self._spec(real_registry), {"agent": "brand_new_agent", "question": "q"}, "mail")
        assert decision is not None and decision[0] == "confirm"

    def test_agents_that_read_nothing_hostile_ask_freely(self, real_registry):
        spec = self._spec(real_registry)
        for caller in ("goals", "markets_free", "scheduling", "tasks", "memory", "orchestrator"):
            if caller in UNTRUSTED_CONTENT_AGENTS:
                continue
            assert _argument_gate(spec, {"agent": "system", "question": "q"}, caller) is None, caller

    def test_the_chain_counts_not_just_the_caller(self, real_registry):
        spec = self._spec(real_registry)
        # markets reads nothing hostile, but the browser asked it, so browser -> markets -> system is still the page talking
        assert _argument_gate(spec, {"agent": "system", "question": "q"}, "markets", chain=("browser",))[0] == "confirm"
        assert _argument_gate(spec, {"agent": "system", "question": "q"}, "markets", chain=("goals",)) is None

    def test_a_routed_orchestrator_turn_that_includes_a_reader_asks(self, real_registry):
        spec = self._spec(real_registry)
        args = {"agent": "mail", "question": "q"}
        assert _argument_gate(spec, args, "orchestrator", frozenset({"research_info"}))[0] == "confirm"
        assert _argument_gate(spec, args, "orchestrator", frozenset({"goals"})) is None

    def test_the_other_argument_spellings_are_gated_the_same(self, real_registry):
        spec = self._spec(real_registry)
        assert _argument_gate(spec, {"subagent": "mail", "task": "q"}, "browser")[0] == "confirm"

    def test_declining_stops_the_request_before_the_other_agent_starts(self):
        registry = _fake_registry("browser", "mail")
        client = FakeClient([_call("ask_agent", agent="mail", question="read my inbox"), _text("understood")])
        prompts = []
        report = _run(registry, client, "browser", confirmation_gate=lambda p: prompts.append(p) or False)
        assert len(prompts) == 1 and "read my inbox" in prompts[0]
        (result,) = _tool_results(report, "ask_agent")
        assert result.startswith("DECLINED BY USER")
        assert len(client.chat.completions.calls) == 2  # mail never ran

    def test_approving_lets_it_through(self):
        registry = _fake_registry("browser", "mail")
        client = FakeClient([
            _call("ask_agent", agent="mail", question="read my inbox"),
            _text("mail: 3 unread"),
            _text("you have 3 unread"),
        ])
        report = _run(registry, client, "browser", confirmation_gate=lambda p: True)
        assert "mail: 3 unread" in _tool_results(report, "ask_agent")[0]

    def test_a_research_agent_asked_by_mail_needs_no_prompt(self):
        registry = _fake_registry("mail", "research_info")
        client = FakeClient([_call("ask_agent", agent="research_info", question="who wrote this"), _text("an author"), _text("ok")])
        prompts = []
        _run(registry, client, "mail", confirmation_gate=lambda p: prompts.append(p) or True)
        assert prompts == []


# ------------------------------------------------------------ discovery


class TestListAgents:
    def _list(self, registry, **arguments):
        client = FakeClient([_call("list_agents", **arguments), _text("ok")])
        report = _run(registry, client, "alpha")
        return _tool_results(report, "list_agents")[0]

    def test_no_argument_lists_every_agent_with_what_it_does(self, real_registry):
        text = self._list(real_registry)
        assert text.startswith(f"{len(real_registry.subagent_names)} agents you can ask")
        for name in real_registry.subagent_names:
            assert f"- {name} (" in text

    def test_one_agent_in_detail_marks_the_tools_that_ask_the_owner(self, real_registry):
        text = self._list(real_registry, agent="browser")
        assert text.startswith("browser (")
        assert "- browser_snapshot:" in text
        assert "- browser_submit [asks the owner first]:" in text

    def test_unknown_agent_is_an_honest_error(self, real_registry):
        assert self._list(real_registry, agent="ghost").startswith("ERROR: unknown agent 'ghost'")


# ------------------------------------------------------------ the Claude CLI bridge


class TestBridge:
    def test_ask_agent_is_not_exposed_to_the_external_cli_but_list_agents_is(self, real_registry):
        from dourmouse.mcp_bridge import _EXCLUDED_TOOL_NAMES, exposed_tools

        names = {t.name for t in exposed_tools(real_registry)}
        assert "ask_agent" in _EXCLUDED_TOOL_NAMES and "ask_agent" not in names
        assert "list_agents" in names


# ------------------------------------------------------------ the models are told


class TestTheModelsAreToldItIsAllowed:
    def test_the_pinned_chat_directive_names_the_peer_tools_and_keeps_the_pin(self):
        from dourmouse.webui import _focus_routing_directive

        text = _focus_routing_directive("browser")
        assert "'browser'" in text
        assert "never another subagent's tools, except the shared desk tools" in text
        assert "ask_agent" in text and "list_agents" in text and "delegate_to_models" in text
        # live, finding #189: a model read "ONLY the reviewer's tools" and refused ask_agent when the
        # allowance sat in a separate sentence; the peer tools are named inside the exception itself
        assert "and the two peer tools ask_agent and list_agents, which every chat screen may use" in text
        assert text.index("except the shared desk tools") < text.index("ask_agent") < text.index("Never ask your own agent")
        assert "Never ask your own agent" in text
        assert "\u2014" not in text  # house style: no em dash

    def test_the_claude_cli_briefing_allows_handing_one_job_to_a_named_agent(self):
        from dourmouse.model_context import claude_orchestrator_preamble, reset_cache

        reset_cache()
        try:
            text = claude_orchestrator_preamble()
        finally:
            reset_cache()
        assert "TALKING TO OTHER AGENTS" in text
        assert "mcp__dourmouse__list_agents" in text
        assert "mcp__dourmouse__delegate_to_models with a single task" in text
        assert '"agent": "<name>"' in text
        assert "with one exception" in text  # the old "never delegate a single step" rule now has its exception
