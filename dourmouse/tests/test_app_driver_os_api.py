"""Phase F1: the /api/os/apps/* router, against a fake backend (no macOS calls)."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import os_api
from dourmouse.app_driver import backend as backend_mod
from dourmouse.app_driver import driver, policy, safety
from dourmouse.os_api import ApiError, Request
from dourmouse.tests.test_app_driver import FakeBackend

ROUTES = [("GET", "/api/os/apps/status"), ("GET", "/api/os/apps/allowed"), ("POST", "/api/os/apps/allow"),
          ("POST", "/api/os/apps/deny"), ("POST", "/api/os/apps/kill"), ("POST", "/api/os/apps/resume"),
          ("GET", "/api/os/apps/snapshot"), ("POST", "/api/os/apps/act")]


@pytest.fixture
def fake(monkeypatch):
    fb = FakeBackend()
    backend_mod.set_backend(fb)
    driver.clear_cache()
    monkeypatch.setattr(safety, "_killed", False)
    monkeypatch.setattr(driver, "_sleep", lambda s: None)
    yield fb
    backend_mod.set_backend(None)
    driver.clear_cache()


def call(method, path, body=None, query=None):
    handler = os_api.find(method, path)
    assert handler is not None, f"{method} {path} is not mounted"
    try:
        return handler(Request(server=None, query=query or {}, body=body or {}, user=None))
    except ApiError as exc:
        return exc.status, {"error": str(exc)}


def test_every_route_is_mounted_and_the_module_imported():
    assert "apps" not in os_api.failed()
    for method, path in ROUTES:
        assert os_api.find(method, path) is not None


def test_allow_flow_and_deny_list(fake):
    status, data = call("POST", "/api/os/apps/allow", {"app": "Terminal"})
    assert status == 403 and "deny list" in data["error"]
    status, data = call("POST", "/api/os/apps/allow", {"app": "textedit"})
    assert status == 200 and data["entry"]["bundle_id"] == "com.apple.TextEdit" and data["entry"]["name"] == "TextEdit"
    status, data = call("GET", "/api/os/apps/allowed")
    running = {a["name"]: a for a in data["running"]}
    assert running["TextEdit"]["allowed"] and running["Terminal"]["denied"] and not running["Terminal"]["allowed"]
    status, data = call("POST", "/api/os/apps/deny", {"app": "TextEdit"})
    assert status == 200 and data["removed"] is True and data["allowed"] == []


def test_snapshot_and_act_with_real_statuses(fake):
    status, data = call("GET", "/api/os/apps/snapshot", query={"app": ["TextEdit"]})
    assert status == 403
    call("POST", "/api/os/apps/allow", {"app": "TextEdit"})
    status, snap = call("GET", "/api/os/apps/snapshot", query={"app": ["TextEdit"]})
    assert status == 200 and snap["count"] == 8
    sid = snap["snapshot_id"]
    status, data = call("POST", "/api/os/apps/act", {"action": "click", "snapshot_id": sid, "element_id": "0.0"})
    assert status == 200 and data["ok"] and ("press", 4242, ("h", 0, 0)) in fake.calls
    status, data = call("POST", "/api/os/apps/act", {"action": "type", "snapshot_id": sid, "element_id": "0.1.0", "text": "Tr0ub4dor&3"})
    assert status == 422
    fake.tree["windows"][0]["children"].insert(0, {**fake.tree["windows"][0]["children"][0], "title": "New"})
    status, data = call("POST", "/api/os/apps/act", {"action": "click", "snapshot_id": sid, "element_id": "0.0"})
    assert status == 409 and "changed" in data["error"]
    status, data = call("POST", "/api/os/apps/act", {"action": "press_key", "app": "TextEdit", "key": "return", "dry_run": True})
    assert status == 200 and data["dry_run"] is True
    status, data = call("POST", "/api/os/apps/act", {"action": "click", "snapshot_id": sid, "element_id": "0.0", "amount": "lots"})
    assert status == 400


def test_kill_then_resume(fake):
    call("POST", "/api/os/apps/allow", {"app": "TextEdit"})
    status, data = call("POST", "/api/os/apps/kill", {"reason": "owner pressed stop"})
    assert status == 200 and data["kill"]["engaged"] is True and data["kill"]["reason"] == "owner pressed stop"
    status, data = call("GET", "/api/os/apps/snapshot", query={"app": ["TextEdit"]})
    assert status == 423
    status, data = call("GET", "/api/os/apps/status")
    assert data["kill"]["engaged"] is True and data["trusted"] is True and data["backend"] == "fake"
    status, data = call("POST", "/api/os/apps/resume", {})
    assert status == 200 and data["kill"]["engaged"] is False
    assert call("GET", "/api/os/apps/snapshot", query={"app": ["TextEdit"]})[0] == 200


def test_not_trusted_is_503_with_the_fix(fake):
    call("POST", "/api/os/apps/allow", {"app": "TextEdit"})
    fake.trusted = False
    status, data = call("GET", "/api/os/apps/snapshot", query={"app": ["TextEdit"]})
    assert status == 503 and "Privacy & Security > Accessibility" in data["error"]
    status, data = call("GET", "/api/os/apps/status")
    assert data["trusted"] is False and "Accessibility" in data["trust_help"]


def test_through_the_real_server(fake, monkeypatch, tmp_path):
    """The router is reached through webui's own hook, behind its auth gate."""
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    from dourmouse.general_roster import build_general_registry
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request("POST", "/api/os/apps/allow", body=json.dumps({"app": "Keychain Access"}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        assert resp.status == 403 and "deny list" in str(data), data
        assert policy.list_allowed() == []
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
