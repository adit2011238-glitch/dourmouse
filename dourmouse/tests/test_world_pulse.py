"""Hermetic tests for the v5.27 self-hosted World Pulse monitor.

No network: ``world_pulse._http_get`` and the live_feeds market helpers are
monkeypatched with canned real-shaped payloads (Google-News/GDACS/CISA/
ReliefWeb RSS + World Bank JSON). The overriding contract: every source is
failure-isolated, nothing is fabricated, and the snapshot NEVER raises.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import world_pulse as wp
from dourmouse.general_roster import build_general_registry

_NEWS_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><item>
<title>Global markets rally on rate-cut hopes</title><source>Reuters</source>
<pubDate>Fri, 13 Aug 2026 10:00:00 GMT</pubDate><link>https://n.example/1</link>
</item></channel></rss>"""

_DISASTER_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><item>
<title>EQ - 6.8 - Alaska</title><description>Earthquake alertlevel: Red. A 6.8 magnitude
earthquake struck Alaska.</description><pubDate>Fri, 13 Aug 2026 09:00:00 GMT</pubDate>
<link>https://g.example/1</link></item></channel></rss>"""

_CYBER_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><item>
<title>New advisory: critical RCE in a router vendor</title>
<description>Critical severity — remote code execution.</description>
<pubDate>Fri, 13 Aug 2026 08:00:00 GMT</pubDate><link>https://c.example/1</link>
</item></channel></rss>"""

_CONFLICT_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><item>
<title>ReliefWeb: displacement rises in the Horn of Africa</title>
<description><![CDATA[<p>Humanitarian update on displacement.</p>]]></description>
<pubDate>Fri, 13 Aug 2026 07:00:00 GMT</pubDate><link>https://r.example/1</link>
</item></channel></rss>"""

_WB_JSON = json.dumps(
    [
        {"page": 1, "pages": 1, "per_page": 1, "total": 1},
        [{"indicator": {"id": "NY.GDP.MKTP.KD.ZG"}, "country": {"id": "US"}, "date": "2025", "value": 2.1}],
    ]
)


def _fake_http_get(url: str) -> str:
    if "news.google.com" in url:
        return _NEWS_RSS
    if "gdacs.org" in url:
        return _DISASTER_RSS
    if "cisa.gov" in url:
        return _CYBER_RSS
    if "reliefweb.int" in url:
        return _CONFLICT_RSS
    if "worldbank.org" in url:
        return _WB_JSON
    raise AssertionError(f"unexpected url in test: {url}")


def _fake_movers(direction="gainers", count=5):
    rows = [
        {"symbol": "AAA", "name": "Alpha Co", "price": 12.5, "currency": "USD", "change": 1.0, "change_pct": 8.0},
        {"symbol": "BBB", "name": "Beta Co", "price": 5.0, "currency": "USD", "change": 0.3, "change_pct": 6.0},
    ]
    return rows[:count]


def _fake_quote(symbol):
    return {"symbol": symbol, "price": 100.0, "currency": "USD", "as_of": 1_700_000_000}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(wp, "_http_get", _fake_http_get)
    import dourmouse.live_feeds as lf

    monkeypatch.setattr(lf, "market_movers", _fake_movers)
    monkeypatch.setattr(lf, "stock_quote", _fake_quote)
    monkeypatch.setenv("DOURMOUSE_WORLD_PULSE_TTL", "300")  # cache across calls in one test
    with wp._cache_lock:
        wp._cache.update({"at": 0.0, "snapshot": None})
    yield
    with wp._cache_lock:
        wp._cache.update({"at": 0.0, "snapshot": None})


class TestSnapshot:
    def test_all_sources_aggregate(self):
        snap = wp.world_pulse_snapshot(force=True)
        assert snap["engine"].startswith("world-pulse")
        assert set(snap["sources"]) == {"markets", "news", "disasters", "cyber", "conflict", "macro"}
        for name, src in snap["sources"].items():
            assert src["ok"] is True, f"{name} should be up: {src}"
            assert src["count"] > 0
        assert isinstance(snap["pulse_score"], int) and 5 <= snap["pulse_score"] <= 95
        assert snap["pulse_label"] in ("STABLE", "ELEVATED", "HEIGHTENED", "CRITICAL")

    def test_disaster_severity_parsed(self):
        snap = wp.world_pulse_snapshot(force=True)
        disasters = snap["items"]["disasters"]
        assert any(it["severity"] == "critical" for it in disasters)

    def test_cyber_severity_parsed(self):
        snap = wp.world_pulse_snapshot(force=True)
        assert any(it["severity"] == "high" for it in snap["items"]["cyber"])

    def test_macro_parsed_from_world_bank(self):
        snap = wp.world_pulse_snapshot(force=True)
        macro = snap["items"]["macro"]
        assert any("2.1%" in it["title"] for it in macro)

    def test_cache_reuses_snapshot(self):
        first = wp.world_pulse_snapshot(force=True)
        second = wp.world_pulse_snapshot()  # cached
        assert first["generated_at"] == second["generated_at"]

    def test_source_failure_is_isolated(self, monkeypatch):
        calls = []

        def _flaky(url: str) -> str:
            calls.append(url)
            if "gdacs.org" in url:
                raise RuntimeError("gdacs down")
            return _fake_http_get(url)

        monkeypatch.setattr(wp, "_http_get", _flaky)
        with wp._cache_lock:
            wp._cache.update({"at": 0.0, "snapshot": None})
        snap = wp.world_pulse_snapshot(force=True)
        assert snap["sources"]["disasters"]["ok"] is False
        assert "down" in snap["sources"]["disasters"]["error"]
        for name in ("markets", "news", "cyber", "conflict", "macro"):
            assert snap["sources"][name]["ok"] is True, f"{name} must survive the gdacs failure"


class TestDetails:
    def test_details_returns_items(self):
        det = wp.world_pulse_details("markets")
        assert det["ok"] is True
        assert det["items"]
        assert det["health"]["ok"] is True

    def test_unknown_source_is_honest(self):
        det = wp.world_pulse_details("nope")
        assert det["ok"] is False
        assert "unknown source" in det["error"]

    def test_status_shape(self):
        st = wp.world_pulse_status()
        assert st["configured"] is True
        assert st["sources_up"] == 6
        assert st["sources_total"] == 6
        assert 5 <= st["pulse_score"] <= 95


class TestWiring:
    def test_worldmonitor_subagent_has_pulse_tools(self):
        registry = build_general_registry()
        sub = registry.get_subagent("worldmonitor")
        names = {t.name for t in sub.tools}
        assert {"world_pulse", "world_pulse_details"} <= names

    def test_world_pulse_endpoint(self):
        from dourmouse.tests.test_webui import _echo_registry
        from dourmouse.webui import run_server

        srv = run_server(_echo_registry(), port=0, client=None, config=None)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            port = srv.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/world/pulse")
            resp = conn.getresponse()
            body = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 200
            assert "pulse_score" in body
            assert "sources" in body
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
