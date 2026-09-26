"""GOALS routes (finding #147): board, pause, resume, create."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse.goals import GoalStore, get_goal_store, set_goal_store


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.webui import run_server

    set_goal_store(GoalStore(None))
    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)
    set_goal_store(None)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_the_board_is_empty_on_a_fresh_store(server):
    status, data = call(server, "GET", "/api/os/goals/board")
    assert status == 200 and data["goals"] == [] and data["total_goals"] == 0


def test_create_then_board_counts_tasks_from_real_rows(server):
    status, made = call(server, "POST", "/api/os/goals/create",
                        {"objective": "tidy the notes", "steps": ["collect", "sort", "file"], "priority": "high"})
    assert status == 200 and made["steps"] == 3 and made["goal"]["status"] == "EXECUTING"
    store = get_goal_store()
    tasks = store.list_tasks(made["goal"]["id"])
    store.update_task_status(tasks[0]["id"], "COMPLETED", result={"verified": True})
    store.update_task_status(tasks[2]["id"], "CANCELLED")
    _, board = call(server, "GET", "/api/os/goals/board")
    g = board["goals"][0]
    assert (g["tasks_total"], g["tasks_done"], g["tasks_verified"]) == (2, 1, 1)
    assert [t["status"] for t in g["tasks"]] == ["COMPLETED", "PENDING", "CANCELLED"]
    assert "result" not in g


def test_create_without_steps_makes_the_objective_the_one_task(server):
    _, made = call(server, "POST", "/api/os/goals/create", {"objective": "just this"})
    assert made["steps"] == 1
    assert [t["description"] for t in get_goal_store().list_tasks(made["goal"]["id"])] == ["just this"]


@pytest.mark.parametrize("body,fragment", [
    ({}, "objective is required"),
    ({"objective": "x" * 601}, "longer than"),
    ({"objective": "ok", "priority": "urgent-ish"}, "priority must be"),
    ({"objective": "ok", "steps": ["a"] * 13}, "at most 12"),
    ({"objective": "ok", "steps": ["a" * 501]}, "longer than 500"),
    ({"objective": "ok", "steps": 7}, "list of lines"),
])
def test_create_refuses_bad_input_and_creates_nothing(server, body, fragment):
    status, data = call(server, "POST", "/api/os/goals/create", body)
    assert status == 400 and fragment in data["error"]
    assert get_goal_store().list_goals() == []


def test_pause_and_resume_round_trip_restores_the_status(server):
    _, made = call(server, "POST", "/api/os/goals/create", {"objective": "pausable", "steps": ["a", "b"]})
    gid = made["goal"]["id"]
    status, paused = call(server, "POST", "/api/os/goals/pause", {"id": gid})
    assert status == 200 and paused["goal"]["status"] == "PAUSED"
    assert call(server, "POST", "/api/os/goals/pause", {"id": gid})[0] == 409
    status, back = call(server, "POST", "/api/os/goals/resume", {"id": gid})
    assert status == 200 and back["goal"]["status"] == "EXECUTING"
    assert call(server, "POST", "/api/os/goals/resume", {"id": gid})[0] == 409


def test_pause_and_resume_refuse_unknown_missing_and_malformed_ids(server):
    assert call(server, "POST", "/api/os/goals/pause", {"id": "goal_nope"})[0] == 404
    assert call(server, "POST", "/api/os/goals/resume", {"id": "goal_nope"})[0] == 404
    assert call(server, "POST", "/api/os/goals/pause", {})[0] == 400
    status, data = call(server, "POST", "/api/os/goals/pause", {"id": "../../etc/passwd"})
    assert status == 400 and "not a valid" in data["error"]


def test_a_finished_goal_cannot_be_paused(server):
    _, made = call(server, "POST", "/api/os/goals/create", {"objective": "done soon"})
    get_goal_store().update_goal_status(made["goal"]["id"], "COMPLETED")
    assert call(server, "POST", "/api/os/goals/pause", {"id": made["goal"]["id"]})[0] == 409


def test_the_board_reports_the_runtime_and_the_prompts_a_task_is_parked_on(server):
    _, made = call(server, "POST", "/api/os/goals/create", {"objective": "needs a yes", "steps": ["send it"]})
    store = get_goal_store()
    task = store.list_tasks(made["goal"]["id"])[0]
    store.update_task_status(task["id"], "WAITING_FOR_APPROVAL", result={"pending_prompts": ["send the mail to X?"]})
    _, board = call(server, "GET", "/api/os/goals/board")
    assert isinstance(board["runtime"]["running"], bool)
    assert board["goals"][0]["tasks"][0]["pending_prompts"] == ["send the mail to X?"]
