"""Finding #146: POST /api/os/news/research."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

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
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


URL = "https://example.org/a/story"


class TestToResearch:
    def test_creates_a_record_with_the_link_as_its_first_source(self, server):
        status, data = call(server, "POST", "/api/os/news/research", {"url": URL, "title": "Grid load rises", "channel": "news"})
        assert status == 200 and data["ok"] and data["created"] and data["source_added"]
        assert data["question"] == "Grid load rises" and data["sources"] == [URL]
        from dourmouse.research_pipeline_tools import _store

        rec = _store().load("Grid load rises")
        assert rec is not None and rec.sources == (URL,) and len(rec.plan) == 1

    def test_the_same_headline_twice_does_not_duplicate_the_source(self, server):
        call(server, "POST", "/api/os/news/research", {"url": URL, "title": "Same"})
        status, data = call(server, "POST", "/api/os/news/research", {"url": URL, "title": "Same"})
        assert status == 200 and not data["created"] and not data["source_added"] and data["sources"] == [URL]

    def test_a_second_link_for_a_known_headline_is_added(self, server):
        call(server, "POST", "/api/os/news/research", {"url": URL, "title": "Same"})
        status, data = call(server, "POST", "/api/os/news/research", {"url": "https://example.org/other", "title": "Same"})
        assert status == 200 and data["source_added"] and len(data["sources"]) == 2

    def test_it_calls_no_model(self, server, monkeypatch):
        import dourmouse.research_pipeline.stages as stages

        def boom(*a, **k):
            raise AssertionError("the plan stage calls a model and must not run here")

        monkeypatch.setattr(stages, "plan", boom)
        status, _ = call(server, "POST", "/api/os/news/research", {"url": URL, "title": "No model"})
        assert status == 200

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"title": "t"},
            {"url": URL},
            {"url": "   ", "title": "t"},
            {"url": "javascript:alert(1)", "title": "t"},
            {"url": "file:///etc/passwd", "title": "t"},
            {"url": "ftp://example.org/x", "title": "t"},
            {"url": "https://", "title": "t"},
            {"url": "https://exa mple.org/x", "title": "t"},
            {"url": "https://example.org/x\nSet-Cookie: a=b", "title": "t"},
            {"url": "https://example.org/" + "a" * 2100, "title": "t"},
            {"url": URL, "title": "   "},
            {"url": URL, "title": "t", "channel": "../../etc"},
        ],
    )
    def test_bad_input_is_refused_with_a_reason(self, server, body):
        status, data = call(server, "POST", "/api/os/news/research", body)
        assert status == 400 and data.get("error")

    def test_a_long_title_is_bounded(self, server):
        status, data = call(server, "POST", "/api/os/news/research", {"url": URL, "title": "x" * 5000})
        assert status == 200 and len(data["question"]) == 300
