"""Pre-submission risk check for broker orders (finding P4-24).

``trading212_ops.t212_order`` and ``mt5_ops.mt5_order`` (and the MT5 worker's
``mt5_probe.cmd_order``) call :func:`enforce` right before they send an order.
It builds the account snapshot from what the broker reports, asks
``guardrails.evaluate_trade`` (with ``latch=True``: this is the submission
path, so a breach is remembered) and raises :class:`OrderRefused` when the
answer is no. The caller then never contacts the broker's order endpoint.

Risk-reducing orders (W3R-2). An order that only reduces an existing position
(a sell of at most what a long holds, a buy of at most what a short holds,
never flipping through zero) is approved by :func:`approve_reduction` even
when the kill switch is tripped, the account equity cannot be read, another
position cannot be valued or the state file is damaged: closing a losing
position must never need a shell command. The callers decide "reduces" from
QUANTITIES the broker reports for that one instrument, never from a price.
Anything that opens or increases exposure still fails closed.

State kept between calls and between processes (the MT5 worker is a separate
process) lives in one small JSON file in Dourmouse's settings folder
(``config.user_config_dir()``), outside the workspace (W3R-1):

* the kill switch latch, cleared only by :func:`rearm`, which the command
  line (``python -m dourmouse.order_guard rearm``) runs only from an
  interactive terminal after the owner types REARM;
* the equity first seen for each account on each local day, which is the
  start-of-day baseline for the daily-loss limit.

The model's file tools refuse that folder and these file names
(``protected_paths``), and the file carries an HMAC-SHA256 (``_mac``) keyed by
``order_guard.key`` in the same folder (mode 0600, a name the file tools
treat as a secret). A state file that was edited, or deleted once the key
exists, refuses every exposure-increasing order until the owner re-arms.
Every load, evaluate and save runs under an ``fcntl.flock`` on
``order_guard.lock`` (W3R-6), so the T212 path, the in-process MT5 path and
the MT5 worker process cannot lose each other's latch or baseline.

Honest limits, stated where they matter:

* The baseline is the equity at the first order attempt of the day, not at
  the market open, so a loss taken before that first attempt is not counted.
* Brokers report no sector here, so each symbol is its own sector and the
  sector-concentration limit degrades to the per-position limit.
* Notional is quantity x price in the instrument's own currency, with no
  currency conversion.
* The HMAC key sits next to the state: it stops an edit by anything that
  cannot read that folder (the workspace tools, the sandboxed shell), not a
  command the owner approved through run_privileged_command.
* Two instruments that are the same underlying under different broker
  symbols are counted as two positions.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import sys
import threading
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

from dourmouse import config
from dourmouse.guardrails import (
    AccountState,
    KillSwitch,
    Position,
    ProposedTrade,
    RiskDecision,
    Side,
    evaluate_trade,
)
from dourmouse.state_file import atomic_write_text

try:
    import fcntl
except ImportError:  # Windows: the MT5 terminal's home; msvcrt is used instead
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "OrderRefused",
    "approve_reduction",
    "check_mt5_order",
    "enforce",
    "is_pure_reduction",
    "protected_paths",
    "rearm",
    "state_path",
]

_LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()
_KEEP_DAYS = 7
_STATE_NAME = "order_guard_state.json"
_KEY_NAME = "order_guard.key"
_LOCK_NAME = "order_guard.lock"
_MAC_FIELD = "_mac"
_REARM_HINT = "re-arm from a terminal with `python -m dourmouse.order_guard rearm`"
#: MetaTrader 5 ``account_info().margin_mode`` values where one symbol has one net
#: position (ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_EXCHANGE).
#: Anything else, including a missing value, is treated as hedging.
_MT5_NETTING_MODES = frozenset({0, 1})


class OrderRefused(ValueError):
    """The order was not sent: a guardrail said no, or the check could not be made."""


def _guard_dir() -> Path:
    return Path(config.user_config_dir()).expanduser()


def state_path() -> Path:
    return _guard_dir() / _STATE_NAME


def _key_path() -> Path:
    return _guard_dir() / _KEY_NAME


def _lock_path() -> Path:
    return _guard_dir() / _LOCK_NAME


def _legacy_state_path() -> Path:
    """Where wave 2b kept the state: the workspace, which the model's file tools can write."""
    return config.workspace_dir() / _STATE_NAME


def protected_paths() -> tuple[Path, ...]:
    """The guard's own files. The model's file tools must never write, edit or delete them."""
    return (state_path(), _key_path(), _lock_path(), _legacy_state_path())


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """One writer at a time, across threads AND processes (the MT5 worker is a separate process)."""
    with _LOCK:
        path = _lock_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise OrderRefused(f"order not placed: could not open the guardrail lock {path} ({exc})") from exc
        try:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                else:
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise OrderRefused(f"order not placed: could not lock the guardrail state ({exc})") from exc
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the flock / msvcrt lock


def _read_key(create: bool) -> bytes | None:
    """The HMAC key, or None when it does not exist and ``create`` is False."""
    path = _key_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _read_key(create=False)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key.hex().encode("ascii") + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        return key
    except OSError as exc:
        raise OrderRefused(f"order not placed: the guardrail key {path} cannot be read ({exc})") from exc
    try:
        key = bytes.fromhex(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        key = b""
    if len(key) < 32:
        raise OrderRefused(f"order not placed: the guardrail key {path} is damaged; {_REARM_HINT}")
    return key


def _mac(key: bytes, state: dict[str, Any]) -> str:
    body = json.dumps({k: v for k, v in state.items() if k != _MAC_FIELD}, sort_keys=True, separators=(",", ":"))
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def _legacy_state() -> dict[str, Any]:
    """First run after the move out of the workspace: carry over a latched switch, nothing else.

    The old file was writable by the model's tools, so its baselines are not trusted; an unreadable
    old file counts as latched (it may have been broken on purpose to forget a latch)."""
    path = _legacy_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"tripped": True, "tripped_reason": f"the old guardrail state {path} could not be read"}
    if not isinstance(data, dict):
        return {"tripped": True, "tripped_reason": f"the old guardrail state {path} is not a JSON object"}
    if data.get("tripped"):
        return {"tripped": True, "tripped_reason": str(data.get("tripped_reason") or "latched before the move")}
    return {}


def _load_state() -> dict[str, Any]:
    path = state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if _read_key(create=False) is not None:
            raise OrderRefused(
                f"order not placed: the guardrail state file {path} is missing although it was written before; "
                f"a deleted state file must not forget a latched kill switch, so {_REARM_HINT}."
            ) from None
        return _legacy_state()
    except OSError as exc:
        raise OrderRefused(
            f"order not placed: the guardrail state file {path} cannot be read ({exc}); "
            f"refusing rather than forgetting a latched kill switch. Fix it or {_REARM_HINT}."
        ) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise OrderRefused(
            f"order not placed: the guardrail state file {path} cannot be read ({exc}); "
            f"refusing rather than forgetting a latched kill switch. Fix it or {_REARM_HINT}."
        ) from exc
    if not isinstance(data, dict):
        raise OrderRefused(f"order not placed: the guardrail state file {path} is not a JSON object; {_REARM_HINT}")
    key = _read_key(create=False)
    mac = data.pop(_MAC_FIELD, None)
    if key is None or not isinstance(mac, str) or not hmac.compare_digest(mac, _mac(key, data)):
        raise OrderRefused(
            f"order not placed: the guardrail state file {path} failed its integrity check (it was changed "
            f"outside the order guard); {_REARM_HINT} after checking why."
        )
    return data


def _save_state(state: dict[str, Any]) -> None:
    days = state.get("day")
    if isinstance(days, dict):
        for old in sorted(days)[:-_KEEP_DAYS]:
            days.pop(old, None)
    try:
        key = _read_key(create=True)
        if key is None:
            raise OSError(f"the guardrail key {_key_path()} disappeared while it was being created")
        signed = dict(state)
        signed[_MAC_FIELD] = _mac(key, state)
        atomic_write_text(state_path(), json.dumps(signed, indent=1, sort_keys=True) + "\n", mode=0o600)
    except OSError as exc:
        raise OrderRefused(f"order not placed: could not record the guardrail state ({exc})") from exc


def rearm() -> None:
    """Manual re-arm of the kill switch: the only way to clear a latched one.

    A state file that is damaged, unsigned or missing is replaced by a fresh signed one (its
    baselines are not trusted); a valid one keeps its baselines."""
    with _locked():
        try:
            state = _load_state()
        except OrderRefused as exc:
            _LOG.warning("order guard re-armed over an unusable state file: %s", exc)
            state = {}
        state.pop("tripped", None)
        state.pop("tripped_reason", None)
        _save_state(state)


def _finite(value: Any, what: str) -> float:
    if isinstance(value, bool):
        raise OrderRefused(f"order not placed: {what} must be a number, got {value!r}")
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError, AttributeError):
        raise OrderRefused(f"order not placed: {what} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise OrderRefused(f"order not placed: {what} must be a finite number, got {value!r}")
    return number


def is_pure_reduction(side: Side, quantity: Any, held_quantity: Any) -> bool:
    """True when the order only shrinks an existing position in the same instrument: a SELL of at
    most a long's quantity or a BUY of at most a short's, never through zero. Quantities only."""
    try:
        qty, held = _finite(quantity, "quantity"), _finite(held_quantity, "held quantity")
    except OrderRefused:
        return False
    if qty <= 0 or held == 0:
        return False
    if side == Side.SELL:
        return held > 0 and qty <= held
    return held < 0 and qty <= -held


def _note_baseline(account_key: str, equity: Any) -> tuple[bool, str]:
    """Best effort for a risk-reducing order: record today's baseline and latch a breach.
    Returns (switch tripped, problem text). Never raises: the reduction goes ahead regardless."""
    try:
        eq = _finite(equity, "equity")
        if eq <= 0:
            raise OrderRefused(f"account equity {equity!r} is not positive")
        cfg = config.load_guardrail_config()
        with _locked():
            state = _load_state()
            day = date.today().isoformat()
            baselines = state.setdefault("day", {}).setdefault(day, {})
            changed = account_key not in baselines
            start = float(baselines.setdefault(account_key, eq))
            switch = KillSwitch(cfg.daily_loss_limit_pct, tripped=bool(state.get("tripped")))
            if switch.update(start, eq) and not state.get("tripped"):
                state["tripped"] = True
                state["tripped_reason"] = f"{account_key} lost {(start - eq) / start:.1%} of its {day} start equity"
                changed = True
            if changed:
                _save_state(state)
            return switch.tripped, ""
    except (OrderRefused, OSError, ValueError, TypeError) as exc:
        return False, str(exc)


def approve_reduction(
    account_key: str, symbol: str, side: Side, quantity: Any, held_quantity: Any, equity: Any = None,
) -> RiskDecision:
    """Approve an order that only reduces the position the broker reports (W3R-2).

    The kill switch, an unreadable equity, other positions that cannot be valued and a damaged state
    file do not block it; a breach seen in passing is still latched when the state can be written."""
    if not is_pure_reduction(side, quantity, held_quantity):
        raise OrderRefused(
            f"order not placed: {side.value} {quantity!r} {symbol} does not only reduce the held "
            f"{held_quantity!r}, so it is not a risk-reducing order"
        )
    tripped, problem = _note_baseline(account_key, equity)
    reasons = ["risk-reducing order: it only shrinks the existing position, so the guardrails allow it"]
    if tripped:
        reasons.append("the kill switch is tripped; only risk-reducing orders pass until a manual re-arm")
    if problem:
        reasons.append(f"guardrail state not updated ({problem})")
    _LOG.warning(
        "order guard: risk-reducing %s %s %s (held %s) allowed on %s; tripped=%s%s",
        side.value, quantity, symbol, held_quantity, account_key, tripped, f"; {problem}" if problem else "",
    )
    return RiskDecision(approved=True, requires_confirmation=False, reasons=reasons, checks={"risk_reducing": True})


def enforce(
    account_key: str,
    symbol: str,
    side: Side,
    quantity: float,
    price: float,
    equity: float,
    held: dict[str, float],
) -> RiskDecision:
    """Raise :class:`OrderRefused` unless the guardrails approve this order.

    ``held`` maps symbol -> signed market value of what the account already holds."""
    try:
        cfg = config.load_guardrail_config()
    except (TypeError, ValueError) as exc:
        raise OrderRefused(f"order not placed: could not read the guardrail inputs ({exc})") from exc
    qty, px, eq = _finite(quantity, "quantity"), _finite(price, "price"), _finite(equity, "account equity")
    if qty <= 0 or px <= 0:
        raise OrderRefused(f"order not placed: quantity {quantity!r} and price {price!r} must be positive numbers")
    if eq <= 0:
        raise OrderRefused(f"order not placed: account equity {equity!r} is not positive, so no limit can be checked")
    positions = {}
    for s, v in held.items():
        value = _finite(v, f"the held value of {s}")
        if value:
            positions[s] = Position(s, value, s)
    with _locked():
        state = _load_state()
        day = date.today().isoformat()
        baselines = state.setdefault("day", {}).setdefault(day, {})
        changed = account_key not in baselines
        start = float(baselines.setdefault(account_key, eq))
        switch = KillSwitch(cfg.daily_loss_limit_pct, tripped=bool(state.get("tripped")))
        account = AccountState(equity=eq, start_of_day_equity=start, positions=positions)
        trade = ProposedTrade(symbol, side, qty, px, symbol)
        try:
            decision = evaluate_trade(trade, account, cfg, switch, latch=True)
        except ValueError as exc:
            raise OrderRefused(f"order not placed: {exc}") from exc
        if switch.tripped and not state.get("tripped"):
            state["tripped"] = True
            state["tripped_reason"] = f"{account_key} lost {(start - eq) / start:.1%} of its {day} start equity"
            changed = True
        if changed:
            _save_state(state)
    if not decision.approved:
        raise OrderRefused(
            "order not placed: blocked by the trading guardrails (" + "; ".join(decision.reasons) + "). "
            "Limits come from DOURMOUSE_MAX_POSITION_PCT, DOURMOUSE_MAX_SECTOR_PCT and "
            "DOURMOUSE_DAILY_LOSS_LIMIT_PCT; while the kill switch is tripped only orders that reduce an "
            "existing position pass, and the owner clears it with "
            "`python -m dourmouse.order_guard rearm` in a terminal."
        )
    return decision


def _mt5_signed_volume(p: Any) -> float:
    volume = _finite(getattr(p, "volume", None), f"the volume of the open {getattr(p, 'symbol', '?')} position")
    return volume if getattr(p, "type", None) == 0 else -volume


def check_mt5_order(
    mt5mod: Any, symbol: str, side: str, volume: Any, price: Any, *, position: int | None = None,
) -> RiskDecision:
    """Guard a MetaTrader 5 order using the connected terminal's own account data.

    ``mt5mod`` is the MetaTrader5 module (a fake in tests). Notional is
    lots x contract size x price; anything that cannot be sized refuses.

    Reductions (W3R-2, W3R-4): an order that names a ``position`` ticket closes (part of) that one
    position and passes as risk-reducing when it is the opposite side and at most its volume. Without
    a ticket only a NETTING account can reduce (one net position per symbol); on a hedging account,
    or when the margin mode is unknown, a plain deal opens a separate position, so every such order
    counts as adding gross exposure."""
    side_name = str(side).strip().upper()
    if side_name not in ("BUY", "SELL"):
        raise OrderRefused(f"order not placed: side must be BUY or SELL, got {side!r}")
    trade_side = Side.BUY if side_name == "BUY" else Side.SELL
    lots = _finite(volume, "volume")
    if lots <= 0:
        raise OrderRefused(f"order not placed: volume must be positive, got {volume!r}")
    raw_positions = mt5mod.positions_get()
    if raw_positions is None:
        # MetaTrader5 answers None on an error and an empty tuple when nothing is open: None is not "flat".
        raise OrderRefused(f"order not placed: the terminal could not read the open positions ({mt5mod.last_error()})")
    positions = list(raw_positions)
    info = mt5mod.account_info()
    account_key = f"mt5:{getattr(info, 'login', '')}"
    equity = getattr(info, "equity", None) if info is not None else None

    if position is not None:
        pos = next((p for p in positions if getattr(p, "ticket", None) == position), None)
        if pos is None:
            raise OrderRefused(f"order not placed: there is no open position with ticket {position}")
        if pos.symbol != symbol:
            raise OrderRefused(f"order not placed: position {position} is {pos.symbol}, not {symbol}")
        held_lots = _mt5_signed_volume(pos)
        if not is_pure_reduction(trade_side, lots, held_lots):
            raise OrderRefused(
                f"order not placed: an order on position {position} must be the opposite side and at most "
                f"its volume ({abs(held_lots)} lots, {'long' if held_lots > 0 else 'short'})"
            )
        return approve_reduction(account_key, symbol, trade_side, lots, held_lots, equity)

    netting = info is not None and getattr(info, "margin_mode", None) in _MT5_NETTING_MODES
    if netting:
        net = sum(_mt5_signed_volume(p) for p in positions if p.symbol == symbol)
        if is_pure_reduction(trade_side, lots, net):
            return approve_reduction(account_key, symbol, trade_side, lots, net, equity)
    if info is None:
        raise OrderRefused("order not placed: no MT5 account to check risk limits against")

    def contract_size(name: str) -> float:
        sym = mt5mod.symbol_info(name)
        size = float(getattr(sym, "trade_contract_size", 0) or 0)
        if not math.isfinite(size) or size <= 0:
            raise OrderRefused(f"order not placed: cannot size {name}: the terminal reports no contract size")
        return size

    held: dict[str, float] = {}
    for p in positions:
        px = getattr(p, "price_current", None) or getattr(p, "price_open", None)
        if not px:
            raise OrderRefused(f"order not placed: cannot value the open {p.symbol} position (no price)")
        value = _mt5_signed_volume(p) * contract_size(p.symbol) * _finite(px, f"the {p.symbol} price")
        # Hedging: a long and a short in one symbol are two exposures, not a net.
        held[p.symbol] = held.get(p.symbol, 0.0) + (value if netting else abs(value))
    return enforce(
        account_key,
        symbol,
        # On a hedging account the deal opens its own position: it only ever adds gross exposure.
        trade_side if netting else Side.BUY,
        lots * contract_size(symbol),
        price,
        _finite(getattr(info, "equity", None), "account equity"),
        held,
    )


def _confirm_rearm_interactively() -> str | None:
    """None when a person at a terminal typed REARM, else why the re-arm was refused."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return (
            "refused: rearm only runs from an interactive terminal, so a tool or a script cannot "
            "clear the kill switch. Open Terminal and run `python -m dourmouse.order_guard rearm` yourself."
        )
    try:
        answer = input("This clears the trading kill switch. Type REARM to confirm: ")
    except EOFError:
        answer = ""
    if answer.strip() != "REARM":
        return "not re-armed: the confirmation was not REARM"
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["rearm"]:
        refusal = _confirm_rearm_interactively()
        if refusal:
            print(refusal)
            return 1
        rearm()
        print("kill switch re-armed")
        return 0
    if args == ["status"]:
        try:
            state = _load_state()
        except OrderRefused as exc:
            print(json.dumps({"tripped": None, "error": str(exc)}))
            return 1
        print(json.dumps({"tripped": bool(state.get("tripped")), "reason": state.get("tripped_reason", "")}))
        return 0
    print("usage: python -m dourmouse.order_guard status|rearm")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
