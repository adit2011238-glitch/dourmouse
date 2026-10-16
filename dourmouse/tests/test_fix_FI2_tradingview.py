"""FI2: TradingView bridge (P4-59 bus address, P4-60 paper log). The paper log is a CSV in a
temp FOREX_DATA_PATH; no broker, no TradingView, no network."""

from __future__ import annotations

import csv
import json
import os
import threading
import time

import pytest

from dourmouse import tradingview_ops as tv
from dourmouse.message_bus import BROADCAST, MessageBus

HEADER = ["key", "side", "venue", "entry_date", "entry_price", "contract",
          "exit_date", "exit_price", "pnl_pct", "pnl_usd", "notes"]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    monkeypatch.delenv("TV_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("FOREX_DATA_PATH", str(tmp_path / "fx"))
    return tmp_path


def _log():
    return tv._paper_log_path()


def _seed(rows):
    p = _log()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)
    return p


def _rows():
    with open(_log(), newline="", encoding="utf-8") as fh:
        return [r for r in csv.reader(fh) if r and r[0] != "key"]


OPEN_ROW = ["HE_8", "short", "tradingview", "2026-08-01", "92.50000", "HE1!", "", "", "", "", "tv alert open"]


# ---------------------------------------------------------------- P4-59

def test_signal_is_posted_to_the_bus_broadcast_address():
    bus = MessageBus()
    body = json.dumps({"ticker": "HE1!", "strategy": "HE_8", "side": "sell", "price": "92.5"}).encode()
    out = tv.handle_tv_webhook(body, "application/json", bus=bus)
    assert out["ok"]
    # every agent's inbox (and unread badge) includes it
    assert any("TV" in m["subject"] for m in bus.inbox("markets"))
    assert bus.unread_count("atlas") == 1
    assert bus.snapshot()[-1]["to"] == BROADCAST


# ---------------------------------------------------------------- P4-60

def test_unreadable_log_refuses_to_write_and_keeps_the_history():
    p = _seed([OPEN_ROW, ["ZC_12", "long", "tradingview", "2026-01-01", "450.00000", "ZC1!",
                          "2026-02-01", "465.00000", "0.0333", "3.33", "tv alert close"]])
    before = p.read_bytes()
    os.chmod(p, 0)
    try:
        r = tv.route_to_paper({"strategy": "HE_8", "side": "sell", "price": "93"})
    finally:
        os.chmod(p, 0o644)
    assert r["applied"] is False and "unreadable" in r["reason"]
    assert p.read_bytes() == before  # the history was not replaced by one row


def test_a_failed_replace_leaves_the_old_log(monkeypatch):
    p = _seed([OPEN_ROW])
    before = p.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    r = tv.route_to_paper({"strategy": "HE_8", "side": "buy", "price": "90"})
    assert r["applied"] is False and "write failed" in r["reason"]
    assert p.read_bytes() == before


def test_rewrite_goes_through_a_temp_file_not_truncate_in_place():
    p = _seed([OPEN_ROW])
    ino = p.stat().st_ino
    r = tv.route_to_paper({"strategy": "ZC_12", "side": "buy", "price": "450"})
    assert r["applied"]
    assert p.stat().st_ino != ino
    assert len(_rows()) == 2


def test_short_foreign_row_does_not_crash_a_close():
    _seed([["HE_8"], OPEN_ROW, ["junk", "row"]])  # rows an external script left behind
    r = tv.route_to_paper({"strategy": "HE_8", "side": "buy", "price": "90.0"})
    assert r["applied"] and r["action"] == "close"
    rows = _rows()
    assert rows[1][7] == "90.00000" and rows[1][10] == "tv alert close"


def test_matching_open_row_with_a_short_or_bad_entry_is_reported_not_raised():
    _seed([["HE_8", "short", "tradingview", "2026-08-01"]])  # no entry price at all
    r = tv.route_to_paper({"strategy": "HE_8", "side": "buy", "price": "90"})
    assert r["applied"] is False and "entry price" in r["reason"]
    _seed([["HE_8", "short", "tradingview", "2026-08-01", "0", "HE1!", "", "", "", "", "x"]])
    r = tv.route_to_paper({"strategy": "HE_8", "side": "buy", "price": "90"})
    assert r["applied"] is False and "entry price" in r["reason"]
    _seed([["HE_8", "short", "tradingview", "2026-08-01", "abc", "HE1!", "", "", "", "", "x"]])
    r = tv.route_to_paper({"strategy": "HE_8", "side": "buy", "price": "90"})
    assert r["applied"] is False


@pytest.mark.parametrize("price", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_non_finite_price_is_rejected(price):
    r = tv.route_to_paper({"strategy": "HE_8", "side": "sell", "price": price})
    assert r["applied"] is False and "price" in r["reason"]
    assert not _log().exists() or _rows() == []


def test_webhook_never_raises_on_a_corrupt_log():
    _seed([["HE_8"], ["HE_8", "short", "x", "d", "zzz"]])
    body = json.dumps({"ticker": "HE1!", "strategy": "HE_8", "side": "buy", "price": "90"}).encode()
    out = tv.handle_tv_webhook(body, "application/json")
    assert out["ok"] is True and out["paper"]["applied"] is False


def test_paper_log_lock_is_held_across_processes():
    """An external writer holding the sidecar lock makes route_to_paper wait for it."""
    import fcntl

    _seed([OPEN_ROW])
    lock_path = tv._paper_lock_path()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    result = {}
    t = threading.Thread(target=lambda: result.update(
        r=tv.route_to_paper({"strategy": "HE_8", "side": "buy", "price": "90"})), daemon=True)
    t.start()
    time.sleep(0.6)
    still_waiting = t.is_alive()
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    t.join(timeout=10)
    assert still_waiting, "route_to_paper did not wait for the inter-process lock"
    assert result["r"]["applied"]


def test_ordinary_open_and_close_still_work():
    r1 = tv.route_to_paper({"strategy": "HE_8", "side": "sell", "price": "92.5"})
    assert r1["applied"] and r1["action"] == "open"
    r2 = tv.route_to_paper({"strategy": "HE_8", "side": "buy", "price": "95.0"})
    assert r2["applied"] and r2["action"] == "close" and r2["pnl_pct"] < 0
