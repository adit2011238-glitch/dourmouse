"""Home screen backend (finding 200): GET /api/os/today and the throwaway tab of a quick ask."""

from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timezone

import pytest

from dourmouse import projects_store as ps
from dourmouse.general_roster import build_general_registry
from dourmouse.goals import GoalStore, set_goal_store
from dourmouse.os_api import today as today_mod


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("DOURMOUSE_OWNER_NAME", raising=False)
    from dourmouse.webui import run_server

    set_goal_store(GoalStore(None))
    ps.set_project_store(ps.ProjectStore(tmp_path / "ws"))
    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(timeout=2)
    set_goal_store(None)
    ps.set_project_store(None)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


@pytest.mark.parametrize("hour,word", [(4, "night"), (5, "morning"), (11, "morning"), (12, "afternoon"), (16, "afternoon"), (17, "evening"), (21, "evening"), (22, "night")])
def test_part_of_day(hour, word):
    assert today_mod.part_of_day(hour) == word


def test_greeting_has_the_date_and_no_invented_name(monkeypatch):
    monkeypatch.delenv("DOURMOUSE_OWNER_NAME", raising=False)
    g = today_mod.greeting(datetime(2026, 11, 14, 9, 30))
    assert g == {"part": "morning", "date": "Saturday 14 November", "name": ""}
    monkeypatch.setenv("DOURMOUSE_OWNER_NAME", "Adit")
    assert today_mod.greeting(datetime(2026, 11, 14, 9, 30))["name"] == "Adit"


def test_an_empty_day_reports_empty_lists_not_samples(server):
    st, d = call(server, "GET", "/api/os/today")
    assert st == 200 and d["ok"]
    assert d["needs_you"] == [] and d["needs_total"] == 0 and d["projects"] == []
    assert d["office"]["working"] == 0 and d["office"]["waiting"] == 0 and d["office"]["line"] is None
    assert len(d["office"]["agents"]) == 14 and all(a["status"] == "idle" for a in d["office"]["agents"])
    assert d["calendar"]["state"] == "signed_out" and d["calendar"]["events"] == []


def test_running_and_parked_jobs_drive_the_office_and_needs_you(server):
    from dourmouse import jobs
    from dourmouse.goals import get_goal_store

    gs = get_goal_store()
    running = jobs.create_job("Find three papers", ["search"], owner="researcher")
    parked = jobs.create_job("Send email to Ms Carter", ["draft"], owner="mail_calendar")
    gs.update_goal_status(parked["id"], "WAITING_FOR_APPROVAL")
    st, d = call(server, "GET", "/api/os/today")
    by = {a["id"]: a["status"] for a in d["office"]["agents"]}
    assert by["researcher"] == "working" and by["mail_calendar"] == "waiting"
    assert d["office"]["working"] == 1 and d["office"]["waiting"] == 1
    assert d["office"]["line"] == {"agent": "Researcher", "text": "is working on Find three papers"}
    assert [n["title"] for n in d["needs_you"]] == ["Send email to Ms Carter"] and d["needs_total"] == 1
    assert d["needs_you"][0]["kind"] == "job" and d["needs_you"][0]["id"] == parked["id"] and running["id"]


def test_attention_feed_joins_needs_you_and_is_capped(server):
    for i in range(8):
        server.attention._add("fabrication", f"item {i}", "HOME", "detail")
    st, d = call(server, "GET", "/api/os/today")
    assert d["needs_total"] == 8 and len(d["needs_you"]) == today_mod.NEEDS_MAX
    assert all(n["kind"] == "attention" and n["dismiss_id"] for n in d["needs_you"])


def test_continue_tiles_are_projects_pinned_first_and_capped(server):
    store = ps.get_project_store()
    made = [store.create(f"Project {i}") for i in range(6)]
    store.update(made[4]["id"], pinned=True)
    st, d = call(server, "GET", "/api/os/today")
    names = [p["name"] for p in d["projects"]]
    assert len(names) == today_mod.TILE_MAX and names[0] == "Project 4"
    assert all("line" in p and p["id"] for p in d["projects"])


def test_tile_line_prefers_the_goal_then_open_work(server):
    store = ps.get_project_store()
    store.create("With goal", goal="Finish the IA")
    school = store.create("Schoolwork", template="school")
    st, d = call(server, "GET", "/api/os/today")
    lines = {p["name"]: p["line"] for p in d["projects"]}
    assert lines["With goal"] == "Finish the IA"
    assert school["work_items"] and lines["Schoolwork"]


def test_calendar_signed_in_lists_todays_events_in_local_time(monkeypatch):
    from dourmouse import google_services as gs

    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
    seen = {}

    def fake(method, url, token, body=None):
        seen["url"] = url
        return {"items": [
            {"summary": "Chemistry revision", "start": {"dateTime": "2026-11-14T10:00:00+00:00"}},
            {"summary": "Birthday", "start": {"date": "2026-11-14"}},
        ]}

    monkeypatch.setattr(gs, "_http_json", fake)
    req = type("R", (), {"user": "a@b.c"})()
    cal = today_mod.calendar(req, datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc))
    assert cal["state"] == "ok" and [e["title"] for e in cal["events"]] == ["Chemistry revision", "Birthday"]
    assert cal["events"][1]["time"] == "All day" and cal["events"][0]["time"].count(":") == 1
    assert "timeMin=" in seen["url"] and "timeMax=" in seen["url"] and "singleEvents=true" in seen["url"]


def test_calendar_error_is_reported_not_turned_into_an_empty_day(monkeypatch):
    from dourmouse import google_services as gs

    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

    def boom(*a, **k):
        raise RuntimeError("GOOGLE API 403 on calendar")

    monkeypatch.setattr(gs, "_http_json", boom)
    cal = today_mod.calendar(type("R", (), {"user": "a@b.c"})())
    assert cal["state"] == "error" and "403" in cal["note"] and cal["events"] == []


def test_calendar_with_no_events_says_so(monkeypatch):
    from dourmouse import google_services as gs

    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
    monkeypatch.setattr(gs, "_http_json", lambda *a, **k: {"items": []})
    cal = today_mod.calendar(type("R", (), {"user": None})())
    assert cal["state"] == "empty" and "Nothing" in cal["note"]


def test_quick_ask_tab_keeps_no_memory_and_writes_to_the_pruned_helper_folder(server, tmp_path):
    for tab in ("quick:abc1", "plain-tab"):
        # the reply is an event stream (an error event here: no model is configured), so it is read raw
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
        conn.request("POST", "/api/chat", body=json.dumps({"prompt": "hello", "tab_id": tab, "screen": "HOME"}))
        conn.getresponse().read()
        conn.close()
    quick = server.sessions_by_tab["quick:abc1"]
    plain = server.sessions_by_tab["plain-tab"]
    assert quick.memory is None and plain.memory is server.memory
    assert "helper" in str(quick.session_file.parent).lower()
    assert "helper" not in str(plain.session_file.parent).lower()


def test_the_new_feed_joins_needs_you_without_repeating_jobs(server):
    from dourmouse import attention

    attention.set_attention_store(attention.AttentionStore(server_ws(server)))
    try:
        attention.notify("needs_you", "A step needs your approval", screen="home", source="approvals")
        attention.notify("needs_you", "Needs you: tidy", screen="goals", params={"goal": "g1"}, source="jobs")
        attention.notify("finished", "Done: x", screen="goals")
        status, d = call(server, "GET", "/api/os/today")
        assert [n["title"] for n in d["needs_you"]] == ["A step needs your approval"]
        assert d["needs_you"][0]["kind"] == "feed" and d["needs_you"][0]["target"] == "home"
    finally:
        attention.set_attention_store(None)


def server_ws(srv):
    from dourmouse.config import workspace_dir

    return workspace_dir()
