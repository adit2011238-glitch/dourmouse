"""Phase B1: the OS API doors to the Electron shell's browser records
(dourmouse/os_api/browser.py), against a stand-in pane bridge."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dourmouse.general_roster import build_general_registry


class FakeBridge:
    """Records every request the server makes and answers from a script."""

    def __init__(self):
        self.requests: list[dict] = []
        self.answers: dict[tuple[str, str], tuple[int, bytes]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({
                    "method": self.command, "path": self.path, "body": json.loads(body) if body else None,
                    "origin": self.headers.get("Origin"), "site": self.headers.get("Sec-Fetch-Site"),
                })
                key = (self.command, self.path.split("?")[0])
                status, raw = outer.answers.get(key, (200, b'{"ok": true}'))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _serve

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def answer(self, method, path, status, payload):
        self.answers[(method, path)] = (status, payload if isinstance(payload, bytes) else json.dumps(payload).encode())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


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


@pytest.fixture
def bridge(monkeypatch):
    fake = FakeBridge()
    monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(fake.port))
    yield fake
    fake.close()


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=8)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {"_raw": raw.decode("utf-8", "replace")}
    return resp.status, data


ROUTES = [
    ("GET", "/api/os/browser/tabs", None),
    ("POST", "/api/os/browser/tabs/new", {"url": "https://a.example/"}),
    ("POST", "/api/os/browser/tabs/close", {"id": 2}),
    ("POST", "/api/os/browser/tabs/select", {"id": 2}),
    ("POST", "/api/os/browser/tabs/reopen", {}),
    ("GET", "/api/os/browser/downloads", None),
    ("POST", "/api/os/browser/downloads/cancel", {"id": "abc"}),
    ("GET", "/api/os/browser/history", None),
    ("POST", "/api/os/browser/history/add", {"url": "https://a.example/"}),
    ("POST", "/api/os/browser/history/remove", {"id": "x"}),
    ("POST", "/api/os/browser/history/clear", {}),
    ("GET", "/api/os/browser/bookmarks", None),
    ("POST", "/api/os/browser/bookmarks/add", {"url": "https://a.example/"}),
    ("POST", "/api/os/browser/bookmarks/remove", {"id": "x"}),
]


class TestOutsideTheElectronApp:
    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_every_route_says_why_it_cannot_answer_and_invents_nothing(self, server, monkeypatch, method, path, body):
        monkeypatch.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
        status, data = call(server, method, path, body)
        assert status == 503
        assert "Electron" in json.dumps(data)

    def test_a_port_that_is_not_a_port_is_treated_as_absent(self, server, monkeypatch):
        for bad in ("0", "70000", "abc", "-5", ""):
            monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", bad)
            assert call(server, "GET", "/api/os/browser/tabs")[0] == 503, bad

    def test_a_dead_shell_is_a_503_not_a_crash(self, server, monkeypatch):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(port))
        status, data = call(server, "GET", "/api/os/browser/tabs")
        assert status == 503 and "did not answer" in json.dumps(data)


class TestForwarding:
    def test_tabs_are_listed_from_the_shell(self, server, bridge):
        bridge.answer("GET", "/tabs", 200, {"ok": True, "tabs": [{"id": 1, "url": "", "active": True}], "active": 1, "closedTabs": 0, "secret": "dropped"})
        status, data = call(server, "GET", "/api/os/browser/tabs")
        assert status == 200
        assert data == {"ok": True, "tabs": [{"id": 1, "url": "", "active": True}], "active": 1, "closedTabs": 0}  # only the named fields pass

    def test_new_select_close_reopen_forward_their_arguments(self, server, bridge):
        bridge.answer("POST", "/tabs/new", 200, {"ok": True, "id": 7})
        assert call(server, "POST", "/api/os/browser/tabs/new", {"url": "https://a.example/", "background": True}) == (200, {"ok": True, "id": 7})
        call(server, "POST", "/api/os/browser/tabs/select", {"id": 3})
        call(server, "POST", "/api/os/browser/tabs/close", {"id": 3})
        call(server, "POST", "/api/os/browser/tabs/close", {})
        call(server, "POST", "/api/os/browser/tabs/reopen", {})
        sent = [(r["method"], r["path"], r["body"]) for r in bridge.requests]
        assert sent == [
            ("POST", "/tabs/new", {"url": "https://a.example/", "background": True}),
            ("POST", "/tabs/select", {"id": 3}),
            ("POST", "/tabs/close", {"id": 3}),
            ("POST", "/tabs/close", {}),
            ("POST", "/tabs/reopen", {}),
        ]

    def test_the_server_never_sends_what_would_make_the_bridge_refuse_it(self, server, bridge):
        call(server, "GET", "/api/os/browser/tabs")
        assert bridge.requests[0]["origin"] is None and bridge.requests[0]["site"] is None

    def test_history_search_and_limit_are_forwarded_and_encoded(self, server, bridge):
        bridge.answer("GET", "/history", 200, {"ok": True, "history": []})
        call(server, "GET", "/api/os/browser/history?q=a%20b%26c&limit=5")
        path = bridge.requests[0]["path"]
        assert path.startswith("/history?") and "q=a+b%26c" in path and "limit=5" in path

    def test_history_and_bookmark_records_round_trip(self, server, bridge):
        bridge.answer("POST", "/bookmarks/add", 200, {"ok": True, "added": True, "bookmark": {"id": "b1", "url": "https://a.example/", "title": "A"}})
        status, data = call(server, "POST", "/api/os/browser/bookmarks/add", {"url": "https://a.example/", "title": "A"})
        assert status == 200 and data["bookmark"]["id"] == "b1" and data["added"] is True
        call(server, "POST", "/api/os/browser/history/remove", {"url": "https://a.example/"})
        call(server, "POST", "/api/os/browser/history/remove", {"id": "h1", "url": "ignored"})
        assert bridge.requests[-2]["body"] == {"url": "https://a.example/"}
        assert bridge.requests[-1]["body"] == {"id": "h1"}  # an id wins over an address

    def test_a_download_can_be_cancelled_but_there_is_no_route_to_open_or_reveal_one(self, server, bridge):
        assert call(server, "POST", "/api/os/browser/downloads/cancel", {"id": "d1"})[0] == 200
        assert bridge.requests[0]["path"] == "/downloads/cancel"
        for verb in ("open", "reveal", "show", "launch"):
            status, _ = call(server, "POST", f"/api/os/browser/downloads/{verb}", {"id": "d1"})
            assert status == 404, verb
        assert len(bridge.requests) == 1


class TestValidation:
    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/os/browser/tabs/select", {}),
            ("/api/os/browser/tabs/select", {"id": "2"}),
            ("/api/os/browser/tabs/select", {"id": 0}),
            ("/api/os/browser/tabs/select", {"id": True}),
            ("/api/os/browser/tabs/close", {"id": -1}),
            ("/api/os/browser/tabs/new", {"url": 5}),
            ("/api/os/browser/tabs/new", {"url": "x" * 9000}),
            ("/api/os/browser/downloads/cancel", {}),
            ("/api/os/browser/history/add", {}),
            ("/api/os/browser/history/remove", {}),
            ("/api/os/browser/bookmarks/add", {"title": "no url"}),
            ("/api/os/browser/bookmarks/add", {"url": ["a"]}),
            ("/api/os/browser/bookmarks/remove", {}),
        ],
    )
    def test_a_bad_request_is_a_400_and_never_reaches_the_shell(self, server, bridge, path, body):
        status, _ = call(server, "POST", path, body)
        assert status == 400
        assert bridge.requests == []


class TestShellAnswers:
    def test_the_shells_own_refusal_reaches_the_caller_with_its_reason(self, server, bridge):
        bridge.answer("POST", "/tabs/select", 404, {"ok": False, "error": "no such tab"})
        status, data = call(server, "POST", "/api/os/browser/tabs/select", {"id": 9})
        assert status == 404 and "no such tab" in json.dumps(data)

    def test_ok_false_with_a_200_is_still_a_failure(self, server, bridge):
        bridge.answer("POST", "/tabs/reopen", 200, {"ok": False, "error": "no closed tab to reopen"})
        status, data = call(server, "POST", "/api/os/browser/tabs/reopen", {})
        assert status >= 400 and "no closed tab" in json.dumps(data)

    def test_a_shell_fault_or_garbage_is_a_502(self, server, bridge):
        bridge.answer("GET", "/bookmarks", 500, {"ok": False, "error": "boom"})
        assert call(server, "GET", "/api/os/browser/bookmarks")[0] == 502
        bridge.answer("GET", "/downloads", 200, b"<html>not json</html>")
        assert call(server, "GET", "/api/os/browser/downloads")[0] == 502
        bridge.answer("GET", "/tabs", 200, b"[1, 2]")
        assert call(server, "GET", "/api/os/browser/tabs")[0] == 502
