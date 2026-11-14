"""Jobs foundation (finding 198): kind, budget, board, routes, and the real GoalRuntime stopping a job at its budget."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import jobs
from dourmouse.general_roster import build_general_registry
from dourmouse.goal_runtime import GoalRuntime
from dourmouse.goals import GoalStore, get_goal_store, set_goal_store


@pytest.fixture
def store():
    return GoalStore(None)


def test_create_job_pins_agent_and_orders_steps(store):
    g = jobs.create_job("Tidy mail", ["read inbox", "draft replies"], owner="mail", store=store)
    assert g["status"] == "EXECUTING"
    assert g["resource_limits"]["kind"] == "agent" and g["resource_limits"]["owner_agent"] == "mail_calendar"
    tasks = store.list_tasks(g["id"])
    assert [t["description"] for t in tasks] == ["read inbox", "draft replies"]
    assert tasks[1]["depends_on"] == [tasks[0]["id"]] and {t["assigned_agent"] for t in tasks} == {"mail_calendar"}


def test_no_steps_means_the_title_is_the_one_step(store):
    g = jobs.create_job("Check the weather", store=store)
    assert [t["description"] for t in store.list_tasks(g["id"])] == ["Check the weather"]


@pytest.mark.parametrize("kwargs,msg", [
    ({"title": " "}, "title"),
    ({"title": "x", "owner": "nobody-here"}, "no agent"),
    ({"title": "x", "budget": {"max_runs": 0}}, "at least 1"),
    ({"title": "x", "budget": {"max_runs": True}}, "whole number"),
    ({"title": "x", "budget": {"max_runs": 10**6}}, "at most"),
    ({"title": "x", "budget": {"colour": 3}}, "unknown budget"),
    ({"title": "x", "steps": ["s"] * 25}, "at most"),
])
def test_bad_input_is_refused(store, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        jobs.create_job(store=store, **kwargs)


def test_plain_goals_are_not_jobs(store):
    store.create_goal_with_steps("a chat goal", ["one"])
    assert jobs.board(store)["total"] == 0


def test_runtime_stops_a_job_at_its_run_budget_and_raising_it_resumes(store):
    g = jobs.create_job("Loop", ["a", "b", "c"], budget={"max_runs": 1, "max_tool_calls": 50}, store=store)
    first = store.list_tasks(g["id"])[0]
    store.update_task_status(first["id"], "COMPLETED", increment_attempt=True)
    rt = GoalRuntime(store, registry=None)
    rt.tick()
    now = store.get_goal(g["id"])
    assert now["status"] == "BLOCKED" and "1 of 1 step runs" in now["blocked_reason"]
    assert store.list_tasks(g["id"])[1]["attempt_count"] == 0  # nothing ran past the budget
    done = jobs.set_budget(store, g["id"], {"max_runs": 5})
    assert done["status"] == "EXECUTING"


def test_tool_calls_count_against_the_budget(store):
    g = jobs.create_job("Tools", ["a", "b"], budget={"max_tool_calls": 2}, store=store)
    for _ in range(2):
        store.log_event(g["id"], "tool_call", {"name": "x"})
    assert "2 of 2 tool calls" in jobs.over_budget(store, g)


def test_raising_the_budget_does_not_unpark_a_job_waiting_on_the_owner(store):
    g = jobs.create_job("Wait", ["a"], store=store)
    store.update_goal_status(g["id"], "WAITING_FOR_APPROVAL", blocked_reason="needs a yes")
    out = jobs.set_budget(store, g["id"], {"max_runs": 99})
    assert out["status"] == "WAITING_FOR_APPROVAL"


def test_native_kinds_keep_their_own_budget(store):
    g = store.create_goal("Research: x", resource_limits={"kind": "research", "budget": {"max_papers": 3}})
    store.create_task(g["id"], "brief", assigned_agent="job:research")
    assert jobs.over_budget(store, g) is None and jobs.summary(store, g)["budget"] is None
    with pytest.raises(ValueError, match="own budget"):
        jobs.set_budget(store, g["id"], {"max_runs": 3})


def test_board_columns(store):
    a = jobs.create_job("A", store=store)
    b = jobs.create_job("B", store=store)
    store.pause_goal(b["id"])
    cols = {c["key"]: [j["id"] for j in c["jobs"]] for c in jobs.board(store)["columns"]}
    assert cols["running"] == [a["id"]] and cols["paused"] == [b["id"]]


# ---- routes -------------------------------------------------------------------------------------


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.webui import run_server

    set_goal_store(GoalStore(None))
    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(timeout=2)
    set_goal_store(None)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_routes_start_view_pause_resume_budget_cancel(server):
    st, made = call(server, "POST", "/api/os/jobs/start",
                    {"title": "Summarise PDFs", "steps": "open\nsummarise", "owner": "docs", "budget": {"max_runs": 7}})
    assert st == 200 and made["job"]["steps_total"] == 2 and made["job"]["budget"]["max_runs"] == 7
    jid = made["id"]
    st, board = call(server, "GET", "/api/os/jobs")
    assert st == 200 and board["total"] == 1
    st, d = call(server, "GET", f"/api/os/jobs/job?id={jid}")
    assert st == 200 and d["can_pause"] and len(d["steps"]) == 2 and d["log"]
    assert call(server, "POST", "/api/os/jobs/pause", {"id": jid})[1]["job"]["status"] == "PAUSED"
    assert call(server, "POST", "/api/os/jobs/pause", {"id": jid})[0] == 409
    assert call(server, "POST", "/api/os/jobs/resume", {"id": jid})[0] == 200
    assert call(server, "POST", "/api/os/jobs/budget", {"id": jid, "max_runs": 9})[1]["job"]["budget"]["max_runs"] == 9
    assert call(server, "POST", "/api/os/jobs/budget", {"id": jid, "max_runs": -1})[0] == 400
    assert call(server, "POST", "/api/os/jobs/cancel", {"id": jid})[1]["job"]["status"] == "CANCELLED"
    assert call(server, "POST", "/api/os/jobs/cancel", {"id": jid})[0] == 409


def test_routes_refuse_bad_input_and_plain_goals(server):
    assert call(server, "POST", "/api/os/jobs/start", {"title": ""})[0] == 400
    assert call(server, "POST", "/api/os/jobs/start", {"title": "x", "owner": "zzz"})[0] == 400
    assert call(server, "POST", "/api/os/jobs/pause", {"id": "../x"})[0] == 400
    g = get_goal_store().create_goal_with_steps("plain", ["one"])
    assert call(server, "POST", "/api/os/jobs/pause", {"id": g["id"]})[0] == 404
    assert call(server, "GET", f"/api/os/jobs/job?id={g['id']}")[0] == 404
