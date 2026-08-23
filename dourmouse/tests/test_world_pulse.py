"""Hermetic tests for the v5.27 self-hosted World Pulse monitor.

No network: ``world_pulse._http_get`` and the live_feeds market helpers are
monkeypatched with canned real-shaped payloads (Google-News/GDACS/CISA/
ReliefWeb RSS + World Bank JSON). The overriding contract: every source is
failure-isolated, nothing is fabricated, and the snapshot NEVER raises.
"""

from __future__ import annotations

import base64
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
    if "nhc.noaa.gov" in url:
        return _STORMS_JSON
    if "ll.thespacedevs.com" in url:
        return _LAUNCHES_JSON
    if "web-api.tp.entsoe.eu" in url:
        return _ENTSOE_XML
    if "air-quality-api.open-meteo.com" in url:
        return _OPEN_METEO_AQ_JSON
    if "api.energy-charts.info" in url:
        return _ENERGY_CHARTS_JSON
    raise AssertionError(f"unexpected url in test: {url}")


def _fake_http_post(url: str, data: bytes) -> str:
    if "overpass-api.de" in url:
        return _OVERPASS_JSON
    raise AssertionError(f"unexpected POST url in test: {url}")


_STORMS_JSON = json.dumps({
    "activeStorms": [
        {
            "id": "AL012026", "name": "Test", "classification": "HU", "intensity": "100",
            "pressure": "955", "latitudeNumeric": 25.4, "longitudeNumeric": -70.2,
            "movementDir": "NW", "movementSpeed": "12 mph", "lastUpdate": "2026-08-23T09:00:00Z",
        },
    ]
})
_STORMS_EMPTY_JSON = json.dumps({"activeStorms": []})

_LAUNCHES_JSON = json.dumps({
    "results": [
        {
            "name": "Test Rocket Flight 1", "net": "2026-08-24T10:00:00Z",
            "status": {"name": "Go for Launch"},
            "pad": {"name": "LC-39A", "location": {"latitude": "28.6080", "longitude": "-80.6040"}},
        },
        {
            "name": "Test Rocket Flight 2", "net": "2026-08-25T04:00:00Z",
            "status": {"name": "To Be Determined"},
            "pad": {"name": "Vostochny", "location": {"latitude": "51.8843", "longitude": "128.3327"}},
        },
    ]
})
_LAUNCHES_EMPTY_JSON = json.dumps({"results": []})

_OVERPASS_JSON = json.dumps({
    "elements": [
        {"type": "way", "id": 111, "center": {"lat": 40.0, "lon": 20.0}, "tags": {"man_made": "pipeline"}},
        {"type": "node", "id": 222, "lat": 43.3, "lon": 5.4, "tags": {"harbour": "yes", "name": "Test Port"}},
    ]
})
_OVERPASS_EMPTY_JSON = json.dumps({"elements": []})

_FIRMS_CSV = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
    "confidence,version,bright_ti5,frp,daynight\n"
    "34.05,-118.25,330.1,0.4,0.4,2026-08-23,0900,N,VIIRS,n,2.0,290.0,62.5,D\n"
    "37.77,-122.42,310.2,0.4,0.4,2026-08-23,0910,N,VIIRS,l,2.0,285.0,8.0,D\n"
)
_FIRMS_BAD_KEY = "Invalid MAP_KEY"

# v8.23: real batched-response shape from Open-Meteo's air-quality API,
# verified live on 23 Aug 2026 (see the module docstring on
# _fetch_air_quality) — one object per requested city, in request order.
# Deliberately shorter than the real 15-city _AQ_CITIES list; zip() over
# the shorter one is exactly what real parsing does when a request
# returns fewer usable rows than cities asked for.
_OPEN_METEO_AQ_JSON = json.dumps([
    {"latitude": 28.6, "longitude": 77.2,
     "current": {"time": "2026-08-23T16:00", "pm2_5": 65.4, "pm10": 90.0, "us_aqi": 152}},
    {"latitude": 39.9, "longitude": 116.4,
     "current": {"time": "2026-08-23T16:00", "pm2_5": 8.1, "pm10": 12.0, "us_aqi": 34}},
])
_OPEN_METEO_AQ_EMPTY_JSON = json.dumps([])

_ENTSOE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">'
    "<TimeSeries><Period>"
    "<Point><position>1</position><quantity>42500</quantity></Point>"
    "<Point><position>2</position><quantity>43100</quantity></Point>"
    "</Period></TimeSeries></GL_MarketDocument>"
)
# v8.24: real shape from energy-charts.info's /total_power, verified live
# on 23 Aug 2026 — the trailing None mirrors the real, observed behavior
# (the most recent bucket is often not yet finalized), which is exactly
# what forces the "walk back to the last real value" logic being tested.
_ENERGY_CHARTS_JSON = json.dumps({
    "unix_seconds": [1, 2, 3],
    "production_types": [
        {"name": "Solar", "data": [10.0, 20.0, 30.0]},
        {"name": "Load (incl. self-consumption)", "data": [42000.0, 43000.0, None]},
    ],
})
_ENERGY_CHARTS_NO_LOAD_JSON = json.dumps({
    "unix_seconds": [1], "production_types": [{"name": "Solar", "data": [10.0]}],
})
_ENERGY_CHARTS_ALL_NULL_JSON = json.dumps({
    "unix_seconds": [1, 2],
    "production_types": [{"name": "Load (incl. self-consumption)", "data": [None, None]}],
})

_ENTSOE_EMPTY_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">'
    "</GL_MarketDocument>"
)


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
    monkeypatch.setattr(wp, "_http_post", _fake_http_post)
    import dourmouse.live_feeds as lf

    monkeypatch.setattr(lf, "market_movers", _fake_movers)
    monkeypatch.setattr(lf, "stock_quote", _fake_quote)
    monkeypatch.setenv("DOURMOUSE_WORLD_PULSE_TTL", "300")  # cache across calls in one test
    # v8.20/v8.23: the three still-key-gated channels (ships/wildfires/
    # power_grid) are deliberately left UNCONFIGURED by default — no test
    # should silently open a real socket or make a real keyed HTTP call
    # just because the aggregate snapshot function touches all 15 sources.
    # air_quality moved off OPENAQ_API_KEY entirely in v8.23 (now keyless,
    # via Open-Meteo) — the env var is still deleted defensively in case a
    # real .env leaks into the test environment, but nothing in the code
    # reads it anymore. Tests that need the configured path set their own
    # fake key + mock the transport explicitly (see TestNewChannels below).
    for key in ("AISSTREAM_API_KEY", "FIRMS_MAP_KEY", "OPENAQ_API_KEY", "ENTSOE_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
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
        # v8.20: seven more channels joined. All fifteen are always
        # REGISTERED (world_pulse_status must report their real state even
        # unconfigured). v8.23: air_quality moved off a key entirely (now
        # Open-Meteo, keyless). v8.24: power_grid did too (energy-charts.info
        # as an always-on keyless base, ENTSO-E merged in only if a token
        # is ALSO configured) — only ships/wildfires are still NOT
        # CONFIGURED by default in this fixture, matching the honest
        # default state of a real install with no keys set. See
        # TestNewChannels for the fully-configured path exercised end to
        # end.
        assert set(snap["sources"]) == {
            "markets", "news", "disasters", "cyber", "conflict", "macro",
            "quakes", "flights",
            "ships", "wildfires", "storms", "air_quality", "power_grid",
            "launches", "infrastructure",
        }
        always_on = {
            "markets", "news", "disasters", "cyber", "conflict", "macro",
            "quakes", "flights", "storms", "launches", "infrastructure",
            "air_quality", "power_grid",
        }
        gated = {"ships", "wildfires"}
        for name in always_on:
            src = snap["sources"][name]
            assert src["ok"] is True, f"{name} should be up: {src}"
            assert src["count"] > 0
        for name in gated:
            src = snap["sources"][name]
            assert src["ok"] is False, f"{name} should be NOT CONFIGURED by default: {src}"
            assert "NOT CONFIGURED" in src["error"]
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
        # v8.20/v8.23/v8.24: 15 sources registered; 13 always-on (up by
        # default in this fixture — air_quality since v8.23, power_grid
        # since v8.24) + 2 key-gated ones honestly reporting down (NOT
        # CONFIGURED) with no keys set — see TestSnapshot.test_all_sources_
        # aggregate for the exact up/down split by name.
        assert st["sources_total"] == 15
        assert st["sources_up"] == 13
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


class TestWSFrameCodec:
    """Pure encode/decode tests for the hand-rolled RFC 6455 client — no
    socket, no I/O. `ships` is the only non-HTTP channel in this file, so
    its wire-format code carries its own dedicated test layer per the
    module's own stated Rule 2.8 design (the codec is pure, the transport
    is injectable)."""

    def test_round_trip_masked(self):
        payload = b'{"hello":"world"}'
        frame = wp._ws_encode_frame(payload, opcode=0x1, mask=True)
        frames, leftover = wp._ws_decode_frames(frame)
        assert leftover == b""
        assert len(frames) == 1
        opcode, decoded = frames[0]
        assert opcode == 0x1
        assert decoded == payload

    def test_round_trip_unmasked_server_style(self):
        payload = b"server frames are never masked"
        frame = wp._ws_encode_frame(payload, opcode=0x1, mask=False)
        frames, leftover = wp._ws_decode_frames(frame)
        assert leftover == b""
        assert frames == [(0x1, payload)]

    def test_medium_payload_uses_16bit_length(self):
        payload = b"x" * 5000  # > 125, forces the 126-length-prefix branch
        frame = wp._ws_encode_frame(payload, opcode=0x1, mask=False)
        assert frame[1] & 0x7F == 126
        frames, leftover = wp._ws_decode_frames(frame)
        assert leftover == b""
        assert frames[0][1] == payload

    def test_split_frame_across_two_reads(self):
        """A frame arriving split across two TCP reads must not be
        decoded until the second chunk arrives — this is what the real
        `_fetch_ships` loop relies on (buf accumulates, leftover is fed
        back in)."""
        frame = wp._ws_encode_frame(b"complete message", opcode=0x1, mask=False)
        part_a, part_b = frame[:5], frame[5:]
        frames, leftover = wp._ws_decode_frames(part_a)
        assert frames == []
        assert leftover == part_a
        frames, leftover = wp._ws_decode_frames(leftover + part_b)
        assert leftover == b""
        assert frames == [(0x1, b"complete message")]

    def test_two_frames_in_one_buffer(self):
        f1 = wp._ws_encode_frame(b"one", opcode=0x1, mask=False)
        f2 = wp._ws_encode_frame(b"two", opcode=0x1, mask=False)
        frames, leftover = wp._ws_decode_frames(f1 + f2)
        assert leftover == b""
        assert frames == [(0x1, b"one"), (0x1, b"two")]


class _FakeAISSocket:
    """In-memory stand-in for the TLS socket `_fetch_ships` opens.

    Answers a REAL RFC 6455 handshake for whatever Sec-WebSocket-Key the
    client actually sends (computing a real Sec-WebSocket-Accept, not a
    canned one), so `_ws_handshake`'s own verification passes because it
    is genuinely correct — not because the test skipped it. Then serves
    pre-baked frames on subsequent recv() calls.
    """

    def __init__(self, frames):
        self._frames = list(frames)
        self._handshake_response = None
        self.closed = False
        self.sent = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)
        if self._handshake_response is None and data.startswith(b"GET "):
            text = data.decode("iso-8859-1")
            key = ""
            for line in text.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            import hashlib as _hashlib

            accept = base64.b64encode(
                _hashlib.sha1((key + wp._WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            self._handshake_response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")

    def recv(self, _n: int) -> bytes:
        if self._handshake_response:
            resp, self._handshake_response = self._handshake_response, None
            return resp
        if self._frames:
            return self._frames.pop(0)
        raise TimeoutError("no more fake frames")

    def close(self) -> None:
        self.closed = True


def _fake_position_frame(name: str, lat: float, lon: float) -> bytes:
    """Documented shape from aisstream.io's own docs example — text frame
    (0x1), capitalized Latitude/Longitude. Still accepted (fallback), but
    NOT what the live API actually sends — see _fake_position_frame_live."""
    payload = json.dumps({
        "MessageType": "PositionReport",
        "MetaData": {"ShipName": name, "Latitude": lat, "Longitude": lon},
        "Message": {"PositionReport": {}},
    }).encode("utf-8")
    return wp._ws_encode_frame(payload, opcode=0x1, mask=False)


def _fake_position_frame_live(name: str, lat: float, lon: float) -> bytes:
    """The REAL shape, verified live against aisstream.io on 23 Aug 2026
    with a real key: BINARY frames (0x2, not 0x1), and MetaData carries
    lowercase latitude/longitude — the docs example (capitalized, text
    frame) does not match the live wire format. Two real bugs were caught
    this way and fixed in _fetch_ships; this fixture locks in the actual
    behavior so a future refactor can't silently regress back to the
    documented-but-wrong shape."""
    payload = json.dumps({
        "MetaData": {"MMSI": 123456789, "ShipName": name, "latitude": lat, "longitude": lon,
                      "time_utc": "2026-08-23 16:29:44.000000000 +0000 UTC"},
        "MessageType": "PositionReport",
        "Message": {"PositionReport": {"MessageID": 1, "Latitude": lat, "Longitude": lon}},
    }).encode("utf-8")
    return wp._ws_encode_frame(payload, opcode=0x2, mask=False)


class TestNewChannels:
    """v8.20 — the seven channels added to the world monitor. Each gets
    its own not-configured / severity / honest-empty-vs-honest-error
    coverage; the aggregate default-state split is in
    TestSnapshot.test_all_sources_aggregate."""

    # --- ships (AIS over hand-rolled WebSocket) --------------------------

    def test_ships_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            wp._fetch_ships()

    def test_ships_parses_real_position_reports(self, monkeypatch):
        monkeypatch.setenv("AISSTREAM_API_KEY", "fake-test-key")
        frames = [
            _fake_position_frame("QUEEN MARY 2", 50.8812, -1.3983),
            _fake_position_frame("EVER GIVEN", 30.02, 32.58),
        ]
        fake_sock = _FakeAISSocket(frames)
        out = wp._fetch_ships(open_socket=lambda: fake_sock, listen_seconds=1.0, max_messages=5)
        assert {i["title"] for i in out} == {"QUEEN MARY 2", "EVER GIVEN"}
        for it in out:
            assert -90 <= it["lat"] <= 90 and -180 <= it["lon"] <= 180
        assert fake_sock.closed is True

    def test_ships_parses_real_live_wire_format(self, monkeypatch):
        """Locks in the ACTUAL live behavior, verified against a real
        aisstream.io connection with a real key on 23 Aug 2026: binary
        (0x2) frames, lowercase latitude/longitude in MetaData. Two real
        bugs were caught this way — this test exists specifically so a
        future refactor can't silently regress back to the
        documented-but-wrong (text frame, capitalized) shape that
        test_ships_parses_real_position_reports above also still accepts
        as a fallback."""
        monkeypatch.setenv("AISSTREAM_API_KEY", "fake-test-key")
        frames = [
            _fake_position_frame_live("WELLINGDORF", 54.31544, 10.135),
            _fake_position_frame_live("BIG SLOOP", 52.30902, 5.24501),
        ]
        fake_sock = _FakeAISSocket(frames)
        out = wp._fetch_ships(open_socket=lambda: fake_sock, listen_seconds=1.0, max_messages=5)
        assert {i["title"] for i in out} == {"WELLINGDORF", "BIG SLOOP"}
        wellingdorf = next(i for i in out if i["title"] == "WELLINGDORF")
        assert wellingdorf["lat"] == 54.31544 and wellingdorf["lon"] == 10.135

    def test_ships_no_traffic_is_honest_error(self, monkeypatch):
        """No PositionReport frames arriving is a real, reportable failure
        — never silently returns an empty list pretending nothing was
        wrong (this channel, unlike storms/infrastructure, isn't one
        where zero is a normal state)."""
        monkeypatch.setenv("AISSTREAM_API_KEY", "fake-test-key")
        fake_sock = _FakeAISSocket(frames=[])
        with pytest.raises(RuntimeError, match="no position reports"):
            wp._fetch_ships(open_socket=lambda: fake_sock, listen_seconds=0.3, max_messages=5)

    # --- wildfires (FIRMS) ------------------------------------------------

    def test_wildfires_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            wp._fetch_wildfires()

    def test_wildfires_parses_and_ranks_by_frp(self, monkeypatch):
        monkeypatch.setenv("FIRMS_MAP_KEY", "fake-map-key")
        monkeypatch.setattr(
            wp, "_http_get",
            lambda url: _FIRMS_CSV if "firms.modaps" in url else _fake_http_get(url),
        )
        out = wp._fetch_wildfires()
        assert len(out) == 2
        # Strongest FRP (62.5 MW, critical) must lead.
        assert out[0]["severity"] == "critical"
        assert out[1]["severity"] == "watch"

    def test_wildfires_bad_key_is_honest(self, monkeypatch):
        monkeypatch.setenv("FIRMS_MAP_KEY", "wrong-key")
        monkeypatch.setattr(
            wp, "_http_get",
            lambda url: _FIRMS_BAD_KEY if "firms.modaps" in url else _fake_http_get(url),
        )
        with pytest.raises(RuntimeError, match="rejected"):
            wp._fetch_wildfires()

    # --- storms (NOAA/NHC) -------------------------------------------------

    def test_storms_parses_classification_severity(self):
        out = wp._fetch_storms()
        assert len(out) == 1
        assert out[0]["severity"] == "critical"  # HU -> critical
        assert "Test" in out[0]["title"]

    def test_storms_zero_active_is_not_an_error(self, monkeypatch):
        """The whole point of this channel: no active storms is normal,
        not a failure — unlike quakes/flights/launches, which raise on
        empty."""
        monkeypatch.setattr(
            wp, "_http_get",
            lambda url: _STORMS_EMPTY_JSON if "nhc.noaa" in url else _fake_http_get(url),
        )
        assert wp._fetch_storms() == []

    # --- air_quality (Open-Meteo, keyless since v8.23) ----------------------

    def test_air_quality_needs_no_key_at_all(self, monkeypatch):
        """The whole point of the v8.23 swap: no key, no env var, no
        NOT-CONFIGURED path — it either has real data or a real error."""
        monkeypatch.delenv("OPENAQ_API_KEY", raising=False)
        out = wp._fetch_air_quality()
        assert len(out) == 2

    def test_air_quality_parses_batched_response_and_epa_bands(self):
        out = wp._fetch_air_quality()
        assert len(out) == 2
        # 65.4 ug/m3 -> critical (>=55.5); 8.1 ug/m3 -> info (well within "good")
        assert out[0]["severity"] == "critical"
        assert "152" in out[0]["title"]  # US AQI surfaced when present
        assert out[1]["severity"] == "info"
        assert out[0]["lat"] == 28.6 and out[0]["lon"] == 77.2

    def test_air_quality_empty_response_is_an_honest_error(self, monkeypatch):
        monkeypatch.setattr(
            wp, "_http_get",
            lambda url: _OPEN_METEO_AQ_EMPTY_JSON if "open-meteo" in url else _fake_http_get(url),
        )
        with pytest.raises(RuntimeError, match="no rows"):
            wp._fetch_air_quality()

    # --- power_grid (energy-charts.info keyless base + optional ENTSO-E) ----

    def test_power_grid_works_with_no_token_at_all(self, monkeypatch):
        """v8.24: the whole point of adding energy-charts.info — power_grid
        is never NOT CONFIGURED any more."""
        monkeypatch.delenv("ENTSOE_API_TOKEN", raising=False)
        out = wp._fetch_power_grid()
        assert {it["country"] for it in out} == {"DE"}

    def test_energy_charts_walks_back_to_last_real_value(self):
        """The real, observed behavior: the most recent bucket is often
        still null. Must report the last REAL value (43000, not the
        trailing None and not the first value 42000)."""
        out = wp._fetch_energy_charts_load()
        assert len(out) == 1
        assert "43,000 MW" in out[0]["title"]

    def test_energy_charts_missing_load_field_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            wp, "_http_get",
            lambda url: _ENERGY_CHARTS_NO_LOAD_JSON if "energy-charts" in url else _fake_http_get(url),
        )
        assert wp._fetch_energy_charts_load() == []

    def test_energy_charts_all_null_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            wp, "_http_get",
            lambda url: _ENERGY_CHARTS_ALL_NULL_JSON if "energy-charts" in url else _fake_http_get(url),
        )
        assert wp._fetch_energy_charts_load() == []

    def test_power_grid_merges_entsoe_zones_when_token_present(self, monkeypatch):
        monkeypatch.setenv("ENTSOE_API_TOKEN", "fake-token")
        out = wp._fetch_power_grid()
        # DE from the always-on keyless base, plus FR/GB/NL from ENTSO-E
        # once a token is configured — all four present, not one replacing
        # the other.
        assert {it["country"] for it in out} == {"DE", "FR", "GB", "NL"}

    def test_power_grid_isolates_one_entsoe_zone_failure(self, monkeypatch):
        """One bad ENTSO-E zone must not sink the others, and must not
        take down the always-on energy-charts.info base either — same
        failure-isolation discipline as every other multi-call fetcher in
        this file (e.g. _fetch_markets' per-symbol try/except)."""
        monkeypatch.setenv("ENTSOE_API_TOKEN", "fake-token")

        def _flaky(url):
            if "GB" in url:
                raise RuntimeError("entsoe rejected GB zone")
            return _fake_http_get(url)

        monkeypatch.setattr(wp, "_http_get", _flaky)
        out = wp._fetch_power_grid()
        assert {it["country"] for it in out} == {"DE", "FR", "NL"}

    def test_power_grid_entsoe_failure_still_leaves_keyless_base(self, monkeypatch):
        """If ENTSO-E fails entirely (bad token, network down), power_grid
        must still report the energy-charts.info base rather than the
        whole channel going down."""
        monkeypatch.setenv("ENTSOE_API_TOKEN", "fake-token")

        def _entsoe_down(url):
            if "entsoe" in url:
                raise RuntimeError("connection refused")
            return _fake_http_get(url)

        monkeypatch.setattr(wp, "_http_get", _entsoe_down)
        out = wp._fetch_power_grid()
        assert {it["country"] for it in out} == {"DE"}

    # --- launches (Launch Library 2) ----------------------------------------

    def test_launches_parses_real_pad_coordinates(self):
        out = wp._fetch_launches()
        assert len(out) == 2
        assert any("LC-39A" in it["title"] for it in out)
        for it in out:
            assert -90 <= it["lat"] <= 90 and -180 <= it["lon"] <= 180

    def test_launches_empty_is_an_honest_error(self, monkeypatch):
        monkeypatch.setattr(
            wp, "_http_get",
            lambda url: _LAUNCHES_EMPTY_JSON if "thespacedevs" in url else _fake_http_get(url),
        )
        with pytest.raises(RuntimeError, match="no upcoming launches"):
            wp._fetch_launches()

    # --- infrastructure (OSM Overpass) --------------------------------------

    def test_infrastructure_parses_ways_and_nodes(self):
        out = wp._fetch_infrastructure()
        assert len(out) == 2
        titles = " ".join(it["title"] for it in out)
        assert "pipeline" in titles
        assert "Test Port" in titles
        for it in out:
            assert it["severity"] == "infra"

    def test_infrastructure_zero_matches_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(
            wp, "_http_post",
            lambda url, data: _OVERPASS_EMPTY_JSON if "overpass" in url else _fake_http_post(url, data),
        )
        assert wp._fetch_infrastructure() == []

    # --- fully-configured integration: all fifteen sources genuinely up ----

    def test_all_fifteen_sources_when_fully_configured(self, monkeypatch):
        # air_quality needs no key at all since v8.23 (Open-Meteo) — only
        # the remaining three gated channels need a fake key/token here.
        monkeypatch.setenv("AISSTREAM_API_KEY", "fake-key")
        monkeypatch.setenv("FIRMS_MAP_KEY", "fake-key")
        monkeypatch.setenv("ENTSOE_API_TOKEN", "fake-token")

        def _all_http_get(url):
            if "firms.modaps" in url:
                return _FIRMS_CSV
            return _fake_http_get(url)

        monkeypatch.setattr(wp, "_http_get", _all_http_get)

        fake_sock = _FakeAISSocket([_fake_position_frame_live("TEST SHIP", 10.0, 20.0)])
        monkeypatch.setattr(wp, "_ws_open", lambda host: fake_sock)

        snap = wp.world_pulse_snapshot(force=True)
        assert len(snap["sources"]) == 15
        for name, src in snap["sources"].items():
            assert src["ok"] is True, f"{name} should be up when fully configured: {src}"

    # --- geo endpoint includes the new locatable channels -------------------

    def test_geo_includes_all_new_locatable_channels(self):
        geo = wp.world_pulse_geo()
        # storms/launches/infrastructure are always-on and carry real
        # coordinates, so they must be plotted, not listed as unmappable —
        # same bar v8.9 set for quakes/flights.
        for chan in ("storms", "launches", "infrastructure"):
            assert chan in geo["layers"], f"{chan} should be mappable: {geo.get('unmappable')}"


class TestNewChannelEndpoints:
    """v8.20: real HTTP round-trips through webui.py for the new
    /api/world/* routes, same server-spin-up pattern as
    TestWiring.test_world_pulse_endpoint above. Network is still hermetic
    (the autouse fixture's fakes cover everything these routes touch)."""

    def _server(self):
        from dourmouse.tests.test_webui import _echo_registry
        from dourmouse.webui import run_server

        srv = run_server(_echo_registry(), port=0, client=None, config=None)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        return srv, thread

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        conn.close()
        return resp.status, body

    def _post(self, port, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps(payload).encode()
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        out = json.loads(resp.read().decode())
        conn.close()
        return resp.status, out

    def test_history_and_brief_and_correlations_endpoints(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        srv, thread = self._server()
        try:
            port = srv.server_address[1]

            status, body = self._get(port, "/api/world/history/range?hours=24")
            assert status == 200
            assert "range" in body

            status, body = self._get(port, "/api/world/history?minutes_ago=0")
            assert status == 200
            assert "found" in body

            status, body = self._get(port, "/api/world/correlations")
            assert status == 200
            assert "correlations" in body

            status, body = self._get(port, "/api/world/brief")
            assert status == 200
            assert body.get("mode") == "template"
            assert "text" in body
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_regions_crud_over_http(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        srv, thread = self._server()
        try:
            port = srv.server_address[1]

            status, body = self._get(port, "/api/world/regions")
            assert status == 200 and body["regions"] == []

            status, body = self._post(port, "/api/world/regions", {
                "name": "Test Box", "min_lat": 10, "max_lat": 20, "min_lon": 10, "max_lon": 20,
            })
            assert status == 200 and body["ok"] is True
            region_id = body["region"]["id"]

            status, body = self._get(port, "/api/world/regions")
            assert status == 200 and len(body["regions"]) == 1

            status, body = self._get(port, "/api/world/regions/hits")
            assert status == 200 and region_id in body["hits"]

            status, body = self._post(port, "/api/world/regions/delete", {"id": region_id})
            assert status == 200 and body["deleted"] is True

            status, body = self._get(port, "/api/world/regions")
            assert status == 200 and body["regions"] == []
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_region_create_validation_error_is_400(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        srv, thread = self._server()
        try:
            port = srv.server_address[1]
            status, body = self._post(port, "/api/world/regions", {
                "name": "", "min_lat": 10, "max_lat": 20, "min_lon": 10, "max_lon": 20,
            })
            assert status == 400 and body["ok"] is False
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
