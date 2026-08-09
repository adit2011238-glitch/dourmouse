"""v5.12 World Monitor bridge tests (worldmonitor.py + wiring).

Every test is hermetic (Rule 2.1): the SDK's injectable ``transport`` is
replaced with a fake that answers canned World Monitor-shaped payloads, so
no network is ever touched. Verifies:

- worldmonitor_status: ok when the public health endpoint answers, honest
  when the API is unreachable, and key_configured reflects env
- worldmonitor_catalog: parses the real MCP tools/list shape
- worldmonitor_call_tool: unknown-name refusal, NOT CONFIGURED without a
  key, real data when a key is present, honest API/MCP error surfacing
- the worldmonitor subagent carries the three tools
- connections.check_connections reports the worldmonitor row honestly
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from worldmonitor_sdk import Client

from dourmouse import connections as conn
from dourmouse import worldmonitor as wm
from dourmouse.general_roster import build_general_registry

HEALTH_OK = {
    "status": "WARNING",
    "summary": {"total": 257, "ok": 248, "warn": 8, "crit": 1},
    "checkedAt": "2026-08-09T11:10:57.508Z",
}

TOOLS_RESULT = {
    "tools": [
        {"name": "get_market_data", "description": "Real-time equity quotes, commodities, crypto."},
        {"name": "get_country_risk", "description": "Country risk / resilience scores."},
    ]
}

MARKET_DATA_RESULT = {
    "cached_at": "2026-08-09T10:00:00Z",
    "stale": False,
    "data": {"crypto": {"quotes": [{"symbol": "BTC", "price": 61234.5, "changePercent": 1.2}]}},
}


def _make_fake_transport(responses: dict[str, Any]):
    """Build a transport callable that answers from a canned response table.

    ``responses`` maps a substring of the request URL to a dict of the form
    ``{"status": int, "content_type": str, "body": <json-serializable>}``.
    Unmatched requests raise AssertionError so a test that unexpectedly hits
    the network fails loudly instead of silently passing.
    """

    def transport(request: dict[str, Any], timeout: float):
        url = request["url"]
        for key, canned in responses.items():
            if key in url:
                body = json.dumps(canned["body"]).encode()
                return canned.get("status", 200), canned.get("content_type", "application/json"), body.decode()
        raise AssertionError(f"unexpected transport request: {request}")

    return transport


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    """The suite must never depend on (or leak) a real env key."""
    monkeypatch.delenv("WORLDMONITOR_API_KEY", raising=False)
    monkeypatch.delenv("WM_API_KEY", raising=False)
    # Point the SDK at nowhere-real so a test bug can't reach the internet.
    monkeypatch.setenv("WORLDMONITOR_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("WORLDMONITOR_MCP_URL", "http://127.0.0.1:1/mcp")


class TestStatus:
    def test_ok_without_key(self, monkeypatch):
        monkeypatch.setattr(
            wm, "_client",
            lambda transport=None: Client(transport=_make_fake_transport(
                {"health": {"body": HEALTH_OK}}
            )),
        )
        st = wm.worldmonitor_status()
        assert st["ok"] is True
        assert st["key_configured"] is False
        assert "257" in st["detail"]

    def test_ok_with_key(self, monkeypatch):
        monkeypatch.setenv("WORLDMONITOR_API_KEY", "wm_test")
        monkeypatch.setattr(
            wm, "_client",
            lambda transport=None: Client(transport=_make_fake_transport(
                {"health": {"body": HEALTH_OK}}
            )),
        )
        st = wm.worldmonitor_status()
        assert st["ok"] is True
        assert st["key_configured"] is True

    def test_unreachable_honest(self, monkeypatch):
        def boom(request, timeout):
            raise OSError("connection refused")

        monkeypatch.setattr(wm, "_client", lambda transport=None: Client(transport=boom))
        st = wm.worldmonitor_status()
        assert st["ok"] is False
        assert "unreachable" in st["detail"]


class TestCatalog:
    def test_parses_tools(self, monkeypatch):
        monkeypatch.setattr(
            wm, "_client",
            lambda transport=None: Client(transport=_make_fake_transport(
                {"mcp": {"body": {"jsonrpc": "2.0", "id": 1, "result": TOOLS_RESULT}}}
            )),
        )
        catalog = wm.worldmonitor_catalog()
        assert [t["name"] for t in catalog] == ["get_market_data", "get_country_risk"]
        assert catalog[0]["description"].startswith("Real-time")

    def test_unavailable_honest(self, monkeypatch):
        def boom(request, timeout):
            raise OSError("refused")

        monkeypatch.setattr(wm, "_client", lambda transport=None: Client(transport=boom))
        with pytest.raises(wm.WorldMonitorNotAvailable):
            wm.worldmonitor_catalog()


class TestCallTool:
    def test_unknown_name_refused(self, monkeypatch):
        with pytest.raises(wm.WorldMonitorNotAvailable, match="Unknown World Monitor tool"):
            wm.worldmonitor_call_tool("get_nonexistent", {})

    def test_empty_name_refused(self, monkeypatch):
        with pytest.raises(wm.WorldMonitorNotAvailable, match="non-empty"):
            wm.worldmonitor_call_tool("  ", {})

    def test_not_configured_without_key(self, monkeypatch):
        with pytest.raises(wm.WorldMonitorNotAvailable, match="NOT CONFIGURED"):
            wm.worldmonitor_call_tool("get_market_data", {})

    def test_returns_real_data_with_key(self, monkeypatch):
        monkeypatch.setenv("WORLDMONITOR_API_KEY", "wm_test")

        def transport(request, timeout):
            if "mcp" in request["url"]:
                payload = json.loads(request["body"])
                method = payload.get("method")
                if method == "tools/list":
                    body = {"jsonrpc": "2.0", "id": 1, "result": TOOLS_RESULT}
                elif method == "tools/call":
                    assert payload["params"]["name"] == "get_market_data"
                    body = {"jsonrpc": "2.0", "id": 1, "result": MARKET_DATA_RESULT}
                else:
                    raise AssertionError(f"unexpected method {method}")
                return 200, "application/json", json.dumps(body)
            raise AssertionError(f"unexpected url {request['url']}")

        monkeypatch.setattr(wm, "_client", lambda _t=transport: Client(transport=_t))
        result = wm.worldmonitor_call_tool("get_market_data", {"symbols": ["BTC"]})
        assert result["data"]["crypto"]["quotes"][0]["symbol"] == "BTC"

    def test_api_error_surfaced_honestly(self, monkeypatch):
        monkeypatch.setenv("WORLDMONITOR_API_KEY", "wm_test")

        def transport(request, timeout):
            return 500, "application/json", json.dumps({"error": "boom"})

        monkeypatch.setattr(wm, "_client", lambda _t=transport: Client(transport=_t))
        with pytest.raises(wm.WorldMonitorNotAvailable, match="failed"):
            wm.worldmonitor_call_tool("get_market_data", {})

    def test_mcp_auth_error_surfaced_honestly(self, monkeypatch):
        monkeypatch.setenv("WORLDMONITOR_API_KEY", "wm_test")

        def transport(request, timeout):
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32001, "message": "Authentication required."},
            }
            return 200, "application/json", json.dumps(body)

        monkeypatch.setattr(wm, "_client", lambda _t=transport: Client(transport=_t))
        with pytest.raises(wm.WorldMonitorNotAvailable, match="failed"):
            wm.worldmonitor_call_tool("get_market_data", {})

    def test_live_catalog_validates_name_not_in_frozen_fallback(self, monkeypatch):
        """Reviewer fix: the LIVE catalog is the source of truth. A tool the
        catalog advertises (even one NOT in the frozen allow-list fallback)
        must be callable; the frozen set is only an offline fallback."""
        monkeypatch.setenv("WORLDMONITOR_API_KEY", "wm_test")
        # Catalog advertises a tool that is NOT in _ALLOWED_TOOLS.
        live_tools = {
            "tools": [
                {"name": "get_brand_new_tool", "description": "added after our freeze"},
            ]
        }

        def transport(request, timeout):
            if "mcp" in request["url"]:
                payload = json.loads(request["body"])
                if payload.get("method") == "tools/list":
                    body = {"jsonrpc": "2.0", "id": 1, "result": live_tools}
                elif payload.get("method") == "tools/call":
                    assert payload["params"]["name"] == "get_brand_new_tool"
                    body = {"jsonrpc": "2.0", "id": 1, "result": {"ok": "new"}}
                else:
                    raise AssertionError(payload.get("method"))
                return 200, "application/json", json.dumps(body)
            raise AssertionError(f"unexpected url {request['url']}")

        monkeypatch.setattr(wm, "_client", lambda _t=transport: Client(transport=_t))
        assert "get_brand_new_tool" not in wm._ALLOWED_TOOLS  # the point
        result = wm.worldmonitor_call_tool("get_brand_new_tool", {})
        assert result == {"ok": "new"}


class TestToolHandlers:
    def test_status_tool(self, monkeypatch):
        monkeypatch.setattr(
            wm, "_client",
            lambda transport=None: Client(transport=_make_fake_transport(
                {"health": {"body": HEALTH_OK}}
            )),
        )
        text = wm._worldmonitor_status_tool({})
        assert "WORLD MONITOR STATUS" in text and "OK" in text

    def test_call_tool_not_configured_text(self, monkeypatch):
        text = wm._worldmonitor_call_tool({"tool_name": "get_market_data"})
        assert "NOT CONFIGURED" in text

    def test_call_tool_unknown_text(self, monkeypatch):
        text = wm._worldmonitor_call_tool({"tool_name": "nope"})
        assert "Unknown World Monitor tool" in text

    def test_call_tool_real_data_text(self, monkeypatch):
        monkeypatch.setenv("WORLDMONITOR_API_KEY", "wm_test")
        monkeypatch.setattr(
            wm, "_client",
            lambda transport=None: Client(transport=_make_fake_transport(
                {"mcp": {"body": {"jsonrpc": "2.0", "id": 1, "result": MARKET_DATA_RESULT}}}
            )),
        )
        text = wm._worldmonitor_call_tool({"tool_name": "get_market_data"})
        assert "WORLD MONITOR get_market_data (live)" in text
        assert "BTC" in text

    def test_call_tool_arguments_must_be_object(self):
        text = wm._worldmonitor_call_tool({"tool_name": "get_market_data", "arguments": [1, 2]})
        assert "must be a JSON object" in text


class TestRosterAndConnections:
    def test_worldmonitor_subagent_registered(self):
        registry = build_general_registry()
        sub = registry.get_subagent("worldmonitor")
        assert sub is not None
        names = {t.name for t in sub.tools}
        assert {"worldmonitor_status", "worldmonitor_catalog", "worldmonitor_call_tool"} <= names

    def test_connections_worldmonitor_keyless_skips_probe(self, monkeypatch):
        """Keyless poll must NOT make a remote call (module contract: no
        network on HUD polls) — it reports the missing key from env alone."""
        monkeypatch.setenv("WORLDMONITOR_API_KEY", "")
        called = {}

        def _boom_client(transport=None, timeout=None):
            called["client"] = True
            raise AssertionError("no remote probe on a keyless poll")

        monkeypatch.setattr(wm, "_client", _boom_client)
        report = conn.check_connections()
        row = report["worldmonitor"]
        assert row["ok"] is False
        assert "no WORLDMONITOR_API_KEY" in row["detail"]
        assert not called  # the probe was never reached

    def test_connections_worldmonitor_probes_when_key_present(self, monkeypatch):
        """With a key, the health probe runs and ok reflects the real answer."""
        monkeypatch.setenv("WORLDMONITOR_API_KEY", "wm_test")
        monkeypatch.setattr(
            wm, "_client",
            lambda transport=None, timeout=None: Client(transport=_make_fake_transport(
                {"health": {"body": HEALTH_OK}}
            )),
        )
        report = conn.check_connections()
        row = report["worldmonitor"]
        assert row["ok"] is True
        assert "key present" in row["detail"]
        assert "wm_test" not in json.dumps(report)  # never leak the key
