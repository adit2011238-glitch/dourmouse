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


# v8.9 geo channels. USGS is [lon, lat, depth] — the reverse of the RSS
# feeds' lat/lon order — so the canned payload deliberately uses a point
# where swapping the two would be obvious (lon -150, lat 61: Alaska, not
# the Indian Ocean).
_USGS_JSON = json.dumps(
    {
        "features": [
            {
                "properties": {"mag": 6.4, "place": "Southern Alaska", "url": "https://usgs.example/1"},
                "geometry": {"coordinates": [-150.5, 61.2, 35.0]},
            },
            {
                "properties": {"mag": 3.1, "place": "Nevada", "url": "https://usgs.example/2"},
                "geometry": {"coordinates": [-117.0, 38.0, 8.0]},
            },
        ]
    }
)

# OpenSky state vectors: [icao, callsign, origin, ..., lon, lat, alt, ...]
_OPENSKY_JSON = json.dumps(
    {
        "states": [
            ["abc123", "TEST123 ", "United Kingdom", 0, 0, -0.45, 51.47, 11000.0],
            ["def456", "TEST456 ", "France", 0, 0, 2.35, 48.85, 9500.0],
        ]
    }
)

_BINANCE_JSON = json.dumps({"lastPrice": "63000.00", "priceChangePercent": "1.25"})
_FX_JSON = json.dumps({"date": "2026-08-16", "rates": {"USD": 1.15, "GBP": 0.85, "JPY": 183.0}})


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
    if "earthquake.usgs.gov" in url:
        return _USGS_JSON
    if "opensky-network.org" in url:
        return _OPENSKY_JSON
    if "binance.com" in url:
        return _BINANCE_JSON
    if "frankfurter.app" in url:
        return _FX_JSON
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
        # v8.9: quakes (USGS) and flights (OpenSky) joined the roster. Both
        # carry real coordinates, which is what makes the map possible —
        # asserted explicitly rather than by count so a channel silently
        # disappearing still fails this test.
        assert set(snap["sources"]) == {
            "markets", "news", "disasters", "cyber", "conflict", "macro",
            "quakes", "flights",
        }
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
        assert st["sources_up"] == 8
        assert st["sources_total"] == 8
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


class TestGeoParsing:
    """v8.9: coordinates are what make the map possible.

    GDACS was already shipping georss:point on every alert and the parser
    was discarding it, so the map had nothing to plot. These pin the parse
    and — just as importantly — pin that an item WITHOUT a location gets no
    lat/lon at all rather than a placeholder.
    """

    def _node(self, xml: str):
        import xml.etree.ElementTree as ET

        return ET.fromstring(xml)

    def test_parses_georss_point(self):
        node = self._node(
            '<item xmlns:georss="http://www.georss.org/georss">'
            "<georss:point>-7.9322 120.5855</georss:point></item>"
        )
        assert wp._parse_point(node) == (-7.9322, 120.5855)

    def test_parses_geo_lat_long_pair(self):
        node = self._node(
            '<item xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#">'
            "<geo:lat>51.5</geo:lat><geo:long>-0.12</geo:long></item>"
        )
        assert wp._parse_point(node) == (51.5, -0.12)

    def test_rejects_out_of_range(self):
        node = self._node(
            '<item xmlns:georss="http://www.georss.org/georss">'
            "<georss:point>999 999</georss:point></item>"
        )
        assert wp._parse_point(node) == (None, None)

    def test_malformed_point_does_not_raise(self):
        node = self._node(
            '<item xmlns:georss="http://www.georss.org/georss">'
            "<georss:point>not a coordinate</georss:point></item>"
        )
        assert wp._parse_point(node) == (None, None)

    def test_item_omits_location_when_absent(self):
        """No coordinates must mean NO keys — not 0,0.

        A placeholder would plot every unlocated advisory in the Gulf of
        Guinea, which reads as real data.
        """
        it = wp._item("no location here")
        assert "lat" not in it and "lon" not in it

    def test_item_keeps_location_when_present(self):
        it = wp._item("somewhere", lat=1.5, lon=-2.5)
        assert it["lat"] == 1.5 and it["lon"] == -2.5


class TestGeoEndpoint:
    def test_geo_reports_unmappable_channels(self):
        """The map payload must NAME what it cannot plot.

        Returning only the plottable layers would let the UI imply full
        coverage while silently dropping five of eight channels.
        """
        geo = wp.world_pulse_geo()
        assert "layers" in geo and "unmappable" in geo
        # Every channel is accounted for in exactly one of the two buckets.
        snap = wp.world_pulse_snapshot()
        assert set(geo["layers"]) | set(geo["unmappable"]) == set(snap["items"])
        assert not (set(geo["layers"]) & set(geo["unmappable"]))

    def test_every_plotted_item_has_real_coordinates(self):
        geo = wp.world_pulse_geo()
        for chan, items in (geo.get("layers") or {}).items():
            for it in items:
                assert -90 <= it["lat"] <= 90, chan
                assert -180 <= it["lon"] <= 180, chan
