"""FIX-R4 (ledger W4R-1..W4R-13): queued orders, broker reply shapes, STOP sizing, key repair, atomic
check-and-send, MT5 unknown status, hedge legs, rearm output, prompt text, float and huge numbers,
the legacy state read, state freshness.

Only FAKE broker clients are used: urllib's urlopen is replaced for Trading 212, a fake MetaTrader5
module for MT5 and a fake subprocess.run for the MT5 worker. Every state file lives in the per-test temp
dirs the conftest sets (DOURMOUSE_WORKSPACE, DOURMOUSE_CONFIG_DIR). Nothing here can contact a broker.
Every refusal test also asserts the fake order endpoint was never called."""
from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import subprocess
import threading
import time
import types

import pytest

from dourmouse import config, mt5_ops, order_guard
from dourmouse import trading212_ops as t
from dourmouse.guardrails import Side


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "fake-key")
    monkeypatch.setenv("T212_ENV", "demo")
    for name in ("T212_ALLOW_LIVE", "T212_CONFIRM_LIVE", "DOURMOUSE_MAX_POSITION_PCT",
                 "DOURMOUSE_MAX_SECTOR_PCT", "DOURMOUSE_DAILY_LOSS_LIMIT_PCT",
                 "DOURMOUSE_TRADE_CONFIRM_USD"):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# Fake Trading 212 (queued orders, odd reply shapes, a slow order POST)
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
    """positions / orders may be set to any raw bytes-able reply via the *_reply attributes."""

    def __init__(self, equity=10000.0, positions=None, orders=None, account_id=None):
        self.equity = equity
        self.account_id = account_id
        self.positions = positions if positions is not None else [{"ticker": "AAPL", "quantity": 3, "currentPrice": 100.0}]
        self.orders = orders if orders is not None else []
        self.positions_reply = None  # raw bytes override
        self.orders_reply = None
        self.orders_error: int | None = None
        self.post_delay = 0.0
        self.record_posts_as_orders = False
        self.calls: list[tuple[str, str, float]] = []
        self.payloads: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, req, timeout=None):
        with self._lock:
            self.calls.append((req.get_method(), req.full_url, time.monotonic()))
        if req.get_method() == "POST":
            payload = json.loads(req.data)
            time.sleep(self.post_delay)
            with self._lock:
                self.payloads.append(payload)
                if self.record_posts_as_orders:
                    self.orders.append({"id": len(self.orders) + 1, "status": "NEW", **payload})
            return _Resp(b'{"id": "ORD-1"}')
        url = req.full_url
        if url.endswith("/account/info"):
            body = {"totalValue": self.equity}
            if self.account_id is not None:
                body["id"] = self.account_id
            return _Resp(json.dumps(body).encode())
        if url.endswith("/account/cash"):
            return _Resp(b"{}")
        if url.endswith("/equity/orders"):
            if self.orders_error:
                import urllib.error

                raise urllib.error.HTTPError(url, self.orders_error, "boom", {}, None)
            if self.orders_reply is not None:
                return _Resp(self.orders_reply)
            return _Resp(json.dumps(self.orders).encode())
        if self.positions_reply is not None:
            return _Resp(self.positions_reply)
        return _Resp(json.dumps(self.positions).encode())


@pytest.fixture
def t212(monkeypatch):
    fake = FakeT212()
    monkeypatch.setattr(t.urllib.request, "urlopen", fake)
    return fake


def _order(**kw):
    args = {"ticker": "AAPL", "quantity": 1, "paper_confirm": True}
    args.update(kw)
    return t._wrap(t.t212_order)(args)


def _trip(fake):
    assert '"ORD-1"' in _order(quantity=1)
    fake.equity = 9000.0
    assert "kill-switch tripped" in _order(quantity=1)
    fake.equity = 10000.0
    return len(fake.payloads)


# --------------------------------------------------------------------------- #
# W4R-1 / R4-1: queued orders are exposure
# --------------------------------------------------------------------------- #

def test_a_queued_market_buy_of_a_held_ticker_counts_toward_the_limit(t212):
    # 3 held (300) + a queued 6 shares (600) = 900; one more share is 1000, two are 1100 > 1000
    t212.orders = [{"id": 1, "ticker": "AAPL", "type": "MARKET", "quantity": 6, "status": "NEW"}]
    assert '"ORD-1"' in _order(quantity=1)
    out = _order(quantity=2)
    assert "position size" in out and len(t212.payloads) == 1


def test_without_the_queued_order_the_same_buy_passes(t212):
    assert '"ORD-1"' in _order(quantity=2)


def test_a_queued_limit_buy_of_an_unheld_ticker_counts_at_its_limit(t212):
    t212.orders = [{"id": 1, "ticker": "VOO", "type": "LIMIT", "quantity": 9, "limitPrice": 100.0, "status": "NEW"}]
    out = _order(ticker="VOO", quantity=2, order_type="LIMIT", limit_price=100.0)  # 900 + 200 > 1000
    assert "position size" in out and t212.payloads == []
    assert '"ORD-1"' in _order(ticker="VOO", quantity=1, order_type="LIMIT", limit_price=100.0)


def test_ten_queued_buys_cannot_stack_up_past_the_limit(t212):
    t212.record_posts_as_orders = True
    results = [_order(ticker="VOO", quantity=9, order_type="LIMIT", limit_price=100.0) for _ in range(10)]
    assert '"ORD-1"' in results[0]
    assert all("position size" in r for r in results[1:])
    assert len(t212.payloads) == 1


def test_a_queued_market_buy_with_no_price_makes_every_non_reduction_refuse(t212):
    t212.orders = [{"id": 1, "ticker": "TSLA", "type": "MARKET", "quantity": 50, "status": "NEW"}]
    out = _order(ticker="VOO", quantity=1, order_type="LIMIT", limit_price=10.0)
    assert out.startswith("ERROR") and "cannot value the pending BUY order for TSLA" in out
    assert t212.payloads == []
    assert '"ORD-1"' in _order(quantity=-1)  # a plain reduction of a held position still goes


def test_two_resting_sells_of_the_whole_holding_are_not_both_reductions(t212):
    sent = _trip(t212)
    t212.record_posts_as_orders = True
    out = _order(quantity=-3, order_type="LIMIT", limit_price=150.0)
    assert '"ORD-1"' in out and len(t212.payloads) == sent + 1
    out = _order(quantity=-3, order_type="LIMIT", limit_price=150.0)
    assert "kill-switch tripped" in out and len(t212.payloads) == sent + 1


def test_a_partial_reduction_leaves_the_rest_reducible(t212):
    sent = _trip(t212)
    t212.orders = [{"id": 1, "ticker": "AAPL", "type": "LIMIT", "quantity": -2, "limitPrice": 150.0, "status": "NEW"}]
    assert '"ORD-1"' in _order(quantity=-1)
    assert "kill-switch tripped" in _order(quantity=-2)
    assert len(t212.payloads) == sent + 1


@pytest.mark.parametrize("status", ["CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FILLED", "filled"])
def test_finished_orders_do_not_count(t212, status):
    t212.orders = [{"id": 1, "ticker": "AAPL", "type": "LIMIT", "quantity": 50, "limitPrice": 100.0, "status": status}]
    assert '"ORD-1"' in _order(quantity=2)


def test_the_filled_part_of_a_partly_filled_order_is_not_counted_twice(t212):
    t212.orders = [{"id": 1, "ticker": "AAPL", "type": "LIMIT", "quantity": 8, "filledQuantity": 6,
                    "limitPrice": 100.0, "status": "PARTIALLY_FILLED"}]
    assert '"ORD-1"' in _order(quantity=4)  # 3 held + 2 left to fill + 4 = 900


def test_a_queued_order_under_a_case_variant_ticker_counts(t212):
    t212.orders = [{"id": 1, "ticker": "VOO_US_EQ", "type": "LIMIT", "quantity": 9, "limitPrice": 100.0, "status": "NEW"}]
    out = _order(ticker="voo_us_eq", quantity=2, order_type="LIMIT", limit_price=100.0)
    assert "position size" in out and t212.payloads == []


@pytest.mark.parametrize("reply", [None, b"", b"null", b"{}", b'{"message": "gateway"}', b'{"items": null}', b'"x"', b"7"])
def test_an_unreadable_order_list_refuses_non_reductions_but_not_a_reduction(t212, reply):
    t212.orders_reply = reply if reply is not None else b""
    out = _order(quantity=1)
    assert out.startswith("ERROR") and "pending orders could not be read" in out
    assert t212.payloads == []
    assert '"ORD-1"' in _order(quantity=-1)


def test_a_failing_order_list_call_refuses_non_reductions(t212):
    t212.orders_error = 500
    out = _order(quantity=1)
    assert "pending orders could not be read" in out and t212.payloads == []


def test_an_order_list_object_with_an_items_list_is_read(t212):
    t212.orders_reply = json.dumps({"items": [{"ticker": "AAPL", "type": "LIMIT", "quantity": 9,
                                                "limitPrice": 100.0, "status": "NEW"}]}).encode()
    assert "position size" in _order(quantity=1)


def test_a_value_based_queued_order_counts_its_value(t212):
    t212.orders = [{"id": 1, "ticker": "VOO", "type": "MARKET", "value": 950.0, "side": "BUY", "status": "NEW"}]
    out = _order(ticker="VOO", quantity=1, order_type="LIMIT", limit_price=100.0)
    assert "position size" in out and t212.payloads == []


# --------------------------------------------------------------------------- #
# W4R-2 / R4-2: a positions reply that is not a list is an error, never "flat"
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reply", [b"", b"null", b"{}", b'{"message": "gateway timeout"}', b'{"items": null}',
                                   b'{"items": "none"}', b'{"positions": {"a": 1}}', b'"ok"', b"12"])
def test_a_positions_reply_without_a_list_is_an_error_not_no_positions(t212, reply):
    t212.positions_reply = reply
    for qty in (1, -1):
        out = _order(quantity=qty)
        assert out.startswith("ERROR") and "positions" in out and "not a list" in out
    assert t212.payloads == []


@pytest.mark.parametrize("reply", [b"[]", b'{"items": []}', b'{"positions": []}'])
def test_an_empty_list_is_the_only_flat_portfolio(t212, reply):
    t212.positions_reply = reply
    assert '"ORD-1"' in _order(quantity=1, order_type="LIMIT", limit_price=100.0)


# --------------------------------------------------------------------------- #
# W4R-3 / R4-3: a STOP order is sized like a market order
# --------------------------------------------------------------------------- #

def test_a_buy_stop_for_an_unheld_ticker_is_refused(t212):
    out = _order(ticker="TSLA", quantity=1000, order_type="STOP", stop_price=1.0)
    assert out.startswith("ERROR") and "STOP_LIMIT" in out and t212.payloads == []


def test_a_buy_stop_for_a_held_ticker_is_sized_at_the_market_price(t212):
    out = _order(quantity=9, order_type="STOP", stop_price=1.0)  # (3 + 9) x 100 = 1200 > 1000
    assert "position size" in out and t212.payloads == []
    assert '"ORD-1"' in _order(quantity=2, order_type="STOP", stop_price=1.0)


def test_a_stop_above_the_market_is_sized_at_the_stop(t212):
    out = _order(quantity=2, order_type="STOP", stop_price=400.0)  # (3 + 2) x 400
    assert "position size" in out and t212.payloads == []


def test_stop_limit_and_a_stop_that_sells_a_held_position_still_work(t212):
    assert '"ORD-1"' in _order(ticker="VOO", quantity=1, order_type="STOP_LIMIT", limit_price=100.0, stop_price=90.0)
    assert '"ORD-1"' in _order(quantity=-3, order_type="STOP", stop_price=80.0)


# --------------------------------------------------------------------------- #
# W4R-4 / R4-4: rearm repairs a damaged or empty key
# --------------------------------------------------------------------------- #

def _terminal(monkeypatch, answer="REARM"):
    monkeypatch.setattr(order_guard.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(order_guard.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)


@pytest.mark.parametrize("content", [b"", b"abc\n", b"zz" * 40, b"00" * 8 + b"\n"])
def test_rearm_repairs_an_empty_or_damaged_key_file(t212, monkeypatch, capsys, content):
    assert '"ORD-1"' in _order()
    key = order_guard._key_path()
    key.write_bytes(content)
    out = _order()
    assert "damaged" in out and len(t212.payloads) == 1
    _terminal(monkeypatch)
    assert order_guard.main(["rearm"]) == 0
    printed = capsys.readouterr().out
    assert str(order_guard.state_path().parent) in printed and "moved aside" in printed
    assert key.read_bytes() != content and len(bytes.fromhex(key.read_text().strip())) == 32
    assert list(key.parent.glob("order_guard.key.bad-*"))
    assert '"ORD-1"' in _order()


def test_rearm_python_api_repairs_an_empty_key_without_a_traceback(t212):
    _order()
    order_guard._key_path().write_bytes(b"")
    notes = order_guard.rearm()
    assert any("key was unusable" in n for n in notes)
    assert order_guard._load_state().get("tripped") is None


def test_a_half_written_key_never_exists_under_its_final_name(monkeypatch):
    path = order_guard._key_path()

    def boom(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(order_guard.os, "fsync", boom)
    with pytest.raises(order_guard.OrderRefused):
        order_guard._read_key(create=True)
    assert not path.exists()
    assert not list(path.parent.glob(".order_guard.key.*"))


def test_the_key_is_never_replaced_when_it_is_fine(t212):
    _order()
    before = order_guard._key_path().read_bytes()
    order_guard.rearm()
    assert order_guard._key_path().read_bytes() == before


# --------------------------------------------------------------------------- #
# W4R-5 / R4-5: check and send are one critical section
# --------------------------------------------------------------------------- #

def test_two_threads_never_check_against_the_same_snapshot(t212):
    t212.record_posts_as_orders = True
    t212.post_delay = 0.4
    results: list[str] = []

    def go():
        results.append(_order(ticker="VOO", quantity=9, order_type="LIMIT", limit_price=100.0))

    threads = [threading.Thread(target=go) for _ in range(2)]
    threads[0].start()
    time.sleep(0.15)
    threads[1].start()
    for th in threads:
        th.join(30)
    assert len(t212.payloads) == 1
    assert sum('"ORD-1"' in r for r in results) == 1 and sum("position size" in r for r in results) == 1
    # the second order's account reads came after the first order's POST
    first_post = next(c[2] for c in t212.calls if c[0] == "POST")
    later_gets = [c for c in t212.calls if c[0] == "GET" and c[2] > first_post]
    assert later_gets


def test_without_the_lock_the_same_two_threads_would_both_be_sent(t212, monkeypatch):
    """Proof the test above can see the defect: with the lock turned off both orders go out."""
    import contextlib

    monkeypatch.setattr(order_guard, "order_section", lambda timeout=None: contextlib.nullcontext())
    t212.record_posts_as_orders = True
    t212.post_delay = 0.4
    threads = [threading.Thread(target=lambda: _order(ticker="VOO", quantity=9, order_type="LIMIT", limit_price=100.0))
               for _ in range(2)]
    threads[0].start()
    time.sleep(0.15)
    threads[1].start()
    for th in threads:
        th.join(30)
    assert len(t212.payloads) == 2


def test_an_order_waiting_too_long_for_the_lock_is_refused_and_sends_nothing(t212, monkeypatch):
    monkeypatch.setattr(order_guard, "ORDER_LOCK_WAIT_SECONDS", 0.2)
    held, release = threading.Event(), threading.Event()

    def holder():
        with order_guard.order_section():
            held.set()
            release.wait(10)

    th = threading.Thread(target=holder)
    th.start()
    assert held.wait(5)
    try:
        out = _order(quantity=1)
    finally:
        release.set()
        th.join(10)
    assert out.startswith("ERROR") and "another order" in out and t212.payloads == []
    assert '"ORD-1"' in _order(quantity=1)


def _hold_section(cfg, ws, started, release):
    os.environ["DOURMOUSE_CONFIG_DIR"] = cfg
    os.environ["DOURMOUSE_WORKSPACE"] = ws
    with order_guard.order_section():
        started.set()
        release.wait(20)


def test_the_order_lock_works_across_processes():
    ctx = multiprocessing.get_context("spawn")
    started, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_section, args=(str(config.user_config_dir()), str(config.workspace_dir()), started, release))
    proc.start()
    try:
        assert started.wait(30)
        with pytest.raises(order_guard.OrderRefused, match="another order"), order_guard.order_section(timeout=0.3):
            pass
    finally:
        release.set()
        proc.join(30)
    with order_guard.order_section(timeout=5):
        pass


# --------------------------------------------------------------------------- #
# Fake MetaTrader 5 (pending orders, slow sends)
# --------------------------------------------------------------------------- #

class FakeMT5:
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self, equity=1000.0, price=100.0, contract=10.0, positions=(), margin_mode=2, orders=()):
        self.equity, self.price, self.contract = equity, price, contract
        self.margin_mode = margin_mode
        self._positions = list(positions)
        self._orders = list(orders)
        self.orders_unreadable = False
        self.sent: list[dict] = []
        self.send_delay = 0.0
        self.send_result = None
        self.send_raises: Exception | None = None
        self.inside = 0
        self.max_inside = 0
        self.guard = threading.Lock()

    def initialize(self):
        return True

    def shutdown(self):
        return None

    def last_error(self):
        return None

    def account_info(self):
        info = types.SimpleNamespace(login=777, server="Broker-Demo", equity=self.equity, balance=self.equity,
                                     margin_free=self.equity, currency="USD", trade_mode=0)  # 0 = demo (FI1 P3-41)
        if self.margin_mode is not None:
            info.margin_mode = self.margin_mode
        return info

    def symbol_info(self, name):
        if name not in ("CORN",):
            return None
        return types.SimpleNamespace(name=name, visible=True, trade_contract_size=self.contract)

    def symbol_info_tick(self, name):
        return types.SimpleNamespace(bid=self.price, ask=self.price, time=1)

    def positions_get(self):
        with self.guard:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
        return list(self._positions)

    def orders_get(self):
        if self.orders_unreadable:
            return None
        return list(self._orders)

    def order_send(self, request):
        try:
            time.sleep(self.send_delay)
            if self.send_raises:
                raise self.send_raises
            self.sent.append(request)
            if self.send_result is not None:
                return self.send_result
            return types.SimpleNamespace(retcode=10009, order=1, deal=2, price=request["price"], comment="done")
        finally:
            with self.guard:
                self.inside -= 1


def _pos(ticket, side, volume, price=100.0):
    return types.SimpleNamespace(ticket=ticket, symbol="CORN", type=0 if side == "BUY" else 1,
                                 volume=volume, price_open=price, price_current=price)


def _pending(kind, volume, price=100.0, position_id=0):
    return types.SimpleNamespace(ticket=900 + kind, symbol="CORN", type=kind, volume_current=volume,
                                 volume_initial=volume, price_open=price, position_id=position_id)


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


# --------------------------------------------------------------------------- #
# W4R-1 on MT5: pending orders count
# --------------------------------------------------------------------------- #

def test_mt5_a_pending_buy_limit_counts_as_exposure(mt5):
    mt5._orders = [_pending(2, 0.09)]  # 90 queued
    out = _mt5_order(volume=0.05)  # 90 + 50 > 100
    assert "position size" in out["error"] and mt5.sent == []
    assert _mt5_order(volume=0.01).get("filled") is True


def test_mt5_a_pending_order_on_a_hedging_account_adds_gross_exposure(mt5):
    mt5._positions = [_pos(11, "BUY", 0.05)]
    mt5._orders = [_pending(3, 0.05)]  # a pending SELL_LIMIT may open its own short
    out = _mt5_order(volume=0.02)  # 50 + 50 + 20 > 100
    assert "position size" in out["error"] and mt5.sent == []


@pytest.mark.parametrize("how", ["none", "missing"])
def test_mt5_an_unreadable_pending_list_refuses_non_reductions(mt5, monkeypatch, how):
    if how == "none":
        mt5.orders_unreadable = True
    else:
        monkeypatch.delattr(FakeMT5, "orders_get")
    out = _mt5_order(volume=0.01)
    assert "pending orders" in out["error"] or "pending order" in out["error"]
    assert mt5.sent == []


def test_mt5_an_unreadable_pending_list_still_lets_a_close_by_ticket_go(mt5):
    mt5._positions = [_pos(11, "BUY", 0.05)]
    mt5.orders_unreadable = True
    assert _mt5_order(side="SELL", volume=0.05, position=11).get("filled") is True


def test_mt5_netting_a_resting_sell_uses_up_what_a_sell_may_reduce(mt5):
    mt5.margin_mode = 0
    mt5.contract = 1.0
    mt5._positions = [_pos(11, "BUY", 0.1)]
    assert _mt5_order(volume=0.01).get("filled") is True  # records the 1000 baseline
    mt5.equity = 900.0
    mt5._orders = [_pending(3, 0.1)]  # a SELL_LIMIT of the whole long is already resting
    mt5.sent.clear()
    out = _mt5_order(side="SELL", volume=0.1)
    assert "kill-switch tripped" in out["error"] and mt5.sent == []


def test_mt5_a_ticket_close_is_not_a_reduction_when_a_pending_order_already_targets_it(mt5):
    mt5.margin_mode = 0
    mt5.contract = 1.0
    mt5._positions = [_pos(11, "BUY", 0.1)]
    assert _mt5_order(volume=0.01).get("filled") is True
    mt5.equity = 900.0
    mt5._orders = [_pending(3, 0.1, position_id=11)]
    mt5.sent.clear()
    assert "kill-switch tripped" in _mt5_order(side="SELL", volume=0.1, position=11)["error"]
    mt5._orders = []
    assert _mt5_order(side="SELL", volume=0.1, position=11).get("filled") is True


# --------------------------------------------------------------------------- #
# W4R-5 on MT5: the check and the send do not overlap
# --------------------------------------------------------------------------- #

def test_mt5_two_threads_never_overlap_between_check_and_send(mt5):
    mt5.send_delay = 0.2
    results = []
    threads = [threading.Thread(target=lambda: results.append(_mt5_order(volume=0.01))) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert len(mt5.sent) == 3 and mt5.max_inside == 1


# --------------------------------------------------------------------------- #
# W4R-6 / R4-6: an MT5 order whose outcome is unknown is not reported as a refusal
# --------------------------------------------------------------------------- #

def _fake_run(monkeypatch, behaviour):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["timeout"] = cmd, kw.get("timeout")
        return behaviour(cmd, kw)

    monkeypatch.setattr(mt5_ops.subprocess, "run", run)
    return seen


def _tool(**kw):
    args = {"code": "ZC", "side": "BUY", "volume": 0.01, "paper_confirm": True}
    args.update(kw)
    return mt5_ops._mt5_order_tool(args)


def test_a_worker_timeout_on_an_order_reads_as_status_unknown(monkeypatch):
    def timeout(cmd, kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    seen = _fake_run(monkeypatch, timeout)
    out = _tool()
    assert "STATUS UNKNOWN" in out and "REFUSED" not in out and "before retrying" in out
    assert "positions" in out and seen["timeout"] > 12


def test_a_worker_crash_or_garbage_on_an_order_reads_as_status_unknown(monkeypatch):
    _fake_run(monkeypatch, lambda cmd, kw: types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback"))
    assert "STATUS UNKNOWN" in _tool()
    _fake_run(monkeypatch, lambda cmd, kw: types.SimpleNamespace(returncode=0, stdout="not json", stderr=""))
    assert "STATUS UNKNOWN" in _tool()


def test_a_clean_refusal_from_the_worker_is_still_a_refusal(monkeypatch):
    _fake_run(monkeypatch, lambda cmd, kw: types.SimpleNamespace(
        returncode=0, stdout=json.dumps({"error": "order not placed: nope"}), stderr=""))
    out = _tool()
    assert "REFUSED" in out and "UNKNOWN" not in out


def test_a_read_only_worker_timeout_keeps_its_old_message(monkeypatch):
    def timeout(cmd, kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    seen = _fake_run(monkeypatch, timeout)
    out = mt5_ops._mt5_status_tool({})
    assert "NOT CONFIGURED" in out and seen["timeout"] == 12.0


def test_in_process_order_send_failures_are_unknown_not_refused(mt5):
    mt5.send_result = types.SimpleNamespace(retcode=10012, order=0, deal=0, price=0, comment="timeout")
    out = _mt5_order()
    assert out.get("status_unknown") is True and "UNKNOWN" in out["error"]
    mt5.send_result = None
    mt5.send_raises = RuntimeError("terminal gone")
    assert _mt5_order().get("status_unknown") is True


def test_a_definite_rejection_stays_a_rejection(mt5):
    mt5.send_result = types.SimpleNamespace(retcode=10006, order=0, deal=0, price=0, comment="rejected")
    out = _mt5_order()
    assert out["error"].startswith("order rejected") and "status_unknown" not in out


# --------------------------------------------------------------------------- #
# W4R-7 / R4-7: closing one leg of a hedge is not a reduction
# --------------------------------------------------------------------------- #

def _trip_mt5(mt5):
    assert _mt5_order(volume=0.01).get("filled") is True
    mt5.equity = 900.0
    assert "kill-switch tripped" in _mt5_order(volume=0.01)["error"]
    mt5.sent.clear()


def test_closing_the_long_leg_of_a_flat_hedge_is_refused_while_tripped(mt5):
    mt5.contract = 1.0
    mt5._positions = [_pos(11, "BUY", 0.3), _pos(12, "SELL", 0.3)]
    _trip_mt5(mt5)
    out = _mt5_order(side="SELL", volume=0.3, position=11)
    assert "kill-switch tripped" in out["error"] and mt5.sent == []


def test_closing_the_larger_leg_of_an_uneven_hedge_to_flat_is_allowed(mt5):
    mt5.contract = 1.0
    mt5._positions = [_pos(11, "BUY", 0.5), _pos(12, "SELL", 0.2)]
    _trip_mt5(mt5)
    # net +0.3: closing 0.3 of the long leaves net 0.0, which is smaller, so it only reduces
    assert _mt5_order(side="SELL", volume=0.3, position=11).get("filled") is True
    # closing the short leg instead makes the net larger
    mt5.sent.clear()
    out = _mt5_order(side="BUY", volume=0.2, position=12)
    assert "kill-switch tripped" in out["error"] and mt5.sent == []


def test_a_single_position_ticket_close_still_reduces(mt5):
    mt5.contract = 1.0
    mt5._positions = [_pos(11, "BUY", 0.3)]
    _trip_mt5(mt5)
    assert _mt5_order(side="SELL", volume=0.3, position=11).get("filled") is True


# --------------------------------------------------------------------------- #
# W4R-8 / R4-8 and W4R-13 / R4-13: rearm and status say what they did and where
# --------------------------------------------------------------------------- #

def test_rearm_with_a_valid_state_keeps_the_baseline_and_says_it_trips_again(t212, monkeypatch, capsys):
    _trip(t212)
    _terminal(monkeypatch)
    t212.equity = 9000.0
    assert order_guard.main(["rearm"]) == 0
    printed = capsys.readouterr().out
    assert str(order_guard.state_path().parent) in printed and "trips the switch again" in printed
    assert "kill-switch tripped" in _order(quantity=1)  # the same loss still stands


def test_rearm_reset_baseline_is_explicit_and_audited(t212, monkeypatch, capsys):
    _trip(t212)
    t212.equity = 9000.0
    _terminal(monkeypatch)
    assert order_guard.main(["rearm", "--reset-baseline"]) == 0
    assert "baseline was reset" in capsys.readouterr().out
    state = order_guard._load_state()
    assert state.get("tripped") is None and any("reset-baseline" in a["what"] for a in state["audit"])
    assert '"ORD-1"' in _order(quantity=1)  # the baseline restarts from 9000


def test_rearm_over_a_missing_state_says_the_baseline_was_lost_and_audits_it(t212, capsys, monkeypatch):
    _trip(t212)
    order_guard.state_path().unlink()
    _terminal(monkeypatch)
    assert order_guard.main(["rearm"]) == 0
    printed = capsys.readouterr().out
    assert "baseline was lost" in printed
    assert any("baseline was lost" in a["what"] for a in order_guard._load_state()["audit"])


def test_status_prints_the_state_path(t212, capsys):
    _order()
    assert order_guard.main(["status"]) == 0
    assert str(order_guard.state_path()) in capsys.readouterr().out
    order_guard.state_path().write_text("{broken", encoding="utf-8")
    assert order_guard.main(["status"]) == 1
    assert str(order_guard.state_path()) in capsys.readouterr().out


def test_rearm_still_needs_a_person_and_the_word(t212, capsys, monkeypatch):
    _trip(t212)
    assert order_guard.main(["rearm", "--reset-baseline"]) == 1
    _terminal(monkeypatch, "no")
    assert order_guard.main(["rearm"]) == 1
    assert order_guard._load_state()["tripped"] is True


# --------------------------------------------------------------------------- #
# W4R-9 / R4-9: the confirmation text says what will be placed
# --------------------------------------------------------------------------- #

def test_the_confirmation_text_names_environment_side_prices(monkeypatch):
    text = t._confirm_prompt({"ticker": "AAPL", "quantity": -5, "order_type": "stop_limit",
                              "limit_price": 120.5, "stop_price": 119})
    assert "DEMO" in text and "SELL 5 shares of AAPL" in text and "limit 120.5" in text and "stop 119" in text
    monkeypatch.setenv("T212_ENV", "live")
    assert "LIVE" in t._confirm_prompt({"ticker": "X", "quantity": 1})


def test_two_orders_differing_only_in_price_have_different_prompts():
    a = t._confirm_prompt({"ticker": "AAPL", "quantity": 10, "order_type": "LIMIT", "limit_price": 100})
    b = t._confirm_prompt({"ticker": "AAPL", "quantity": 10, "order_type": "LIMIT", "limit_price": 300})
    assert a != b


@pytest.mark.parametrize("qty", ["lots", None, [1], 10 ** 400, float("nan")])
def test_a_garbage_quantity_never_breaks_the_prompt(qty):
    assert "AAPL" in t._confirm_prompt({"ticker": "AAPL", "quantity": qty})


def test_the_tool_spec_uses_the_new_prompt():
    spec = next(s for s in t.build_t212_tool_specs() if s.name == "t212_order")
    assert spec.confirm_prompt({"ticker": "A", "quantity": 1, "limit_price": 5.5, "order_type": "LIMIT"}).count("limit 5.5") == 1


# --------------------------------------------------------------------------- #
# W4R-10 / R4-10: floats and huge integers
# --------------------------------------------------------------------------- #

def test_a_full_close_of_a_float_sum_is_a_reduction():
    held = 0.1 + 0.7
    assert held != 0.8
    assert order_guard.is_pure_reduction(Side.SELL, 0.8, held)
    assert order_guard.is_pure_reduction(Side.BUY, 0.8, -held)
    assert not order_guard.is_pure_reduction(Side.SELL, 0.8000001, held)
    assert not order_guard.is_pure_reduction(Side.SELL, 0.9, held)


def test_mt5_netting_full_close_of_summed_lots_passes_while_tripped(mt5):
    mt5.margin_mode = 0
    mt5.contract = 1.0
    mt5._positions = [_pos(11, "BUY", 0.1), _pos(12, "BUY", 0.7)]
    _trip_mt5(mt5)
    assert _mt5_order(side="SELL", volume=0.8).get("filled") is True


def test_t212_duplicate_lines_that_sum_to_a_float_close_while_tripped(t212):
    t212.positions = [{"ticker": "AAPL", "quantity": 0.1, "currentPrice": 100.0},
                      {"ticker": "AAPL", "quantity": 0.7, "currentPrice": 100.0}]
    sent = _trip(t212)
    assert '"ORD-1"' in _order(quantity=-0.8)
    assert len(t212.payloads) == sent + 1


@pytest.mark.parametrize("field", ["quantity", "limit_price", "stop_price"])
def test_t212_huge_integers_are_refused_not_a_crash(t212, field):
    kw = {"quantity": 1, "order_type": "STOP_LIMIT", "limit_price": 5, "stop_price": 4}
    kw[field] = 10 ** 400
    out = _order(**kw)
    assert out.startswith("ERROR") and "number" in out and t212.payloads == []
    with pytest.raises(order_guard.OrderRefused):
        t.t212_order("AAPL", **{**kw, "quantity": kw.get("quantity")}, paper_confirm=True)


def test_a_huge_integer_in_a_broker_reply_is_an_unreadable_value_not_a_crash(t212, mt5):
    t212.positions = [{"ticker": "AAPL", "quantity": 10 ** 400, "currentPrice": 100.0}]
    out = _order(quantity=1)
    assert out.startswith("ERROR") and "cannot read the position" in out and t212.payloads == []
    mt5.symbol_info_tick = lambda name: types.SimpleNamespace(bid=10 ** 400, ask=10 ** 400, time=1)
    assert "no valid price" in _mt5_order()["error"] and mt5.sent == []
    mt5.symbol_info_tick = lambda name: types.SimpleNamespace(bid=100.0, ask=100.0, time=1)
    mt5.contract = 10 ** 400
    assert "cannot size" in _mt5_order()["error"] and mt5.sent == []


def test_mt5_huge_integers_are_refused_not_a_crash(mt5, monkeypatch):
    assert "volume must be a number" in _mt5_order(volume=10 ** 400)["error"]
    assert mt5_ops._mt5_order_tool({"code": "ZC", "side": "BUY", "volume": 10 ** 400, "paper_confirm": True}
                                   ).startswith("MT5 ORDER: volume must be a number")
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    monkeypatch.delitem(sys.modules, "dourmouse.mt5_probe", raising=False)
    probe = importlib.import_module("dourmouse.mt5_probe")
    assert "volume must be a number" in probe.cmd_order("ZC", "BUY", 10 ** 400, True)["error"]
    assert mt5.sent == []


def test_the_guard_itself_refuses_huge_integers():
    with pytest.raises(order_guard.OrderRefused):
        order_guard._finite(10 ** 400, "x")
    assert order_guard.is_pure_reduction(Side.SELL, 10 ** 400, 5) is False
    with pytest.raises(order_guard.OrderRefused):
        order_guard.enforce("a", "X", Side.BUY, 10 ** 400, 1, 1000.0, {})


# --------------------------------------------------------------------------- #
# W4R-11 / R4-11: the legacy state path is read safely
# --------------------------------------------------------------------------- #

def _legacy():
    path = config.workspace_dir() / "order_guard_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _run_with_timeout(fn, seconds=8):
    box: dict = {}

    def go():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- the test inspects it
            box["error"] = exc

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(seconds)
    return th, box


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_at_the_legacy_path_is_refused_without_blocking(t212):
    path = _legacy()
    os.mkfifo(path)
    try:
        th, box = _run_with_timeout(lambda: _order(quantity=1))
        assert not th.is_alive(), "the legacy read blocked on a FIFO"
        assert "kill-switch tripped" in box["value"] or "could not be read" in box["value"]
        assert t212.payloads == []
    finally:
        with contextlib.suppress(OSError):  # no reader left: nothing to unblock
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))


def test_an_oversized_legacy_file_is_refused_not_read(t212):
    _legacy().write_bytes(b" " * (200 * 1024) + b'{"tripped": false}')
    assert "could not be read" in order_guard._legacy_state()["tripped_reason"]


def test_a_symlink_at_the_legacy_path_is_refused(t212, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    link = _legacy()
    link.symlink_to(target)
    assert order_guard._legacy_state().get("tripped") is True


def test_a_small_regular_legacy_file_still_works(t212):
    _legacy().write_text(json.dumps({"tripped": False, "day": {}}), encoding="utf-8")
    assert order_guard._legacy_state() == {}
    assert '"ORD-1"' in _order()
    _legacy().write_text(json.dumps({"tripped": True, "tripped_reason": "old"}), encoding="utf-8")
    state_before = order_guard.state_path().exists()
    assert state_before  # the real state exists now, so the legacy file is no longer read at all
    assert '"ORD-1"' in _order()


def test_non_utf8_legacy_bytes_count_as_latched(t212):
    _legacy().write_bytes(b"\xff\xfe\x00bad")
    assert order_guard._legacy_state().get("tripped") is True


# --------------------------------------------------------------------------- #
# W4R-12 / R4-12: freshness counter and the T212 account id
# --------------------------------------------------------------------------- #

def test_the_signed_state_carries_a_growing_counter_and_a_creation_time(t212):
    _order()
    first = order_guard._load_state()
    assert first["_seq"] >= 1 and first["_created"]
    assert order_guard._seq_path().read_text().strip() == str(first["_seq"])
    t212.equity = 10001.0
    for _ in range(2):  # a new account changes the baselines, so the state is saved again
        order_guard.rearm()
    assert order_guard._load_state()["_seq"] > first["_seq"]
    assert order_guard._load_state()["_created"] == first["_created"]


def test_an_older_signed_copy_of_the_state_is_refused(t212):
    sent = _trip(t212)
    older = order_guard.state_path().read_text(encoding="utf-8")
    assert json.loads(older)["tripped"] is True
    order_guard.rearm()  # newer state, tripped cleared
    assert '"ORD-1"' in _order()
    # put back the old copy that still holds a different (older) counter
    stale = json.loads(older)
    stale.pop("tripped")
    assert stale["_seq"] < order_guard._read_seq()
    order_guard.state_path().write_text(older, encoding="utf-8")
    out = _order(quantity=1)
    assert "older copy" in out and len(t212.payloads) == sent + 1


def test_a_state_written_before_the_counter_existed_still_loads(t212):
    _order()
    path = order_guard.state_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    for name in ("_seq", "_created", "_mac"):
        data.pop(name, None)
    data["_mac"] = order_guard._mac(order_guard._read_key(create=False), data)
    path.write_text(json.dumps(data), encoding="utf-8")
    order_guard._seq_path().unlink()
    assert '"ORD-1"' in _order()


def test_the_t212_baseline_is_kept_per_account_id(t212):
    t212.account_id = 4242
    assert '"ORD-1"' in _order()
    day = next(iter(order_guard._load_state()["day"].values()))
    assert set(day) == {"t212:demo:4242"}
    # another account on the same environment is not judged against the first one's baseline
    t212.account_id, t212.equity = 777, 5000.0
    assert '"ORD-1"' in _order()
    day = next(iter(order_guard._load_state()["day"].values()))
    assert day["t212:demo:777"] == 5000.0 and day["t212:demo:4242"] == 10000.0


def test_the_guard_files_are_all_protected_paths():
    names = {p.name for p in order_guard.protected_paths()}
    assert {"order_guard.send.lock", "order_guard.seq", "order_guard.key", "order_guard_state.json"} <= names


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_reducible_quantity_and_worst_case_value():
    assert order_guard.reducible_quantity(3, 0, 2) == 1
    assert order_guard.reducible_quantity(3, 0, 5) == 0
    assert order_guard.reducible_quantity(-3, 2, 0) == -1
    assert order_guard.reducible_quantity(0, 5, 5) == 0
    assert order_guard.worst_case_value(300, 600, 0) == 900
    assert order_guard.worst_case_value(0, 0, 500) == -500
    # a SELL of 300 on top of a queued SELL of 300 is judged against the "sells fill" case, not the "cancelled" one
    assert order_guard.worst_case_value(300, 0, 300, trade_value=-300) == 0.0
    assert order_guard.worst_case_value(300, 0, 300, trade_value=300) == 300
