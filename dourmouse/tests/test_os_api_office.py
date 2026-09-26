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
