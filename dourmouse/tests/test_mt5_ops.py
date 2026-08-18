"""v8.3 MetaTrader 5 paper broker tests (mt5_ops.py + roster wiring).

All hermetic (Rule 2.1): the real ``MetaTrader5`` module is never imported
— a fake module is installed into ``sys.modules`` with the attribute
surface the code uses, and the honest NOT CONFIGURED degradation (package
missing / terminal not running / no account) is exercised directly.

Covered:
- NOT CONFIGURED when the package import failed
- NOT CONFIGURED when the terminal is not running / not logged in
- status / universe / quote / account against a fake terminal
- order is paper-first (refuses without paper_confirm) and live is
  double-gated (MT5_ALLOW_LIVE=1 + MT5_CONFIRM_LIVE=1)
- the roster carries the mt5 subagent with all four tools
"""

from __future__ import annotations

import sys
import types

import pytest

import dourmouse.mt5_ops as mt5_ops
from dourmouse.dispatch import Permission
from dourmouse.general_roster import build_general_registry


class _FakeAccount:
    login = 12345678
    server = "Broker-Demo"
    currency = "USD"
    equity = 1000.0
    balance = 1000.0
    margin_free = 950.0


class _FakeSymbol:
    def __init__(self, name):
        self.name = name
        self.visible = True


class _FakeTick:
    bid = 400.25
    ask = 400.35
    time = 1


class _FakePosition:
    symbol = "CORN"
    type = 0
    volume = 0.01
    price_open = 400.0
    profit = 1.25


class _FakeResult:
    retcode = 10009  # TRADE_RETCODE_DONE
    order = 99
    deal = 42
    price = 400.3
    comment = "done"


class _FakeMT5:
    """Fake MetaTrader5 module with just the surface mt5_ops uses."""

    def __init__(self, terminal_up=True, account=None, tick=None):
        self._up = terminal_up
        self._account = account or _FakeAccount()
        self._tick = tick or _FakeTick()
        self.last_err = None
        # constants used by mt5_ops
        self.TRADE_ACTION_DEAL = 1
        self.ORDER_TYPE_BUY = 0
        self.ORDER_TYPE_SELL = 1
        self.ORDER_TIME_GTC = 0
        self.ORDER_FILLING_IOC = 1
        self.TRADE_RETCODE_DONE = 10009

    def initialize(self):
        return self._up

    def last_error(self):
        return self.last_err

    def account_info(self):
        return self._account

    def symbol_info(self, name):
        return _FakeSymbol(name)

    def symbol_info_tick(self, name):
        return self._tick

    def symbols_get(self):
        return []

    def positions_get(self):
        return [_FakePosition()]

    def order_send(self, request):
        return _FakeResult()


@pytest.fixture
def fake_mt5(monkeypatch):
    fake = _FakeMT5()

    def _install():
        mod = types.ModuleType("MetaTrader5")
        for name in dir(fake):
            if not name.startswith("_"):
                setattr(mod, name, getattr(fake, name))
        monkeypatch.setitem(sys.modules, "MetaTrader5", mod)
        # force re-import of the try/except at module top
        mt5_ops.MT5_IMPORT_ERROR = None
        mt5_ops.mt5 = mod

    _install()
    return fake


class TestNotConfigured:
    def test_package_missing_is_honest(self, monkeypatch):
        monkeypatch.setattr(mt5_ops, "MT5_IMPORT_ERROR",
                            ImportError("no package"))
        out = mt5_ops.mt5_status()
        assert out["configured"] is False
        assert "not installed" in out["error"]

    def test_terminal_not_running_is_honest(self, monkeypatch, fake_mt5):
        fake_mt5._up = False
        out = mt5_ops.mt5_status()
        assert out["configured"] is False
        assert "terminal is not running" in out["error"]

    def test_quote_degrades_honestly(self, monkeypatch, fake_mt5):
        fake_mt5._up = False
        out = mt5_ops.mt5_quote("ZC")
        assert out["configured"] is False


class TestLiveCalls:
    def test_status_demo(self, fake_mt5):
        out = mt5_ops.mt5_status()
        assert out["configured"] is True
        assert out["demo"] is True
        assert out["environment"] == "demo"
        assert out["account"] == 12345678
        assert out["equity"] == 1000.0

    def test_universe_finds_available(self, fake_mt5):
        out = mt5_ops.mt5_universe()
        assert out["configured"] is True
        # fake server lists every candidate, so ZC/HE/GC resolve
        assert out["available"]["ZC"] == "CORN"
        assert out["available"]["HE"] == "LEAN_HOGS"
        assert out["available"]["GC"] == "GOLD"

    def test_quote_returns_real_spread(self, fake_mt5):
        out = mt5_ops.mt5_quote("GC")
        assert out["configured"] is True
        assert out["symbol"] == "GOLD"
        assert out["bid"] == 400.25
        assert out["ask"] == 400.35
        assert abs(out["spread"] - 0.10) < 1e-9

    def test_account_summary(self, fake_mt5):
        out = mt5_ops.mt5_account()
        assert out["configured"] is True
        assert out["balance"] == 1000.0
        assert out["open_positions"][0]["symbol"] == "CORN"

    def test_order_paper_first(self, fake_mt5):
        out = mt5_ops.mt5_order("ZC", "BUY")
        assert "paper confirmation required" in out["error"]

    def test_order_fills_on_demo_with_confirm(self, fake_mt5):
        out = mt5_ops.mt5_order("ZC", "BUY", volume=0.01, paper_confirm=True)
        assert out.get("filled") is True
        assert out["side"] == "BUY"
        assert out["symbol"] == "CORN"

    def test_live_account_is_double_gated(self, fake_mt5, monkeypatch):
        fake_mt5._account.server = "Broker-Live"
        monkeypatch.delenv("MT5_ALLOW_LIVE", raising=False)
        monkeypatch.delenv("MT5_CONFIRM_LIVE", raising=False)
        out = mt5_ops.mt5_order("ZC", "BUY", paper_confirm=True)
        assert "MT5_ALLOW_LIVE=1" in out["error"]
        monkeypatch.setenv("MT5_ALLOW_LIVE", "1")
        out = mt5_ops.mt5_order("ZC", "BUY", paper_confirm=True)
        assert "MT5_CONFIRM_LIVE=1" in out["error"]
        monkeypatch.setenv("MT5_CONFIRM_LIVE", "1")
        out = mt5_ops.mt5_order("ZC", "BUY", paper_confirm=True)
        assert out.get("filled") is True


@pytest.fixture(autouse=True)
def _reset_panel_cache():
    """The panel snapshot caches between calls — reset so each test starts
    clean (the cache would otherwise serve a stale value instead of running
    the monkeypatched worker)."""
    mt5_ops._PANEL_CACHE["data"] = None
    mt5_ops._PANEL_CACHE["ts"] = 0.0
    yield
    mt5_ops._PANEL_CACHE["data"] = None
    mt5_ops._PANEL_CACHE["ts"] = 0.0


class TestBoundedWorker:
    """The roster/HUD path runs through the subprocess worker with a hard
    timeout so the MT5 DLL (which can block holding the GIL) never freezes
    the webui. These tests cover the timeout, JSON and cache paths."""

    def test_worker_timeout_is_honest(self, monkeypatch):
        import subprocess as sp

        def _hang(*a, **k):
            raise sp.TimeoutExpired(cmd=["x"], timeout=12)

        monkeypatch.setattr(mt5_ops.subprocess, "run", _hang)
        out = mt5_ops._run_worker(["status"])
        assert out["configured"] is False
        assert "timed out" in out["error"]

    def test_worker_json_passthrough(self, monkeypatch):
        class _R:
            returncode = 0
            stdout = '{"configured": true, "environment": "demo"}'
            stderr = ""

        monkeypatch.setattr(mt5_ops.subprocess, "run", lambda *a, **k: _R())
        out = mt5_ops._run_worker(["status"])
        assert out["configured"] is True
        assert out["environment"] == "demo"

    def test_worker_nonzero_is_honest(self, monkeypatch):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "boom"

        monkeypatch.setattr(mt5_ops.subprocess, "run", lambda *a, **k: _R())
        out = mt5_ops._run_worker(["status"])
        assert out["configured"] is False
        assert "boom" in out["error"]

    def test_worker_bad_json_is_honest(self, monkeypatch):
        class _R:
            returncode = 0
            stdout = "not json"
            stderr = ""

        monkeypatch.setattr(mt5_ops.subprocess, "run", lambda *a, **k: _R())
        out = mt5_ops._run_worker(["status"])
        assert out["configured"] is False
        assert "no JSON" in out["error"]

    def test_panel_snapshot_pending_then_cached(self, monkeypatch):
        class _R:
            returncode = 0
            stdout = '{"configured": true, "environment": "demo"}'
            stderr = ""

        monkeypatch.setattr(mt5_ops.subprocess, "run", lambda *a, **k: _R())
        # first call: cache empty -> returns nested pending shape (the HUD
        # renderer requires {status, universe}), spawns background refresh
        first = mt5_ops.mt5_panel_snapshot()
        assert first["status"]["configured"] is False
        assert "status" in first and "universe" in first
        # second call after the thread lands: serves the cached value
        import time
        time.sleep(0.3)
        second = mt5_ops.mt5_panel_snapshot()
        assert second["status"]["configured"] is True
        assert second["status"]["environment"] == "demo"


class TestRoster:
    def test_mt5_subagent_registered(self):
        registry = build_general_registry()
        tool_names = set(registry.tool_names)
        assert {"mt5_status", "mt5_universe", "mt5_quote", "mt5_order"} <= tool_names

    def test_mt5_order_requires_confirmation(self):
        """v8.15: real trade execution — even paper/demo — is gated like
        every other consequential action (gmail_send, spotify_play), not
        left to a model-settable paper_confirm argument alone."""
        registry = build_general_registry()
        spec = registry.lookup("mt5_order")
        assert spec.permission is Permission.REQUIRES_CONFIRMATION
        assert spec.confirm_prompt is not None
        prompt = spec.confirm_prompt({"code": "ZC", "side": "buy", "volume": 0.5})
        assert "ZC" in prompt and "0.5" in prompt
        # sibling read-only tools stay regular
        for name in ("mt5_status", "mt5_universe", "mt5_quote"):
            assert registry.lookup(name).permission is Permission.REGULAR
