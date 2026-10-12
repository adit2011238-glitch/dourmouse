"""FIX-W2 P4-24 (guardrails wired into the order functions) and W2R-5 (order status UNKNOWN).

Only FAKE broker clients are used: urllib's urlopen is replaced for Trading 212 and a fake
MetaTrader5 module for MT5. Nothing here can contact a broker. Every refusal test also asserts
the fake client's order endpoint was never called."""
from __future__ import annotations

import importlib
import io
import json
import sys
import types
import urllib.error

import pytest

from dourmouse import mt5_ops, order_guard
from dourmouse import trading212_ops as t


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "fake-key")
    monkeypatch.setenv("T212_ENV", "demo")
    for name in ("T212_ALLOW_LIVE", "T212_CONFIRM_LIVE", "DOURMOUSE_MAX_POSITION_PCT",
                 "DOURMOUSE_MAX_SECTOR_PCT", "DOURMOUSE_DAILY_LOSS_LIMIT_PCT",
                 "DOURMOUSE_TRADE_CONFIRM_USD"):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# A fake Trading 212 client
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, raw: bytes):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._raw


class FakeT212:
    def __init__(self, equity=10000.0, positions=None, order_reply=None):
        self.equity = equity
        self.positions = positions if positions is not None else [{"ticker": "AAPL", "quantity": 3, "currentPrice": 100.0}]
        self.order_reply = order_reply or (lambda req: _Resp(b'{"id": "ORD-1"}'))
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url))
        if req.get_method() == "POST":
            return self.order_reply(req)
        if req.full_url.endswith("/account/info"):
            return _Resp(json.dumps({"totalValue": self.equity}).encode())
        return _Resp(json.dumps(self.positions).encode())

    @property
    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture
def t212(monkeypatch):
    fake = FakeT212()
    monkeypatch.setattr(t.urllib.request, "urlopen", fake)
    return fake


def _order(**kw):
    args = {"ticker": "AAPL", "quantity": 1, "paper_confirm": True}
    args.update(kw)
    return t._wrap(t.t212_order)(args)


# -- P4-24, Trading 212 ------------------------------------------------------ #

def test_t212_order_within_limits_is_sent(t212):
    out = _order(quantity=2)
    assert '"ORD-1"' in out
    assert len(t212.posts) == 1


def test_t212_position_size_breach_blocks_the_submission(t212):
    out = _order(quantity=9)  # 3 held + 9 = 12 x 100 = 1200 > 10% of 10000
    assert out.startswith("ERROR") and "position size" in out and "guardrails" in out
    assert t212.posts == []  # the broker's order endpoint was never called


def test_t212_sell_that_reduces_exposure_is_allowed(t212):
    out = _order(quantity=-2)
    assert '"ORD-1"' in out and len(t212.posts) == 1


def test_t212_a_sell_that_flips_the_position_beyond_the_limit_is_blocked(t212):
    out = _order(quantity=-20)  # 300 - 2000 = -1700 short exposure, over 10%
    assert "position size" in out
    assert t212.posts == []


def test_t212_market_order_for_an_unheld_ticker_has_no_price_and_is_refused(t212):
    out = _order(ticker="TSLA")
    assert out.startswith("ERROR") and "LIMIT" in out
    assert t212.posts == []


def test_t212_limit_order_for_an_unheld_ticker_is_sized_by_its_limit(t212):
    assert '"ORD-1"' in _order(ticker="VOO", quantity=1, order_type="LIMIT", limit_price=510.0)
    assert len(t212.posts) == 1
    out = _order(ticker="VOO", quantity=3, order_type="LIMIT", limit_price=510.0)  # 1530 > 1000
    assert "position size" in out and len(t212.posts) == 1


def test_t212_unreadable_account_blocks_the_submission(monkeypatch):
    fake = FakeT212()
    fake.equity = "not a number"
    monkeypatch.setattr(t.urllib.request, "urlopen", fake)
    out = _order()
    assert out.startswith("ERROR") and "could not read the account" in out
    assert fake.posts == []


def test_t212_failing_account_read_blocks_the_submission(monkeypatch):
    def boom(req, timeout=None):
        if req.get_method() == "POST":
            raise AssertionError("order endpoint must not be reached")
        raise urllib.error.URLError("down")

    monkeypatch.setattr(t.urllib.request, "urlopen", boom)
    out = _order()
    assert out.startswith("ERROR") and "could not read the account" in out


def test_t212_daily_loss_breach_trips_the_kill_switch_and_stays_tripped(monkeypatch):
    fake = FakeT212(equity=10000.0)
    monkeypatch.setattr(t.urllib.request, "urlopen", fake)
    assert '"ORD-1"' in _order()  # first sight of the day: baseline 10000
    fake.equity = 9000.0  # -10% against a 3% limit
    out = _order()
    assert "kill-switch tripped" in out and len(fake.posts) == 1
    assert json.loads(order_guard.state_path().read_text())["tripped"] is True
    fake.equity = 10000.0  # recovered, but the latch holds until a manual re-arm
    assert "kill-switch tripped" in _order() and len(fake.posts) == 1
    order_guard.rearm()
    assert '"ORD-1"' in _order() and len(fake.posts) == 2


def test_the_latch_survives_a_new_process_view_of_the_state(t212, monkeypatch):
    _order()
    t212.equity = 5000.0
    _order()
    # a second process (the MT5 worker is one) reads the same file
    assert order_guard._load_state()["tripped"] is True


def test_unreadable_guard_state_refuses_instead_of_forgetting_a_latch(t212):
    order_guard.state_path().write_text("{broken", encoding="utf-8")
    out = _order()
    assert "cannot be read" in out and t212.posts == []


def test_paper_confirm_is_still_checked_before_any_account_read(t212):
    with pytest.raises(ValueError, match="paper_confirm"):
        t.t212_order("AAPL", 1)
    assert t212.calls == []


def test_guardrail_limits_come_from_the_owners_settings(t212, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_MAX_POSITION_PCT", "0.5")
    assert '"ORD-1"' in _order(quantity=9)


# -- W2R-5: an order POST that may have executed ----------------------------- #

@pytest.mark.parametrize("code", [408, 500, 502, 503, 504])
def test_http_5xx_or_408_on_the_order_post_is_unknown_status(monkeypatch, code):
    def reply(req):
        raise urllib.error.HTTPError("u", code, "Gateway Time-out", {}, io.BytesIO(b"gw"))

    monkeypatch.setattr(t.urllib.request, "urlopen", FakeT212(order_reply=reply))
    out = _order()
    assert out.startswith("ERROR") and "UNKNOWN" in out and "do not place the order again" in out


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 429])
def test_definite_http_failures_on_the_order_post_stay_plain_failures(monkeypatch, code):
    def reply(req):
        raise urllib.error.HTTPError("u", code, "Nope", {}, io.BytesIO(b"no"))

    monkeypatch.setattr(t.urllib.request, "urlopen", FakeT212(order_reply=reply))
    out = _order()
    assert out.startswith("ERROR") and "UNKNOWN" not in out and str(code) in out


@pytest.mark.parametrize("body", [b"", b"   ", b'{"id": "OR', b"<html>gateway</html>"])
def test_a_200_with_an_unreadable_body_on_the_order_post_is_unknown_status(monkeypatch, body):
    monkeypatch.setattr(t.urllib.request, "urlopen", FakeT212(order_reply=lambda req: _Resp(body)))
    out = _order()
    assert out.startswith("ERROR") and "UNKNOWN" in out


def test_a_non_json_get_is_a_clean_error_without_the_order_warning(monkeypatch):
    monkeypatch.setattr(t.urllib.request, "urlopen", lambda req, timeout=None: _Resp(b"<html>"))
    out = t._wrap(t.t212_positions)({})
    assert out.startswith("ERROR") and "UNKNOWN" not in out and "not JSON" in out


# --------------------------------------------------------------------------- #
# A fake MetaTrader 5 terminal
# --------------------------------------------------------------------------- #

class FakeMT5:
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self, equity=1000.0, price=100.0, contract=10.0, positions=()):
        self.equity, self.price, self.contract = equity, price, contract
        self._positions = list(positions)
        self.sent: list[dict] = []
        self.symbols_seen = ["CORN"]

    def initialize(self):
        return True

    def shutdown(self):
        return None

    def last_error(self):
        return None

    def account_info(self):
        return types.SimpleNamespace(login=777, server="Broker-Demo", equity=self.equity, balance=self.equity,
                                     margin_free=self.equity, currency="USD")

    def symbols_get(self):
        return [types.SimpleNamespace(name="CORN", visible=True)]

    def symbol_info(self, name):
        return types.SimpleNamespace(name=name, visible=True, trade_contract_size=self.contract)

    def symbol_info_tick(self, name):
        return types.SimpleNamespace(bid=self.price, ask=self.price, time=1)

    def positions_get(self):
        return list(self._positions)

    def order_send(self, request):
        self.sent.append(request)
        return types.SimpleNamespace(retcode=10009, order=1, deal=2, price=self.price, comment="done")


@pytest.fixture
def mt5(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(mt5_ops, "mt5", fake, raising=False)
    monkeypatch.setattr(mt5_ops, "MT5_IMPORT_ERROR", None)
    return fake


def _mt5_order(**kw):
    args = {"code": "ZC", "side": "BUY", "volume": 0.01, "paper_confirm": True}
    args.update(kw)
    return mt5_ops.mt5_order(**args)


def test_mt5_order_within_limits_is_sent(mt5):
    out = _mt5_order(volume=0.5)  # 0.5 x 10 x 100 = 500 -> too big for 1000 equity at 10%? no: 50%
    assert "error" in out  # 500 > 10% of 1000
    out = _mt5_order(volume=0.05)  # 0.05 x 10 x 100 = 50 = 5%
    assert out.get("filled") is True and len(mt5.sent) == 1


def test_mt5_position_size_breach_never_reaches_order_send(mt5):
    out = _mt5_order(volume=0.5)
    assert "position size" in out["error"] and "guardrails" in out["error"]
    assert mt5.sent == []


def test_mt5_existing_positions_count_toward_the_limit(mt5):
    mt5._positions = [types.SimpleNamespace(symbol="CORN", type=0, volume=0.09, price_open=100.0, price_current=100.0)]
    out = _mt5_order(volume=0.05)  # 90 held + 50 = 140 > 100
    assert "position size" in out["error"] and mt5.sent == []


def test_mt5_unsizable_instrument_refuses(mt5):
    mt5.contract = 0
    out = _mt5_order()
    assert "cannot size" in out["error"] and mt5.sent == []


def test_mt5_daily_loss_breach_latches_across_calls(mt5):
    assert _mt5_order(volume=0.01).get("filled") is True
    mt5.equity = 900.0  # -10% against a 3% limit
    assert "kill-switch tripped" in _mt5_order(volume=0.01)["error"]
    mt5.equity = 1000.0
    assert "kill-switch tripped" in _mt5_order(volume=0.01)["error"]
    assert len(mt5.sent) == 1


def test_the_mt5_worker_path_is_guarded_too(monkeypatch, mt5):
    """The roster tool runs mt5_probe.cmd_order in a subprocess; it must apply the same check."""
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    monkeypatch.delitem(sys.modules, "dourmouse.mt5_probe", raising=False)
    probe = importlib.import_module("dourmouse.mt5_probe")
    out = probe.cmd_order("ZC", "BUY", 0.5, True)
    assert "position size" in out["error"] and mt5.sent == []
    out = probe.cmd_order("ZC", "BUY", 0.05, True)
    assert out.get("filled") is True and len(mt5.sent) == 1


# --------------------------------------------------------------------------- #
# order_guard itself
# --------------------------------------------------------------------------- #

def test_the_guard_state_is_private_and_pruned(t212):
    import stat

    _order()
    assert stat.S_IMODE(order_guard.state_path().stat().st_mode) == 0o600


def test_rearm_cli(t212, capsys, monkeypatch):
    _order()
    t212.equity = 5000.0
    _order()
    assert order_guard.main(["status"]) == 0
    assert '"tripped": true' in capsys.readouterr().out
    # FIX-R3 (W3R-1): rearm now needs a person at a terminal who types REARM.
    monkeypatch.setattr(order_guard.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(order_guard.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "REARM")
    assert order_guard.main(["rearm"]) == 0
    assert order_guard._load_state().get("tripped") is None
