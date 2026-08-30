"""Tests for the Phase 2.1 multi-step planner (dourmouse/planner.py).

Covers the deterministic heuristic (looks_multi_step), plan building with
subagent mapping (build_plan), the plan event emitted into the dispatch
transcript (wired in dispatch.py), and that find_agents_for_query still
works via the webui re-export (test_map.py imports it from there).
"""

from __future__ import annotations

import json

from dourmouse.dispatch import run_dispatch, system_message
from dourmouse.general_roster import build_general_registry
from dourmouse.planner import build_plan, find_agents_for_query, looks_multi_step


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
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)

    def create(self, **kwargs):
        if not self._responses:
            raise RuntimeError("fake client exhausted")
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, responses):
        self.chat = _FakeChat(_FakeCompletions(responses))


class TestLooksMultiStep:
    def test_sequencing_markers(self):
        assert looks_multi_step("Search the web, then summarize the results")
        assert looks_multi_step("Find TODOs in the repo and also check coverage")
        assert looks_multi_step("First draft an email, then propose a time slot")

    def test_multiple_outcome_verbs(self):
        assert looks_multi_step("search and summarize NVIDIA news")
        assert looks_multi_step("write a script, run it, then check the output")

    def test_single_step_is_not_multi(self):
        assert not looks_multi_step("Search the web for NVIDIA news")
        assert not looks_multi_step("hello")
        assert not looks_multi_step("draft an email to the team")

    def test_parent_context_boilerplate_does_not_trip_heuristic(self):
        """Nested delegate prompts append '[PARENT CONTEXT — read this; ...]'
        background. The word 'read' and the semicolons must not turn a
        one-word task into a multi-step plan (live: task 'B' got a 3-step
        garbage plan and an extra LLM call)."""
        ctx = (
            "B\n\n[PARENT CONTEXT — read this; it is what the parent "
            "conversation already established]\nuser: A\nuser: go"
        )
        assert not looks_multi_step(ctx)
        registry = build_general_registry()
        assert build_plan(ctx, registry) is None

    def test_user_prompt_mentioning_feature_not_stripped(self):
        """The boilerplate anchor is the exact '[PARENT CONTEXT —' prefix, so
        a user prompt that merely MENTIONS the feature is never truncated."""
        p = "explain what [PARENT CONTEXT] blocks are for"
        assert looks_multi_step(p) is False  # not mangled into a plan
        assert "explain" not in [s["task"] for s in (build_plan(p, build_general_registry()) or [])]

    def test_capability_credit_is_per_verb_not_per_tool(self):
        """Kitchen-sink agents (system: read_path/list_path/open_path;
        memory: search_vault/recall) must not compound capability points
        over focused agents for one intent. 'search the web' must keep
        routing to research_info ahead of memory despite memory owning three
        search-stemmed tools."""
        registry = build_general_registry()
        m = find_agents_for_query(registry, "search the web", limit=4)
        assert m[0]["name"] == "research_info"
        mem = next((r for r in m if r["name"] == "memory"), None)
        res = m[0]
        if mem is not None:
            # memory's search_vault/recall/memory_search_semantic earn ONE
            # capability point total, not three.
            assert mem["score"] <= res["score"]

    def test_empty_prompt(self):
        assert not looks_multi_step("")
        assert not looks_multi_step("   ")


class TestBuildPlan:
    def test_single_step_returns_none(self):
        registry = build_general_registry()
        assert build_plan("Search the web for NVIDIA", registry) is None

    def test_multi_step_returns_numbered_steps_with_subagents(self):
        registry = build_general_registry()
        # "news" routes to the v2.3 news agent, so keep the research step
        # unambiguous to assert research_info routing. v8.11: was "Search
        # the web for NVIDIA earnings" — two accidental credits, both
        # closed by the same fix (the planner's name match stopped being a
        # raw substring check, so "free" no longer matches "freebuff" —
        # dourmouse_capability_denial): "nvidia" no longer name-matches
        # "code_nvidia" by coincidence, and "search" no longer name-matches
        # "research_info" (a substring of the word "research") either. The
        # second one mattered here: without it, "search the web for X" is a
        # genuine near-tie with `mail` (whose gmail_search tool also
        # satisfies the "search" capability verb), settled alphabetically
        # ("mail" < "research_info"). Saying "research" instead of "search"
        # is the honest way to mean it — a real word match, not a substring
        # accident — and routes unambiguously.
        plan = build_plan(
            "Research Apple's latest earnings online, then draft an email about it",
            registry,
        )
        assert plan is not None
        assert len(plan) >= 2
        assert plan[0]["n"] == 1
        assert plan[0]["subagent"] == "research_info"
        assert plan[1]["subagent"] == "comms"

    def test_deterministic_for_same_input(self):
        registry = build_general_registry()
        prompt = "Find TODOs in the workspace, then summarize them"
        assert build_plan(prompt, registry) == build_plan(prompt, registry)

    def test_max_steps_bounded(self):
        registry = build_general_registry()
        plan = build_plan(
            "search A, then B, then C, then D, then E, then F, then G, then H",
            registry,
            max_steps=3,
        )
        assert len(plan) <= 3

    def test_unknown_subject_falls_back_to_orchestrator(self):
        # A registry with no matching agent scores -> orchestrator carries it.
        from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec

        r = DispatchRegistry()
        r.register_subagent(
            Subagent(
                name="zzz",
                domain="Test",
                description="unrelated zebra tooling",
                tools=(ToolSpec(
                    name="zzz_tool",
                    description="zebra stuff only",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda a: "x",
                ),),
            )
        )
        plan = build_plan("Search the web for news, then write a poem", r)
        assert plan is not None
        assert all(s["subagent"] == "orchestrator" for s in plan)


class TestFindAgentsQueryStillWorks:
    def test_webui_reexport_matches(self):
        from dourmouse.webui import find_agents_for_query as webui_version

        registry = build_general_registry()
        assert webui_version is find_agents_for_query
        matches = find_agents_for_query(registry, "search the web", limit=2)
        assert matches[0]["name"] == "research_info"
        assert matches[0]["score"] >= 1


class TestPlanEventInTranscript:
    def test_multi_step_prompt_emits_plan_event_first(self):
        # A text-only answer without touching either planned step fires ONE
        # plan checkpoint (fabrication guard) and asks the model again; the
        # second response ends the run. Two responses keep the fake honest.
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="Final answer.")),
                _FakeResponse(_FakeMessage(content="Final answer.")),
            ]
        )
        # v8.11: same fix as above — "research" instead of "search the web"
        # (see TestBuildPlan.test_multi_step_returns_numbered_steps_with_
        # subagents for why).
        report = run_dispatch(
            "Research the latest Mars rover findings online, then draft an email about it",
            build_general_registry(),
            client=client,
            max_turns=2,
        )
        assert report["transcript"][0]["type"] == "plan"
        assert report["transcript"][0]["total"] >= 2
        assert report["transcript"][0]["steps"][0]["subagent"] == "research_info"
        # The second text-only answer is accepted as final, but with the
        # honesty caveat naming the unexecuted steps (Rule 2.2).
        assert report["final_text"].startswith("Final answer.")
        assert "not executed" in report["final_text"].lower()
        assert "STEP 1/2" in report["final_text"]

    def test_single_step_prompt_has_no_plan_event(self):
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="Final answer.")),
            ]
        )
        report = run_dispatch(
            "Search the web for NVIDIA news",
            build_general_registry(),
            client=client,
            max_turns=2,
        )
        assert all(t["type"] != "plan" for t in report["transcript"])

    def test_plan_persisted_with_session_jsonl(self, tmp_path, monkeypatch):
        """The plan event rides the transcript, so chat.py persists it to the
        session JSONL — audit trails for arbitrary sessions (Phase 2.1)."""
        from dourmouse.chat import ChatSession

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        # Two responses: the first fires the plan checkpoint (text-only with
        # unexecuted steps), the second is accepted as the final answer.
        session = ChatSession(
            build_general_registry(),
            client=FakeClient([
                _FakeResponse(_FakeMessage(content="Done.")),
                _FakeResponse(_FakeMessage(content="Done.")),
            ]),
        )
        session.ask("Search the web, then draft an email about it", max_turns=2)
        records = [
            json.loads(ln)
            for ln in session.session_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert records[0]["transcript"][0]["type"] == "plan"

    def test_system_message_unaffected_by_planner(self):
        assert "ROSTER:" in system_message(build_general_registry())


class TestFindAgentsForQueryRegression:
    """Regression: the planner must route steps to the agent that OWNS the
    named tool, and must not be derailed by file-path junk in the query.

    Live bug (surfaced end-to-end): "use write_file to save ... to
    /tmp/dm_orch_test.txt" routed STEP to admin_ops (which has NO write_file
    tool), so tool scoping never offered write_file and the chain silently
    degraded. The 2-char path fragment 'dm' was substring-matching the name
    'admin_ops' as a 3x name hit."""

    def test_tool_mention_wins_over_description_overlap(self):
        registry = build_general_registry()
        matches = find_agents_for_query(
            registry, "use write_file to save a one-line summary to /tmp/dm_orch_test.txt", limit=6
        )
        assert matches, "expected at least one match"
        assert matches[0]["name"] == "dev_coding", (
            f"write_file must route to its owner; got {matches}"
        )
        assert "write_file" in matches[0]["tools"]

    def test_path_fragment_cannot_name_admin_ops(self):
        registry = build_general_registry()
        # The old scorer counted 'dm' (from /tmp/dm_orch_test.txt) as a name
        # hit inside 'admin_ops' and as a hay hit. With path stripping it
        # must not even be a token, and admin_ops must score lower than any
        # agent that actually matches 'write'.
        matches = find_agents_for_query(
            registry, "save the results to /tmp/dm_orch_test.txt", limit=6
        )
        names = [m["name"] for m in matches]
        assert "admin_ops" not in names[:2]

    def test_agent_name_intent_still_routes(self):
        registry = build_general_registry()
        # 'research' inside research_info is a real name hit and must win.
        m = find_agents_for_query(registry, "research this topic", limit=1)
        assert m and m[0]["name"] == "research_info"

    def test_path_tokens_never_name_admin_ops(self):
        registry = build_general_registry()
        # The 'dm' fragment used to name-hit 'admin_ops'. A bare path-less
        # query like this must not route to admin_ops at all.
        m = find_agents_for_query(registry, "save the results to /tmp/dm_orch_test.txt", limit=6)
        assert m, "expected matches"
        assert "admin_ops" not in [r["name"] for r in m[:2]]

    def test_write_intent_routes_to_write_capable_agent(self):
        """Live bug (end-to-end): "save it to a file named outlook_brief.txt"
        routed to admin_ops — which owns NO write tool — by alphabet tie-
        break, so the model could never write and instead fabricated a saved
        path. A write-intent verb must bonus agents owning write-stemmed
        tools, and dev_coding (owns write_file) must win the step."""
        registry = build_general_registry()
        matches = find_agents_for_query(
            registry, "save it to a file named outlook_brief.txt in your workspace", limit=6
        )
        assert matches, "expected matches"
        assert matches[0]["name"] == "dev_coding", f"got {matches}"
        assert "write_file" in matches[0]["tools"]
        # admin_ops may still score on its "file" description, but it must
        # never WIN the step: it owns no write tool at all.
        assert matches[0]["score"] > matches[1]["score"], f"tie not broken: {matches}"

    def test_write_intent_in_plan_routes_to_dev_coding(self):
        """The full planner path for the live scenario: the "save it to a
        file" step must be planned at dev_coding, not admin_ops."""
        registry = build_general_registry()
        plan = build_plan(
            "Summarize the economic outlook, then save it to a file named "
            "outlook_brief.txt in your workspace",
            registry,
        )
        assert plan is not None and len(plan) >= 2
        write_step = plan[1]
        assert write_step["subagent"] == "dev_coding", f"got {plan}"

    def test_create_intent_routes_to_write_capable_agent(self):
        registry = build_general_registry()
        matches = find_agents_for_query(
            registry, "create a file called hello.txt inside the workspace", limit=6
        )
        assert matches
        assert matches[0]["name"] == "dev_coding", f"got {matches}"

    def test_domain_route_mail_for_inbox(self):
        """v5.2: 'check my inbox' must route to mail, never to admin_ops or
        news by token-overlap tie-break (live misroute)."""
        registry = build_general_registry()
        m = find_agents_for_query(registry, "check my inbox", limit=1)
        assert m and m[0]["name"] == "mail", f"got {m}"

    def test_domain_route_mail_beats_news_for_emails(self):
        """v5.2: 'summarize new emails' must route to mail — 'new' inside
        'news' was stealing the step (live misroute)."""
        registry = build_general_registry()
        m = find_agents_for_query(registry, "summarize new emails", limit=1)
        assert m and m[0]["name"] == "mail", f"got {m}"

    def test_domain_route_comms_for_draft(self):
        """v5.2: 'draft an email' routes to comms (the drafting agent), even
        though 'email' boosts mail — drafting is comms' job."""
        registry = build_general_registry()
        m = find_agents_for_query(registry, "draft an email to my boss", limit=1)
        assert m and m[0]["name"] == "comms", f"got {m}"

    def test_domain_route_markets_for_price(self):
        """v5.2: a price/quote question must reach the markets agent."""
        registry = build_general_registry()
        m = find_agents_for_query(registry, "how much is BTC worth", limit=1)
        assert m and m[0]["name"] == "markets", f"got {m}"

    def test_domain_route_research_for_weather(self):
        """v5.2: a weather question routes to research_info (web search)."""
        registry = build_general_registry()
        m = find_agents_for_query(registry, "weather in london", limit=1)
        assert m and m[0]["name"] == "research_info", f"got {m}"

    def test_domain_routing_is_deterministic_across_runs(self):
        """v5.2: domain boosts are collected as a SET of targets (no
        iteration-order dependence), so repeated runs give the same winner."""
        registry = build_general_registry()
        first = find_agents_for_query(registry, "draft an email to my boss", limit=1)
        for _ in range(10):
            again = find_agents_for_query(registry, "draft an email to my boss", limit=1)
            assert again[0]["name"] == first[0]["name"] == "comms"

    def test_web_is_not_a_domain_word(self):
        """v5.2 (reviewer-caught): 'web' must NOT be a strong domain word —
        "build a web app" is a CODING request and would be stolen to
        research_info by a +8 'web' boost. The 'search the web' intent still
        routes via the search verb + web_search tool stem."""
        registry = build_general_registry()
        m = find_agents_for_query(registry, "build a web app in python", limit=1)
        assert m and m[0]["name"] != "research_info", f"got {m}"

    def test_tool_mention_beats_domain_word(self):
        """v5.2 (reviewer-caught): an explicit tool mention (+5) must beat a
        domain-word boost (+4), so 'use gmail_search' routes to mail even
        when the query also mentions the news domain."""
        registry = build_general_registry()
        m = find_agents_for_query(registry, "use gmail_search to check the news", limit=1)
        assert m and m[0]["name"] == "mail", f"got {m}"

    def test_build_verb_capability_routes_to_write_agents(self):
        """v5.2 (reviewer-caught): 'build'/'make' are write-intent verbs, so
        a coding request reaches the write-capable agent, not a browser tool."""
        registry = build_general_registry()
        m = find_agents_for_query(registry, "write a python script that prints primes", limit=1)
        assert m and m[0]["name"] == "dev_coding", f"got {m}"

    def test_deterministic_with_paths(self):
        registry = build_general_registry()
        prompt = "search X then write_file to /tmp/a/b.txt"
        assert build_plan(prompt, registry) == build_plan(prompt, registry)

    def test_spotify_play_request_routes_to_music(self):
        """v13.1 (live-reported real bug): 'play a song on Spotify' from the
        MEDIA directive box had no domain word pointing at the music agent
        ('song'/'playlist' never appear in its description), so a plain
        typed request silently misrouted."""
        registry = build_general_registry()
        m = find_agents_for_query(
            registry, "play a song on Spotify by Daft Punk", limit=1
        )
        assert m and m[0]["name"] == "music", f"got {m}"

    def test_playlist_request_routes_to_music(self):
        registry = build_general_registry()
        m = find_agents_for_query(registry, "show me my playlists", limit=1)
        assert m and m[0]["name"] == "music", f"got {m}"
