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
* Deposits and withdrawals are not seen: a mid-day deposit hides a loss
  and a withdrawal can trip the switch falsely, because the baseline is
  just the first equity the guard saw that day (R4-12).
* The signed state carries a counter (``_seq``) that is also kept in
  ``order_guard.seq``, so restoring only an older copy of the state file is
  refused. Restoring the whole folder from a backup restores both and
  cannot be told apart from the real thing (R4-12).

Pending orders (R4-1). The callers add the orders the broker reports as
open or queued to what the account holds: a pending BUY counts as exposure
before it fills, a pending SELL is subtracted from what a new SELL may
reduce, and an order list that cannot be read makes every non-reduction
refuse (see :func:`worst_case_value` and :func:`reducible_quantity`).

One order at a time (R4-5). :func:`order_section` is held by the callers
from the check right through the broker's answer, across threads and
processes, so two orders can never be approved against the same snapshot.
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
import stat
import sys
import threading
import time
from collections.abc import Iterator
from datetime import date, datetime
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
    "ORDER_LOCK_WAIT_SECONDS",
    "OrderRefused",
    "approve_reduction",
    "check_mt5_order",
    "enforce",
    "is_pure_reduction",
    "order_section",
    "protected_paths",
    "reducible_quantity",
    "rearm",
    "state_path",
    "worst_case_value",
]

_LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()
_KEEP_DAYS = 7
_STATE_NAME = "order_guard_state.json"
_KEY_NAME = "order_guard.key"
_LOCK_NAME = "order_guard.lock"
_MAC_FIELD = "_mac"
_SEQ_NAME = "order_guard.seq"
_SEND_LOCK_NAME = "order_guard.send.lock"
#: How long an order waits for another order's check-and-send to finish before it refuses.
ORDER_LOCK_WAIT_SECONDS = 30.0
_LEGACY_MAX_BYTES = 64 * 1024
_QTY_TOLERANCE = 1 + 1e-9
_AUDIT_KEEP = 20
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


def _send_lock_path() -> Path:
    return _guard_dir() / _SEND_LOCK_NAME


def _seq_path() -> Path:
    return _guard_dir() / _SEQ_NAME


def _legacy_state_path() -> Path:
    """Where wave 2b kept the state: the workspace, which the model's file tools can write."""
    return config.workspace_dir() / _STATE_NAME


def protected_paths() -> tuple[Path, ...]:
    """The guard's own files. The model's file tools must never write, edit or delete them."""
    return (state_path(), _key_path(), _lock_path(), _send_lock_path(), _seq_path(), _legacy_state_path())


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


_SEND_LOCK = threading.Lock()


@contextlib.contextmanager
def order_section(timeout: float | None = None) -> Iterator[None]:
    """Hold this around the whole check-and-send of one order (R4-5).

    Two orders checked at the same moment would each pass against the same account snapshot and then
    both be sent (two BUYs each under the limit, two SELLs that are each "the whole long"). The caller
    takes this lock before it reads the account and keeps it until the broker has answered, so the
    next order sees what this one did. It works across threads and processes (a lock file in the
    settings folder) and gives up after ``timeout`` seconds, refusing the order instead of waiting
    for ever: the other order may still be in flight, so the message says to look before retrying.
    Not re-entrant: the check functions inside take their own short state lock, a different file."""
    wait = ORDER_LOCK_WAIT_SECONDS if timeout is None else timeout
    deadline = time.monotonic() + wait
    busy = (
        f"order not placed: another order is being checked or sent (waited {wait:g}s). Nothing was sent by "
        "this call. If that order's result is unknown, look at the broker's positions and open orders before "
        "trying again."
    )
    if not _SEND_LOCK.acquire(timeout=max(wait, 0)):
        raise OrderRefused(busy)
    fd = -1
    try:
        path = _send_lock_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise OrderRefused(f"order not placed: could not open the order lock {path} ({exc})") from exc
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise OrderRefused(busy) from exc
                time.sleep(0.05)
        yield
    finally:
        if fd >= 0:
            os.close(fd)  # closing the descriptor releases the lock
        _SEND_LOCK.release()


def _fresh_key_file(path: Path) -> bytes:
    """Create the key at ``path`` so that a half-written key never exists under its final name:
    write a temp file, fsync it, then ``os.link`` it into place (which fails if the key appeared meanwhile)."""
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key.hex().encode("ascii") + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return key


def _read_key(create: bool) -> bytes | None:
    """The HMAC key, or None when it does not exist and ``create`` is False."""
    path = _key_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if not create:
            return None
        try:
            return _fresh_key_file(path)
        except FileExistsError:
            return _read_key(create=False)
        except OSError as exc:
            raise OrderRefused(f"order not placed: the guardrail key {path} cannot be created ({exc})") from exc
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


def _read_legacy_text(path: Path) -> str:
    """The old workspace file's text, read without ever blocking or growing without bound (R4-11).

    The path is writable by the model's sandbox, so it may be a FIFO, a symlink to /dev/zero or a huge
    file: open it non-blocking and without following a symlink, accept only a regular file of at most
    64 KB, and read at most that much. Raises OSError for anything else."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if info.st_size > _LEGACY_MAX_BYTES:
            raise OSError(f"{path} is larger than {_LEGACY_MAX_BYTES} bytes")
        raw = os.read(fd, _LEGACY_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _LEGACY_MAX_BYTES:
        raise OSError(f"{path} is larger than {_LEGACY_MAX_BYTES} bytes")
    return raw.decode("utf-8")


def _legacy_state() -> dict[str, Any]:
    """First run after the move out of the workspace: carry over a latched switch, nothing else.

    The old file was writable by the model's tools, so its baselines are not trusted; an unreadable
    old file (a FIFO, a symlink, an oversized or non-text file included) counts as latched, since it
    may have been broken on purpose to forget a latch. Remove this read one release after wave 2c."""
    path = _legacy_state_path()
    try:
        data = json.loads(_read_legacy_text(path))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):  # ValueError covers bad JSON and bad UTF-8
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
    anchor = _read_seq()
    seq = data.get("_seq")
    if anchor and (isinstance(seq, bool) or not isinstance(seq, int) or seq < anchor):
        raise OrderRefused(
            f"order not placed: the guardrail state file {path} is an older copy than the last one this "
            f"computer wrote (counter {seq!r}, expected at least {anchor}); a restored backup could bring back "
            f"an unlatched switch, so {_REARM_HINT} after checking why."
        )
    return data


def _read_seq() -> int:
    """The highest state counter this computer has written (0 when none yet). A damaged file reads as 0:
    the signed state is still checked, and the counter is only an extra guard against an old copy."""
    try:
        return max(int(_seq_path().read_text(encoding="ascii").strip()), 0)
    except (OSError, ValueError):
        return 0


def _save_state(state: dict[str, Any]) -> None:
    days = state.get("day")
    if isinstance(days, dict):
        for old in sorted(days)[:-_KEEP_DAYS]:
            days.pop(old, None)
    try:
        key = _read_key(create=True)
        if key is None:
            raise OSError(f"the guardrail key {_key_path()} disappeared while it was being created")
        previous = state.get("_seq")
        seq = max(previous if isinstance(previous, int) and not isinstance(previous, bool) else 0, _read_seq()) + 1
        state["_seq"] = seq
        state.setdefault("_created", datetime.now().isoformat(timespec="seconds"))
        signed = dict(state)
        signed[_MAC_FIELD] = _mac(key, state)
        atomic_write_text(state_path(), json.dumps(signed, indent=1, sort_keys=True) + "\n", mode=0o600)
        atomic_write_text(_seq_path(), f"{seq}\n", mode=0o600)
    except OSError as exc:
        raise OrderRefused(f"order not placed: could not record the guardrail state ({exc})") from exc


def _aside(path: Path) -> Path:
    """Move ``path`` to ``<name>.bad-<stamp>`` (never onto an existing file) and return the new path."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    aside = path.with_name(f"{path.name}.bad-{stamp}")
    n = 1
    while aside.exists():
        aside = path.with_name(f"{path.name}.bad-{stamp}-{n}")
        n += 1
    os.replace(path, aside)
    return aside


def _audit(state: dict[str, Any], what: str) -> None:
    """Add one line to the signed audit trail inside the state (and to the log)."""
    _LOG.warning("order guard audit: %s", what)
    trail = state.get("audit")
    trail = list(trail) if isinstance(trail, list) else []
    trail.append({"at": datetime.now().isoformat(timespec="seconds"), "what": what})
    state["audit"] = trail[-_AUDIT_KEEP:]


def rearm(*, reset_baseline: bool = False) -> list[str]:
    """Manual re-arm of the kill switch: the only way to clear a latched one. Returns lines to show the owner.

    A valid state keeps its baselines, so a loss that already tripped the switch trips it again on the
    next order unless the limit is raised or a new day starts; ``reset_baseline`` (an explicit, audited
    choice) forgets today's baseline instead. A state file that is damaged, unsigned or missing is replaced
    by a fresh signed one, and a key file that is empty, short or unreadable is moved aside
    (``order_guard.key.bad-<time>``) and replaced, because nothing signed with it can be trusted (R4-4).
    That also loses today's baseline, which is said out loud and written to the audit trail (R4-8)."""
    notes: list[str] = [f"state file: {state_path()}"]
    with _locked():
        try:
            state = _load_state()
            usable = True
        except OrderRefused as exc:
            _LOG.warning("order guard re-armed over an unusable state file: %s", exc)
            state, usable = {}, False
            notes.append(f"the old state was unusable and is replaced: {exc}")
        if not usable:
            try:
                _read_key(create=False)
            except OrderRefused as exc:
                try:
                    moved = _aside(_key_path())
                except OSError as err:
                    raise OrderRefused(
                        f"the guardrail key {_key_path()} is unusable and could not be moved aside ({err}); "
                        "move or delete it by hand, then run rearm again"
                    ) from err
                notes.append(f"the key was unusable ({exc}); moved aside to {moved} and replaced")
            if state_path().exists():
                try:
                    notes.append(f"the damaged state file was moved aside to {_aside(state_path())}")
                except OSError as err:
                    _LOG.warning("could not move the damaged guard state aside (%s); it is overwritten", err)
            _audit(state, "re-armed over an unusable guard state; today's start-of-day baseline was lost "
                          "and restarts from the next order's equity")
            notes.append("today's start-of-day baseline was lost: the daily loss limit now counts from the "
                         "equity of the next order, not from this morning")
        else:
            was_tripped = bool(state.get("tripped"))
            if reset_baseline:
                state.get("day", {}).pop(date.today().isoformat(), None)
                _audit(state, "re-armed with --reset-baseline: today's start-of-day baseline was dropped on "
                              "the owner's request")
                notes.append("today's baseline was reset: the daily loss limit now counts from the equity of "
                             "the next order")
            elif was_tripped:
                notes.append("the baseline is unchanged: if today's loss still stands the next order trips the "
                             "switch again. Raise DOURMOUSE_DAILY_LOSS_LIMIT_PCT, wait for tomorrow, or run "
                             "`rearm --reset-baseline` (recorded in the audit trail) to restart today's baseline")
            else:
                notes.append("the kill switch was not tripped")
        state.pop("tripped", None)
        state.pop("tripped_reason", None)
        _save_state(state)
    return notes


def _finite(value: Any, what: str) -> float:
    if isinstance(value, bool):
        raise OrderRefused(f"order not placed: {what} must be a number, got {value!r}")
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError, AttributeError, OverflowError):
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
    # Lots summed as floats can land a hair under the typed quantity (0.1 + 0.7 is 0.7999999999999999,
    # a full close of 0.8 must still count), so the HELD side gets a tiny relative allowance (R4-10).
    if side == Side.SELL:
        return held > 0 and qty <= held * _QTY_TOLERANCE
    return held < 0 and qty <= -held * _QTY_TOLERANCE


def reducible_quantity(held: Any, pending_buy: Any = 0.0, pending_sell: Any = 0.0) -> float:
    """What a new order may still shrink: the held quantity (signed) after the orders the broker already
    has queued in the closing direction (R4-1). Two resting SELL LIMITs of the whole long are not each a
    reduction: the second finds nothing left. Zero when nothing is left to reduce."""
    h, buys, sells = _finite(held, "held quantity"), _finite(pending_buy, "pending buy"), _finite(pending_sell, "pending sell")
    if h > 0:
        return max(h - sells, 0.0)
    if h < 0:
        return min(h + buys, 0.0)
    return 0.0


def worst_case_value(
    held_value: Any, pending_buy_value: Any = 0.0, pending_sell_value: Any = 0.0, trade_value: Any = 0.0,
) -> float:
    """The signed value of a holding if the broker's queued orders fill the way that increases exposure the
    most (R4-1): queued BUYs fill and queued SELLs are cancelled, or the queued SELLs fill and the BUYs are
    cancelled. ``trade_value`` is the signed value of the order being checked; the scenario in which that
    order leaves the largest exposure is the one returned."""
    held = _finite(held_value, "held value")
    trade = _finite(trade_value, "trade value")
    up = held + _finite(pending_buy_value, "pending buy value")
    down = held - _finite(pending_sell_value, "pending sell value")
    return up if abs(up + trade) >= abs(down + trade) else down


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


#: MetaTrader 5 order types that buy (BUY, BUY_LIMIT, BUY_STOP, BUY_STOP_LIMIT) and sell (the odd ones).
_MT5_BUY_ORDER_TYPES = frozenset({0, 2, 4, 6})
_MT5_SELL_ORDER_TYPES = frozenset({1, 3, 5, 7})


def _mt5_pending_orders(mt5mod: Any) -> tuple[list[Any] | None, str]:
    """(orders the terminal reports as pending, "") or (None, why they cannot be listed) (R4-1).
    None is "unknown", never "none": a non-reduction must refuse on it."""
    getter = getattr(mt5mod, "orders_get", None)
    if getter is None:
        return None, "the terminal module cannot list pending orders"
    raw = getter()
    if raw is None:
        return None, f"the terminal could not read the pending orders ({mt5mod.last_error()})"
    try:
        return list(raw), ""
    except TypeError:
        return None, "the terminal's pending order list is not a list"


def check_mt5_order(
    mt5mod: Any, symbol: str, side: str, volume: Any, price: Any, *, position: int | None = None,
) -> RiskDecision:
    """Guard a MetaTrader 5 order using the connected terminal's own account data.

    ``mt5mod`` is the MetaTrader5 module (a fake in tests). Notional is
    lots x contract size x price; anything that cannot be sized refuses.

    Reductions (W3R-2, W3R-4): an order that names a ``position`` ticket closes (part of) that one
    position and passes as risk-reducing when it is the opposite side, at most its volume, and does
    not leave the symbol's NET exposure larger than before (R4-7: closing one leg of a hedge makes a
    flat net a naked position). Without a ticket only a NETTING account can reduce (one net position
    per symbol); on a hedging account, or when the margin mode is unknown, a plain deal opens a
    separate position, so every such order counts as adding gross exposure.

    Pending orders (R4-1): what the terminal reports as pending counts. Queued BUYs add to a symbol's
    exposure before they fill, queued opposite-side orders use up the volume a reduction may close,
    and a pending list that cannot be read makes every non-reduction refuse (a reduction still passes:
    it cannot add exposure by itself)."""
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
    pending, pending_problem = _mt5_pending_orders(mt5mod)

    def pending_volume(sym: str, kinds: frozenset[int], ticket: int | None = None) -> float:
        total = 0.0
        for o in pending or []:
            if getattr(o, "symbol", None) != sym or getattr(o, "type", None) not in kinds:
                continue
            if ticket is not None and getattr(o, "position_id", 0) != ticket:
                continue
            total += _finite(getattr(o, "volume_current", getattr(o, "volume_initial", None)),
                             f"the volume of the pending {sym} order")
        return total

    def reducible(held_lots: float, sym: str, ticket: int | None = None) -> float:
        # A pending order in the closing direction will use up part of what is left to close.
        try:
            return reducible_quantity(held_lots, pending_volume(sym, _MT5_BUY_ORDER_TYPES, ticket),
                                      pending_volume(sym, _MT5_SELL_ORDER_TYPES, ticket))
        except OrderRefused:
            return 0.0  # a pending order that cannot be read: nothing is treated as safely reducible

    net_before = sum(_mt5_signed_volume(p) for p in positions if p.symbol == symbol)

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
        net_after = net_before + (-lots if held_lots > 0 else lots)
        leaves_net_larger = abs(net_after) > abs(net_before) * _QTY_TOLERANCE + 1e-12
        if not leaves_net_larger and is_pure_reduction(trade_side, lots, reducible(held_lots, symbol, position)):
            return approve_reduction(account_key, symbol, trade_side, lots, held_lots, equity)
        # Closing one leg of a hedge (R4-7), or volume already spoken for by a pending order (R4-1):
        # this order is judged like any other and counts as adding exposure.

    netting = info is not None and getattr(info, "margin_mode", None) in _MT5_NETTING_MODES
    if netting and position is None and is_pure_reduction(trade_side, lots, reducible(net_before, symbol)):
        return approve_reduction(account_key, symbol, trade_side, lots, net_before, equity)
    if info is None:
        raise OrderRefused("order not placed: no MT5 account to check risk limits against")
    if pending is None:
        raise OrderRefused(
            f"order not placed: {pending_problem}, so queued orders cannot be counted against the risk limits. "
            "Only an order that reduces an open position can go ahead without that."
        )

    def contract_size(name: str) -> float:
        sym = mt5mod.symbol_info(name)
        try:
            size = float(getattr(sym, "trade_contract_size", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            size = 0.0
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
    queued_buy: dict[str, float] = {}
    queued_sell: dict[str, float] = {}
    for o in pending:
        sym = str(getattr(o, "symbol", ""))
        kind = getattr(o, "type", None)
        if not sym or kind not in _MT5_BUY_ORDER_TYPES | _MT5_SELL_ORDER_TYPES:
            raise OrderRefused(f"order not placed: cannot read the pending order on {sym or '?'} (type {kind!r})")
        px = getattr(o, "price_open", None) or getattr(o, "price_current", None)
        if not px:
            raise OrderRefused(f"order not placed: cannot value the pending {sym} order (no price)")
        vol = _finite(getattr(o, "volume_current", getattr(o, "volume_initial", None)),
                      f"the volume of the pending {sym} order")
        value = vol * contract_size(sym) * _finite(px, f"the pending {sym} order price")
        bucket = queued_buy if kind in _MT5_BUY_ORDER_TYPES else queued_sell
        bucket[sym] = bucket.get(sym, 0.0) + value
    trade_value = lots * contract_size(symbol) * _finite(price, "price") * (1 if trade_side == Side.BUY else -1)
    for sym in set(queued_buy) | set(queued_sell):
        if netting:
            held[sym] = worst_case_value(
                held.get(sym, 0.0), queued_buy.get(sym, 0.0), queued_sell.get(sym, 0.0),
                trade_value if sym == symbol else 0.0,
            )
        else:  # hedging: any queued order may open its own position
            held[sym] = held.get(sym, 0.0) + queued_buy.get(sym, 0.0) + queued_sell.get(sym, 0.0)
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


def _confirm_rearm_interactively(reset_baseline: bool = False) -> str | None:
    """None when a person at a terminal typed REARM, else why the re-arm was refused."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return (
            "refused: rearm only runs from an interactive terminal, so a tool or a script cannot "
            "clear the kill switch. Open Terminal and run `python -m dourmouse.order_guard rearm` yourself."
        )
    try:
        extra = " AND restarts today's daily-loss baseline" if reset_baseline else ""
        answer = input(f"This clears the trading kill switch{extra}. Type REARM to confirm: ")
    except EOFError:
        answer = ""
    if answer.strip() != "REARM":
        return "not re-armed: the confirmation was not REARM"
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["rearm"], ["rearm", "--reset-baseline"]):
        reset = len(args) == 2
        print(f"guard state folder: {state_path().parent}")
        refusal = _confirm_rearm_interactively(reset)
        if refusal:
            print(refusal)
            return 1
        try:
            notes = rearm(reset_baseline=reset)
        except OrderRefused as exc:
            print(f"not re-armed: {exc}")
            return 1
        print("kill switch re-armed")
        for note in notes:
            print(f"  {note}")
        return 0
    if args == ["status"]:
        try:
            state = _load_state()
        except OrderRefused as exc:
            print(json.dumps({"tripped": None, "error": str(exc), "state_path": str(state_path())}))
            return 1
        print(json.dumps({
            "tripped": bool(state.get("tripped")),
            "reason": state.get("tripped_reason", ""),
            "state_path": str(state_path()),
            "audit": state.get("audit", []),
        }))
        return 0
    print("usage: python -m dourmouse.order_guard status|rearm [--reset-baseline]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
