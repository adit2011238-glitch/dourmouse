"""Finding #143: the plug-in router the OS shell's screens add their backends through."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import os_api
from dourmouse.general_roster import build_general_registry


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


def call(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


@pytest.fixture
def temp_routes(monkeypatch):
    monkeypatch.setattr(os_api, "_ROUTES", dict(os_api._ROUTES))

    def add(method, path):
        return os_api.route(method, path)

    return add


class TestRouting:
    def test_the_handshake_route_lists_what_is_served(self, server):
        status, data = call(server, "GET", "/api/os/ping")
        assert status == 200 and data["ok"] and "GET /api/os/ping" in data["routes"]

    def test_a_registered_get_route_is_served_with_its_query(self, server, temp_routes):
        @temp_routes("GET", "/api/os/_t/echo")
        def echo(req):
            return 200, {"ok": True, "q": req.need("q")}

        assert call(server, "GET", "/api/os/_t/echo?q=hi") == (200, {"ok": True, "q": "hi"})

    def test_a_registered_post_route_gets_its_json_body(self, server, temp_routes):
        @temp_routes("POST", "/api/os/_t/add")
        def add(req):
            return 200, {"ok": True, "sum": req.body["a"] + req.body["b"]}

        assert call(server, "POST", "/api/os/_t/add", {"a": 2, "b": 3})[1]["sum"] == 5

    def test_an_expected_refusal_carries_its_status_and_message(self, server, temp_routes):
        @temp_routes("GET", "/api/os/_t/refuse")
        def refuse(req):
            raise os_api.ApiError(409, "already paused")

        assert call(server, "GET", "/api/os/_t/refuse") == (409, {"ok": False, "error": "already paused"})

    def test_a_missing_required_argument_is_a_400(self, server, temp_routes):
        @temp_routes("GET", "/api/os/_t/need")
        def need(req):
            return 200, {"ok": True, "id": req.need("id")}

        status, data = call(server, "GET", "/api/os/_t/need")
        assert status == 400 and "id is required" in data["error"]

    def test_an_unexpected_failure_is_an_honest_500(self, server, temp_routes):
        @temp_routes("GET", "/api/os/_t/boom")
        def boom(req):
            raise RuntimeError("the store is gone")

        status, data = call(server, "GET", "/api/os/_t/boom")
        assert status == 500 and data["ok"] is False and "the store is gone" in data["error"]

    def test_an_unknown_path_still_falls_through_to_the_normal_routes(self, server):
        assert call(server, "GET", "/api/attention")[0] == 200


class TestSameGuardsAsEverythingElse:
    def test_a_web_page_cannot_post_to_a_shell_route(self, server, temp_routes):
        @temp_routes("POST", "/api/os/_t/act")
        def act(req):
            return 200, {"ok": True}

        assert call(server, "POST", "/api/os/_t/act", {}, {"Origin": "https://evil.example"})[0] == 403
        assert call(server, "POST", "/api/os/_t/act", {})[0] == 200

    def test_a_foreign_host_cannot_read_one_either(self, server):
        port = server.server_address[1]
        assert call(server, "GET", "/api/os/ping", None, {"Host": f"evil.example:{port}"})[0] == 403


class TestABrokenBackendModuleDoesNotTakeTheOthersDown:
    def test_a_module_that_fails_to_import_is_reported_and_skipped(self, monkeypatch):
        import importlib
        import pkgutil
        from types import SimpleNamespace

        real_iter = pkgutil.iter_modules
        real_import = importlib.import_module

        def fake_iter(path=None, prefix=""):
            return list(real_iter(path, prefix)) + [SimpleNamespace(name="zz_broken")]

        def fake_import(name, package=None):
            if name.endswith(".zz_broken"):
                raise SyntaxError("invalid syntax (a half-written screen backend)")
            # re-run the @route decorators into the emptied table (the module may be cached already)
            return importlib.reload(real_import(name, package))

        monkeypatch.setattr(os_api, "_loaded", False)
        monkeypatch.setattr(os_api, "_FAILED", {})
        monkeypatch.setattr(os_api, "_ROUTES", {})
        monkeypatch.setattr(pkgutil, "iter_modules", fake_iter)
        monkeypatch.setattr(importlib, "import_module", fake_import)
        assert os_api.find("GET", "/api/os/ping") is not None, "the healthy modules still load"
        failures = os_api.failed()
        assert list(failures) == ["zz_broken"] and "SyntaxError" in failures["zz_broken"]

    def test_the_handshake_names_the_failed_modules(self, server, monkeypatch):
        monkeypatch.setattr(os_api, "_FAILED", {"news": "SyntaxError: x"})
        status, data = call(server, "GET", "/api/os/ping")
        assert status == 200 and data["failed_modules"] == {"news": "SyntaxError: x"}
