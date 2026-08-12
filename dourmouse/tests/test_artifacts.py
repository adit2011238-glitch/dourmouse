"""Tests for the artifact renderer (v5.8) — live tables, equity curves,
markdown reports rendered beside the chat.

Covers the ArtifactStore (bounded, thread-safe, kind validation), the
publish_artifact tool (honest errors, JSON decoding for table/series), the
roster wiring (tool registered on research/coding/rnd/atlas agents but NOT
the single-tool orchestrator), the web routes (/api/artifacts, clear), and
the SSE sink that streams live 'artifact' events during a chat run.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import artifacts as art
from dourmouse.dispatch import DispatchRegistry, Permission, Subagent, ToolSpec
from dourmouse.general_roster import build_general_registry
from dourmouse.webui import run_server


@pytest.fixture(autouse=True)
def _fresh_store():
    """Hermetic: every test starts with an empty store singleton."""
    art.reset_default_store()
    yield art.default_store()
    art.reset_default_store()


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A real UI server bound to a free port with a fresh registry."""
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    reg = DispatchRegistry()
    reg.register_subagent(
        Subagent(
            name="echo_agent",
            domain="Test",
            description="echoes",
            tools=(
                ToolSpec(
                    name="echo",
                    description="echo back",
                    parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                    handler=lambda a: f"ECHOED: {a['text']}",
                ),
            ),
        )
    )
    srv = run_server(reg, port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


# --------------------------------------------------------------------------- #
# Store — publish / list / get / clear / cap
# --------------------------------------------------------------------------- #
class TestStore:
    def test_publish_markdown(self, _fresh_store):
        rec = _fresh_store.publish("markdown", "Report", "# Hello")
        assert rec["id"] == "art-1"
        assert rec["kind"] == "markdown"
        assert rec["title"] == "Report"
        assert rec["content"] == "# Hello"
        assert rec["created"] > 0

    def test_publish_table(self, _fresh_store):
        rec = _fresh_store.publish(
            "table",
            "Pairs",
            {"columns": ["pair", "pnl"], "rows": [["EURUSD", "1.5"], ["GBPUSD", "-0.2"]]},
        )
        assert rec["content"]["columns"] == ["pair", "pnl"]
        assert len(rec["content"]["rows"]) == 2

    def test_publish_series(self, _fresh_store):
        rec = _fresh_store.publish(
            "series", "Equity", {"labels": ["d1", "d2"], "values": ["100", "102.5"]}
        )
        assert rec["content"]["values"] == [100.0, 102.5]

    def test_list_newest_first(self, _fresh_store):
        _fresh_store.publish("markdown", "A", "a")
        _fresh_store.publish("markdown", "B", "b")
        ids = [r["id"] for r in _fresh_store.list()]
        assert ids == ["art-2", "art-1"]

    def test_get_and_clear(self, _fresh_store):
        rec = _fresh_store.publish("markdown", "A", "a")
        assert _fresh_store.get(rec["id"])["title"] == "A"
        assert _fresh_store.get("art-999") is None
        assert _fresh_store.clear() == 1
        assert _fresh_store.list() == []

    def test_cap_keeps_newest(self, _fresh_store):
        for i in range(art._MAX_ARTIFACTS + 10):
            _fresh_store.publish("markdown", f"A{i}", "x")
        # list() defaults to 40, so pass an explicit limit to see the cap.
        items = _fresh_store.list(limit=art._MAX_ARTIFACTS + 5)
        assert len(items) == art._MAX_ARTIFACTS
        assert items[0]["title"] == f"A{art._MAX_ARTIFACTS + 9}"

    def test_bad_kind_rejected(self, _fresh_store):
        with pytest.raises(ValueError, match="kind"):
            _fresh_store.publish("image", "T", "x")

    def test_empty_title_rejected(self, _fresh_store):
        with pytest.raises(ValueError, match="title"):
            _fresh_store.publish("markdown", "  ", "x")

    def test_bad_table_shape_rejected(self, _fresh_store):
        with pytest.raises(ValueError, match="columns"):
            _fresh_store.publish("table", "T", {"rows": []})
        with pytest.raises(ValueError, match="columns"):
            _fresh_store.publish("table", "T", {"columns": "x", "rows": []})
        with pytest.raises(ValueError, match="rows"):
            _fresh_store.publish("table", "T", {"columns": ["a"], "rows": "x"})

    def test_series_length_mismatch_rejected(self, _fresh_store):
        with pytest.raises(ValueError, match="equal length"):
            _fresh_store.publish("series", "S", {"labels": ["a"], "values": [1, 2]})

    def test_series_non_numeric_rejected(self, _fresh_store):
        with pytest.raises(ValueError, match="not numeric"):
            _fresh_store.publish("series", "S", {"labels": ["a"], "values": ["NaN?"]})

    def test_series_non_finite_rejected(self, _fresh_store):
        """float() accepts 'nan'/'inf' — reject them so the SVG chart can
        never render NaN coordinates (reviewer-caught)."""
        for bad in ("nan", "inf", "-inf", float("nan"), float("inf")):
            with pytest.raises(ValueError, match="not finite"):
                _fresh_store.publish("series", "S", {"labels": ["a"], "values": [bad]})

    def test_series_empty_rejected(self, _fresh_store):
        with pytest.raises(ValueError, match="at least one"):
            _fresh_store.publish("series", "S", {"labels": [], "values": []})

    def test_sink_fires_on_publish(self, _fresh_store):
        seen: list[dict] = []
        _fresh_store.set_sink(lambda evt: seen.append(evt))
        _fresh_store.publish("markdown", "A", "a")
        assert len(seen) == 1
        assert seen[0]["type"] == "artifact"
        assert seen[0]["artifact"]["id"] == "art-1"

    def test_raising_sink_never_breaks_publish(self, _fresh_store):
        def boom(_evt):
            raise RuntimeError("sink broke")

        _fresh_store.set_sink(boom)
        rec = _fresh_store.publish("markdown", "A", "a")  # must not raise
        assert rec["id"] == "art-1"


# --------------------------------------------------------------------------- #
# Tool — publish_artifact handler
# --------------------------------------------------------------------------- #
class TestTool:
    def test_tool_publishes_markdown(self, _fresh_store):
        text = art.publish_artifact_tool(
            {"kind": "markdown", "title": "Report", "content": "# Hi"}
        )
        assert "ARTIFACT PUBLISHED" in text
        assert "art-1" in text
        assert _fresh_store.list()[0]["title"] == "Report"

    def test_tool_decodes_json_for_table(self, _fresh_store):
        payload = json.dumps({"columns": ["a"], "rows": [["1"]]})
        text = art.publish_artifact_tool({"kind": "table", "title": "T", "content": payload})
        assert "ARTIFACT PUBLISHED" in text
        assert _fresh_store.list()[0]["content"]["columns"] == ["a"]

    def test_tool_honest_error_on_bad_json(self, _fresh_store):
        text = art.publish_artifact_tool(
            {"kind": "table", "title": "T", "content": "{not json"}
        )
        assert text.startswith("ERROR")
        assert "JSON" in text
        assert _fresh_store.list() == []

    def test_tool_honest_error_on_bad_kind(self, _fresh_store):
        text = art.publish_artifact_tool({"kind": "gif", "title": "T", "content": "x"})
        assert text.startswith("ERROR")
        assert "kind" in text

    def test_tool_registered_on_report_agents(self):
        reg = build_general_registry()
        for name in ("research_info", "dev_coding", "rnd", "atlas"):
            sub = reg.get_subagent(name)
            assert sub is not None
            names = {t.name for t in sub.tools}
            assert "publish_artifact" in names, f"{name} missing publish_artifact"

    def test_tool_not_on_orchestrator(self):
        reg = build_general_registry()
        sub = reg.get_subagent("orchestrator")
        assert {t.name for t in sub.tools} == {"delegate_task"}  # single-tool contract

    def test_tool_permission_is_regular(self):
        spec = art.build_artifact_tool_spec()
        assert spec.permission is Permission.REGULAR
        assert spec.parameters["required"] == ["kind", "title", "content"]


# --------------------------------------------------------------------------- #
# Web routes — GET /api/artifacts, POST /api/artifacts/clear, SSE sink
# --------------------------------------------------------------------------- #
class TestWebRoutes:
    def test_get_artifacts_returns_published(self, server):
        srv, port = server
        srv.artifacts.publish("markdown", "Report", "# Hi")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/artifacts")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        assert resp.status == 200
        assert data["artifacts"][0]["title"] == "Report"

    def test_get_artifacts_by_id(self, server):
        srv, port = server
        rec = srv.artifacts.publish("markdown", "Report", "# Hi")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/api/artifacts?id={rec['id']}")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        assert data["artifact"]["id"] == rec["id"]

    def test_clear_route(self, server):
        srv, port = server
        srv.artifacts.publish("markdown", "A", "a")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/api/artifacts/clear")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        assert data["ok"] is True and data["cleared"] == 1
        assert srv.artifacts.list() == []

    def test_chat_streams_live_artifact_event(self, server):
        """A publish_artifact call inside a run emits an SSE 'artifact' event.

        Uses the echo registry's tool mechanism: we publish directly through
        the shared store with the server's sink attached, simulating the
        webui wiring (set_sink(stream.emit) during _handle_chat). The real
        end-to-end path (tool -> sink) is covered by the store sink test;
        this verifies the event SHAPE the frontend consumes.
        """
        srv, _port = server
        events: list[dict] = []

        class _FakeStream:
            def emit(self, payload: dict) -> None:
                events.append(payload)

        srv.artifacts.set_sink(_FakeStream().emit)
        srv.artifacts.publish("series", "Equity", {"labels": ["a"], "values": [1]})
        srv.artifacts.set_sink(None)
        assert events and events[0]["type"] == "artifact"
        assert events[0]["artifact"]["kind"] == "series"
        assert events[0]["artifact"]["title"] == "Equity"

    def test_server_holds_default_store_when_none_passed(self, server):
        srv, _port = server
        assert srv.artifacts is not None
        # Publishing via the tool reaches the same store the server reads.
        art.publish_artifact_tool({"kind": "markdown", "title": "X", "content": "x"})
        assert srv.artifacts.list()[0]["title"] == "X"
