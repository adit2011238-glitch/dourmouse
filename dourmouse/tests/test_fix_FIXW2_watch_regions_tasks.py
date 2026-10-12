"""FIX-W2 P4-32 (webui /api/tasks) and H-FD1-4 (world_watch_regions): a state file that cannot be read is
never replaced by an empty one, and the HTTP routes answer with a clear error instead of a 500."""
from __future__ import annotations

import http.client
import json
import os
import threading

import pytest

from dourmouse import world_watch_regions as wr
from dourmouse.dispatch import DispatchRegistry
from dourmouse.webui import run_server


@pytest.fixture(autouse=True)
def _files(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_WATCH_REGIONS_FILE", str(tmp_path / "watch_regions.json"))
    monkeypatch.setenv("DOURMOUSE_TASKS_FILE", str(tmp_path / "tasks.json"))


def _file(tmp_path):
    return tmp_path / "watch_regions.json"


# -- H-FD1-4: world_watch_regions ------------------------------------------- #

def test_a_corrupt_region_file_is_kept_aside_and_the_add_is_refused(tmp_path):
    _file(tmp_path).write_text("[{broken", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        wr.add_region("Bay", 36.0, 39.0, -123.0, -121.0)
    assert "kept as" in str(exc.value) and "not saved" in str(exc.value)
    assert not _file(tmp_path).exists()
    assert [p.name for p in tmp_path.iterdir() if ".corrupt-" in p.name]
    # the next add starts a new list and works
    assert wr.add_region("Bay", 36.0, 39.0, -123.0, -121.0)["id"] == "region-1"


def test_a_region_file_that_is_not_a_list_is_not_wiped_by_a_delete(tmp_path):
    _file(tmp_path).write_text('{"a": 1}', encoding="utf-8")
    with pytest.raises(RuntimeError):
        wr.delete_region("region-1")
    assert not _file(tmp_path).exists()
    assert [p.name for p in tmp_path.iterdir() if ".corrupt-" in p.name]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_region_file_is_left_exactly_where_it_is(tmp_path):
    wr.add_region("Keep", 1.0, 2.0, 1.0, 2.0)
    os.chmod(_file(tmp_path), 0)
    try:
        with pytest.raises(RuntimeError):
            wr.add_region("New", 3.0, 4.0, 3.0, 4.0)
        with pytest.raises(RuntimeError):
            wr.delete_region("region-1")
        assert _file(tmp_path).exists()
        assert wr.list_regions() == []  # reading still never raises
    finally:
        os.chmod(_file(tmp_path), 0o600)
    assert [r["name"] for r in wr.list_regions()] == ["Keep"]


def test_reading_a_corrupt_file_never_raises_and_does_not_touch_it(tmp_path):
    _file(tmp_path).write_text("nope", encoding="utf-8")
    assert wr.list_regions() == []
    assert _file(tmp_path).read_text(encoding="utf-8") == "nope"
    assert wr.check_region_hits({"layers": {}}) == {}


def test_ids_do_not_collide_after_a_delete():
    a = wr.add_region("A", 0.0, 1.0, 0.0, 1.0)
    b = wr.add_region("B", 2.0, 3.0, 2.0, 3.0)
    assert wr.delete_region(a["id"]) is True
    c = wr.add_region("C", 4.0, 5.0, 4.0, 5.0)
    assert c["id"] == "region-3" and c["id"] != b["id"]


def test_writes_are_atomic_and_concurrent_adds_lose_nothing(tmp_path):
    errs = []

    def add(i):
        try:
            wr.add_region(f"R{i}", 0.0, 1.0, 0.0, 1.0)
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errs
    assert len(wr.list_regions()) == 12 and len({r["id"] for r in wr.list_regions()}) == 12
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


# -- HTTP routes ------------------------------------------------------------ #

@pytest.fixture
def server():
    srv = run_server(DispatchRegistry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw or b"{}")


def test_get_tasks_on_a_corrupt_file_is_a_clear_error_not_a_500(server, tmp_path):
    (tmp_path / "tasks.json").write_text("{broken", encoding="utf-8")
    status, data = _call(server, "GET", "/api/tasks")
    assert status == 200
    assert data["tasks"] == [] and data["total"] == 0
    assert "tasks.json" in data["error"] and "could not be read" in data["error"]
    assert (tmp_path / "tasks.json").read_text(encoding="utf-8") == "{broken"  # reading never moves it


def test_get_tasks_still_works_when_the_file_is_fine(server):
    _call(server, "POST", "/api/tasks", {"title": "do it"})
    status, data = _call(server, "GET", "/api/tasks")
    assert status == 200 and data["total"] == 1 and "error" not in data


def test_add_region_route_reports_an_unreadable_file_clearly(server, tmp_path):
    _file(tmp_path).write_text("[{broken", encoding="utf-8")
    status, data = _call(server, "POST", "/api/world/regions",
                         {"name": "Bay", "min_lat": 36, "max_lat": 39, "min_lon": -123, "max_lon": -121})
    assert status == 409 and data["ok"] is False and "not saved" in data["error"]


def test_delete_region_route_reports_an_unreadable_file_clearly(server, tmp_path):
    _file(tmp_path).write_text("not json", encoding="utf-8")
    status, data = _call(server, "POST", "/api/world/regions/delete", {"id": "region-1"})
    assert status == 409 and data["ok"] is False
