"""World Pulse — the SELF-HOSTED world monitor (v5.27).

Dourmouse's own global-intelligence feed. No SDK, no API key: every source
is a real public, keyless endpoint read over stdlib urllib, so nothing is
ever fabricated (Rule 2.2). Six channels:

- ``markets``  — Yahoo Finance: top day gainers/losers + key index/commodity
                quotes (^GSPC, ^IXIC, CL=F, GC=F, BTC-USD).
- ``news``     — Google News RSS (world + Europe + Asia-Pacific editions).
- ``disasters``— GDACS RSS: live earthquake / flood / cyclone / wildfire
                alerts with severity (green/orange/red).
- ``cyber``    — CISA cybersecurity advisories RSS.
- ``conflict`` — ReliefWeb RSS: humanitarian / conflict / displacement
                updates.
- ``macro``    — World Bank API: GDP growth + inflation for US, CN, EU, IN.

Failure isolation: each source is fetched independently; a dead source is
reported OFFLINE with its real error while every other channel keeps
serving. The snapshot is cached (default 120 s) so the UI never pays the
full fetch cost per poll. ``pulse_score`` is an INTERNAL deterministic
composite of the real signals (documented below), never a fabricated
number.

Concurrency: a small thread pool (max 5) with a per-source timeout, so a
stalled feed cannot stall the whole monitor.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable

from dourmouse import live_feeds

_SOURCE_TIMEOUT = 8.0
_MAX_ITEMS_PER_SOURCE = 8
_SOURCE_COUNT = 8

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
    return {
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
