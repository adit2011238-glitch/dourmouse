"""FI1 fixes for mt5_ops.py / mt5_probe.py: P3-41 (demo/live decided by the
terminal's trade_mode, not a server-name substring), P3-40 (panel command
crash), P3-42 (one panel probe at a time). A fake MetaTrader5 module only:
no terminal, no broker, no network."""

from __future__ import annotations

import importlib
import sys
import threading
import time
import types

import pytest

import dourmouse.mt5_ops as mt5_ops
from dourmouse.tests.test_mt5_ops import _FakeAccount, _FakeMT5


class _Acct(_FakeAccount):
    margin_mode = 0  # netting


@pytest.fixture
def fake_mt5(monkeypatch):
    fake = _FakeMT5(account=_Acct())
    mod = types.ModuleType("MetaTrader5")
    for name in dir(fake):
        if not name.startswith("_"):
            setattr(mod, name, getattr(fake, name))
    mod.ACCOUNT_TRADE_MODE_DEMO = 0
    mod.ACCOUNT_TRADE_MODE_CONTEST = 1
    mod.ACCOUNT_TRADE_MODE_REAL = 2
    monkeypatch.setitem(sys.modules, "MetaTrader5", mod)
    monkeypatch.setattr(mt5_ops, "MT5_IMPORT_ERROR", None)
    monkeypatch.setattr(mt5_ops, "mt5", mod, raising=False)
    monkeypatch.delenv("MT5_ALLOW_LIVE", raising=False)
    monkeypatch.delenv("MT5_CONFIRM_LIVE", raising=False)
    return fake


# --------------------------------------------------------------------------- #
# P3-41
# --------------------------------------------------------------------------- #

def test_real_account_on_a_server_named_demo_is_live(fake_mt5):
    fake_mt5._account.server = "Broker-DemoAndLive"
    fake_mt5._account.trade_mode = 2  # REAL
    assert mt5_ops.mt5_status()["environment"] == "live"
    out = mt5_ops.mt5_order("ZC", "BUY", paper_confirm=True)
    assert "MT5_ALLOW_LIVE=1" in out["error"]


def test_demo_trade_mode_is_demo_whatever_the_server_is_called(fake_mt5):
    fake_mt5._account.server = "Broker-Live01"
    fake_mt5._account.trade_mode = 0  # DEMO
    st = mt5_ops.mt5_status()
    assert st["environment"] == "demo" and st["demo"] is True


def test_contest_and_unknown_trade_modes_are_not_treated_as_paper(fake_mt5):
    fake_mt5._account.trade_mode = 1
    assert mt5_ops.mt5_status()["environment"] == "live"
    fake_mt5._account.trade_mode = None
    assert mt5_ops.mt5_status()["environment"] == "live"


def test_missing_trade_mode_attribute_is_live(fake_mt5):
    class NoMode:
        login = 1
        server = "X-Demo"
        currency = "USD"
        equity = 1000.0
        balance = 1000.0
        margin_free = 900.0

    fake_mt5._account = NoMode()
    mod = sys.modules["MetaTrader5"]
    mod.account_info = lambda: fake_mt5._account
    assert mt5_ops.mt5_status()["environment"] == "live"


def test_oversized_volume_is_refused_by_the_guard_before_order_send(fake_mt5, monkeypatch, tmp_path):
    """Regression for the 'no cap on size' half of P3-41: the order guard
    (position size vs equity) already stops a mistyped 100 lots."""
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    fake_mt5._account.trade_mode = 0
    sent = []
    mod = sys.modules["MetaTrader5"]
    mod.order_send = lambda req: sent.append(req)
    out = mt5_ops.mt5_order("ZC", "BUY", volume=100, paper_confirm=True)
    assert "error" in out and sent == []


# --------------------------------------------------------------------------- #
# P3-40
# --------------------------------------------------------------------------- #

def test_panel_command_lists_a_universe_instead_of_crashing(fake_mt5, monkeypatch):
    fake_mt5._account.trade_mode = 0
    monkeypatch.delitem(sys.modules, "dourmouse.mt5_probe", raising=False)
    probe = importlib.import_module("dourmouse.mt5_probe")
    try:
        out = probe.cmd_panel()
    finally:
        sys.modules.pop("dourmouse.mt5_probe", None)
    assert out["status"]["configured"] is True
    uni = out["universe"]
    assert uni["configured"] is True
    assert uni["available"]["ZC"] == "CORN"
    assert uni["not_listed"] == []


# --------------------------------------------------------------------------- #
# P3-42
# --------------------------------------------------------------------------- #

def test_stale_cache_starts_only_one_probe_at_a_time(monkeypatch):
    mt5_ops._PANEL_CACHE["data"] = None
    mt5_ops._PANEL_CACHE["ts"] = 0.0
    release = threading.Event()
    started = []

    def slow_worker(args, timeout=12.0):
        started.append(args)
        release.wait(10)
        return {"configured": False, "error": "stuck"}

    monkeypatch.setattr(mt5_ops, "_run_worker", slow_worker)
    for _ in range(6):
        mt5_ops.mt5_panel_snapshot()
    time.sleep(0.3)
    assert len(started) == 1
    release.set()
    for _ in range(100):
        if mt5_ops._PANEL_CACHE["data"] is not None:
            break
        time.sleep(0.05)
    time.sleep(0.2)
    # the cache is now fresh, and a later stale period can probe again
    mt5_ops._PANEL_CACHE["ts"] = 0.0
    mt5_ops.mt5_panel_snapshot()
    time.sleep(0.3)
    assert len(started) == 2
    release.set()
    mt5_ops._PANEL_CACHE["data"] = None
    mt5_ops._PANEL_CACHE["ts"] = 0.0
