"""Tests for the Phase 2.1 multi-step planner (dourmouse/planner.py).

Covers the deterministic heuristic (looks_multi_step), plan building with
subagent mapping (build_plan), the plan event emitted into the dispatch
transcript (wired in dispatch.py), and that find_agents_for_query still
works via the webui re-export (test_map.py imports it from there).
"""

from __future__ import annotations

import json

import pytest

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
        # unambiguous to assert research_info routing.
        plan = build_plan(
            "Search the web for NVIDIA earnings, then draft an email about it",
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
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="Final answer.")),
            ]
        )
        report = run_dispatch(
            "Search the web for facts about fusion, then draft an email about it",
            build_general_registry(),
            client=client,
            max_turns=2,
        )
        assert report["transcript"][0]["type"] == "plan"
        assert report["transcript"][0]["total"] >= 2
        assert report["transcript"][0]["steps"][0]["subagent"] == "research_info"

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
        import dourmouse.chat as chat_module
        from dourmouse.chat import ChatSession

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        session = ChatSession(
            build_general_registry(),
            client=FakeClient([_FakeResponse(_FakeMessage(content="Done."))]),
        )
        session.ask("Search the web, then draft an email about it", max_turns=2)
        records = [
            json.loads(ln)
            for ln in session.session_file.read_text().splitlines()
            if ln.strip()
        ]
        assert records[0]["transcript"][0]["type"] == "plan"

    def test_system_message_unaffected_by_planner(self):
        assert "ROSTER:" in system_message(build_general_registry())
