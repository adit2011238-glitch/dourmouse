"""Projects store (finding 199): templates, instructions, files, per-project passage stores, work items and jobs,
outputs, threads (quick asks never stored), timeline, search, pin and archive, and the routes."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import projects_store as ps
from dourmouse.general_roster import build_general_registry
from dourmouse.goals import GoalStore, set_goal_store


@pytest.fixture
def store(tmp_path):
    return ps.ProjectStore(tmp_path)


def test_templates_seed_instructions_agents_and_work(store):
    p = store.create("Year 12 Physics", template="school")
    assert p["template"] == "school" and "MLA" in p["instructions"]
    assert p["agents"] == ["researcher", "docs", "planner"] and len(p["work_items"]) == 3
    assert {t for t in ps.TEMPLATES} == {"blank", "school", "research", "personal_admin", "coding"}
    blank = store.create("Empty")
    assert blank["instructions"] == "" and blank["work_items"] == [] and blank["agents"] == []


@pytest.mark.parametrize("kwargs,msg", [
    ({"name": " "}, "required"), ({"name": "x" * 121}, "too long"), ({"name": "a\x00b"}, "control"),
    ({"name": "x", "template": "nope"}, "no template"), ({"name": "x", "agents": ["nobody-here"]}, "no agent"),
    ({"name": "x", "instructions": "i" * 8001}, "too long"), ({"name": "x", "goal": 5}, "text"),
])
def test_bad_input_refused(store, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        store.create(**kwargs)


def test_update_pin_archive_and_list_order(store):
    a, b = store.create("Alpha"), store.create("Beta")
    store.update(a["id"], pinned=True)
    assert [p["name"] for p in store.list()][0] == "Alpha"
    store.update(b["id"], archived=True)
    assert [p["name"] for p in store.list()] == ["Alpha"]
    assert {p["name"] for p in store.list(include_archived=True)} == {"Alpha", "Beta"}
    assert [p["name"] for p in store.list(query="alp")] == ["Alpha"]
    with pytest.raises(ValueError, match="true or false"):
        store.update(a["id"], pinned="yes")
    with pytest.raises(ValueError, match="cannot change"):
        store.update(a["id"], id="other")
    kinds = [e["kind"] for e in store.timeline(b["id"])]
    assert "archived" in kinds and "created" in kinds


def test_instructions_are_searchable_and_context_carries_them(store):
    p = store.create("Notes", instructions="Always answer in British English and cite the page.")
    hits = store.search(p["id"], "british english")
    assert hits and hits[0]["source"] == "instructions"
    ctx = store.context_for(p["id"], "anything")
    assert "British English" in ctx and "Notes" in ctx
    store.update(p["id"], instructions="Use metric units.")
    assert store.search(p["id"], "british") == [] and store.search(p["id"], "metric")


def test_files_are_copied_indexed_and_cited(store):
    p = store.create("Research")
    f = store.add_file(p["id"], "notes.md", text="Photosynthesis converts light to chemical energy.\n\nChlorophyll absorbs red and blue light.")
    assert f["indexed"] is True
    hit = store.search(p["id"], "chlorophyll")[0]
    assert hit["citation"] == "notes.md, passage 1" and "[" in hit["snippet"]
    name, raw = store.read_file(p["id"], f["id"])
    assert name == "notes.md" and b"Chlorophyll" in raw
    binary = store.add_file(p["id"], "pic.png", data=b"\x89PNG\r\n")
    assert binary["indexed"] is False
    assert "Relevant passages" in store.context_for(p["id"], "chlorophyll")
    store.remove_file(p["id"], f["id"])
    assert store.search(p["id"], "chlorophyll") == []
    with pytest.raises(LookupError):
        store.read_file(p["id"], f["id"])


@pytest.mark.parametrize("name", ["../x.txt", "a/b.txt", "..", "a\\b"])
def test_file_names_cannot_be_paths(store, name):
    p = store.create("P")
    with pytest.raises(ValueError):
        store.add_file(p["id"], name, text="x")


def test_file_limits(store, monkeypatch):
    p = store.create("P")
    with pytest.raises(ValueError, match="limit"):
        store.add_file(p["id"], "big.txt", data=b"x" * (ps.FILE_MAX_BYTES + 1))
    with pytest.raises(ValueError, match="either"):
        store.add_file(p["id"], "n.txt")
    monkeypatch.setattr(ps, "FILES_PER_PROJECT", 1)
    store.add_file(p["id"], "one.txt", text="1")
    with pytest.raises(ValueError, match="at most"):
        store.add_file(p["id"], "two.txt", text="2")


def test_projects_never_read_each_others_stores(store):
    a, b = store.create("A"), store.create("B")
    store.remember(a["id"], "The vault code word is pomegranate.")
    assert store.search(a["id"], "pomegranate")
    assert store.search(b["id"], "pomegranate") == []
    assert "pomegranate" not in store.context_for(b["id"], "pomegranate")
    assert (store.library / f"{a['id']}.db").exists() and (store.library / f"{b['id']}.db").exists()
    with pytest.raises(LookupError):
        store.passages("../etc")


def test_fts_operators_are_not_passed_through(store):
    p = store.create("P")
    store.remember(p["id"], "alpha beta gamma")
    for q in ['"', 'alpha OR', 'NEAR(', '*', 'alpha" AND "x', "", "   "]:
        store.search(p["id"], q)  # must not raise
    assert store.search(p["id"], "alpha gamma")


def test_memory_forget(store):
    p = store.create("P")
    m = store.remember(p["id"], "likes tea")
    assert store.get(p["id"])["memory"][0]["text"] == "likes tea"
    store.forget(p["id"], m["id"])
    assert store.search(p["id"], "tea") == [] and store.get(p["id"])["memory"] == []
    with pytest.raises(LookupError):
        store.forget(p["id"], m["id"])


def test_work_items_status_assignee_and_timeline(store):
    p = store.create("P")
    w = store.add_work(p["id"], "Draft essay", assignee="docs")
    assert w["status"] == "todo" and w["assignee"] == "docs"
    w = store.update_work(p["id"], w["id"], status="doing")
    assert w["status"] == "doing"
    with pytest.raises(ValueError, match="status"):
        store.update_work(p["id"], w["id"], status="finished")
    with pytest.raises(ValueError, match="no agent"):
        store.add_work(p["id"], "x", assignee="ghost")
    summaries = [e["summary"] for e in store.timeline(p["id"])]
    assert any("Draft essay: doing" in s for s in summaries)
    store.remove_work(p["id"], w["id"])
    with pytest.raises(LookupError):
        store.work_item(p["id"], w["id"])


def test_outputs_shelf(store):
    p = store.create("P")
    f = store.add_file(p["id"], "report.txt", text="done")
    o = store.add_output(p["id"], "Final report", kind="report", body="text", file_id=f["id"])
    assert store.get_output(p["id"], o["id"])["body"] == "text"
    assert store.get(p["id"])["outputs"][0]["title"] == "Final report"
    with pytest.raises(ValueError, match="not in this project"):
        store.add_output(p["id"], "x", file_id="nope")


def test_threads_store_only_real_project_turns(store):
    p = store.create("P")
    t = store.new_thread(p["id"])
    store.record_turn(p["id"], t["id"], "What is 2+2?", "4")
    got = store.thread(p["id"], t["id"])
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"] and got["title"] == "What is 2+2?"
    with pytest.raises(LookupError):  # a quick ask has no thread: nothing to write to
        store.record_turn(p["id"], "no-thread", "hi", "hello")
    other = store.create("Q")
    with pytest.raises(LookupError):  # a thread of another project is not reachable through this one
        store.record_turn(other["id"], t["id"], "hi", "hello")
    store.summarise_thread(p["id"], t["id"], "They asked a sum and got four.")
    assert store.search(p["id"], "sum")[0]["source"] == "thread"
    store.archive_thread(p["id"], t["id"])
    assert store.get(p["id"])["threads"] == []


def test_delete_removes_everything(store):
    p = store.create("P", instructions="x y z")
    f = store.add_file(p["id"], "a.txt", text="hello")
    store.delete(p["id"])
    with pytest.raises(LookupError):
        store.get(p["id"])
    assert not (store.library / f"{p['id']}.db").exists() and not (store.dir / p["id"]).exists()
    assert store.list(include_archived=True) == []
    assert f["id"]


# ---- routes -------------------------------------------------------------------------------------------------------

@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
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


def test_routes_end_to_end(server):
    st, tpl = call(server, "GET", "/api/os/project/templates")
    assert st == 200 and len(tpl["templates"]) == 5
    st, made = call(server, "POST", "/api/os/project/create", {"name": "Thesis", "template": "research"})
    assert st == 200
    pid = made["project"]["id"]
    assert call(server, "POST", "/api/os/project/create", {"name": ""})[0] == 400
    assert call(server, "POST", "/api/os/project/create", {"name": "x", "template": "zzz"})[0] == 400
    assert call(server, "GET", "/api/os/project/get?id=nope-nope")[0] == 404
    assert call(server, "GET", "/api/os/project/get")[0] == 400

    st, f = call(server, "POST", "/api/os/project/file/add", {"id": pid, "name": "s.txt", "text": "Graphene is a single layer of carbon."})
    assert st == 200
    st, res = call(server, "GET", f"/api/os/project/search?id={pid}&q=graphene")
    assert st == 200 and res["results"][0]["citation"] == "s.txt, passage 1"
    assert call(server, "GET", f"/api/os/project/file?id={pid}&file={f['file']['id']}")[1]["text"].startswith("Graphene")
    assert call(server, "POST", "/api/os/project/file/add", {"id": pid, "name": "b.bin", "data_base64": "!!"})[0] == 400

    assert call(server, "POST", "/api/os/project/memory/add", {"id": pid, "text": "Supervisor is Dr Lee"})[0] == 200
    assert call(server, "POST", "/api/os/project/update", {"id": pid, "pinned": True, "goal": "Finish chapter 2"})[1]["project"]["pinned"] is True
    assert call(server, "GET", "/api/os/project/list?q=thesis")[1]["projects"][0]["pinned"] is True

    st, th = call(server, "POST", "/api/os/project/thread/new", {"id": pid})
    tid = th["thread"]["id"]
    assert call(server, "POST", "/api/os/project/thread/turn", {"id": pid, "thread": tid, "user": "hi", "assistant": "hello"})[0] == 200
    assert call(server, "POST", "/api/os/project/thread/turn", {"id": pid, "thread": "ghost", "user": "hi", "assistant": "x"})[0] == 404
    assert len(call(server, "GET", f"/api/os/project/thread?id={pid}&thread={tid}")[1]["thread"]["messages"]) == 2
    assert call(server, "GET", f"/api/os/project/timeline?id={pid}")[0] == 200

    assert call(server, "POST", "/api/os/project/delete", {"id": pid})[0] == 400  # needs confirm
    assert call(server, "POST", "/api/os/project/delete", {"id": pid, "confirm": True})[0] == 200
    assert call(server, "GET", f"/api/os/project/get?id={pid}")[0] == 404


def test_work_run_starts_a_project_job_and_follows_it(server):
    st, made = call(server, "POST", "/api/os/project/create", {"name": "Admin", "template": "personal_admin"})
    pid = made["project"]["id"]
    st, w = call(server, "POST", "/api/os/project/work/add", {"id": pid, "title": "Sort the post", "assignee": "mail_calendar"})
    wid = w["work"]["id"]
    st, run = call(server, "POST", "/api/os/project/work/run", {"id": pid, "work": wid})
    assert st == 200 and run["work"]["status"] == "doing" and run["work"]["job_id"] == run["job_id"]
    assert call(server, "POST", "/api/os/project/work/run", {"id": pid, "work": wid})[0] == 409
    assert call(server, "POST", "/api/os/project/work/update", {"id": pid, "work": wid, "job_id": "x"})[0] == 400

    st, board = call(server, "GET", f"/api/os/jobs?project={pid}")
    assert board["total"] == 1
    assert call(server, "GET", "/api/os/jobs?project=someone-else")[1]["total"] == 0

    from dourmouse.goals import get_goal_store
    gs = get_goal_store()
    gs.update_goal_status(run["job_id"], "COMPLETED")
    got = call(server, "GET", f"/api/os/project/get?id={pid}")[1]["project"]
    item = next(x for x in got["work_items"] if x["id"] == wid)
    assert item["status"] == "done" and any(o["job_id"] == run["job_id"] for o in got["outputs"])


def test_stopped_job_sends_the_item_back_to_todo(server):
    pid = call(server, "POST", "/api/os/project/create", {"name": "Code", "template": "coding"})[1]["project"]["id"]
    wid = call(server, "POST", "/api/os/project/work/add", {"id": pid, "title": "Fix bug"})[1]["work"]["id"]
    run = call(server, "POST", "/api/os/project/work/run", {"id": pid, "work": wid})[1]
    from dourmouse.goals import get_goal_store
    get_goal_store().cancel_goal(run["job_id"])
    got = call(server, "GET", f"/api/os/project/get?id={pid}")[1]["project"]
    item = next(x for x in got["work_items"] if x["id"] == wid)
    assert item["status"] == "todo" and "stopped" in item["notes"]
