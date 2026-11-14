"""Agent profiles: thirteen agents plus Agent Smith over the old roster (PL-10 T3, finding #193)."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import agent_profiles as ap
from dourmouse.general_roster import build_general_registry


@pytest.fixture(scope="module")
def registry():
    return build_general_registry()


def test_there_are_thirteen_agents_plus_agent_smith():
    ids = ap.agent_ids()
    assert len(ids) == 14 and len(set(ids)) == 14
    assert ids[0] == "orchestrator" and ids[-1] == "agent_smith"


def test_every_registered_agent_is_a_skill_of_exactly_one_profile(registry):
    registered = sorted(s.name for s in registry.all_subagents())
    claimed = [skill for p in ap.PROFILES for skill in p.skills]
    assert len(claimed) == len(set(claimed)), "an old agent is claimed by two profiles"
    assert sorted(claimed) == registered
    payload = ap.build_agents_payload(registry)
    assert payload["unclaimed"] == [] and payload["missing_skills"] == []


def test_no_tool_is_lost_by_the_merge(registry):
    reachable = {t.name for p in ap.PROFILES for t in ap.profile_tools(p, registry)}
    assert reachable == registry.tool_names


def test_tools_two_agents_share_are_only_the_known_ones(registry):
    """A new overlap between agents shows up here instead of blurring who does what."""
    owners: dict[str, set[str]] = {}
    for p in ap.PROFILES:
        for t in ap.profile_tools(p, registry):
            owners.setdefault(t.name, set()).add(p.id)
    shared = {name: ids for name, ids in owners.items() if len(ids) > 1 and name not in ap.SHARED_LOOKUP_TOOLS}
    assert shared == {
        "drive_search": {"mail_calendar", "docs"},
        "drive_read": {"mail_calendar", "docs"},
        "publish_artifact": {"researcher", "coder"},
    }


def test_google_workspace_is_a_compatibility_name_only(registry):
    mc = ap.get_profile("mail_calendar")
    assert "google_workspace" in mc.skills and "google_workspace" not in mc.run_skills
    own = {t.name for t in ap.profile_tools(mc, registry)}
    assert "drive_create_doc" not in own and "sheets_create" not in own
    assert {"gmail_send", "list_calendar_events", "draft_message"} <= own
    found = ap.resolve("google_workspace")
    assert found == ap.Resolution("mail_calendar", ("google_workspace",), "skill")


@pytest.mark.parametrize("old, owner", [
    ("research_info", "researcher"), ("evidence_pipeline", "researcher"), ("rnd", "researcher"),
    ("study", "researcher"), ("research_mesh", "researcher"), ("memory", "librarian"),
    ("device_wiki", "librarian"), ("admin_ops", "librarian"), ("mail", "mail_calendar"),
    ("comms", "mail_calendar"), ("scheduling", "mail_calendar"), ("system", "mac_operator"),
    ("apps", "mac_operator"), ("app_driver", "mac_operator"), ("compute", "mac_operator"),
    ("panel_control", "mac_operator"), ("dev_coding", "coder"), ("reviewer", "coder"),
    ("code_claude", "coder"), ("code_codex", "coder"), ("code_deepseek", "coder"),
    ("code_nvidia", "coder"), ("code_ollama", "coder"), ("freebuff", "coder"),
    ("forex", "markets"), ("t212", "markets"), ("mt5", "markets"), ("news", "news_world"),
    ("worldmonitor", "news_world"), ("globe", "news_world"), ("music", "media"),
    ("design_3d", "media"), ("goals", "planner"), ("tasks", "planner"),
    ("companion", "orchestrator"), ("messenger", "orchestrator"),
])
def test_an_old_name_resolves_to_its_agent_scoped_to_that_skill(old, owner):
    assert ap.resolve(old) == ap.Resolution(owner, (old,), "skill")
    assert ap.agent_for_skill(old) == owner


@pytest.mark.parametrize("name", ["orchestrator", "docs", "browser", "security", "markets", "media", "agent_smith"])
def test_an_agent_id_that_is_also_an_old_name_reaches_the_whole_agent(name):
    found = ap.resolve(name)
    assert found.via == "agent" and found.agent_id == name
    assert found.skills == ap.get_profile(name).run_skills


def test_new_ids_resolve_to_all_their_run_skills():
    assert ap.resolve("researcher").skills == (
        "research_info", "evidence_pipeline", "rnd", "study", "research_mesh")
    assert ap.resolve("mail_calendar").skills == ("mail", "comms", "scheduling")
    assert ap.resolve(" coder ").agent_id == "coder"


@pytest.mark.parametrize("name", ["", "nobody", "Researcher", "agent/researcher"])
def test_unknown_names_resolve_to_nothing(name):
    assert ap.resolve(name) is None


def test_memory_namespace_and_workspace_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    p = ap.get_profile("researcher")
    assert p.memory_namespace == "agent/researcher"
    folder = ap.workspace_folder("researcher")
    assert folder == tmp_path / "ws" / "agents" / "researcher" and not folder.exists()
    assert ap.ensure_workspace("researcher") == folder and folder.is_dir()
    with pytest.raises(ValueError):
        ap.ensure_workspace("../escape")


def test_profiles_are_complete_and_untested_until_a_test_runs(registry):
    payload = ap.build_agents_payload(registry)
    assert payload["total"] == 14
    for agent in payload["agents"]:
        assert agent["job"] and agent["contract"] and agent["persona"], agent["id"]
        assert agent["tool_count"] > 0 and agent["step_budget"] == ap.DEFAULT_STEP_BUDGET
        assert agent["memory_namespace"] == f"agent/{agent['id']}"
        assert agent["availability"] == "untested" and agent["tests"]["pass_rate"] is None
        assert "—" not in json.dumps(agent), "no em dashes in served copy"


def test_availability_follows_recorded_results(registry):
    p = ap.get_profile("news_world")
    assert ap.build_profile(p, registry, results={"passed": 0, "total": 6})["availability"] == "unavailable"
    ok = ap.build_profile(p, registry, results={"passed": 4, "total": 6, "model": "gpt-oss:120b"})
    assert ok["availability"] == "available" and ok["tests"]["pass_rate"] == pytest.approx(4 / 6)


def test_permission_summary_reads_the_existing_rules(registry):
    from dourmouse.dispatch import Permission

    perms = ap.permission_summary(ap.get_profile("mail_calendar"), registry)
    gated = {t.name for t in ap.profile_tools(ap.get_profile("mail_calendar"), registry)
             if t.permission is Permission.REQUIRES_CONFIRMATION}
    assert set(perms["asks_owner_first"]) == gated
    assert perms["reads_text_from_others"] is True and "mail" in perms["reads_text_from_others_via"]
    smith = ap.permission_summary(ap.get_profile("agent_smith"), registry)
    assert smith["reads_text_from_others"] is False


def test_model_comes_from_the_existing_per_agent_config(registry):
    class Config:
        model = "base"

        def __init__(self, agent_models):
            self.agent_models = agent_models

        def model_for_agent(self, agent):
            return self.agent_models.get((agent or "").upper(), self.model)

    p = ap.get_profile("researcher")
    assert ap.build_profile(p, registry, Config({}))["model"] == "base"
    own = ap.build_profile(p, registry, Config({"RESEARCHER": "big"}))
    assert own["model"] == "big"
    mixed = ap.build_profile(p, registry, Config({"RND": "other"}))
    assert mixed["model"] == "base" and mixed["skill_models"]["rnd"] == "other"
    assert ap.build_profile(p, registry, None)["model"] == "default"


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def call(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_api_lists_the_fourteen_profiles(server):
    status, data = call(server, "/api/os/agents")
    assert status == 200 and data["ok"] and data["total"] == 14
    assert [a["id"] for a in data["agents"]] == list(ap.agent_ids())
    assert data["aliases"]["code_claude"] == "coder"


def test_api_answers_an_old_name_with_the_agent_that_owns_it(server):
    status, data = call(server, "/api/os/agents/profile?id=research_info")
    assert status == 200 and data["via"] == "skill" and data["profile"]["id"] == "researcher"
    status, data = call(server, "/api/os/agents/profile?id=markets")
    assert status == 200 and data["via"] == "agent" and data["profile"]["name"] == "Markets"


def test_api_refuses_unknown_and_missing_ids(server):
    assert call(server, "/api/os/agents/profile?id=nobody")[0] == 404
    assert call(server, "/api/os/agents/profile")[0] == 400


def test_profiles_never_claim_a_capability_test_result(server):
    _, data = call(server, "/api/os/agents")
    assert all(a["availability"] == "untested" for a in data["agents"])
