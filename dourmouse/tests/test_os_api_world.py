"""Finding #146: POST /api/os/world/refresh."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse.os_api import world


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setitem(world._last, "at", 0.0)
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


SNAP = {"generated_at": "2026-09-26T00:00:00+00:00", "pulse_score": 62, "pulse_label": "ELEVATED", "sources": {"quakes": {"ok": True, "count": 3}}, "items": {"quakes": []}}


class TestRefresh:
    def test_forces_a_repoll_and_returns_the_snapshot_shape(self, server, monkeypatch):
        seen = []
        monkeypatch.setattr("dourmouse.world_pulse.world_pulse_snapshot", lambda force=False: seen.append(force) or SNAP)
        status, data = call(server, "POST", "/api/os/world/refresh", {})
        assert status == 200 and data["ok"] and data["pulse_label"] == "ELEVATED" and seen == [True]

    def test_a_second_call_inside_the_gap_is_refused_with_the_wait(self, server, monkeypatch):
        monkeypatch.setattr("dourmouse.world_pulse.world_pulse_snapshot", lambda force=False: SNAP)
        assert call(server, "POST", "/api/os/world/refresh", {})[0] == 200
        status, data = call(server, "POST", "/api/os/world/refresh", {})
        assert status == 200 and data["ok"] is False and data["throttled"] and "seconds" in data["error"]

    def test_a_call_while_one_is_running_is_refused(self, server):
        assert world._lock.acquire(blocking=False)
        try:
            status, data = call(server, "POST", "/api/os/world/refresh", {})
        finally:
            world._lock.release()
        assert status == 200 and data["ok"] is False and data["busy"] and data.get("error")

    def test_get_is_not_a_route(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/api/os/world/refresh")
        status = conn.getresponse().status
        conn.close()
        assert status in (404, 405)
