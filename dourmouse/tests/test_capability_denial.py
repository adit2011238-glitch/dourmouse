"""The capability-denial bug: DOURMOUSE tells the user it can't do things it
can do.

Traced live on the desktop, DOURMOUSE_BRIEF off (unrelated to that feature):
5 fresh sessions of "how much free disk space do I have" answered correctly
ONCE. The other four ended "I don't have the ability to check your system's
free disk space from here" -- while system_info exists, works, and returns
"DISK /home: 33.5 GB free of 220.7 GB".

Two independent defects, fixed here:

1. Scoping (dourmouse/planner.py): find_agents_for_query used `t in
   sub.name`, a raw substring check. The token "free" (from "free disk
   space") is a literal substring of "freebuff", so that query scored
   freebuff a false +3 that outranked system's real (but low, 1-2) match.
   The system agent's tools never got sent to the model at all.
   Separately, "disk"/"cpu" queries scored too low (1-2) to clear the >=3
   scoping threshold even without a competitor, so they were added as
   explicit domain words.

2. Failure mode (dourmouse/dispatch.py): even when scoping fails, the
   system prompt names AGENTS ("system", "dev_coding") in the same voice
   as tools, so the model called the bare agent name as a tool and got a
   generic "unknown tool" error -- which it retried verbatim three times,
   then reported as "I don't have the ability to...". The error now names
   the agent's REAL tools inline so the model can self-correct in the same
   turn instead of surrendering.
"""

from __future__ import annotations

from dourmouse.dispatch import run_dispatch
from dourmouse.general_roster import build_general_registry
from dourmouse.planner import find_agents_for_query
from dourmouse.tests.test_dispatch import FakeClient, _FakeMessage, _FakeResponse


class TestNameHitsIsAWordMatchNotASubstringMatch:
    def test_free_does_not_false_positive_match_freebuff(self):
        """The exact live failure: "free disk space" must not hand its score
        to freebuff just because "free" is a prefix of "freebuff"."""
        registry = build_general_registry()
        matches = find_agents_for_query(
            registry, "how much free disk space do I have", limit=10
        )
        by_name = {m["name"]: m["score"] for m in matches}
        assert "system" in by_name
        if "freebuff" in by_name:
            assert by_name["system"] >= by_name["freebuff"]

    def test_system_is_the_top_match_for_disk_space(self):
        registry = build_general_registry()
        matches = find_agents_for_query(
            registry, "how much free disk space do I have", limit=3
        )
        assert matches
        assert matches[0]["name"] == "system"
        assert matches[0]["score"] >= 3

    def test_system_is_the_top_match_for_cpu(self):
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "what is my CPU usage", limit=3)
        assert matches
        assert matches[0]["name"] == "system"
        assert matches[0]["score"] >= 3

    def test_memory_agent_still_wins_memory_queries(self):
        """The fix must not blur "disk"/"cpu" routing into stealing the real
        `memory` agent's own queries -- "memory" was deliberately NOT added
        as a system domain word for this reason."""
        registry = build_general_registry()
        matches = find_agents_for_query(
            registry, "what do you remember about my last project", limit=3
        )
        names = [m["name"] for m in matches]
        assert "memory" in names
        assert matches[0]["name"] == "memory"

    def test_short_substrings_no_longer_steal_unrelated_agents(self):
        """General regression guard, not just the freebuff case: no agent's
        name should score purely because a query token happens to be a
        PREFIX of it. news/'new', tasks/'task' remain real word matches and
        must still score, but a token must not credit an agent whose name
        merely CONTAINS it as a substring."""
        registry = build_general_registry()
        # "newest" is not a real match for "news" -- "new" would substring
        # inside "news" under the old bug, "newest" would too.
        matches = find_agents_for_query(registry, "show me the newest thing", limit=10)
        by_name = {m["name"]: m["score"] for m in matches}
        # If "news" scores here it must be from real hay/domain overlap
        # (the word "new" is a stopword-adjacent generic token), not a bare
        # name-substring artifact worth 3 points on its own.
        if "news" in by_name:
            assert by_name["news"] < 3


class TestSearchTheWebStillRoutesToResearchInfo:
    """The name_hits fix closed a SECOND accident along with the freebuff
    one: "search" is also a literal substring of "research_info" (re-
    SEARCH-info), so this exact scenario -- and the repo's own foundational
    test_map.py::test_web_search_ranks_research_info_first -- had quietly
    depended on the same bug. Without it, "search" alone is a 3-4-way tie
    with every other agent owning ANY search-shaped tool (memory's
    search_vault, atlas's repo_search, rnd's research_web_search...). Fixed
    with a narrow compound bonus: BOTH "search" and "web" together, credited
    only to the agent owning a tool literally named web_search -- which
    makes the pre-existing code comment ("search the web already routes via
    the tool stem") actually true instead of aspirational.
    """

    def test_search_the_web_routes_to_research_info_outright(self):
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "search the web for facts", limit=5)
        assert matches[0]["name"] == "research_info"
        assert "web_search" in matches[0]["tools"]
        # Not just a tie-break win -- a clear margin over the next agent.
        if len(matches) > 1:
            assert matches[0]["score"] > matches[1]["score"]

    def test_bare_search_without_web_does_not_get_the_bonus(self):
        """The bonus requires BOTH words -- "search my notes" must not
        favor research_info over the agent that actually owns note search."""
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "search my notes for the recipe", limit=5)
        names = [m["name"] for m in matches]
        assert names[0] != "research_info" or "notes" not in names

    def test_build_a_web_app_does_not_collide(self):
        """The bonus is gated on "search" too, so a coding request naming
        "web" alone must never be pulled toward research_info -- the exact
        collision the codebase's own comment on _DOMAIN_ROUTE warns against
        for a bare "web" domain word."""
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "build a web app", limit=5)
        names = [m["name"] for m in matches]
        assert "research_info" not in names


class TestUnknownToolGuardNamesTheRealTools:
    def _call(self, tool_name, arguments="{}"):
        from dourmouse.tests.test_planner import _FakeFunction, _FakeToolCall

        registry = build_general_registry()
        first = _FakeMessage(
            content=None,
            tool_calls=[_FakeToolCall("c1", tool_name, arguments)],
        )
        second = _FakeMessage(content="done")
        client = FakeClient([_FakeResponse(first), _FakeResponse(second)])
        report = run_dispatch("test", registry, client=client)
        tool_result = next(
            e for e in report["transcript"] if e.get("type") == "tool_result"
        )
        return tool_result["text"]

    def test_calling_an_agent_name_as_a_tool_names_its_real_tools(self):
        text = self._call("system")
        assert "AGENT, not a tool" in text
        assert "system_info" in text
        assert "delegate_task" in text
        assert "subagent='system'" in text

    def test_a_genuinely_unknown_name_keeps_the_old_message(self):
        """Only an actual agent-name collision gets the new guidance --
        a typo'd or hallucinated tool name that matches nothing keeps the
        plain error, which is already correct and honest."""
        text = self._call("not_a_real_tool_or_agent_xyz")
        assert "not in the registered roster" in text
        assert "AGENT" not in text

    def test_dev_coding_also_gets_named_tools(self):
        text = self._call("dev_coding")
        assert "AGENT, not a tool" in text
        assert "subagent='dev_coding'" in text
