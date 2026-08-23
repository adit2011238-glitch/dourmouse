"""World Pulse — the SELF-HOSTED world monitor (v8.20).

Dourmouse's own global-intelligence feed. No SDK: every source is a real
public endpoint read over stdlib urllib (or, for one channel, a hand-rolled
stdlib-only WebSocket client) — nothing is ever fabricated (Rule 2.2).
Fifteen channels, eight keyless and seven either keyless or key-gated:

- ``markets``  — Yahoo Finance: top day gainers/losers + key index/commodity
                quotes (^GSPC, ^IXIC, CL=F, GC=F, BTC-USD).
- ``news``     — Google News RSS (world + Europe + Asia-Pacific editions).
- ``disasters``— GDACS RSS: live earthquake / flood / cyclone / wildfire
                alerts with severity (green/orange/red).
- ``cyber``    — CISA cybersecurity advisories RSS.
- ``conflict`` — ReliefWeb RSS: humanitarian / conflict / displacement
                updates.
- ``macro``    — World Bank API: GDP growth + inflation for US, CN, EU, IN.
- ``quakes``   — USGS magnitude 2.5+, last 24h. Keyless.
- ``flights``  — OpenSky live aircraft over Europe/N-Africa. Keyless.
- ``ships``    — aisstream.io live AIS positions over the same bbox as
                ``flights``. Needs ``AISSTREAM_API_KEY``; honestly NOT
                CONFIGURED without one (Rule 2.2). The only non-HTTP
                channel — aisstream is WebSocket-only, so this module
                carries a small hand-rolled RFC 6455 client (stdlib
                ``socket``/``ssl`` only, no new dependency) rather than
                breaking the file's stdlib-only rule for one channel.
- ``wildfires``— NASA FIRMS VIIRS thermal detections, last 24h. Needs
                ``FIRMS_MAP_KEY`` (free at firms.modaps.eosdis.nasa.gov).
- ``storms``   — NOAA/NHC active tropical cyclones. Keyless. An empty
                result is a normal, non-error state (most days have zero
                active named storms) — unlike quakes/flights, this channel
                never raises on zero.
- ``air_quality``— real PM2.5 for a fixed spread of major world cities,
                via Open-Meteo's Air Quality API. Keyless (v8.23 — the
                original OpenAQ v3 integration needed a key and both keys
                supplied for it were rejected by OpenAQ's own server;
                Open-Meteo needs nothing at all).
- ``power_grid``— real actual total load. Keyless base (v8.24):
                energy-charts.info (Fraunhofer ISE), verified live,
                Germany only — that source's Load-field coverage for
                other countries was not confirmed live and isn't relied
                on. If ``ENTSOE_API_TOKEN`` is ALSO configured (issued by
                email, not self-serve), ENTSO-E's FR/GB/NL zones are
                merged in too. Never NOT CONFIGURED any more.
- ``launches`` — Launch Library 2 (thespacedevs.com), upcoming launches.
                Keyless.
- ``infrastructure``— OSM Overpass: pipelines + harbours over the same
                bbox as ``flights``/``ships``, as a static base layer.
                Keyless. Zero matches is a legitimate result here (OSM tag
                coverage varies by area) — this channel never raises on
                zero, unlike quakes/flights/launches.

Failure isolation: each source is fetched independently; a dead source is
reported OFFLINE with its real error while every other channel keeps
serving. A key-gated source with no key configured reports NOT CONFIGURED
the same honest way — cheap, no network call, never a fabricated result.
The snapshot is cached (default 120 s) so the UI never pays the full fetch
cost per poll. ``pulse_score`` is an INTERNAL deterministic composite of
the real signals (documented below), never a fabricated number.

Concurrency: a small thread pool (one worker per source) with a per-source
timeout, so a stalled feed cannot stall the whole monitor.
"""

from __future__ import annotations

import base64
import concurrent.futures
import csv
import hashlib
import io
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from dourmouse import live_feeds

_SOURCE_TIMEOUT = 8.0
_MAX_ITEMS_PER_SOURCE = 8
_SOURCE_COUNT = 15
# Shared bbox for the three geo channels that need one: Europe + N. Africa
# + the Med, matching the box _fetch_flights() already used before this
# file grew ships/infrastructure — one box, one place it's defined, so the
# three channels are always looking at the same region.
_GEO_BBOX = {"south": 35.0, "west": -11.0, "north": 60.0, "east": 31.0}

#: Cached snapshot: {at, snapshot}. A monitor refresh is bounded by the TTL.
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "snapshot": None}


def _ttl() -> float:
    try:
        return float(os.environ.get("DOURMOUSE_WORLD_PULSE_TTL", "120"))
    except ValueError:
        return 120.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http_get(url: str) -> str:
    """Keyless GET via the existing live_feeds helper (honest errors)."""
    return live_feeds._http_get(url, timeout=_SOURCE_TIMEOUT)


def _http_post(url: str, data: bytes) -> str:
    """POST via stdlib urllib — only the Overpass channel needs a query
    body rather than a query string, everything else in this file is GET.
    Same honest-error contract as `_http_get`, and the same seam: tests
    monkeypatch this function directly rather than reaching into
    urllib.request, matching every other source in this file."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": live_feeds._UA})
    try:
        with urllib.request.urlopen(req, timeout=_SOURCE_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error posting to {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"timeout posting to {url}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Source fetchers — each returns a list of normalized items or raises.
# --------------------------------------------------------------------------- #


def _item(
    title: str,
    summary: str = "",
    link: str = "",
    at: str = "",
    severity: str = "",
    lat: float | None = None,
    lon: float | None = None,
    country: str = "",
) -> dict[str, Any]:
    """One feed item.

    v8.9: ``lat``/``lon``/``country`` are OPTIONAL and omitted entirely when
    a source has no location. That is deliberate — a consumer (the map) must
    be able to tell "this happened at this point" from "this has no place",
    and a placeholder 0/0 would silently plot every unlocated advisory off
    the coast of Africa. Absent means absent.
    """
    out: dict[str, Any] = {
        "title": (title or "").strip()[:200],
        "summary": (summary or "").strip()[:300],
        "link": (link or "").strip(),
        "at": at,
        "severity": severity,
    }
    if lat is not None and lon is not None:
        out["lat"] = lat
        out["lon"] = lon
    if country:
        out["country"] = country.strip()[:80]
    return out


def _parse_point(node: ET.Element) -> tuple[float | None, float | None]:
    """Pull coordinates out of an RSS item's geo tags.

    GDACS publishes BOTH ``georss:point`` ("lat lon" in one string) and the
    older ``geo:lat`` / ``geo:long`` pair. Read either. Namespaces are
    stripped before matching because the feeds are inconsistent about
    prefixes. Malformed values return None rather than raising — one bad
    entry must not cost the whole channel.
    """
    lat = lon = None
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        text = (child.text or "").strip()
        if not text:
            continue
        try:
            if tag == "point":
                parts = text.replace(",", " ").split()
                if len(parts) >= 2:
                    lat, lon = float(parts[0]), float(parts[1])
            elif tag == "lat":
                lat = float(text)
            elif tag in ("long", "lon"):
                lon = float(text)
        except (TypeError, ValueError):
            continue
    # Reject anything outside the real coordinate space instead of passing
    # a nonsense point downstream.
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None, None
    return lat, lon


def _rss_items(
    raw: str, max_items: int, pick: Callable[[ET.Element], dict[str, Any]]
) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"unparseable RSS: {exc}") from exc
    out: list[dict[str, str]] = []
    for node in root.iter("item"):
        out.append(pick(node))
        if len(out) >= max_items:
            break
    if not out:
        raise RuntimeError("feed returned no items")
    return out


def _fetch_quakes() -> list[dict[str, Any]]:
    """USGS magnitude 2.5+ in the last 24h — keyless GeoJSON with real points.

    Added alongside GDACS rather than replacing it: GDACS covers floods,
    cyclones and wildfires too, while USGS is the authoritative seismic
    source and gives cleaner coordinates and magnitudes.
    """
    raw = _http_get(
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
    )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"unparseable USGS GeoJSON: {exc}") from exc
    feats = data.get("features") or []
    if not feats:
        raise RuntimeError("USGS returned no features")
    # Strongest first — a magnitude 6 matters more than twenty magnitude 2s.
    feats.sort(key=lambda f: (f.get("properties") or {}).get("mag") or 0, reverse=True)
    out: list[dict[str, Any]] = []
    for f in feats[:_MAX_ITEMS_PER_SOURCE]:
        p = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        mag = p.get("mag")
        if len(coords) < 2 or mag is None:
            continue
        # USGS GeoJSON is [lon, lat, depth] — the reverse of the RSS feeds.
        lon, lat = float(coords[0]), float(coords[1])
        if mag >= 6:
            sev = "critical"
        elif mag >= 5:
            sev = "high"
        elif mag >= 4:
            sev = "watch"
        else:
            sev = "info"
        depth = coords[2] if len(coords) > 2 else None
        out.append(_item(
            f"M{mag} {p.get('place') or 'unknown location'}",
            summary=(f"depth {depth:.0f} km · " if isinstance(depth, (int, float)) else "")
                    + "USGS",
            link=p.get("url") or "",
            severity=sev, lat=lat, lon=lon,
        ))
    if not out:
        raise RuntimeError("USGS features carried no usable coordinates")
    return out


def _fetch_flights() -> list[dict[str, Any]]:
    """Live aircraft from OpenSky — keyless, real positions.

    OpenSky returns thousands of aircraft; the monitor keeps a small sample
    for the feed while the map endpoint reads the full set separately. The
    anonymous tier is rate-limited, so a 429 here is normal and is reported
    as such rather than retried in a loop.
    """
    raw = _http_get("https://opensky-network.org/api/states/all?lamin=35&lomin=-11&lamax=60&lomax=31")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"unparseable OpenSky response: {exc}") from exc
    states = data.get("states") or []
    if not states:
        raise RuntimeError("OpenSky returned no aircraft")
    out: list[dict[str, Any]] = []
    for s in states:
        # Index map per OpenSky's documented state vector.
        try:
            callsign = (s[1] or "").strip()
            origin = s[2] or ""
            lon, lat = s[5], s[6]
            alt = s[7]
        except (IndexError, TypeError):
            continue
        if lat is None or lon is None or not callsign:
            continue
        out.append(_item(
            f"{callsign} {origin}",
            summary=(f"altitude {alt:,.0f} m" if isinstance(alt, (int, float)) else "on ground"),
            severity="info", lat=float(lat), lon=float(lon), country=origin,
        ))
        if len(out) >= _MAX_ITEMS_PER_SOURCE:
            break
    if not out:
        raise RuntimeError("OpenSky states carried no usable positions")
    return out


# --------------------------------------------------------------------------- #
# v8.20 world-monitor expansion — seven new channels.
#
# Three are keyless and pure stdlib urllib, same shape as everything above
# (storms, launches, infrastructure). Three are honestly key-gated — NOT
# CONFIGURED without a real key, never a fabricated reading (wildfires, air
# quality, power grid). One (ships) needs both a key AND a transport this
# file has never used before: aisstream.io is WebSocket-only, so a small,
# self-contained RFC 6455 client lives directly below, built from stdlib
# ``socket``/``ssl`` only — no new dependency, matching this file's own
# stated stdlib-only rule rather than quietly breaking it for one channel.
# --------------------------------------------------------------------------- #


def _env(*names: str) -> str:
    """First non-empty value among the given env var names, else ''."""
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


# --- AIS (ships) — hand-rolled RFC 6455 WebSocket client, stdlib only ----- #
#
# Split into two layers deliberately: the frame codec (`_ws_encode_frame`,
# `_ws_decode_frames`) is pure — no socket, no I/O — so it is fully unit-
# testable the same way everything else in this file is. The transport
# (`_ws_open`, `_fetch_ships`) takes an injectable socket factory (Rule 2.8:
# "the client is injectable so tests swap a fake transport and never touch
# the network" — the same rule worldmonitor.py states for its own client),
# so a test can hand it a fake in-memory socket serving canned frames
# without ever opening a real connection.

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_encode_frame(payload: bytes, opcode: int = 0x1, mask: bool = True) -> bytes:
    """Encode one complete (FIN=1) WebSocket frame. Client frames MUST be
    masked per RFC 6455 §5.1 — the server rejects (or silently drops) an
    unmasked client frame, so `mask` defaults True for the real client path;
    tests exercise `mask=False` to check the raw payload framing directly."""
    out = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        out.append(mask_bit | length)
    elif length < 65536:
        out.append(mask_bit | 126)
        out += length.to_bytes(2, "big")
    else:
        out.append(mask_bit | 127)
        out += length.to_bytes(8, "big")
    if mask:
        key = secrets.token_bytes(4)
        out += key
        out += bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    else:
        out += payload
    return bytes(out)


def _ws_decode_frames(buf: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Decode as many COMPLETE frames as `buf` holds. Returns
    ([(opcode, payload), ...], leftover_undecoded_bytes) — the leftover is
    fed back in on the next read, since a frame can arrive split across TCP
    reads. Server->client frames are never masked (RFC 6455 §5.1), so no
    unmasking here. Malformed/truncated input simply yields fewer frames
    and returns the rest as leftover, never raises — the caller decides
    when a genuinely broken stream should become an honest error."""
    frames: list[tuple[int, bytes]] = []
    pos = 0
    n = len(buf)
    while True:
        if pos + 2 > n:
            break
        b0, b1 = buf[pos], buf[pos + 1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        hdr = 2
        if length == 126:
            if pos + 4 > n:
                break
            length = int.from_bytes(buf[pos + 2:pos + 4], "big")
            hdr = 4
        elif length == 127:
            if pos + 10 > n:
                break
            length = int.from_bytes(buf[pos + 2:pos + 10], "big")
            hdr = 10
        mask_key = b""
        if masked:
            if pos + hdr + 4 > n:
                break
            mask_key = buf[pos + hdr:pos + hdr + 4]
            hdr += 4
        end = pos + hdr + length
        if end > n:
            break
        payload = buf[pos + hdr:end]
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        frames.append((opcode, payload))
        pos = end
    return frames, buf[pos:]


def _ws_handshake(sock: Any, host: str, path: str) -> None:
    """RFC 6455 opening handshake over an already-connected TLS socket.
    Raises RuntimeError with the real reason on any failure — a wrong
    status, a missing/mismatched Sec-WebSocket-Accept, or a dead
    connection are all reported honestly, never treated as a soft
    success."""
    ws_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    sock.sendall(request)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed during WebSocket handshake")
        buf += chunk
        if len(buf) > 16384:
            raise RuntimeError("WebSocket handshake headers too large")
    header_text = buf.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
    lines = header_text.split("\r\n")
    status_line = lines[0] if lines else ""
    if " 101 " not in f" {status_line} ":
        raise RuntimeError(f"WebSocket handshake rejected: {status_line!r}")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    accept = headers.get("sec-websocket-accept", "")
    expected = base64.b64encode(hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
    if accept != expected:
        raise RuntimeError("WebSocket handshake failed Sec-WebSocket-Accept verification")


def _ws_open(host: str, port: int = 443, timeout: float = _SOURCE_TIMEOUT) -> Any:
    """Open a real TLS-wrapped TCP socket to (host, port). The only piece
    of `_fetch_ships` that isn't injectable — tests replace this whole
    function via monkeypatch rather than mocking sockets one level lower,
    since a real ssl.SSLSocket is not something a test should construct."""
    raw = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.create_default_context()
    return ctx.wrap_socket(raw, server_hostname=host)


def _fetch_ships(
    open_socket: Callable[[], Any] | None = None,
    *,
    listen_seconds: float = 6.0,
    max_messages: int = _MAX_ITEMS_PER_SOURCE,
) -> list[dict[str, Any]]:
    """Live AIS ship positions from aisstream.io over the shared geo bbox.

    Needs AISSTREAM_API_KEY — honestly NOT CONFIGURED without one, no
    connection attempted. `open_socket` is injectable (defaults to a real
    TLS socket via `_ws_open`) so a test can hand this a fake in-memory
    socket serving canned WebSocket frames without ever touching the
    network — the frame codec above is already tested standalone; this
    function is the thin, testable glue on top of it (Rule 2.8).
    """
    key = _env("AISSTREAM_API_KEY")
    if not key:
        raise RuntimeError(
            "NOT CONFIGURED: ships needs AISSTREAM_API_KEY (free at aisstream.io) "
            "— nothing was connected and no positions were fabricated."
        )
    host = "stream.aisstream.io"
    box = _GEO_BBOX
    subscribe = json.dumps({
        "APIKey": key,
        "BoundingBoxes": [[[box["south"], box["west"]], [box["north"], box["east"]]]],
        "FilterMessageTypes": ["PositionReport"],
    }).encode("utf-8")

    sock = (open_socket or (lambda: _ws_open(host)))()
    try:
        _ws_handshake(sock, host, "/v0/stream")
        sock.sendall(_ws_encode_frame(subscribe, opcode=0x1, mask=True))
        out: list[dict[str, Any]] = []
        buf = b""
        deadline = time.monotonic() + listen_seconds
        while len(out) < max_messages and time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except (TimeoutError, OSError):
                break
            if not chunk:
                break
            buf += chunk
            frames, buf = _ws_decode_frames(buf)
            for opcode, payload in frames:
                if opcode == 0x8:  # close frame — server ended the stream
                    deadline = 0
                    break
                if opcode not in (0x1, 0x2):  # text OR binary — verified live: aisstream.io
                    continue                  # actually sends its JSON as binary (0x2) frames
                try:
                    msg = json.loads(payload.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if msg.get("MessageType") != "PositionReport":
                    continue
                meta = msg.get("MetaData") or {}
                # Verified live: MetaData carries lowercase latitude/longitude,
                # not the Latitude/Longitude shown in aisstream.io's own docs
                # example — the docs and the real wire format disagree here.
                lat = meta.get("latitude", meta.get("Latitude"))
                lon = meta.get("longitude", meta.get("Longitude"))
                if lat is None or lon is None:
                    continue
                name = (meta.get("ShipName") or "").strip() or f"MMSI {meta.get('MMSI', '?')}"
                out.append(_item(
                    name, summary="live AIS position (aisstream.io)",
                    severity="info", lat=float(lat), lon=float(lon),
                ))
                if len(out) >= max_messages:
                    break
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001 - closing must never mask a real result/error
            pass
    if not out:
        raise RuntimeError(
            "aisstream.io returned no position reports in the listen window "
            "(no traffic in the bbox right now, or the connection was rejected)"
        )
    return out


def _fetch_wildfires() -> list[dict[str, Any]]:
    """VIIRS thermal fire detections, last 24h, via NASA FIRMS.

    Needs FIRMS_MAP_KEY (free, self-serve at firms.modaps.eosdis.nasa.gov/
    api/area/). NOT CONFIGURED honestly without one. Response is CSV, not
    JSON — parsed with the stdlib csv module. Ranked by FRP (fire radiative
    power, MW) — a real physical intensity measure, not a guess — so the
    strongest detections lead, same convention as quakes leading by
    magnitude.
    """
    key = _env("FIRMS_MAP_KEY")
    if not key:
        raise RuntimeError(
            "NOT CONFIGURED: wildfires needs FIRMS_MAP_KEY (free at "
            "firms.modaps.eosdis.nasa.gov/api/area) — nothing was fetched."
        )
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/VIIRS_NOAA20_NRT/world/1"
    raw = _http_get(url)
    # A bad/expired key returns a short plain-text error body, not CSV —
    # catch that honestly rather than parsing it as zero-row CSV.
    if len(raw) < 200 and ("Invalid" in raw or "key" in raw.lower()):
        raise RuntimeError(f"FIRMS rejected the request: {raw.strip()[:150]}")
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows:
        raise RuntimeError("FIRMS returned no fire detections")

    def _frp(row: dict[str, str]) -> float:
        try:
            return float(row.get("frp") or 0)
        except ValueError:
            return 0.0

    rows.sort(key=_frp, reverse=True)
    out: list[dict[str, Any]] = []
    for row in rows[:_MAX_ITEMS_PER_SOURCE]:
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (KeyError, ValueError):
            continue
        frp = _frp(row)
        if frp >= 50:
            sev = "critical"
        elif frp >= 15:
            sev = "high"
        elif frp >= 5:
            sev = "watch"
        else:
            sev = "info"
        conf = row.get("confidence", "?")
        out.append(_item(
            f"Fire detection — {frp:.0f} MW FRP",
            summary=f"confidence {conf} · {row.get('acq_date', '')} {row.get('acq_time', '')} UTC (VIIRS/FIRMS)",
            severity=sev, lat=lat, lon=lon,
        ))
    if not out:
        raise RuntimeError("FIRMS rows carried no usable coordinates")
    return out


def _fetch_storms() -> list[dict[str, Any]]:
    """Active tropical cyclones from NOAA/NHC — keyless.

    Zero active storms is a NORMAL state (most days, most of the year,
    have none) — unlike quakes/flights, this never raises on an empty
    result; an empty list here means "checked, genuinely none right now,"
    not "the feed is broken."
    """
    raw = _http_get("https://www.nhc.noaa.gov/CurrentStorms.json")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"unparseable NHC response: {exc}") from exc
    storms = data.get("activeStorms")
    if storms is None:
        raise RuntimeError("NHC response missing 'activeStorms'")
    out: list[dict[str, Any]] = []
    for s in storms[:_MAX_ITEMS_PER_SOURCE]:
        lat, lon = s.get("latitudeNumeric"), s.get("longitudeNumeric")
        if lat is None or lon is None:
            continue
        classification = str(s.get("classification", "")).upper()
        if classification in ("HU", "TY", "STY"):
            sev = "critical"
        elif classification in ("TS", "STS"):
            sev = "high"
        elif classification == "TD":
            sev = "watch"
        else:
            sev = "info"
        out.append(_item(
            f"{classification or 'STORM'} {s.get('name', '?')} — {s.get('intensity', '?')} kt",
            summary=f"moving {s.get('movementDir', '?')} at {s.get('movementSpeed', '?')} · "
                    f"updated {s.get('lastUpdate', 'unknown time')} (NHC)",
            severity=sev, lat=float(lat), lon=float(lon),
        ))
    return out  # empty is a legitimate, non-error result — see docstring


# v8.23: 15 real, fixed, well-known city coordinates as the fetch points
# for Open-Meteo's air-quality API. Open-Meteo has no "give me whatever
# stations reported recently" sweep the way OpenAQ does — it answers a
# real reading for whatever coordinates you ask it for, so a representative
# spread of major cities across every populated continent is the honest
# way to get global-ish coverage from a point API, not a station directory.
_AQ_CITIES = [
    ("Delhi", 28.6139, 77.2090), ("Beijing", 39.9042, 116.4074),
    ("Lagos", 6.5244, 3.3792), ("Cairo", 30.0444, 31.2357),
    ("Sao Paulo", -23.5505, -46.6333), ("Mexico City", 19.4326, -99.1332),
    ("Jakarta", -6.2088, 106.8456), ("Los Angeles", 34.0522, -118.2437),
    ("London", 51.5074, -0.1278), ("Moscow", 55.7558, 37.6173),
    ("Lahore", 31.5497, 74.3436), ("Bangkok", 13.7563, 100.5018),
    ("Johannesburg", -26.2041, 28.0473), ("Sydney", -33.8688, 151.2093),
    ("New York", 40.7128, -74.0060),
]


def _fetch_air_quality() -> list[dict[str, Any]]:
    """Real, current PM2.5 for a fixed spread of major world cities, via
    Open-Meteo's Air Quality API.

    v8.23: replaces the original OpenAQ v3 integration entirely — v3 needs
    a key, and both keys supplied for it during live testing on 23 Aug
    2026 were rejected by OpenAQ's own server (confirmed with raw curl,
    independent of this codebase). Open-Meteo's air-quality endpoint is
    genuinely keyless — verified live, no signup, no header, real batched
    multi-location response — so this channel can never again go NOT
    CONFIGURED or fail on a bad key; it either has real data or reports a
    real network/parse error. Severity buckets follow the real EPA PM2.5
    breakpoints (µg/m3), not a guessed scale.
    """
    lat_str = ",".join(str(lat) for _n, lat, _lon in _AQ_CITIES)
    lon_str = ",".join(str(lon) for _n, _lat, lon in _AQ_CITIES)
    url = (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={lat_str}&longitude={lon_str}&current=pm2_5,pm10,us_aqi"
    )
    raw = _http_get(url)
    try:
        rows = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"unparseable Open-Meteo response: {exc}") from exc
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Open-Meteo air-quality returned no rows")
    out: list[dict[str, Any]] = []
    for (name, req_lat, req_lon), row in zip(_AQ_CITIES, rows):
        if not isinstance(row, dict):
            continue
        current = row.get("current") or {}
        pm25 = current.get("pm2_5")
        if pm25 is None:
            continue
        pm25 = float(pm25)
        if pm25 >= 55.5:
            sev = "critical"
        elif pm25 >= 35.5:
            sev = "high"
        elif pm25 >= 12.1:
            sev = "watch"
        else:
            sev = "info"
        aqi = current.get("us_aqi")
        lat = row.get("latitude", req_lat)
        lon = row.get("longitude", req_lon)
        out.append(_item(
            f"{name}: PM2.5 {pm25:.1f} µg/m³" + (f" (US AQI {aqi:.0f})" if aqi is not None else ""),
            summary=f"PM10 {current.get('pm10', '?')} µg/m³ · {current.get('time', '')} UTC (Open-Meteo)",
            severity=sev, lat=float(lat), lon=float(lon), country=name,
        ))
        if len(out) >= _MAX_ITEMS_PER_SOURCE:
            break
    if not out:
        raise RuntimeError("Open-Meteo rows carried no usable PM2.5 readings")
    return out


# Deliberately small and honest: EIC bidding-zone codes for three zones
# this was verified against ENTSO-E's own published area list at the time
# this was written. A wrong EIC code fails loudly (an empty/error XML
# response, reported honestly below) rather than silently — so if this
# list is ever extended, verify the new code against ENTSO-E's area list
# rather than guessing one from memory.
_ENTSOE_ZONES = {"FR": "10YFR-RTE------C", "GB": "10YGB----------A", "NL": "10YNL----------L"}
# Representative zone centroid, purely for placing a zone-wide reading on
# the map — NOT the location of any sensor. Said explicitly in the item's
# own summary text so this is never mistaken for a point measurement.
_ENTSOE_CENTROIDS = {"FR": (46.6, 2.5), "GB": (54.0, -2.0), "NL": (52.1, 5.3)}


# v8.24: energy-charts.info (Fraunhofer ISE) — genuinely keyless, verified
# live. Its "Load (incl. self-consumption)" field is the same real
# actual-total-load signal ENTSO-E's A65 document gives, just for a
# different, narrower set of countries: only "de" was confirmed live to
# actually carry that field (the endpoint 404s for several other real
# country codes tried live — at/it/es all 404'd, and the service rate-
# limits aggressively on rapid successive requests) — so this stays
# scoped to the one zone actually verified rather than guessing others
# would work. Extend this list only after live-checking a new code the
# same way (curl the endpoint, confirm "Load (incl. self-consumption)"
# is actually present, not just a 200).
_ENERGY_CHARTS_ZONES = {"DE": (51.1657, 10.4515)}  # label -> representative centroid


def _fetch_energy_charts_load() -> list[dict[str, Any]]:
    """Keyless real total-load reading(s) from energy-charts.info. Never
    NOT CONFIGURED — this is what makes power_grid always-on regardless
    of whether an ENTSO-E token is ever configured."""
    out: list[dict[str, Any]] = []
    for label, (lat, lon) in _ENERGY_CHARTS_ZONES.items():
        url = f"https://api.energy-charts.info/total_power?country={label.lower()}"
        try:
            raw = _http_get(url)
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 - one zone's failure must not sink the others
            continue
        series = next(
            (pt for pt in (data.get("production_types") or [])
             if pt.get("name") == "Load (incl. self-consumption)"),
            None,
        )
        if not series:
            continue
        values = series.get("data") or []
        # The most recent bucket is often still null (not yet finalized) —
        # walk back to the last REAL value rather than reporting a
        # fabricated-looking "current" reading that's actually empty.
        value = next((v for v in reversed(values) if v is not None), None)
        if value is None:
            continue
        out.append(_item(
            f"{label} load {float(value):,.0f} MW",
            summary=f"actual total load, zone-wide (energy-charts.info) · point is the "
                    f"{label} zone centroid, not a sensor location",
            severity="quote", lat=lat, lon=lon, country=label,
        ))
    return out


def _fetch_entsoe_load(token: str) -> tuple[list[dict[str, Any]], list[str]]:
    """ENTSO-E path (needs a real token) — extracted from the original
    _fetch_power_grid so it can run as an OPTIONAL supplement to the
    always-on energy-charts.info base rather than being the only source.
    Unlike every other keyed channel in this file, this token is NOT
    self-serve — ENTSO-E issues it by email after a manual request. The
    request/parse logic follows ENTSO-E's documented GL_MarketDocument
    schema exactly (TimeSeries -> Period -> Point, each Point holding a
    position/quantity pair)."""
    now = datetime.now(timezone.utc)
    period_end = now.strftime("%Y%m%d%H%M")
    period_start = (now - timedelta(hours=2)).strftime("%Y%m%d%H%M")
    out: list[dict[str, Any]] = []
    failures: list[str] = []
    for label, eic in _ENTSOE_ZONES.items():
        url = (
            "https://web-api.tp.entsoe.eu/api"
            f"?securityToken={token}&documentType=A65&processType=A16"
            f"&outBiddingZone_Domain={eic}"
            f"&periodStart={period_start}&periodEnd={period_end}"
        )
        try:
            raw = _http_get(url)
        except Exception as exc:  # noqa: BLE001 - one zone's failure must not sink the others
            failures.append(f"{label}: {str(exc)[:80]}")
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            failures.append(f"{label}: unparseable response ({exc})")
            continue
        points = [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "Point"]
        if not points:
            failures.append(f"{label}: no data points in response")
            continue
        last = points[-1]
        quantity = None
        for child in last:
            if child.tag.rsplit("}", 1)[-1] == "quantity":
                quantity = child.text
        if quantity is None:
            failures.append(f"{label}: point carried no quantity")
            continue
        lat, lon = _ENTSOE_CENTROIDS.get(label, (None, None))
        if lat is None:
            continue
        out.append(_item(
            f"{label} load {float(quantity):,.0f} MW",
            summary=f"actual total load, zone-wide (ENTSO-E) · point is the {label} "
                    "zone centroid, not a sensor location",
            severity="quote", lat=lat, lon=lon, country=label,
        ))
    return out, failures


def _fetch_power_grid() -> list[dict[str, Any]]:
    """Real actual-total-load readings for European grid zones.

    v8.24: always-on, never NOT CONFIGURED — energy-charts.info (keyless,
    verified live) supplies at least Germany's real load unconditionally.
    If ENTSOE_API_TOKEN is also configured, its zones (FR/GB/NL) are
    fetched too and merged in, deduplicating by country so a zone covered
    by both sources doesn't appear twice (energy-charts.info's reading
    wins on overlap, since it's the one actually verified live end to
    end — see _fetch_entsoe_load's docstring on why its own live behavior
    remains comparatively less proven).
    """
    out = _fetch_energy_charts_load()
    covered = {it.get("country") for it in out}
    token = _env("ENTSOE_API_TOKEN")
    if token:
        entsoe_out, _failures = _fetch_entsoe_load(token)
        out.extend(it for it in entsoe_out if it.get("country") not in covered)
    if not out:
        raise RuntimeError(
            "no power_grid source reachable — energy-charts.info failed and "
            + ("ENTSO-E also failed" if token else "no ENTSOE_API_TOKEN is configured for a fallback")
        )
    return out


def _fetch_launches() -> list[dict[str, Any]]:
    """Upcoming orbital launches from Launch Library 2 — keyless.

    Includes launches from the last 24h per the API's own semantics.
    Global launch cadence essentially never reaches zero across every
    provider at once, so — like quakes/flights, unlike storms — an empty
    result here is treated as a fetch problem, not a real "nothing
    scheduled" state.
    """
    raw = _http_get("https://ll.thespacedevs.com/2.0.0/launch/upcoming/?limit=8&mode=list")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"unparseable Launch Library response: {exc}") from exc
    results = data.get("results") or []
    if not results:
        raise RuntimeError("Launch Library returned no upcoming launches")
    out: list[dict[str, Any]] = []
    for r in results[:_MAX_ITEMS_PER_SOURCE]:
        # The pad's own name ("LC-39A") and its coordinates live at two
        # different nesting levels in LL2's shape: pad.name is the specific
        # pad, pad.location.{latitude,longitude,name} is the broader site
        # — read each from the level that actually carries it, not one
        # borrowed from the other.
        pad_obj = r.get("pad") or {}
        location = pad_obj.get("location") or {}
        lat, lon = location.get("latitude"), location.get("longitude")
        if lat is None or lon is None:
            continue
        status = (r.get("status") or {}).get("name", "?")
        out.append(_item(
            f"{r.get('name', 'Launch')} — {pad_obj.get('name', '?')}",
            summary=f"status: {status} · net {r.get('net', 'TBD')} (Launch Library 2)",
            severity="launch", lat=float(lat), lon=float(lon),
        ))
    if not out:
        raise RuntimeError("Launch Library rows carried no usable pad coordinates")
    return out


def _fetch_infrastructure() -> list[dict[str, Any]]:
    """Pipelines + harbours over the shared geo bbox, from OSM Overpass —
    keyless, POST query.

    A static base layer, not a hazard feed — items carry severity="infra"
    (no urgency implied). Zero matches is a legitimate result (OSM tag
    density for these specific tags varies a lot by area); unlike
    quakes/flights/launches, this never raises on an empty result.
    """
    box = _GEO_BBOX
    bbox = f"{box['south']},{box['west']},{box['north']},{box['east']}"
    query = (
        "[out:json][timeout:8];("
        f'way["man_made"="pipeline"]({bbox});'
        f'node["harbour"="yes"]({bbox});'
        f'node["seamark:type"="harbour"]({bbox});'
        ");out center 30;"
    )
    data_bytes = urllib.parse.urlencode({"data": query}).encode("ascii")
    raw = _http_post("https://overpass-api.de/api/interpreter", data_bytes)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"unparseable Overpass response: {exc}") from exc
    elements = data.get("elements") or []
    out: list[dict[str, Any]] = []
    for el in elements[:_MAX_ITEMS_PER_SOURCE]:
        if "lat" in el and "lon" in el:
            lat, lon = el["lat"], el["lon"]
        else:
            center = el.get("center") or {}
            lat, lon = center.get("lat"), center.get("lon")
        if lat is None or lon is None:
            continue
        tags = el.get("tags") or {}
        kind = tags.get("man_made") or ("harbour" if "harbour" in tags or tags.get("seamark:type") == "harbour" else "infrastructure")
        out.append(_item(
            f"{kind} · {tags.get('name') or el.get('type', '?')} #{el.get('id', '?')}",
            summary="OpenStreetMap / Overpass",
            severity="infra", lat=float(lat), lon=float(lon),
        ))
    return out  # empty is a legitimate, non-error result — see docstring


def _fetch_crypto_fx() -> tuple[list[dict[str, Any]], list[str]]:
    """Keyless crypto + FX, from providers that are NOT Yahoo.

    v8.9: Yahoo rate-limits by source IP and had blocked this host on every
    edge, which took the whole markets channel down with it. These two
    providers were verified reachable from the compute host at the same
    moment Yahoo was returning 429, so they keep the channel alive on its
    own legs rather than as a Yahoo retry.
    """
    items: list[dict[str, Any]] = []
    failures: list[str] = []

    # Binance: 24h ticker, gives price AND change percent in one call.
    for sym, label in (("BTCUSDT", "BTC/USD"), ("ETHUSDT", "ETH/USD")):
        try:
            d = json.loads(_http_get(
                f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}"
            ))
            price = float(d["lastPrice"])
            pct = float(d["priceChangePercent"])
            items.append(_item(
                f"{label} {price:,.2f}",
                summary=f"{pct:+.2f}% over 24h (Binance)",
                severity="up" if pct > 0 else ("down" if pct < 0 else "flat"),
            ))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}: {str(exc)[:70]}")

    # Frankfurter: ECB reference rates, keyless and unthrottled.
    try:
        d = json.loads(_http_get("https://api.frankfurter.app/latest?from=EUR&to=USD,GBP,JPY"))
        rates = d.get("rates") or {}
        for cur, val in list(rates.items())[:3]:
            items.append(_item(
                f"EUR/{cur} {val}",
                summary=f"ECB reference rate, {d.get('date', 'latest')}",
                severity="quote",
            ))
    except Exception as exc:  # noqa: BLE001
        failures.append(f"fx: {str(exc)[:70]}")

    return items, failures


def _fetch_markets() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    failures: list[str] = []
    for direction in ("gainers", "losers"):
        try:
            rows = live_feeds.market_movers(direction, count=5)
        except Exception as exc:  # noqa: BLE001 - one sub-call may fail
            rows = []
            failures.append(f"{direction}: {str(exc)[:100]}")
        for r in rows:
            pct = r.get("change_pct") or 0
            items.append(
                _item(
                    f"{direction.upper()} {r.get('symbol', '?')} {r.get('name', '')[:40]}",
                    summary=(
                        f"{r.get('price', '?')} {r.get('currency', '')} "
                        f"({pct:+.2f}%) today"
                    ),
                    severity="up" if pct > 0 else ("down" if pct < 0 else "flat"),
                )
            )
    for sym in ("^GSPC", "^IXIC", "CL=F", "GC=F", "BTC-USD"):
        try:
            q = live_feeds.stock_quote(sym)
            items.append(
                _item(
                    f"{q['symbol']} {q['price']} {q['currency']}",
                    summary=f"as of {q.get('as_of') or 'now'}",
                    severity="quote",
                )
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{sym}: {str(exc)[:80]}")

    # v8.9: crypto + FX from non-Yahoo providers. Added AFTER the Yahoo
    # attempts so equity data still leads when Yahoo is reachable, but the
    # channel no longer dies with it.
    extra, extra_fail = _fetch_crypto_fx()
    items.extend(extra)
    failures.extend(extra_fail)

    if not items:
        # Honest: nothing real came back (provider blocked/rate-limited) —
        # the source is OFFLINE, not "ok with warn placeholders".
        raise RuntimeError("no market provider reachable: " + "; ".join(failures[:4]))
    if failures:
        # Partial is reported as partial. A channel that quietly drops
        # equities and shows only crypto would read as a complete market
        # picture, which it is not.
        items.append(_item(
            "EQUITIES UNAVAILABLE",
            summary=(
                "Yahoo is rate-limiting this host (HTTP 429); crypto and FX "
                "below are live. No keyless equity source is configured."
            ),
            severity="warn",
        ))
    return items[: _MAX_ITEMS_PER_SOURCE]


def _fetch_news() -> list[dict[str, str]]:
    feeds = {
        "WORLD": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
        "EUROPE": "https://news.google.com/rss?hl=en-GB&gl=GB&ceid=GB:en",
        "APAC": "https://news.google.com/rss?hl=en-AU&gl=AU&ceid=AU:en",
    }
    out: list[dict[str, str]] = []
    for region, url in feeds.items():
        try:
            raw = _http_get(url)
        except Exception as exc:  # noqa: BLE001
            out.append(_item(f"{region} NEWS UNAVAILABLE", summary=str(exc)[:150], severity="warn"))
            continue
        for it in _rss_items(raw, 3, _pick_news):
            it["severity"] = "news"
            out.append(it)
    if not out:
        raise RuntimeError("all news editions failed")
    return out[:_MAX_ITEMS_PER_SOURCE]


def _pick_news(node: ET.Element) -> dict[str, str]:
    title = source = published = link = ""
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1]
        text = (child.text or "").strip()
        if tag == "title":
            title = text
        elif tag == "source":
            source = text
        elif tag == "pubDate":
            published = text
        elif tag == "link":
            link = text
    return _item(title, summary=f"via {source}" if source else "", link=link, at=published)


def _fetch_disasters() -> list[dict[str, Any]]:
    raw = _http_get("https://www.gdacs.org/xml/rss.xml")
    out: list[dict[str, str]] = []

    def _pick(node: ET.Element) -> dict[str, Any]:
        title = summary = link = published = country = ""
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if tag == "title":
                title = text
            elif tag == "description":
                summary = text
            elif tag == "link":
                link = text
            elif tag == "pubDate":
                published = text
            elif tag == "country":
                country = text
        lowered = (title + " " + summary).lower()
        if "alertlevel: red" in lowered or "alertlevel red" in lowered:
            sev = "critical"
        elif "orange" in lowered:
            sev = "high"
        elif "green" in lowered:
            sev = "info"
        else:
            sev = "watch"
        # v8.9: GDACS ships georss:point on essentially every alert. Reading
        # it is what makes the map possible; it was being discarded.
        lat, lon = _parse_point(node)
        return _item(
            title, summary=summary, link=link, at=published, severity=sev,
            lat=lat, lon=lon, country=country,
        )

    for it in _rss_items(raw, _MAX_ITEMS_PER_SOURCE, _pick):
        out.append(it)
    return out


def _fetch_cyber() -> list[dict[str, str]]:
    raw = _http_get("https://www.cisa.gov/cybersecurity-advisories/all.xml")
    out: list[dict[str, str]] = []

    def _pick(node: ET.Element) -> dict[str, str]:
        title = summary = link = published = ""
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if tag == "title":
                title = text
            elif tag == "description":
                summary = text
            elif tag == "link":
                link = text
            elif tag == "pubDate":
                published = text
        sev = "high" if re.search(r"\b(critical|high)\b", summary.lower()) else "advisory"
        return _item(title, summary=summary[:200], link=link, at=published, severity=sev)

    for it in _rss_items(raw, _MAX_ITEMS_PER_SOURCE, _pick):
        out.append(it)
    return out


def _fetch_conflict() -> list[dict[str, str]]:
    raw = _http_get("https://reliefweb.int/updates/rss.xml")
    out: list[dict[str, str]] = []

    def _pick(node: ET.Element) -> dict[str, str]:
        title = summary = link = published = ""
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if tag == "title":
                title = text
            elif tag == "description":
                summary = re.sub(r"<[^>]+>", " ", text)[:280]
            elif tag == "link":
                link = text
            elif tag == "pubDate":
                published = text
        return _item(title, summary=summary, link=link, at=published, severity="humanitarian")

    for it in _rss_items(raw, _MAX_ITEMS_PER_SOURCE, _pick):
        out.append(it)
    return out


def _fetch_macro() -> list[dict[str, str]]:
    # World Bank keyless API: latest GDP growth + inflation per economy.
    countries = {"US": "United States", "CN": "China", "EU": "European Union", "IN": "India"}
    indicators = {
        "NY.GDP.MKTP.KD.ZG": "GDP GROWTH %",
        "FP.CPI.TOTL.ZG": "INFLATION %",
    }
    out: list[dict[str, str]] = []
    for cc, name in countries.items():
        for ind, label in indicators.items():
            url = (
                f"https://api.worldbank.org/v2/country/{cc}/indicator/{ind}"
                "?format=json&per_page=1&date=2020:2035"
            )
            try:
                raw = _http_get(url)
            except Exception as exc:  # noqa: BLE001
                out.append(_item(f"{name} {label} UNAVAILABLE", summary=str(exc)[:120], severity="warn"))
                continue
            try:
                parsed = _wb_parse(raw)
            except Exception as exc:  # noqa: BLE001
                out.append(_item(f"{name} {label} UNAVAILABLE", summary=str(exc)[:120], severity="warn"))
                continue
            if parsed is None:
                continue
            value, year = parsed
            out.append(
                _item(
                    f"{name} — {label} {value}%",
                    summary=f"latest year: {year} (World Bank)",
                    severity="macro",
                )
            )
    if not out:
        raise RuntimeError("World Bank API returned nothing")
    return out[:_MAX_ITEMS_PER_SOURCE]


def _wb_parse(raw: str) -> tuple[str, str] | None:
    import json

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("non-JSON from World Bank") from exc
    rows = data[1] if isinstance(data, list) and len(data) > 1 else []
    for row in rows:
        value = row.get("value")
        if value is None:
            continue
        try:
            return f"{float(value):.1f}", str(row.get("date", "?"))
        except (TypeError, ValueError):
            continue
    return None


# --------------------------------------------------------------------------- #
# Registry + aggregation
# --------------------------------------------------------------------------- #

_SOURCES: dict[str, tuple[str, Callable[[], list[dict[str, Any]]]]] = {
    "markets": ("Markets — crypto, FX, and equity quotes when reachable", _fetch_markets),
    "news": ("Google News RSS — world / Europe / APAC", _fetch_news),
    "disasters": ("GDACS — earthquakes, floods, cyclones, wildfires", _fetch_disasters),
    "cyber": ("CISA — cybersecurity advisories", _fetch_cyber),
    "conflict": ("ReliefWeb — humanitarian + conflict updates", _fetch_conflict),
    "macro": ("World Bank — GDP growth + inflation", _fetch_macro),
    # v8.9 geo channels — both keyless, both carry real coordinates and so
    # are the ones the map can actually plot.
    "quakes": ("USGS — magnitude 2.5+ earthquakes, last 24h", _fetch_quakes),
    "flights": ("OpenSky — live aircraft positions", _fetch_flights),
    # v8.20 world-monitor expansion. ships/wildfires/air_quality/power_grid
    # are honestly NOT CONFIGURED without their respective key/token —
    # they still register here (rather than being conditionally added)
    # so /api/world/sources always reports their real state, keyed or not.
    "ships": ("aisstream.io — live AIS ship positions (needs AISSTREAM_API_KEY)", _fetch_ships),
    "wildfires": ("NASA FIRMS — VIIRS thermal detections, last 24h (needs FIRMS_MAP_KEY)", _fetch_wildfires),
    "storms": ("NOAA/NHC — active tropical cyclones", _fetch_storms),
    "air_quality": ("Open-Meteo — real PM2.5 for major world cities, keyless", _fetch_air_quality),
    "power_grid": ("energy-charts.info (keyless) + ENTSO-E if configured — actual total load", _fetch_power_grid),
    "launches": ("Launch Library 2 — upcoming orbital launches", _fetch_launches),
    "infrastructure": ("OSM Overpass — pipelines + harbours (static base layer)", _fetch_infrastructure),
}


def _compute_pulse(items: dict[str, list[dict[str, str]]]) -> tuple[int, str]:
    """Deterministic composite of the REAL signals (documented, internal).

    The score reacts to SIGNAL, not feed volume (feeds always carry a
    baseline of routine items). Base 50.

    - disasters: only high/critical alert levels move it (-2 / -3 each).
    - cyber: only high-severity advisories count (-1 each, capped 6).
    - conflict: an information channel — no score impact.
    - markets: losers dominating pulls -5; gainers dominating adds +5.
    - news / macro: information channels — no score impact.

    Clamped 5..95.
    """
    score = 50
    for it in items.get("disasters", []):
        sev = it.get("severity")
        if sev == "critical":
            score -= 3
        elif sev == "high":
            score -= 2
    score -= min(sum(1 for it in items.get("cyber", []) if it.get("severity") == "high"), 6)
    up = sum(1 for it in items.get("markets", []) if it.get("severity") == "up")
    down = sum(1 for it in items.get("markets", []) if it.get("severity") == "down")
    if down > up and down >= 3:
        score -= 5
    elif up > down and up >= 3:
        score += 5
    score = max(5, min(95, score))
    if score >= 70:
        label = "STABLE"
    elif score >= 55:
        label = "ELEVATED"
    elif score >= 40:
        label = "HEIGHTENED"
    else:
        label = "CRITICAL"
    return score, label


def world_pulse_snapshot(force: bool = False) -> dict[str, Any]:
    """The aggregated monitor snapshot (cached). Never raises."""
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache["snapshot"] and (now - _cache["at"]) < _ttl():
            return _cache["snapshot"]

    results: dict[str, Any] = {}
    items: dict[str, list[dict[str, str]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_SOURCE_COUNT) as pool:
        futures = {name: pool.submit(fn) for name, (label, fn) in _SOURCES.items()}
        for name, fut in futures.items():
            started = time.perf_counter()
            try:
                got = fut.result(timeout=_SOURCE_TIMEOUT + 2)
                latency_ms = int((time.perf_counter() - started) * 1000)
                results[name] = {"ok": True, "latency_ms": latency_ms, "count": len(got)}
                items[name] = got
            except Exception as exc:  # noqa: BLE001 - per-source isolation
                results[name] = {"ok": False, "error": str(exc)[:200], "count": 0}
                items[name] = []

    score, label = _compute_pulse(items)
    snapshot = {
        "generated_at": _now_iso(),
        "engine": "world-pulse v5.27 (self-hosted, keyless)",
        "pulse_score": score,
        "pulse_label": label,
        "sources": results,
        "items": items,
        "note": "pulse_score is an internal composite of the real source signals — never a fabricated rating.",
    }
    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["snapshot"] = snapshot
    return snapshot


def world_pulse_geo() -> dict[str, Any]:
    """Every locatable item, grouped by channel, for the map.

    v8.9. Deliberately reports which channels are mappable and which are
    not, instead of silently returning a short list: a map that shows three
    layers when the operator believes it shows eight is worse than one that
    says "cyber has no coordinates". ``unmappable`` names those channels and
    why, so the UI can state it rather than imply full coverage.
    """
    snap = world_pulse_snapshot()
    items = snap.get("items") or {}
    layers: dict[str, list[dict[str, Any]]] = {}
    unmappable: dict[str, str] = {}
    for chan, lst in items.items():
        located = [i for i in (lst or []) if isinstance(i, dict) and "lat" in i and "lon" in i]
        if located:
            layers[chan] = located
        else:
            src = (snap.get("sources") or {}).get(chan) or {}
            unmappable[chan] = (
                src.get("error") or "source carries no coordinates"
            )[:160]
    geo = {
        "generated_at": snap.get("generated_at"),
        "pulse_score": snap.get("pulse_score"),
        "pulse_label": snap.get("pulse_label"),
        "layers": layers,
        "counts": {k: len(v) for k, v in layers.items()},
        "unmappable": unmappable,
        "note": (
            "Only channels with real coordinates are plotted. Channels listed "
            "under 'unmappable' are omitted rather than given placeholder "
            "positions."
        ),
    }
    # v8.20: feed the time-scrubber history store on every call. Cheap —
    # record_snapshot() itself no-ops within its own min-interval, so
    # calling this from every /api/worldmap hit is safe, not a write storm.
    # Never lets a history-write problem break the map response itself.
    try:
        from dourmouse.world_pulse_history import record_snapshot

        record_snapshot(geo)
    except Exception:  # noqa: BLE001 - history is a nice-to-have, never load-bearing here
        pass
    return geo


def world_pulse_details(source: str) -> dict[str, Any]:
    """One source's items + health. Never raises."""
    name = (source or "").strip().lower()
    if name not in _SOURCES:
        return {
            "ok": False,
            "error": f"unknown source {source!r} — known: {', '.join(sorted(_SOURCES))}",
        }
    snap = world_pulse_snapshot()
    return {
        "ok": True,
        "source": name,
        "label": _SOURCES[name][0],
        "health": snap["sources"].get(name, {}),
        "items": snap["items"].get(name, []),
        "generated_at": snap["generated_at"],
    }


def world_pulse_status() -> dict[str, Any]:
    """Small health view for connections/setup (fast, cached). Never raises."""
    snap = world_pulse_snapshot()
    return {
        "configured": True,
        "online": bool(snap["sources"]),
        "sources_up": sum(1 for s in snap["sources"].values() if s.get("ok")),
        "sources_total": len(snap["sources"]),
        "pulse_score": snap["pulse_score"],
        "pulse_label": snap["pulse_label"],
        "generated_at": snap["generated_at"],
    }
