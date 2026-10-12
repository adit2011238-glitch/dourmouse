"""FIX-R3 (ledger W3R-1..W3R-6): the order guard's state, risk-reducing orders, MARKET sizing,
hedging accounts, ticker normalisation and cross-process locking.

Only FAKE broker clients are used: urllib's urlopen is replaced for Trading 212 and a fake
MetaTrader5 module for MT5, and every state file lives in the per-test temp dirs the conftest
sets (DOURMOUSE_WORKSPACE, DOURMOUSE_CONFIG_DIR). Nothing here can contact a broker. Every
refusal test also asserts the fake order endpoint was never called."""
from __future__ import annotations

import json
import math
import multiprocessing
import os
import stat
import time
import types
from pathlib import Path

import pytest

from dourmouse import config, mt5_ops, order_guard
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
# Fake Trading 212
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
    def __init__(self, equity=10000.0, positions=None):
        self.equity = equity
        self.positions = positions if positions is not None else [{"ticker": "AAPL", "quantity": 3, "currentPrice": 100.0}]
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict] = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url))
        if req.get_method() == "POST":
            self.payloads.append(json.loads(req.data))
            return _Resp(b'{"id": "ORD-1"}')
        if req.full_url.endswith("/account/info"):
            return _Resp(json.dumps({"totalValue": self.equity}).encode())
        if req.full_url.endswith("/account/cash"):
            return _Resp(b"{}")
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
    """Record a 10000 baseline, then drop equity 10% so the kill switch latches."""
    assert '"ORD-1"' in _order(quantity=1)
    fake.equity = 9000.0
    assert "kill-switch tripped" in _order(quantity=1)
    sent = len(fake.payloads)
    return sent


# --------------------------------------------------------------------------- #
# W3R-1: the state file is out of the model's reach and integrity-checked
# --------------------------------------------------------------------------- #

def test_state_lives_in_the_settings_folder_not_the_workspace():
    state = order_guard.state_path().resolve()
    assert config.workspace_dir().resolve() not in state.parents
    assert Path(config.user_config_dir()).resolve() in state.parents


def test_state_and_key_files_are_private(t212):
    _order()
    for path in (order_guard.state_path(), order_guard._key_path()):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("name", ["order_guard_state.json", "Order_Guard_State.JSON", "order_guard.key"])
def test_workspace_file_tools_refuse_the_guard_files(name):
    from dourmouse import general_roster as gr

    ws = config.workspace_dir()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / name).write_text('{"tripped": true}', encoding="utf-8")
    out = gr._write_file_tool({"path": name, "content": "{}"})
    assert "refused" in out.lower()
    assert "refused" in gr._delete_file_tool({"path": name}).lower()
    assert (ws / name).read_text(encoding="utf-8") == '{"tripped": true}'


def test_absolute_path_tools_refuse_the_guard_state(t212):
    from dourmouse import system_access as sa

    _order()
    state = order_guard.state_path()
    before = state.read_text(encoding="utf-8")
    assert sa._write_path_tool({"path": str(state), "content": "{}"}).startswith("REFUSED")
    out = sa._delete_path_tool({"path": str(state)})
    assert out.startswith("REFUSED") and state.read_text(encoding="utf-8") == before
    # a case variant names the same file on the default macOS volume
    variant = state.with_name(state.name.upper())
    assert sa._delete_path_tool({"path": str(variant)}).startswith("REFUSED")
    assert state.exists()


def test_an_edited_state_file_fails_closed(t212):
    sent = _trip(t212)
    path = order_guard.state_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("tripped")  # the re-arm a prompt-injected model would try
    path.write_text(json.dumps(data), encoding="utf-8")
    t212.equity = 10000.0
    out = _order(quantity=1)
    assert "integrity" in out and len(t212.payloads) == sent


def test_a_lowered_baseline_fails_closed(t212):
    _order()
    path = order_guard.state_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    for accounts in data["day"].values():
        for acct in accounts:
            accounts[acct] = 1.0
    path.write_text(json.dumps(data), encoding="utf-8")
    out = _order()
    assert "integrity" in out and len(t212.payloads) == 1


def test_a_deleted_state_file_does_not_forget_the_latch(t212):
    sent = _trip(t212)
    order_guard.state_path().unlink()
    t212.equity = 10000.0
    out = _order(quantity=1)
    assert "missing" in out and len(t212.payloads) == sent


def test_rearm_refuses_without_a_person_at_a_terminal(t212, capsys, monkeypatch):
    _trip(t212)
    assert order_guard.main(["rearm"]) == 1  # pytest's stdin is not a terminal
    assert order_guard._load_state()["tripped"] is True
    monkeypatch.setattr(order_guard.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(order_guard.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert order_guard.main(["rearm"]) == 1
    assert order_guard._load_state()["tripped"] is True


def test_rearm_replaces_a_tampered_file_with_a_valid_one(t212):
    _trip(t212)
    order_guard.state_path().write_text("{}", encoding="utf-8")
    order_guard.rearm()
    assert order_guard._load_state().get("tripped") is None
    t212.equity = 10000.0
    assert '"ORD-1"' in _order()


def test_a_latch_in_the_old_workspace_file_is_carried_over(t212):
    legacy = config.workspace_dir() / "order_guard_state.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"tripped": True, "day": {}}), encoding="utf-8")
    out = _order()
    assert "kill-switch tripped" in out and t212.payloads == []


# --------------------------------------------------------------------------- #
# W3R-2: risk-reducing orders stay possible, increases fail closed
# --------------------------------------------------------------------------- #

def test_tripped_switch_still_lets_a_long_be_trimmed_and_closed(t212):
    sent = _trip(t212)
    t212.equity = 10000.0  # recovered: the latch still holds for increases
    assert "kill-switch tripped" in _order(quantity=1)
    assert '"ORD-1"' in _order(quantity=-2)  # 3 held, sell 2
    assert '"ORD-1"' in _order(quantity=-3)  # close all of it
    assert len(t212.payloads) == sent + 2


def test_tripped_switch_refuses_a_sell_that_flips_through_zero(t212):
    sent = _trip(t212)
    out = _order(quantity=-4)  # 3 held: this opens a short
    assert "kill-switch tripped" in out and len(t212.payloads) == sent


def test_unvaluable_other_position_blocks_buys_but_not_a_close(t212):
    t212.positions.append({"ticker": "TSLA", "quantity": 1})  # no currentPrice
    out = _order(quantity=1)
    assert "could not read the account" in out and t212.payloads == []
    assert '"ORD-1"' in _order(quantity=-3)


@pytest.mark.parametrize("equity", ["not a number", 0, -5, float("nan")])
def test_bad_or_zero_equity_blocks_buys_but_not_a_close(t212, equity):
    t212.equity = equity
    out = _order(quantity=1)
    assert out.startswith("ERROR") and t212.payloads == []
    assert '"ORD-1"' in _order(quantity=-1)
    assert len(t212.payloads) == 1


def test_unreadable_state_blocks_buys_but_not_a_close(t212):
    path = order_guard.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert "cannot be read" in _order(quantity=1) and t212.payloads == []
    assert '"ORD-1"' in _order(quantity=-1)


def test_a_sell_of_a_ticker_not_held_is_not_a_reduction(t212):
    t212.equity = "broken"
    out = _order(ticker="MSFT", quantity=-1, order_type="LIMIT", limit_price=10.0)
    assert "could not read the account" in out and t212.payloads == []


# --------------------------------------------------------------------------- #
# W3R-3: order type validated, MARKET never sized from a model price
# --------------------------------------------------------------------------- #

def test_market_order_with_a_limit_price_is_refused(t212):
    out = _order(ticker="TSLA", quantity=5000, order_type="MARKET", limit_price=0.01)
    assert out.startswith("ERROR") and "MARKET" in out and t212.payloads == []


def test_held_ticker_is_sized_at_no_less_than_the_broker_price(t212):
    # 3 held at 100; a LIMIT buy of 20 at 0.01 must be sized at 100, not 0.01: 2300 > 1000
    out = _order(quantity=20, order_type="LIMIT", limit_price=0.01)
    assert "position size" in out and t212.payloads == []


@pytest.mark.parametrize("kwargs", [
    {"order_type": "FOK"},
    {"order_type": None},
    {"order_type": "LIMIT"},  # no limit_price
    {"order_type": "STOP", "limit_price": 5.0},
    {"order_type": "LIMIT", "limit_price": 5.0, "stop_price": 4.0},
    {"order_type": "STOP_LIMIT", "limit_price": 5.0},
    {"order_type": "LIMIT", "limit_price": "nan"},
    {"order_type": "LIMIT", "limit_price": -1},
])
def test_bad_order_type_or_price_fields_are_refused(t212, kwargs):
    out = _order(ticker="VOO", **kwargs)
    assert out.startswith("ERROR") and t212.payloads == []


def test_the_payload_is_exactly_what_the_guard_checked(t212):
    assert '"ORD-1"' in _order(ticker=" aapl ", quantity="2", order_type="limit", limit_price="101.5")
    assert t212.payloads == [{"ticker": "AAPL", "quantity": 2.0, "type": "LIMIT", "limitPrice": 101.5}]


# --------------------------------------------------------------------------- #
# Adversarial inputs
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("qty", ["nan", float("nan"), "inf", float("-inf"), True, "", "five", 0, "0", [1], None])
def test_bad_quantities_are_refused_before_anything_is_sent(t212, qty):
    out = _order(quantity=qty)
    assert out.startswith("ERROR") and t212.payloads == []


@pytest.mark.parametrize("ticker", ["", "   ", "AAPL; DROP", "../x", "A" * 80, 42, None])
def test_bad_tickers_are_refused(t212, ticker):
    out = _order(ticker=ticker)
    assert out.startswith("ERROR") and t212.payloads == []


def test_a_case_variant_ticker_counts_the_held_position(t212):
    # 3 held + 9 = 1200 > 1000: the variant must not hide the held 300
    out = _order(ticker="aapl", quantity=9, order_type="LIMIT", limit_price=100.0)
    assert "position size" in out and t212.payloads == []


def test_string_numbers_are_read_not_concatenated(t212):
    assert '"ORD-1"' in _order(quantity="1e0")
    assert t212.payloads[-1]["quantity"] == 1.0


# --------------------------------------------------------------------------- #
# Fake MetaTrader 5 (W3R-2, W3R-4, W3R-5)
# --------------------------------------------------------------------------- #

class FakeMT5:
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self, equity=1000.0, price=100.0, contract=10.0, positions=(), margin_mode=2):
        self.equity, self.price, self.contract = equity, price, contract
        self.margin_mode = margin_mode
        self._positions = list(positions)
        self.sent: list[dict] = []
        self.tick_calls = 0
        self.tick_none = False

    def initialize(self):
        return True

    def shutdown(self):
        return None

    def last_error(self):
        return None

    def account_info(self):
        info = types.SimpleNamespace(login=777, server="Broker-Demo", equity=self.equity, balance=self.equity,
                                     margin_free=self.equity, currency="USD")
        if self.margin_mode is not None:
            info.margin_mode = self.margin_mode
        return info

    def symbol_info(self, name):
        if name not in ("CORN",):
            return None
        return types.SimpleNamespace(name=name, visible=True, trade_contract_size=self.contract)

    def symbol_info_tick(self, name):
        self.tick_calls += 1
        if self.tick_none:
            return None
        px = self.price + (self.tick_calls - 1) * 0.5  # every read is a new tick
        return types.SimpleNamespace(bid=px, ask=px, time=1)

    def positions_get(self):
        return list(self._positions)

    def order_send(self, request):
        self.sent.append(request)
        return types.SimpleNamespace(retcode=10009, order=1, deal=2, price=request["price"], comment="done")


def _pos(ticket, side, volume, price=100.0):
    return types.SimpleNamespace(ticket=ticket, symbol="CORN", type=0 if side == "BUY" else 1,
                                 volume=volume, price_open=price, price_current=price)


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


def test_hedging_sell_without_a_ticket_counts_as_new_exposure(mt5):
    mt5._positions = [_pos(11, "BUY", 0.1)]  # 0.1 x 10 x 100 = 100 = exactly the 10% limit
    out = _mt5_order(side="SELL", volume=0.1)
    assert "position size" in out["error"] and mt5.sent == []


def test_hedging_sell_with_the_long_ticket_closes_it(mt5):
    mt5._positions = [_pos(11, "BUY", 0.1)]
    out = _mt5_order(side="SELL", volume=0.1, position=11)
    assert out.get("filled") is True and mt5.sent[0]["position"] == 11


@pytest.mark.parametrize("kw", [
    {"side": "BUY", "volume": 0.05, "position": 11},  # same side as the long
    {"side": "SELL", "volume": 0.2, "position": 11},  # more than the position
    {"side": "SELL", "volume": 0.05, "position": 99},  # no such ticket
    {"side": "SELL", "volume": 0.05, "position": "x"},
    {"side": "SELL", "volume": 0.05, "position": True},
])
def test_ticket_orders_must_close_part_of_that_position(mt5, kw):
    mt5._positions = [_pos(11, "BUY", 0.1)]
    out = _mt5_order(**kw)
    assert "error" in out and mt5.sent == []


def test_netting_sell_of_a_long_is_a_reduction(mt5):
    mt5.margin_mode = 0
    mt5._positions = [_pos(11, "BUY", 0.1)]
    assert _mt5_order(side="SELL", volume=0.1).get("filled") is True
    mt5.sent.clear()
    assert "position size" in _mt5_order(side="SELL", volume=0.3)["error"]  # flips to a 200 short
    assert mt5.sent == []


def test_unknown_margin_mode_is_treated_as_hedging(mt5):
    mt5.margin_mode = None
    mt5._positions = [_pos(11, "BUY", 0.1)]
    assert "position size" in _mt5_order(side="SELL", volume=0.1)["error"]


def test_mt5_tripped_switch_still_lets_a_position_close(mt5):
    mt5.margin_mode = 0
    assert _mt5_order(volume=0.05).get("filled") is True
    mt5._positions = [_pos(11, "BUY", 0.05)]
    mt5.equity = 900.0
    assert "kill-switch tripped" in _mt5_order(volume=0.01)["error"]
    assert _mt5_order(side="SELL", volume=0.05).get("filled") is True
    assert _mt5_order(side="SELL", volume=0.05, position=11).get("filled") is True
    assert "kill-switch tripped" in _mt5_order(side="SELL", volume=0.06)["error"]


def test_mt5_close_needs_no_contract_size_or_equity(mt5):
    mt5.contract = 0
    mt5.equity = float("nan")
    mt5._positions = [_pos(11, "BUY", 0.05)]
    assert _mt5_order(side="SELL", volume=0.05, position=11).get("filled") is True
    assert "error" in _mt5_order(volume=0.01)


def test_missing_tick_is_a_clean_error(mt5):
    mt5.tick_none = True
    out = _mt5_order()
    assert "no valid price" in out["error"] and mt5.sent == []


def test_guard_and_request_use_the_same_tick(mt5, monkeypatch):
    seen = {}
    real = order_guard.check_mt5_order

    def spy(mod, name, side, volume, price, **kw):
        seen["price"] = price
        return real(mod, name, side, volume, price, **kw)

    monkeypatch.setattr(order_guard, "check_mt5_order", spy)
    assert _mt5_order(volume=0.05).get("filled") is True
    assert mt5.sent[0]["price"] == seen["price"]


@pytest.mark.parametrize("volume", [-0.01, 0, "nan", float("inf"), True, "lots"])
def test_mt5_bad_volumes_are_refused(mt5, volume):
    out = _mt5_order(volume=volume)
    assert "error" in out and mt5.sent == []


def test_mt5_code_case_and_alias_resolve_to_the_server_symbol(mt5):
    mt5._positions = [_pos(11, "BUY", 0.09)]
    out = _mt5_order(code=" zc ", volume=0.05)  # zc -> CORN: 90 held + 50 > 100
    assert "position size" in out["error"] and mt5.sent == []


def test_mt5_paper_confirm_must_be_literally_true(mt5, monkeypatch):
    assert "paper confirmation" in _mt5_order(paper_confirm="false")["error"]
    calls = []
    monkeypatch.setattr(mt5_ops, "_run_worker", lambda args, timeout=12.0: calls.append(args) or {"error": "x"})
    mt5_ops._mt5_order_tool({"code": "ZC", "side": "BUY", "volume": 0.01, "paper_confirm": "false"})
    assert calls[0][4] == "false"
    assert mt5_ops._mt5_order_tool({"code": "ZC", "side": "BUY", "volume": "lots"}).startswith("MT5 ORDER: volume")


def test_the_worker_passes_the_ticket_through(monkeypatch, mt5):
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    monkeypatch.delitem(sys.modules, "dourmouse.mt5_probe", raising=False)
    probe = importlib.import_module("dourmouse.mt5_probe")
    mt5._positions = [_pos(11, "BUY", 0.1)]
    assert "position size" in probe.cmd_order("ZC", "SELL", 0.1, True)["error"]
    out = probe.cmd_order("ZC", "SELL", 0.1, True, "11")
    assert out.get("filled") is True and mt5.sent[-1]["position"] == 11


# --------------------------------------------------------------------------- #
# W3R-6: concurrent processes share one lock (real multiprocessing)
# --------------------------------------------------------------------------- #

def _child_env(cfg: str, ws: str) -> None:
    os.environ["DOURMOUSE_CONFIG_DIR"] = cfg
    os.environ["DOURMOUSE_WORKSPACE"] = ws
    for name in ("DOURMOUSE_MAX_POSITION_PCT", "DOURMOUSE_MAX_SECTOR_PCT", "DOURMOUSE_DAILY_LOSS_LIMIT_PCT"):
        os.environ.pop(name, None)


def _slow_loader(loaded=None, delay=0.6):
    real = order_guard._load_state

    def slow():
        data = real()
        if loaded is not None:
            loaded.set()
        time.sleep(delay)
        return data

    return slow


def _child_baseline(cfg, ws, account, loaded, delay, out):
    _child_env(cfg, ws)
    order_guard._load_state = _slow_loader(loaded, delay)
    try:
        order_guard.enforce(account, "X", order_guard.Side.BUY, 1, 1, 1000.0, {})
        out.put((account, "ok"))
    except order_guard.OrderRefused as exc:
        out.put((account, str(exc)))


def _child_after(cfg, ws, account, equity, wait_for, out):
    _child_env(cfg, ws)
    wait_for.wait(20)
    try:
        order_guard.enforce(account, "X", order_guard.Side.BUY, 1, 1, equity, {})
        out.put((account, "ok"))
    except order_guard.OrderRefused as exc:
        out.put((account, str(exc)))


def _dirs():
    return str(config.user_config_dir()), str(config.workspace_dir())


def _run(procs, out, n):
    for p in procs:
        p.start()
    results = [out.get(timeout=60) for _ in range(n)]
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    return dict(results)


def test_a_stale_writer_cannot_drop_another_process_baseline():
    ctx = multiprocessing.get_context("spawn")
    cfg, ws = _dirs()
    loaded, out = ctx.Event(), ctx.Queue()
    a = ctx.Process(target=_child_baseline, args=(cfg, ws, "acct-a", loaded, 0.6, out))
    b = ctx.Process(target=_child_after, args=(cfg, ws, "acct-b", 1000.0, loaded, out))
    results = _run([a, b], out, 2)
    assert results == {"acct-a": "ok", "acct-b": "ok"}
    baselines = next(iter(order_guard._load_state()["day"].values()))
    assert set(baselines) == {"acct-a", "acct-b"}


def test_a_stale_writer_cannot_drop_a_latch_another_process_set():
    order_guard.enforce("acct-x", "X", order_guard.Side.BUY, 1, 1, 1000.0, {})  # baseline 1000
    ctx = multiprocessing.get_context("spawn")
    cfg, ws = _dirs()
    loaded, out = ctx.Event(), ctx.Queue()
    a = ctx.Process(target=_child_baseline, args=(cfg, ws, "acct-a", loaded, 0.6, out))
    b = ctx.Process(target=_child_after, args=(cfg, ws, "acct-x", 900.0, loaded, out))
    results = _run([a, b], out, 2)
    assert "kill-switch tripped" in results["acct-x"]
    assert order_guard._load_state()["tripped"] is True


def test_many_first_orders_of_the_day_keep_every_baseline():
    ctx = multiprocessing.get_context("spawn")
    cfg, ws = _dirs()
    out = ctx.Queue()
    names = [f"acct-{i}" for i in range(4)]
    procs = [ctx.Process(target=_child_baseline, args=(cfg, ws, n, None, 0.3, out)) for n in names]
    results = _run(procs, out, len(names))
    assert all(v == "ok" for v in results.values())
    baselines = next(iter(order_guard._load_state()["day"].values()))
    assert set(baselines) == set(names)
    assert all(math.isclose(v, 1000.0) for v in baselines.values())


# --------------------------------------------------------------------------- #
# H-FIXR3-1: positions_get() answers None on an error; that is not "no positions"
# --------------------------------------------------------------------------- #

def test_unreadable_mt5_positions_refuse_instead_of_counting_as_flat(mt5, monkeypatch):
    mt5._positions = [_pos(11, "BUY", 0.09)]  # 90 held: a 0.05 buy would breach the 100 limit
    monkeypatch.setattr(mt5, "positions_get", lambda: None)
    out = _mt5_order(volume=0.05)
    assert "open positions" in out["error"] and mt5.sent == []
    out = _mt5_order(side="SELL", volume=0.05, position=11)
    assert "open positions" in out["error"] and mt5.sent == []


def test_mt5_account_reports_unreadable_positions(mt5, monkeypatch):
    monkeypatch.setattr(mt5, "positions_get", lambda: None)
    out = mt5_ops.mt5_account()
    assert "error" in out and "open_positions" not in out
