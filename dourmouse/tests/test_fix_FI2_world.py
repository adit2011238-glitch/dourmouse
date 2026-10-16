"""FI2: world feeds (P5-66..70, P5-62, P5-63 check, P3-70, P4-62, P4-63, P5-42). No network:
every transport is replaced; the history and region files live in temp dirs."""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from dourmouse import live_feeds
from dourmouse import world_pulse as wp


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_WORLD_HISTORY_FILE", str(tmp_path / "hist.jsonl"))
    monkeypatch.setenv("DOURMOUSE_WATCH_REGIONS_FILE", str(tmp_path / "regions.json"))
    for k in ("FIRMS_MAP_KEY", "ENTSOE_API_TOKEN", "AISSTREAM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with wp._cache_lock:
        wp._cache.update({"at": 0.0, "snapshot": None})
    yield
    with wp._cache_lock:
        wp._cache.update({"at": 0.0, "snapshot": None})


# ---------------------------------------------------------------- P5-66

FIRMS_KEY = "FIRMSKEY0123456789abcdef"
ENTSOE_TOKEN = "11111111-2222-3333-4444-555555555555"


def test_firms_key_is_not_in_the_error_text(monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", FIRMS_KEY)
    import urllib.error

    def boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 503, "unavailable", {}, io.BytesIO(b""))

    monkeypatch.setattr(live_feeds.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as exc:
        wp._fetch_wildfires()
    assert FIRMS_KEY not in str(exc.value)
    assert "503" in str(exc.value) and "***" in str(exc.value)


def test_firms_key_is_not_in_a_network_error_either(monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", FIRMS_KEY)
    import urllib.error

    def boom(req, timeout=0):
        raise urllib.error.URLError("dns down")

    monkeypatch.setattr(live_feeds.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as exc:
        wp._fetch_wildfires()
    assert FIRMS_KEY not in str(exc.value)


def test_entsoe_token_is_not_in_the_per_zone_failure_text(monkeypatch):
    import urllib.error

    def boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "denied", {}, io.BytesIO(b""))

    monkeypatch.setattr(live_feeds.urllib.request, "urlopen", boom)
    monkeypatch.setenv("ENTSOE_API_TOKEN", ENTSOE_TOKEN)
    _items, failures = wp._fetch_entsoe_load(ENTSOE_TOKEN)
    assert failures
    assert ENTSOE_TOKEN[:10] not in " ".join(failures)  # the text is cut at 80 chars, so a prefix leaks too


def test_snapshot_error_field_never_carries_a_secret(monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", FIRMS_KEY)

    def leaky():
        raise RuntimeError(f"HTTP 503 from https://x/csv/{FIRMS_KEY}/VIIRS")

    monkeypatch.setattr(wp, "_SOURCES", {"wildfires": ("w", leaky)})
    snap = wp.world_pulse_snapshot(force=True)
    assert FIRMS_KEY not in json.dumps(snap)


# ---------------------------------------------------------------- P5-67

def test_a_stalled_source_does_not_hold_the_snapshot(monkeypatch):
    release = threading.Event()

    def stalled():
        release.wait(30)
        return []

    monkeypatch.setattr(wp, "_SOURCE_TIMEOUT", 0.3)
    monkeypatch.setattr(wp, "_SOURCES", {
        "quakes": ("q", lambda: [{"title": "ok"}]),
        "flights": ("f", stalled),
    })
    t0 = time.monotonic()
    snap = wp.world_pulse_snapshot(force=True)
    elapsed = time.monotonic() - t0
    release.set()
    assert elapsed < 5, f"snapshot waited {elapsed:.1f}s for a stalled source"
    assert snap["sources"]["quakes"]["ok"] is True
    assert snap["sources"]["flights"]["ok"] is False
    assert "timed out" in snap["sources"]["flights"]["error"]


def test_concurrent_callers_share_one_fan_out(monkeypatch):
    calls: list[int] = []

    def slow():
        calls.append(1)
        time.sleep(0.4)
        return [{"title": "x"}]

    monkeypatch.setattr(wp, "_SOURCES", {"quakes": ("q", slow)})
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(wp.world_pulse_snapshot())) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(results) == 6
    assert len(calls) == 1, f"{len(calls)} fan-outs for 6 simultaneous callers"


def test_force_waits_for_an_in_flight_build_instead_of_starting_another(monkeypatch):
    calls: list[int] = []

    def slow():
        calls.append(1)
        time.sleep(0.4)
        return []

    monkeypatch.setattr(wp, "_SOURCES", {"quakes": ("q", slow)})
    out: list = []
    t = threading.Thread(target=lambda: out.append(wp.world_pulse_snapshot(force=True)))
    t.start()
    time.sleep(0.1)
    out.append(wp.world_pulse_snapshot(force=True))
    t.join(timeout=20)
    assert len(calls) == 1


# ---------------------------------------------------------------- P5-68

def _markets_with(monkeypatch, *, movers_fail=True, quotes=5):
    def movers(direction, count=5):
        if movers_fail and direction == "gainers":
            raise RuntimeError("HTTP 429 from https://query1.finance.yahoo.com/x")
        return [{"symbol": f"{direction[0]}{i}", "name": "n", "price": 1.0, "currency": "USD",
                 "change_pct": 1.0} for i in range(5)]

    monkeypatch.setattr(live_feeds, "market_movers", movers)
    monkeypatch.setattr(live_feeds, "stock_quote",
                        lambda s: {"symbol": s, "price": 1.0, "currency": "USD", "as_of": 1})
    monkeypatch.setattr(wp, "_fetch_crypto_fx", lambda: (
        [wp._item("BTC/USD 1", summary="x", severity="up") for _ in range(2)], ["fx: Frankfurter timeout"]))


def test_partial_market_failure_is_visible_even_with_many_items(monkeypatch):
    _markets_with(monkeypatch)
    items = wp._fetch_markets()
    assert len(items) <= wp._MAX_ITEMS_PER_SOURCE
    warn = [i for i in items if i.get("severity") == "warn"]
    assert len(warn) == 1, [i["title"] for i in items]
    assert items[0] is warn[0]
    text = json.dumps(warn[0])
    assert "gainers" in text and "429" in text
    assert "fx" in text  # the Frankfurter failure is named too, not blamed on Yahoo


def test_no_failures_means_no_warning(monkeypatch):
    _markets_with(monkeypatch, movers_fail=False)
    monkeypatch.setattr(wp, "_fetch_crypto_fx", lambda: ([wp._item("BTC", summary="x", severity="up")], []))
    items = wp._fetch_markets()
    assert not [i for i in items if i.get("severity") == "warn"]


# ---------------------------------------------------------------- P5-69

RSS_OK = ("<rss><channel><item><title>World headline one</title><source>S</source>"
          "<pubDate>Mon</pubDate></item></channel></rss>")


def test_one_bad_news_edition_keeps_the_others(monkeypatch):
    def fake_get(url):
        if "hl=en-AU" in url:
            return "<rss><channel></channel></rss>"       # empty feed
        if "hl=en-GB" in url:
            return "this is not xml at all <<<"             # unparseable
        return RSS_OK
    monkeypatch.setattr(wp, "_http_get", fake_get)
    items = wp._fetch_news()
    titles = [i["title"] for i in items]
    assert any("World headline one" in t for t in titles)
    assert any("EUROPE NEWS UNAVAILABLE" in t for t in titles)
    assert any("APAC NEWS UNAVAILABLE" in t for t in titles)


# ---------------------------------------------------------------- P5-70

def _zip_of(member_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("20260823183000.export.CSV", member_bytes)
    return buf.getvalue()


def test_gdelt_uses_https():
    assert wp._GDELT_LASTUPDATE_URL.startswith("https://data.gdeltproject.org/")


def test_gdelt_export_url_must_be_on_the_gdelt_host(monkeypatch):
    monkeypatch.setattr(wp, "_http_get", lambda url: "1 a http://evil.example.org/x.export.CSV.zip\n")
    fetched: list[str] = []
    monkeypatch.setattr(wp, "_http_get_bytes", lambda url: fetched.append(url) or b"")
    with pytest.raises(RuntimeError, match="data.gdeltproject.org"):
        wp._fetch_conflict_events()
    assert fetched == []


def test_gdelt_export_is_fetched_over_https(monkeypatch):
    monkeypatch.setattr(wp, "_http_get", lambda url: "1 a http://data.gdeltproject.org/gdeltv2/2026.export.CSV.zip\n")
    fetched: list[str] = []
    monkeypatch.setattr(wp, "_http_get_bytes", lambda url: fetched.append(url) or _zip_of(b""))
    with contextlib.suppress(RuntimeError):  # an empty export may report no events; only the URL matters here
        wp._fetch_conflict_events()
    assert fetched == ["https://data.gdeltproject.org/gdeltv2/2026.export.CSV.zip"]


def test_zip_bomb_member_is_refused_before_it_is_read(monkeypatch):
    monkeypatch.setattr(wp, "_MAX_GDELT_UNZIPPED_BYTES", 10_000)
    bomb = _zip_of(b"0" * 5_000_000)  # tiny on the wire, huge when unzipped
    assert len(bomb) < 20_000
    monkeypatch.setattr(wp, "_http_get", lambda url: "1 a http://data.gdeltproject.org/gdeltv2/2026.export.CSV.zip\n")
    monkeypatch.setattr(wp, "_http_get_bytes", lambda url: bomb)
    with pytest.raises(RuntimeError, match="too large"):
        wp._fetch_conflict_events()


def test_http_get_bytes_caps_the_download(monkeypatch):
    class Resp:
        def __init__(self):
            self.read_sizes = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            self.read_sizes.append(n)
            return b"x" * (n if n and n > 0 else 10**9)

    r = Resp()
    monkeypatch.setattr(wp.urllib.request, "urlopen", lambda req, timeout=0: r)
    monkeypatch.setattr(wp, "_MAX_DOWNLOAD_BYTES", 1000)
    with pytest.raises(RuntimeError, match="too large"):
        wp._http_get_bytes("https://data.gdeltproject.org/x.zip")
    assert r.read_sizes and r.read_sizes[0] == 1001


# ---------------------------------------------------------------- P5-62

def test_brief_does_not_say_no_items_when_the_source_counted_items():
    from dourmouse.world_brief import generate_brief as build_brief

    snap = {"pulse_score": 60, "pulse_label": "ELEVATED", "generated_at": "2026-10-16T00:00:00+00:00",
            "sources": {"quakes": {"ok": True, "count": 7}}, "items": {"quakes": []}}
    text = build_brief(snap)["text"]
    assert "No earthquakes items" not in text and "no earthquakes" not in text.lower().replace("details unavailable", "")
    assert "7" in text and "details unavailable" in text


def test_brief_still_says_no_items_when_the_count_is_zero():
    from dourmouse.world_brief import generate_brief as build_brief

    snap = {"pulse_score": 60, "pulse_label": "ELEVATED", "generated_at": "2026-10-16T00:00:00+00:00",
            "sources": {"quakes": {"ok": True, "count": 0}}, "items": {"quakes": []}}
    assert "No " in build_brief(snap)["text"]


# ---------------------------------------------------------------- P5-63 (already fixed by wave 2b: regression check)

def test_region_ids_do_not_collide_after_a_deletion():
    from dourmouse import world_watch_regions as wr

    a = wr.add_region("A", 0, 1, 0, 1)
    b = wr.add_region("B", 2, 3, 2, 3)
    assert wr.delete_region(a["id"]) is True
    c = wr.add_region("C", 4, 5, 4, 5)
    assert c["id"] != b["id"]
    assert [r["name"] for r in wr.list_regions()] == ["B", "C"]
    assert wr.delete_region(c["id"]) is True
    assert [r["name"] for r in wr.list_regions()] == ["B"]


# ---------------------------------------------------------------- P3-70

from dourmouse import world_pulse_history as wph  # noqa: E402


def _geo(**counts):
    return {"counts": counts, "layers": {}, "pulse_score": 50, "generated_at": "x"}


def _seed_history(records):
    path = wph._history_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _iso(delta):
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def test_record_appends_one_line_and_does_not_rewrite_the_file():
    old = {"at": _iso(timedelta(hours=2)), "geo": _geo(a=1)}
    path = _seed_history([old])
    before = path.read_bytes()
    ino = path.stat().st_ino
    wph.record_snapshot(_geo(a=2))
    after = path.read_bytes()
    assert after.startswith(before)               # the earlier line is untouched
    assert path.stat().st_ino == ino              # appended in place
    assert len(after.splitlines()) == 2


def test_retention_is_applied_when_recording():
    path = _seed_history([
        {"at": _iso(timedelta(hours=40)), "geo": _geo(a=1)},
        {"at": _iso(timedelta(hours=30)), "geo": _geo(a=2)},
        {"at": _iso(timedelta(hours=2)), "geo": _geo(a=3)},
    ])
    wph.record_snapshot(_geo(a=4))
    kept = [json.loads(line)["geo"]["counts"]["a"] for line in path.read_text().splitlines()]
    assert kept == [3, 4]


def test_a_crash_torn_last_line_does_not_glue_onto_the_next_record():
    good = {"at": _iso(timedelta(hours=2)), "geo": _geo(a=1)}
    path = _seed_history([good])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"at": "2026-01-01T00:00:0')          # killed mid-append: no newline, torn JSON
    wph.record_snapshot(_geo(a=9))
    rows = wph.history_range(hours=999)
    assert [r["counts"] for r in rows] == [{"a": 1}, {"a": 9}]


def test_a_failed_rewrite_leaves_the_old_history(monkeypatch):
    path = _seed_history([{"at": _iso(timedelta(hours=40)), "geo": _geo(a=1)},
                          {"at": _iso(timedelta(hours=2)), "geo": _geo(a=2)}])
    before = path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    wph.record_snapshot(_geo(a=3))        # needs a prune rewrite; must not truncate the file
    assert path.read_bytes() == before


def test_prune_old_is_atomic(monkeypatch):
    path = _seed_history([{"at": _iso(timedelta(hours=40)), "geo": _geo(a=1)},
                          {"at": _iso(timedelta(hours=2)), "geo": _geo(a=2)}])
    before = path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert wph.prune_old(hours=24) == 0
    assert path.read_bytes() == before


def test_an_unreadable_history_is_not_replaced_by_one_record():
    path = _seed_history([{"at": _iso(timedelta(hours=40)), "geo": _geo(a=1)},
                          {"at": _iso(timedelta(hours=2)), "geo": _geo(a=2)}])
    before = path.read_bytes()
    os.chmod(path, 0)
    try:
        wph.record_snapshot(_geo(a=3))
        assert wph.prune_old(hours=24) == 0
    finally:
        os.chmod(path, 0o644)
    assert path.read_bytes() == before


def test_cap_still_holds(monkeypatch):
    monkeypatch.setattr(wph, "_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(wph, "_MAX_LINES", 5)
    for i in range(20):
        wph.record_snapshot(_geo(a=i))
    rows = wph.history_range(hours=999)
    assert len(rows) <= 5 and rows[-1]["counts"] == {"a": 19}


# ---------------------------------------------------------------- P4-62 / P4-63

from worldmonitor_sdk import Client  # noqa: E402

from dourmouse import worldmonitor as wm  # noqa: E402


@pytest.fixture
def _wm_env(monkeypatch):
    monkeypatch.setenv("WORLDMONITOR_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("WORLDMONITOR_MCP_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("WORLDMONITOR_API_KEY", "wm_test")
    wm._confirmed_tools.clear()


def _transport(seen):
    def t(request, timeout):
        payload = json.loads(request["body"])
        seen.append((payload["method"], timeout))
        if payload["method"] == "tools/list":
            body = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "get_brand_new_tool", "description": "x"}]}}
        else:
            body = {"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}
        return 200, "application/json", json.dumps(body)
    return t


def test_data_call_gets_the_long_timeout_not_the_probe_one(monkeypatch, _wm_env):
    seen: list = []
    monkeypatch.setattr(wm, "_client", lambda _t=_transport(seen): Client(transport=_t, timeout=wm._PROBE_TIMEOUT))
    wm.worldmonitor_call_tool("get_market_data", {})
    calls = [t for m, t in seen if m == "tools/call"]
    assert calls == [30.0]


def test_a_frozen_tool_name_costs_no_catalog_round_trip(monkeypatch, _wm_env):
    seen: list = []
    monkeypatch.setattr(wm, "_client", lambda _t=_transport(seen): Client(transport=_t))
    wm.worldmonitor_call_tool("get_market_data", {})
    assert [m for m, _ in seen] == ["tools/call"]


def test_live_catalog_result_is_remembered_for_a_few_minutes(monkeypatch, _wm_env):
    seen: list = []
    monkeypatch.setattr(wm, "_client", lambda _t=_transport(seen): Client(transport=_t))
    wm.worldmonitor_call_tool("get_brand_new_tool", {})
    wm.worldmonitor_call_tool("get_brand_new_tool", {})
    assert [m for m, _ in seen].count("tools/list") == 1
    assert [m for m, _ in seen].count("tools/call") == 2


@pytest.mark.parametrize("desc", ["   ", "\n\n", " \n "])
def test_blank_description_does_not_crash_the_catalog(monkeypatch, desc):
    def t(request, timeout):
        body = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [
            {"name": "blank", "description": desc}, {"name": "ok", "description": "first line\nsecond"}]}}
        return 200, "application/json", json.dumps(body)

    monkeypatch.setattr(wm, "_client", lambda _t=t: Client(transport=_t))
    cat = wm.worldmonitor_catalog()
    assert cat == [{"name": "blank", "description": ""}, {"name": "ok", "description": "first line"}]
    assert "blank" in wm._tool_names_text()


# ---------------------------------------------------------------- P5-42

import subprocess  # noqa: E402

from dourmouse import research_agent as ra  # noqa: E402


def test_research_timeout_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="atlas", timeout=900)

    monkeypatch.setattr(ra, "run_atlas_research", boom)
    out = ra.call_research_tool({"symbols": ["SPY"]})
    assert out.startswith("ATLAS RUN FAILED") and "timed out" in out


def test_research_missing_symbols_is_reported_not_raised():
    assert "symbols" in ra.call_research_tool({})
    assert "symbols" in ra.call_research_tool({"symbols": "SPY"})
    assert "symbols" in ra.call_research_tool({"symbols": []})


def test_research_unrunnable_interpreter_is_reported(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no such python")

    monkeypatch.setattr(ra, "run_atlas_research", boom)
    assert ra.call_research_tool({"symbols": ["SPY"]}).startswith("ATLAS RUN FAILED")


def test_research_bad_numeric_argument_is_reported(monkeypatch):
    monkeypatch.setattr(ra, "run_atlas_research", lambda **k: {"ok": True})
    assert "population_size" in ra.call_research_tool({"symbols": ["SPY"], "population_size": "lots"})
    assert json.loads(ra.call_research_tool({"symbols": ["SPY"], "generations": "3"})) == {"ok": True}


# ---------------------------------------------------------------- H-FI2-1: the POST helper had the same unbounded read

def test_http_post_caps_the_response(monkeypatch):
    class Resp:
        sizes: list = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            Resp.sizes.append(n)
            return b"x" * (n if n and n > 0 else 10**9)

    monkeypatch.setattr(wp.urllib.request, "urlopen", lambda req, timeout=0: Resp())
    monkeypatch.setattr(wp, "_MAX_DOWNLOAD_BYTES", 500)
    with pytest.raises(RuntimeError, match="too large"):
        wp._http_post("https://overpass-api.de/api/interpreter", b"q")
    assert Resp.sizes[0] == 501
