"""PL-10 T3 step 2: agent ids and old names both pin a run (finding 195)."""

from __future__ import annotations

import pytest

from dourmouse.agent_profiles import get_profile
from dourmouse.dispatch import agent_scope, is_known_agent, known_agent_names
from dourmouse.general_roster import _routing_directive, build_general_registry
from dourmouse.tests.test_ask_agent import _call, _run, _text
from dourmouse.tests.test_dispatch import FakeClient


@pytest.fixture(scope="module")
def registry():
    return build_general_registry()


@pytest.fixture(autouse=True)
def _no_fast_lane(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")


def test_every_agent_id_and_old_name_is_known(registry):
    from dourmouse.agent_profiles import agent_ids

    for name in list(agent_ids()) + list(registry.subagent_names):
        assert is_known_agent(registry, name), name
    assert not is_known_agent(registry, "nobody")
    assert "researcher" in known_agent_names(registry)


def test_old_name_scopes_to_exactly_that_skill(registry):
    agent_id, skills = agent_scope(registry, "research_info", "find a paper")
    assert agent_id == "researcher"
    assert skills == ("research_info",)


def test_agent_id_scopes_to_its_skills(registry):
    agent_id, skills = agent_scope(registry, "researcher", "")
    assert agent_id == "researcher"
    assert set(skills) == set(get_profile("researcher").run_skills) & set(registry.subagent_names)


def test_routing_directive_keeps_old_wording_and_names_new_agents(registry):
    assert "ONLY the 'mail' subagent" in _routing_directive(registry, "mail", "x")
    assert "as the 'researcher' agent" in _routing_directive(registry, "researcher", "x")


def test_run_pinned_to_agent_id_gets_that_agents_tools_not_others(registry):
    client = FakeClient([_text("ok")])
    _run(registry, client, "news_world")
    sent = {t["function"]["name"] for t in client.chat.completions.calls[0]["tools"]}
    news_tools = {t.name for t in registry.get_subagent("news").tools}
    mail_tools = {t.name for t in registry.get_subagent("mail").tools}
    assert news_tools & sent
    gmail = {n for n in mail_tools if n.startswith('gmail_')}
    assert gmail and not (gmail & sent)


def test_ask_agent_accepts_an_agent_id(registry):
    client = FakeClient([
        _call("ask_agent", agent="news_world", question="headlines?"),
        _text("three headlines"),
        _text("relayed"),
    ])
    report = _run(registry, client, "planner")
    assert report["final_text"] == "relayed"


def test_an_id_that_is_also_an_old_name_keeps_the_old_toolset(registry):
    for name in ("media", "markets", "docs", "browser", "security", "orchestrator", "agent_smith"):
        _, skills = agent_scope(registry, name, "open the thing")
        assert name in skills, name
