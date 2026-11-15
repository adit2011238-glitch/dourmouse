"""OFFICE floors route (finding #147)."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse.os_api.office import FLOORS, build_floors


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


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_every_agent_on_the_real_roster_appears_on_exactly_one_floor(server):
    status, data = call(server, "GET", "/api/os/office/floors")
    _, roster = call(server, "GET", "/api/roster")
    names = sorted(s["name"] for s in roster["subagents"])
    placed = sorted(a["name"] for f in data["floors"] for a in f["agents"])
    assert status == 200 and data["ok"]
    assert placed == names and data["total"] == len(names)
    assert data["unassigned"] == [] and data["unknown"] == []
    assert data["rule"]


def test_a_new_agent_is_never_dropped():
    subs = [{"name": "orchestrator", "tools": []}, {"name": "brand_new_agent", "tools": [1, 2]}]
    out = build_floors(subs)
    assert out["unassigned"] == ["brand_new_agent"]
    last = out["floors"][-1]
    assert last["id"] == "unassigned" and last["agents"][0]["tool_count"] == 2
    assert "orchestrator" not in out["unknown"] and "comms" in out["unknown"]


def test_no_agent_is_named_by_two_floors():
    names = [n for _, _, ns in FLOORS for n in ns]
    assert len(names) == len(set(names))


def test_mail_sits_in_the_lounge():
    out = build_floors([{"name": "mail", "tools": []}])
    lounge = next(f for f in out["floors"] if f["id"] == "lounge")
    assert [a["name"] for a in lounge["agents"]] == ["mail"]


def test_the_grouping_rule_is_plain_words_with_no_file_path():
    from dourmouse.os_api import office

    assert ".py" not in office.RULE and "dourmouse/" not in office.RULE
    assert "Unassigned" in office.RULE


# ---- Floor and Activity from real events (plan 1.7, finding 203) ----

def test_fourteen_agents_in_rooms_and_each_agent_sits_in_exactly_one(server):
    from dourmouse.agent_profiles import agent_ids

    status, data = call(server, "GET", "/api/os/office/floor")
    assert status == 200 and data["ok"]
    placed = [a for r in data["rooms"] for a in r["agents"]]
    assert sorted(placed) == sorted(agent_ids()) and len(placed) == len(set(placed)) == 14
    assert [r["id"] for r in data["rooms"]][-1] == "meeting"
    assert {a["status"] for a in data["agents"]} == {"idle"}  # a fresh workspace: nobody is doing anything


def test_every_profile_has_a_room():
    from dourmouse.agent_profiles import agent_ids
    from dourmouse.os_api.office import ROOMS

    named = [n for _, _, ns in ROOMS for n in ns]
    assert sorted(named) == sorted(agent_ids())


def test_a_feed_poll_does_not_make_a_desk_work_or_open_a_lane(server):
    server.tracker.on_event({"type": "live", "name": "news_headlines", "raw_arguments": "{}", "text": "ok"})
    _, floor = call(server, "GET", "/api/os/office/floor")
    _, act = call(server, "GET", "/api/os/office/activity")
    assert floor["counts"]["working"] == 0
    assert act["lanes"] == []
    assert [f["skill"] for f in act["feeds"]] == ["news"] and floor["feeds"] == act["feeds"]


def test_a_real_run_lights_the_desk_of_the_agent_that_owns_the_skill_and_opens_a_lane(server):
    server.tracker.on_event({"type": "tool_use", "name": "news_headlines", "raw_arguments": "{}", "call_id": "call-1"})
    _, floor = call(server, "GET", "/api/os/office/floor")
    desk = next(a for a in floor["agents"] if a["id"] == "news_world")
    assert desk["status"] == "working" and desk["calls"] == 1 and desk["doing"]["tool"] == "news_headlines"
    _, act = call(server, "GET", "/api/os/office/activity")
    assert [(lane["call_id"], lane["agent"], lane["agent_name"]) for lane in act["lanes"]] == [("call-1", "news_world", "News and World")]
    assert act["names"]["news"] == "News and World"
    server.tracker.on_event({"type": "done"})
    _, after = call(server, "GET", "/api/os/office/activity")
    assert after["lanes"] == []


def test_a_computing_call_without_a_call_id_lights_the_desk_but_opens_no_lane(server):
    server.tracker.on_event({"type": "tool_use", "name": "news_headlines", "raw_arguments": "{}"})
    _, floor = call(server, "GET", "/api/os/office/floor")
    _, act = call(server, "GET", "/api/os/office/activity")
    assert next(a for a in floor["agents"] if a["id"] == "news_world")["status"] == "working"
    assert act["lanes"] == []


def test_waiting_for_approval_is_needs_you_and_a_split_task_seats_the_agent_in_the_meeting_room(server):
    server.tracker.on_event({"type": "tool_use", "name": "news_headlines", "raw_arguments": "{}", "call_id": "c1"})
    server.tracker.on_event({"type": "confirmation_requested", "prompt": "ok?"})
    server.tracker.on_event({"type": "delegate_parallel_branch", "run_id": "r1", "index": 0, "total": 2, "agent": "markets", "phase": "start", "task": "t"})
    _, floor = call(server, "GET", "/api/os/office/floor")
    by_id = {a["id"]: a for a in floor["agents"]}
    assert by_id["news_world"]["status"] == "needs_you"
    assert by_id["markets"]["room"] == "meeting" and by_id["markets"]["meeting"] == "r1" and by_id["markets"]["status"] == "working"
    meeting = next(r for r in floor["rooms"] if r["id"] == "meeting")
    assert meeting["agents"] == ["markets"]
    assert "markets" not in next(r for r in floor["rooms"] if r["id"] == "trading")["agents"]


def test_agent_to_agent_messages_become_lines_between_two_desks(server):
    server.bus.post("orchestrator", "news", "Headlines please", "what is new today")
    server.bus.post("orchestrator", "BROADCAST", "hello all", "x")
    server.bus.post("mystery", "news", "who", "x")
    _, floor = call(server, "GET", "/api/os/office/floor")
    assert [(m["from"], m["to"], m["subject"]) for m in floor["lines"]] == [("orchestrator", "news_world", "Headlines please")]


def test_a_running_job_makes_its_owner_work(monkeypatch):
    from dourmouse.os_api.office import build_floor

    columns = {"running": [{"owner": "researcher", "title": "Compare two papers", "id": "j1"}],
               "needs_you": [{"owner": "coder", "title": "Pick a branch", "id": "j2"}]}
    floor = build_floor({"agents": {}, "fanouts": {}}, columns, [])
    by_id = {a["id"]: a for a in floor["agents"]}
    assert by_id["researcher"]["status"] == "working" and by_id["researcher"]["doing"]["text"] == "Compare two papers"
    assert by_id["coder"]["status"] == "needs_you"
    assert floor["counts"] == {"working": 1, "needs_you": 1, "idle": 12}
