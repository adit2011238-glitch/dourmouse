"""Finding #192: the step budget ran out on ordinary multi-part requests.

Live repro (2026-11-06): "top three headlines, how the S&P 500 is doing today, and my free disk space" used
all 8 steps and answered nothing. Three steps were waste (list_agents twice, then "markets.stock_quote", a real
tool under a made-up namespace), the markets agent was never offered because the planner scored "S&P 500" 1
(the bar is 3), and the last-chance summary call came back empty so the headlines and the disk figure already
gathered were thrown away.
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
    run_dispatch_messages,
    system_message,
)
from dourmouse.planner import domain_agents_in_query, find_agents_for_query


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _Call:
    def __init__(self, call_id, name, arguments="{}"):
        self.id, self.function = call_id, _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [type("Choice", (), {"message": msg})()]


class _Completions:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


class _Client:
    def __init__(self, script):
        self.chat = type("Chat", (), {"completions": _Completions(script)})()

    @property
    def calls(self):
        return self.chat.completions.calls


def _tool(name, handler=lambda a: "ok", **props):
    return ToolSpec(
        name=name, description=f"{name} tool",
        parameters={"type": "object", "properties": props, "required": []},
        handler=handler, permission=Permission.REGULAR,
    )


def _registry():
    r = DispatchRegistry()
    r.register_subagent(Subagent(
        name="worker", domain="Test", description="does work",
        tools=(_tool("echo", lambda a: f"ECHOED: {a.get('text', '')}", text={"type": "string"}),
               _tool("list_agents", lambda a: "roster of agents")),
    ))
    r.register_subagent(Subagent(name="other", domain="Test", description="not offered", tools=(_tool("secret"),)))
    return r


def _run(script, *, max_turns=8, registry=None):
    registry = registry or _registry()
    client = _Client(script)
    messages = [
        {"role": "system", "content": system_message(registry)},
        {"role": "user", "content": "do the work"},
    ]
    report = run_dispatch_messages(messages, registry, client=client, forced_agent="worker", max_turns=max_turns)
    return report, client


def _uses(report):
    return [e for e in report["transcript"] if e.get("type") == "tool_use"]


class TestFreeLookups:
    def test_reading_the_roster_does_not_spend_the_budget(self):
        """Two list_agents turns, then real work and the answer, inside a budget of 2. Charged, the run would
        end before the real work."""
        script = [
            _Resp(_Msg(tool_calls=[_Call("a", "list_agents")])),
            _Resp(_Msg(tool_calls=[_Call("b", "list_agents")])),
            _Resp(_Msg(tool_calls=[_Call("c", "echo", json.dumps({"text": "hi"}))])),
            _Resp(_Msg(content="done")),
        ]
        report, client = _run(script, max_turns=2)
        assert report["final_text"] == "done"
        assert not any(e.get("type") == "budget_exhausted" for e in report["transcript"])
        assert [u["name"] for u in _uses(report)] == ["list_agents", "list_agents", "echo"]
        assert len(client.calls) == 4

    def test_a_model_that_loops_on_lookups_is_still_stopped(self):
        """The refund is capped: after _MAX_FREE_LOOKUP_TURNS the lookups are charged like any other step."""
        report, _client = _run([_Resp(_Msg(tool_calls=[_Call("a", "list_agents")]))], max_turns=2)
        assert len(_uses(report)) == 2 + dispatch_module._MAX_FREE_LOOKUP_TURNS
        assert any(e.get("type") == "budget_exhausted" for e in report["transcript"])
        assert report["final_text"].strip()

    def test_real_work_is_still_charged(self):
        report, _client = _run([_Resp(_Msg(tool_calls=[_Call("a", "echo", json.dumps({"text": "x"}))]))], max_turns=3)
        assert len(_uses(report)) == 3
        assert any(e.get("type") == "budget_exhausted" for e in report["transcript"])


class TestNamespacedToolNames:
    def test_agent_dot_tool_runs_the_tool_in_the_same_step(self):
        script = [
            _Resp(_Msg(tool_calls=[_Call("a", "worker.echo", json.dumps({"text": "hi"}))])),
            _Resp(_Msg(content="done")),
        ]
        report, client = _run(script)
        (use,) = _uses(report)
        assert use["name"] == "echo" and use["called_as"] == "worker.echo"
        result = next(e for e in report["transcript"] if e.get("type") == "tool_result")
        assert result["text"] == "ECHOED: hi"
        assert len(client.calls) == 2  # no step spent on "did you mean"

    @pytest.mark.parametrize("written", ["functions.echo", "worker/echo", "tools:echo"])
    def test_other_separators(self, written):
        report, _client = _run([_Resp(_Msg(tool_calls=[_Call("a", written, json.dumps({"text": "x"}))])), _Resp(_Msg(content="ok"))])
        assert _uses(report)[0]["name"] == "echo"

    def test_a_tool_the_run_was_not_offered_does_not_resolve(self):
        """The alias never reaches a tool outside the run's scope: 'other.secret' keeps the old answer."""
        report, _client = _run([_Resp(_Msg(tool_calls=[_Call("a", "other.secret")])), _Resp(_Msg(content="ok"))])
        result = next(e for e in report["transcript"] if e.get("type") == "tool_result")
        assert result["text"].startswith("ERROR: unknown tool 'other.secret'")
        assert "called_as" not in _uses(report)[0]

    def test_resolver_unit(self):
        offered = {"stock_quote", "web_search"}
        assert dispatch_module._resolve_tool_alias("markets.stock_quote", offered) == "stock_quote"
        assert dispatch_module._resolve_tool_alias("stock_quote", offered) is None  # not an alias
        assert dispatch_module._resolve_tool_alias("markets.nothing", offered) is None
        assert dispatch_module._resolve_tool_alias("", offered) is None


class TestTheLastChanceSummary:
    def test_an_empty_first_reply_gets_one_more_try_as_a_user_turn(self):
        script = [
            _Resp(_Msg(tool_calls=[_Call("a", "echo", json.dumps({"text": "x"}))])),
            _Resp(_Msg(content="")),
            _Resp(_Msg(content="Here is the answer.")),
        ]
        report, client = _run(script, max_turns=1)
        assert report["final_text"] == "Here is the answer."
        assert len(client.calls) == 3
        assert client.calls[1]["tools"] == [] and client.calls[1]["messages"][-1]["role"] == "system"
        retry = client.calls[2]["messages"]
        assert retry[-1]["role"] == "user" and retry[-2]["role"] == "assistant"  # never a tool message then a user one
        assert client.calls[2]["tools"] == []

    def test_still_empty_shows_what_the_tools_returned(self):
        script = [
            _Resp(_Msg(tool_calls=[_Call("a", "echo", json.dumps({"text": "the disk has 156 GB free"}))])),
            _Resp(_Msg(content="")),
            _Resp(_Msg(content="   ")),
        ]
        report, client = _run(script, max_turns=1)
        text = report["final_text"]
        assert "tool budget" in text
        assert "- echo: ECHOED: the disk has 156 GB free" in text
        assert len(client.calls) == 3

    def test_errors_are_not_listed_as_gathered_results(self):
        digest = dispatch_module._tool_digest([
            {"type": "tool_result", "name": "stock_quote", "text": "ERROR: nope"},
            {"type": "tool_result", "name": "system_info", "text": "DISK: 156.4 GB free"},
        ])
        assert digest == "- system_info: DISK: 156.4 GB free"


class TestRouting:
    def _registry(self):
        from dourmouse.general_roster import build_general_registry

        return build_general_registry()

    @pytest.mark.parametrize("query", [
        "how is the S&P 500 doing today",
        "how is the S&P500 doing",
        "how is the s & p doing",
        "where is the nasdaq right now",
        "what is bitcoin at",
    ])
    def test_a_markets_question_routes_to_the_markets_agent(self, query):
        top = find_agents_for_query(self._registry(), query, limit=3)[0]
        assert top["name"] == "markets" and top["score"] >= 3

    def test_a_request_naming_three_domains_names_three_agents(self):
        named = domain_agents_in_query(
            "Give me a quick briefing: the top three news headlines right now, how the S&P 500 is doing today, "
            "and how much free disk space I have."
        )
        assert {"news", "markets", "system"} <= named

    def test_a_single_domain_request_names_one(self):
        assert domain_agents_in_query("how much free disk space do I have") == {"system"}

    def test_the_first_step_is_offered_all_three_agents_tools(self):
        registry = self._registry()
        client = _Client([_Resp(_Msg(content="ok"))])
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "Give me a quick briefing: the top three news headlines right now, how the "
                                        "S&P 500 is doing today, and how much free disk space I have."},
        ]
        run_dispatch_messages(messages, registry, client=client)
        offered = {t["function"]["name"] for t in client.calls[0]["tools"]}
        assert {"news_headlines", "stock_quote", "system_info"} <= offered

    def test_the_prompt_tells_the_model_to_batch_and_to_use_exact_names(self):
        assert "several tool calls at once" in dispatch_module._SYSTEM_PROMPT
        assert "exact name" in dispatch_module._SYSTEM_PROMPT


class TestRateLimitedSources:
    """Live: stock_quote was called seven times in one run after Yahoo said it was rate-limiting. The limit
    belongs to the source, so a second call with another symbol only spends a step."""

    def _registry(self, calls, answer):
        r = DispatchRegistry()

        def quote(args):
            calls.append(args.get("symbol"))
            return answer

        r.register_subagent(Subagent(
            name="worker", domain="Test", description="does work",
            tools=(_tool("stock_quote", quote, symbol={"type": "string"}),),
        ))
        return r

    LIMITED = "The source is rate-limiting me right now, so I couldn't get a quote for ^GSPC. Worth retrying in a minute."

    def test_after_a_rate_limit_the_tool_is_not_called_again_even_with_other_arguments(self):
        calls: list = []
        script = [
            _Resp(_Msg(tool_calls=[_Call("a", "stock_quote", '{"symbol": "^GSPC"}')])),
            _Resp(_Msg(tool_calls=[_Call("b", "stock_quote", '{"symbol": "SPY"}')])),
            _Resp(_Msg(content="The quote source is rate-limited right now.")),
        ]
        report, _client = _run(script, registry=self._registry(calls, self.LIMITED))
        assert calls == ["^GSPC"], "the second call must not reach the tool"
        first, second = [e for e in report["transcript"] if e.get("type") == "tool_result"]
        assert first["text"] == self.LIMITED
        assert "rate limit earlier in this run" in second["text"] and self.LIMITED in second["text"]
        assert report["final_text"] == "The quote source is rate-limited right now."

    def test_a_long_result_that_only_mentions_a_rate_limit_does_not_block_the_tool(self):
        calls: list = []
        page = "A long article about API design. " * 30 + "It discusses the 429 status and rate limits."
        script = [
            _Resp(_Msg(tool_calls=[_Call("a", "stock_quote", '{"symbol": "A"}')])),
            _Resp(_Msg(tool_calls=[_Call("b", "stock_quote", '{"symbol": "B"}')])),
            _Resp(_Msg(content="done")),
        ]
        run, _client = _run(script, registry=self._registry(calls, page))
        assert calls == ["A", "B"] and run["final_text"] == "done"

    def test_what_counts_as_a_rate_limit_message(self):
        assert dispatch_module._is_rate_limit_result(self.LIMITED)
        assert dispatch_module._is_rate_limit_result("HTTP 429 Too Many Requests")
        assert not dispatch_module._is_rate_limit_result("QUOTE AAPL: $190.10 USD")
        assert not dispatch_module._is_rate_limit_result("x" * 600 + " rate limit")
        assert not dispatch_module._is_rate_limit_result(None)

    def test_an_agents_answer_never_blocks_delegation(self):
        for name in ("delegate_task", "ask_agent", "delegate_parallel", "delegate_to_models"):
            assert name in dispatch_module._NESTED_RUN_TOOLS


class TestPlansDoNotInventSteps:
    """Live: "...for someone building AI products, and why? Keep it to three sentences." became two plan steps,
    the second ("why? Keep it to three sentences.") aimed at the security agent at score 2, and a good answer
    ended with "[DOURMOUSE: plan step(s) not executed via tools - STEP 2/2 (security)...]"."""

    def _registry(self):
        from dourmouse.general_roster import build_general_registry

        return build_general_registry()

    def test_a_fragment_below_the_scoring_bar_goes_to_the_orchestrator(self):
        from dourmouse.planner import build_plan

        plan = build_plan(
            "Which of those headlines matters most for someone building AI products, and why? Keep it to three sentences.",
            self._registry(),
        )
        assert plan is not None
        assert plan[-1]["task"].startswith("why?") and plan[-1]["subagent"] == "orchestrator"

    def test_a_real_two_domain_plan_keeps_its_agents(self):
        from dourmouse.planner import build_plan

        plan = build_plan("Give me the top news headlines, and tell me how much free disk space I have.", self._registry())
        assert {s["subagent"] for s in plan} == {"news", "system"}

    def test_an_orchestrator_step_is_never_reported_as_not_run(self):
        """End to end: the answer is not followed by a plan footer for a conversational step."""
        registry = self._registry()
        client = _Client([_Resp(_Msg(content="The election result matters most, because it sets AI policy."))])
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "Which of those headlines matters most for someone building AI products, "
                                        "and why? Keep it to three sentences."},
        ]
        report = run_dispatch_messages(messages, registry, client=client)
        # step 1 really is a news question the model answered without a tool, so it may be noted; the invented
        # second step ("why? Keep it to three sentences.") never is
        assert "STEP 2/2" not in report["final_text"] and "(security)" not in report["final_text"]
        assert all(2 not in e.get("steps", []) for e in report["transcript"] if e.get("type") == "plan_reminder")
