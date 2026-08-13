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
_SOURCE_COUNT = 6

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


def _item(title: str, summary: str = "", link: str = "", at: str = "", severity: str = "") -> dict[str, str]:
    return {
        "title": (title or "").strip()[:200],
        "summary": (summary or "").strip()[:300],
        "link": (link or "").strip(),
        "at": at,
        "severity": severity,
    }


def _rss_items(raw: str, max_items: int, pick: Callable[[ET.Element], dict[str, str]]) -> list[dict[str, str]]:
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


def _fetch_markets() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
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
    if not items:
        # Honest: nothing real came back (provider blocked/rate-limited) —
        # the source is OFFLINE, not "ok with warn placeholders".
        raise RuntimeError("Yahoo Finance unreachable: " + "; ".join(failures[:4]))
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


def _fetch_disasters() -> list[dict[str, str]]:
    raw = _http_get("https://www.gdacs.org/xml/rss.xml")
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
        lowered = (title + " " + summary).lower()
        if "alertlevel: red" in lowered or "alertlevel red" in lowered:
            sev = "critical"
        elif "orange" in lowered:
            sev = "high"
        elif "green" in lowered:
            sev = "info"
        else:
            sev = "watch"
        return _item(title, summary=summary, link=link, at=published, severity=sev)

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

_SOURCES: dict[str, tuple[str, Callable[[], list[dict[str, str]]]]] = {
    "markets": ("Yahoo Finance — movers + key quotes", _fetch_markets),
    "news": ("Google News RSS — world / Europe / APAC", _fetch_news),
    "disasters": ("GDACS — earthquakes, floods, cyclones, wildfires", _fetch_disasters),
    "cyber": ("CISA — cybersecurity advisories", _fetch_cyber),
    "conflict": ("ReliefWeb — humanitarian + conflict updates", _fetch_conflict),
    "macro": ("World Bank — GDP growth + inflation", _fetch_macro),
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
