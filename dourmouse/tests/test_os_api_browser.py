"""Finding #153: the BROWSER screen's one backend route, GET /api/os/browser/attached."""

from __future__ import annotations

import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from dourmouse import browser_agent
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


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {"_raw": raw.decode("utf-8", "replace")}
    return resp.status, data


def fake_page(url="https://example.test/a", closed=False):
    return SimpleNamespace(url=url, is_closed=lambda: closed)


class TestAttached:
    def test_no_page_means_not_attached_and_nothing_is_launched(self, server, monkeypatch):
        monkeypatch.setattr(browser_agent, "_PAGE", None)
        launched = []
        monkeypatch.setattr(browser_agent, "_ensure_browser", lambda *a, **k: launched.append(1))
        status, data = call(server, "GET", "/api/os/browser/attached")
        assert status == 200 and data["ok"] is True
        assert data["attached"] is False and data["page_url"] == ""
        assert launched == []

    def test_a_live_page_reports_its_address(self, server, monkeypatch):
        monkeypatch.setattr(browser_agent, "_PAGE", fake_page("https://example.test/a?q=1"))
        status, data = call(server, "GET", "/api/os/browser/attached")
        assert status == 200 and data["attached"] is True
        assert data["page_url"] == "https://example.test/a?q=1"

    def test_a_closed_page_is_not_attached(self, server, monkeypatch):
        monkeypatch.setattr(browser_agent, "_PAGE", fake_page(closed=True))
        status, data = call(server, "GET", "/api/os/browser/attached")
        assert status == 200 and data["attached"] is False and data["page_url"] == ""

    def test_the_blank_pane_reports_an_empty_address(self, server, monkeypatch):
        monkeypatch.setattr(browser_agent, "_PAGE", fake_page("about:blank"))
        _, data = call(server, "GET", "/api/os/browser/attached")
        assert data["attached"] is True and data["page_url"] == ""

    def test_a_page_object_that_raises_is_not_attached_rather_than_a_500(self, server, monkeypatch):
        def boom():
            raise RuntimeError("driver gone")

        monkeypatch.setattr(browser_agent, "_PAGE", SimpleNamespace(url="x", is_closed=boom))
        status, data = call(server, "GET", "/api/os/browser/attached")
        assert status == 200 and data["attached"] is False

    def test_an_unreadable_address_still_says_attached(self, server, monkeypatch):
        class Page:
            def is_closed(self):
                return False

            @property
            def url(self):
                raise RuntimeError("navigating")

        monkeypatch.setattr(browser_agent, "_PAGE", Page())
        _, data = call(server, "GET", "/api/os/browser/attached")
        assert data["attached"] is True and data["page_url"] == ""

    def test_the_address_is_bounded(self, server, monkeypatch):
        monkeypatch.setattr(browser_agent, "_PAGE", fake_page("https://e.test/" + "a" * 5000))
        _, data = call(server, "GET", "/api/os/browser/attached")
        assert len(data["page_url"]) == 500

    def test_the_last_logged_action_and_launch_error_come_from_the_agent(self, server, monkeypatch):
        monkeypatch.setattr(browser_agent, "_PAGE", None)
        monkeypatch.setattr(browser_agent, "_LAUNCH_ERROR", "BROWSER LAUNCH FAILED: no chrome")
        monkeypatch.setattr(browser_agent, "_ACTIVITY", [{"at": "2026-09-27T01:02:03+00:00", "kind": "open", "text": "went to a page"}])
        _, data = call(server, "GET", "/api/os/browser/attached")
        assert data["launch_error"] == "BROWSER LAUNCH FAILED: no chrome"
        assert data["last"] == {"at": "2026-09-27T01:02:03+00:00", "kind": "open", "text": "went to a page"}

    def test_it_is_a_read_only_route(self, server):
        status, _ = call(server, "POST", "/api/os/browser/attached", {})
        assert status in (404, 405)
