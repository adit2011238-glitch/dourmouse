"""ATLAS LAB — LLM-powered backtesting with a synced strategy catalog.

What it does:
1. Syncs a strategy catalog from GitHub (atlas-strategy-lab) — strategies
   that passed a strict six-gate battery, with honest verdicts.
2. Takes a natural-language prompt, sends it to the NVIDIA API, and converts
   the LLM's structured output into a runnable backtest.
3. Runs the backtest through the ATLAS engine and produces a clear report
   with all the metrics a trader actually reads: p-value, t-value, mean
   return, median return, standard deviation, Sharpe, win rate, plus a
   plain-English explanation of the strategy and whether it's worth paper
   trading.

Everything is async (threaded) and pollable — the UI never blocks.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

STRATEGY_LAB_REPO = "https://github.com/valerygordon200-byte/atlas-strategy-lab.git"
STRATEGY_LAB_DIR = Path("/tmp/atlas-strategy-lab")
STRATEGY_CATALOG_PATH = STRATEGY_LAB_DIR / "strategy_catalog.json"
FX_LEADERBOARD_PATH = STRATEGY_LAB_DIR / "reports" / "fx_campaign_leaderboard_full.csv"
SEASONAL_LEADERBOARD_PATH = STRATEGY_LAB_DIR / "reports" / "seasonal_leaderboard.csv"
STRICT_BATTERY_PATH = STRATEGY_LAB_DIR / "reports" / "strict_battery.csv"
FX_STRICT_BATTERY_PATH = STRATEGY_LAB_DIR / "reports" / "fx_strict_battery.csv"
RPT_STRICT = STRATEGY_LAB_DIR / "reports" / "strict_battery.md"
RPT_FX_STRICT = STRATEGY_LAB_DIR / "reports" / "fx_strict_battery.md"
RPT_FX_CAMPAIGN = STRATEGY_LAB_DIR / "reports" / "fx_campaign_report.md"
RPT_SEASONAL = STRATEGY_LAB_DIR / "reports" / "seasonal_campaign_report.md"

# NVIDIA API config (reuses the app's own key).
NVIDIA_BASE_URL = os.environ.get(
    "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
)
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/llama-3.1-nemotron-ultra-253b-v1")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")

#: Response cap for BOTH ATLAS LLM paths (this module's ``_llm_chat`` and
#: atlas_proposals' ``_llm_json``) — see _llm_chat's own call site for the
#: full v13.7 rationale. Read through the accessor at call time, never
#: bound as a def-time default, so ATLAS_LLM_MAX_TOKENS is actually
#: honoured.
ATLAS_LLM_MAX_TOKENS = 8000
ATLAS_LLM_MAX_TOKENS_ENV = "ATLAS_LLM_MAX_TOKENS"


def _atlas_llm_max_tokens() -> int:
    """ATLAS response cap, honouring the env override. Floors at 512."""
    raw = os.environ.get(ATLAS_LLM_MAX_TOKENS_ENV, "").strip()
    if raw:
        try:
            return max(512, int(raw))
        except ValueError:
            pass
    return ATLAS_LLM_MAX_TOKENS

# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #

@dataclass
class StrategyMetrics:
    """One strategy's performance metrics — the numbers a trader reads."""
    name: str = ""
    direction: str = ""
    mean_return_pct: float = 0.0
    median_return_pct: float | None = None
    t_stat: float = 0.0
    p_value: float | None = None
    sharpe: float | None = None
    win_rate_pct: float | None = None
    std_dev_pct: float | None = None
    max_drawdown_pct: float | None = None
    n_observations: int = 0
    cost_ladder_ok: bool | None = None
    passed_gates: str = ""  # e.g. "3/6" or "5/6"
    verdict: str = ""  # "STRONG", "CANDIDATE", "FAILED", "HOLD", "PAPER TRADE"
    description: str = ""  # plain-English explanation


@dataclass
class BacktestRequest:
    """A user's backtest request — the prompt + any overrides."""
    id: str = ""
    prompt: str = ""
    pair: str = "EURUSD"
    start_date: str = "2020-01-01"
    end_date: str = "2024-12-31"
    status: str = "pending"  # pending | running | done | failed
    progress: float = 0.0
    result: dict | None = None
    error: str = ""
    created_at: str = ""


@dataclass
class StrategyLabState:
    """The in-memory state of the ATLAS LAB."""
    strategies: dict[str, StrategyMetrics] = field(default_factory=dict)
    recent_reports: dict[str, str] = field(default_factory=dict)  # id -> markdown
    backtest_requests: dict[str, BacktestRequest] = field(default_factory=dict)
    last_sync: str = ""
    sync_error: str = ""
    version: str = ""


# In-memory state (thread-safe via the GIL + single-threaded access pattern).
_LAB_STATE: StrategyLabState | None = None
_LAB_LOCK = threading.Lock()

#: Auto-sync: the Atlas window is a LIVE leaderboard — every N seconds a
#: daemon thread pulls the GitHub repo and re-parses, so strategies pushed
#: from the other desktop's atlas-strategy-lab flow in with zero user steps.
_AUTO_SYNC_INTERVAL_SECONDS = float(os.environ.get("ATLAS_LAB_SYNC_INTERVAL", "300"))
_auto_sync_started = False

#: SSE events hub (set by run_server at mount — same hub the HUD reads, so
#: fresh strategy syncs appear as live cards in the app without any refresh).
_hub: Any | None = None


def bind_events_hub(hub: Any | None) -> None:
    """Attach the SSE broadcast hub (called by run_server at mount), so
    auto-syncs push ``strategies_synced`` events to every connected HUD."""
    global _hub
    _hub = hub

#: Verdict rank for best→worst ordering (leaderboard semantics).
_VERDICT_RANK = {
    "PAPER TRADE": 5,
    "STRONG": 4,
    "CANDIDATE": 3,
    "HOLD": 2,
    "FAIL": 1,
    "FAILED": 1,
    "": 0,
}

# --------------------------------------------------------------------------- #
# GitHub sync
# --------------------------------------------------------------------------- #

def _ensure_repo() -> str | None:
    """Clone or pull the strategy-lab repo. Returns None on success or an error string."""
    try:
        if STRATEGY_LAB_DIR.is_dir():
            # Already cloned — pull latest.
            result = subprocess.run(
                ["git", "-C", str(STRATEGY_LAB_DIR), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                return f"git pull failed: {result.stderr.strip() or result.stdout.strip()}"
        else:
            # Fresh clone.
            STRATEGY_LAB_DIR.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["git", "clone", STRATEGY_LAB_REPO, str(STRATEGY_LAB_DIR)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return f"git clone failed: {result.stderr.strip() or result.stdout.strip()}"
        return None
    except subprocess.TimeoutExpired:
        return "Git operation timed out — the repo may be large or the network slow."
    except FileNotFoundError:
        return "git not found on PATH — install git to sync the strategy catalog."
    except Exception as exc:
        return f"Git sync error: {exc}"


def _parse_fx_leaderboard(path: Path) -> dict[str, StrategyMetrics]:
    """Parse the FX campaign leaderboard CSV into metrics.

    Columns (observed live): is_t, is_mean, oos_mean, oos_t, oos_nw,
    oos_sharpe, win, n, p, family, pair, note."""
    return _parse_csv_strategies(path, id_cols=("id", "name", "strategy", "key"),
                                 mean_cols=("oos_mean", "is_mean", "mean_return", "mean_pct"),
                                 t_cols=("oos_t", "is_t", "t_stat", "t"),
                                 p_cols=("p", "p_ho", "p_wf"),
                                 win_cols=("win", "oos_win", "is_win"),
                                 sharpe_cols=("oos_sharpe", "sharpe"),
                                 n_cols=("n", "n_obs", "oos_n"),
                                 verdict_cols=("verdict", "status", "VERDICT"),
                                 gates_cols=("gates_passed", "passed", "gates"),
                                 desc_cols=("note", "notes", "description", "label"),
                                 default_verdict="CANDIDATE")


def _parse_seasonal_leaderboard(path: Path) -> dict[str, StrategyMetrics]:
    """Parse the seasonal leaderboard CSV.

    Columns (observed live): kind, key, commodity, months, label, mechanism,
    source, direction, sel_n, sel_mean, sel_t, n_hold, ho_mean, ho_t, win,
    p_mc, ... net_mean_8%, ..."""
    return _parse_csv_strategies(path, id_cols=("id", "key", "commodity", "name"),
                                 mean_cols=("sel_mean", "ho_mean", "mean_pct", "mean_return"),
                                 t_cols=("sel_t", "ho_t", "t", "t_stat"),
                                 p_cols=("p_mc", "p", "p_ho", "p_wf"),
                                 win_cols=("win", "ho_win", "is_win"),
                                 n_cols=("sel_n", "n", "n_obs"),
                                 verdict_cols=("verdict", "status", "VERDICT"),
                                 gates_cols=("gates_passed", "passed", "gates"),
                                 desc_cols=("label", "mechanism", "notes", "description"),
                                 default_verdict="CANDIDATE")


def _parse_strict_battery(path: Path) -> dict[str, StrategyMetrics]:
    """Parse a strict-battery CSV — every strategy ever put through the six
    gates, with its honest PASS/FAIL verdict and the gate names that failed.

    Columns (observed live): key, label, is_mean, is_t, is_win, is_trim,
    p_is, ho_mean, ho_t, ho_win, p_ho, wf_mean, wf_t, wf_win, wf_median,
    wf_std, wf_n, p_wf, boot_p50, boot_p5, boot_p95, p_leq_0, ..., VERDICT,
    gates. The FX variant swaps is_nw/ho_nw for is_win/ho_win and has no
    wf_median/wf_std — the generic mapping handles both."""
    return _parse_csv_strategies(path, id_cols=("key", "id", "name"),
                                 mean_cols=("wf_mean", "ho_mean", "is_mean", "mean_return"),
                                 t_cols=("wf_t", "ho_t", "is_t", "t"),
                                 p_cols=("p_wf", "p_ho", "p_is", "p"),
                                 win_cols=("wf_win", "ho_win", "is_win"),
                                 median_cols=("wf_median", "median"),
                                 std_cols=("wf_std", "std"),
                                 n_cols=("wf_n", "n"),
                                 verdict_cols=("VERDICT", "verdict", "status"),
                                 gates_cols=("gates", "gates_passed", "passed"),
                                 desc_cols=("label", "description", "note"),
                                 default_verdict="HOLD")


def _parse_csv_strategies(
    path: Path,
    *,
    id_cols: tuple[str, ...],
    mean_cols: tuple[str, ...],
    t_cols: tuple[str, ...],
    p_cols: tuple[str, ...] = (),
    win_cols: tuple[str, ...] = (),
    sharpe_cols: tuple[str, ...] = (),
    median_cols: tuple[str, ...] = (),
    std_cols: tuple[str, ...] = (),
    n_cols: tuple[str, ...] = (),
    verdict_cols: tuple[str, ...] = (),
    gates_cols: tuple[str, ...] = (),
    desc_cols: tuple[str, ...] = (),
    default_verdict: str = "HOLD",
) -> dict[str, StrategyMetrics]:
    """Generic CSV → StrategyMetrics parser with column aliases.

    Returns {} honestly on any parse problem — a leaderboard never crashes
    because one file changed shape upstream."""
    strategies = {}
    if not path.is_file():
        return strategies
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        # csv.reader handles quoted commas properly (the FX battery rows
        # contain "..." descriptions with commas that naive split() misaligns
        # — observed live: VERDICT 'FAIL' landing in the gates column).
        rows = list(csv.reader(text.splitlines()))
        if len(rows) < 2:
            return strategies
        headers = [h.strip().strip('"') for h in rows[0]]
        header_set = set(headers)
        for parts in rows[1:]:
            if not parts or not any(p.strip() for p in parts):
                continue
            if len(parts) < len(headers):
                continue
            row = dict(zip(headers, [p.strip().strip('"') for p in parts]))

            def first(cols: tuple[str, ...]) -> str:
                for col in cols:
                    if col in header_set and row.get(col):
                        return row[col]
                return ""

            name = first(id_cols)
            if not name:
                continue
            try:
                def ffloat(cols: tuple[str, ...]) -> float | None:
                    raw = first(cols)
                    try:
                        return float(raw) if raw else None
                    except (ValueError, TypeError):
                        return None

                def fint(cols: tuple[str, ...]) -> int:
                    raw = first(cols)
                    try:
                        return int(float(raw)) if raw else 0
                    except (ValueError, TypeError):
                        return 0

                # CSV returns are DECIMAL FRACTIONS (0.138 = 13.8%); the
                # catalog JSON stores PERCENT (-14.57). Normalize to percent
                # here so the leaderboard shows ONE consistent unit.
                win = ffloat(win_cols)
                m = StrategyMetrics(
                    name=name,
                    direction=row.get("direction", "LONG"),
                    mean_return_pct=(ffloat(mean_cols) or 0.0) * 100.0,
                    median_return_pct=(ffloat(median_cols) * 100.0
                                       if ffloat(median_cols) is not None else None),
                    t_stat=ffloat(t_cols) or 0.0,
                    p_value=ffloat(p_cols),
                    sharpe=ffloat(sharpe_cols),
                    win_rate_pct=win * 100.0 if win is not None else None,
                    std_dev_pct=(ffloat(std_cols) * 100.0
                                 if ffloat(std_cols) is not None else None),
                    n_observations=fint(n_cols),
                    verdict=first(verdict_cols) or default_verdict,
                    passed_gates=first(gates_cols),
                    description=first(desc_cols),
                )
                strategies[name] = m
            except (ValueError, TypeError):
                continue
    except Exception:
        pass
    return strategies


def _parse_catalog_json(path: Path) -> dict[str, StrategyMetrics]:
    """Parse the strategy_catalog.json into metrics."""
    strategies = {}
    if not path.is_file():
        return strategies
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, Exception):
        return strategies
    # Seasonal locked candidates.
    for key, entry in data.get("seasonal_locked", {}).items():
        strategies[key] = StrategyMetrics(
            name=key,
            direction=entry.get("direction", "LONG"),
            mean_return_pct=float(entry.get("is_mean_pct", entry.get("mean_pct", 0)) or 0),
            t_stat=float(entry.get("is_t", entry.get("t", 0)) or 0),
            n_observations=int(entry.get("is_n", entry.get("n", 0)) or 0),
            verdict="CANDIDATE",
            passed_gates="locked after in-sample",
            description=f"Seasonal {entry.get('direction', 'LONG')} on {entry.get('commodity', key)} (month {entry.get('month', '?')})",
        )
    # FX candidates (if present).
    for key, entry in data.get("fx_candidates", {}).items():
        strategies[key] = StrategyMetrics(
            name=key,
            direction=entry.get("direction", "LONG"),
            mean_return_pct=float(entry.get("holdout_mean_pct", 0) or 0),
            t_stat=float(entry.get("holdout_t", 0) or 0),
            n_observations=int(entry.get("n", 0) or 0),
            verdict="CANDIDATE",
            passed_gates=entry.get("gates_passed", ""),
            description=entry.get("description", ""),
        )
    return strategies


def _read_report_md(path: Path) -> str:
    """Read a markdown report file, returning its content or an error message."""
    if not path.is_file():
        return f"*(report not found: {path.name})*"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Truncate to a reasonable size for display.
        if len(text) > 20_000:
            text = text[:20_000] + "\n\n*…report truncated at 20,000 chars*"
        return text
    except Exception as exc:
        return f"*(error reading report: {exc})*"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def get_state() -> StrategyLabState:
    """Get the current lab state, syncing on first access.

    The first sync runs OFF the request thread (the API must never block on
    a git pull / clone — reviewer-caught: the leaderboard endpoint would
    hang up to the git timeout on first load). Data appears as soon as the
    sync lands; until then the state is empty and honest.

    Also arms the auto-sync daemon (once) so the Atlas window's leaderboard
    keeps updating from the GitHub repo with zero user steps."""
    global _LAB_STATE
    first = False
    with _LAB_LOCK:
        if _LAB_STATE is None:
            _LAB_STATE = StrategyLabState()
            first = True
        state = _LAB_STATE
    if first:
        threading.Thread(target=_initial_sync_worker, daemon=True,
                         name="atlas-lab-initial-sync").start()
    start_auto_sync()
    return state


def _initial_sync_worker() -> None:
    """Background first sync (never on the request thread)."""
    try:
        with _LAB_LOCK:
            _sync()
    except Exception:  # noqa: BLE001 -- a failed first sync surfaces via state
        pass


def sync() -> dict[str, Any]:
    """Force a sync of the strategy lab repo. Returns a status dict."""
    global _LAB_STATE
    with _LAB_LOCK:
        result = _sync()
        return result


def _sync() -> dict[str, Any]:
    """Internal: sync the repo and re-parse all data. Caller must hold _LAB_LOCK."""
    global _LAB_STATE
    assert _LAB_STATE is not None

    error = _ensure_repo()
    if error:
        _LAB_STATE.sync_error = error
        return {"ok": False, "error": error}

    # Parse all sources. Strict batteries last: they carry every strategy
    # ever tested (PASS/FAIL verdicts) but are sparse on description — the
    # catalog/locked rows (with descriptions) must win the merge.
    strategies = {}
    strategies.update(_parse_catalog_json(STRATEGY_CATALOG_PATH))
    strategies.update(_parse_fx_leaderboard(FX_LEADERBOARD_PATH))
    strategies.update(_parse_seasonal_leaderboard(SEASONAL_LEADERBOARD_PATH))
    strategies.update(_parse_strict_battery(STRICT_BATTERY_PATH))
    strategies.update(_parse_strict_battery(FX_STRICT_BATTERY_PATH))

    # Read reports.
    reports = {}
    for rpt_id, rpt_path in [
        ("fx_campaign", RPT_FX_CAMPAIGN),
        ("fx_strict_battery", RPT_FX_STRICT),
        ("strict_battery", RPT_STRICT),
        ("seasonal", RPT_SEASONAL),
    ]:
        reports[rpt_id] = _read_report_md(rpt_path)

    # Version from catalog.
    version = ""
    try:
        data = json.loads(STRATEGY_CATALOG_PATH.read_text(encoding="utf-8", errors="replace"))
        version = data.get("version", "")
    except Exception:
        pass

    _LAB_STATE.strategies = strategies
    _LAB_STATE.recent_reports = reports
    _LAB_STATE.last_sync = datetime.now(timezone.utc).isoformat()
    _LAB_STATE.sync_error = ""
    _LAB_STATE.version = version

    # Broadcast to the SSE hub so every connected HUD sees the update live.
    try:
        if _hub is not None:
            _hub.broadcast({
                "type": "strategies_synced",
                "count": len(strategies),
                "last_sync": _LAB_STATE.last_sync,
            })
    except Exception:  # noqa: BLE001 -- a broadcast failure never kills sync
        pass

    return {
        "ok": True,
        "strategy_count": len(strategies),
        "last_sync": _LAB_STATE.last_sync,
        "version": version,
    }


def list_strategies() -> list[dict]:
    """Return all strategies as a list of dicts for the API."""
    return leaderboard(include_description=True)


def leaderboard(include_description: bool = True) -> list[dict]:
    """All tested strategies ranked BEST → WORST for the Atlas window.

    Ordering is deterministic and honest (never an LLM judgment): verdict
    rank first (PAPER TRADE > STRONG > CANDIDATE > HOLD > FAILED), then
    |t-stat| (significance) within the same verdict, then mean return.
    Every metric a trader reads is included; ``include_description`` keeps
    the plain-English explanation (the window renders it as a tooltip/row).
    """
    state = get_state()
    result = []
    for key, s in state.strategies.items():
        item = {
            "id": key,
            "name": s.name,
            "direction": s.direction,
            "mean_return_pct": s.mean_return_pct,
            "median_return_pct": s.median_return_pct,
            "t_stat": s.t_stat,
            "p_value": s.p_value,
            "sharpe": s.sharpe,
            "win_rate_pct": s.win_rate_pct,
            "std_dev_pct": s.std_dev_pct,
            "max_drawdown_pct": s.max_drawdown_pct,
            "n_observations": s.n_observations,
            "verdict": s.verdict,
            "passed_gates": s.passed_gates,
        }
        if include_description:
            item["description"] = s.description[:200] if s.description else ""
        result.append(item)
    result.sort(
        key=lambda x: (
            _VERDICT_RANK.get(x["verdict"], 0),
            abs(x["t_stat"] or 0.0),
            x["mean_return_pct"] or 0.0,
        ),
        reverse=True,
    )
    return result


def _auto_sync_loop() -> None:
    """Daemon: pull the GitHub repo every N seconds and re-parse.

    Deliberately silent on success (a background refresh must never spam),
    but a persistent sync_error lands in the state the window shows. A
    failed pull retries next tick — the leaderboard simply shows the last
    good data plus the error line.

    Guarantees _LAB_STATE is initialized on first tick (safe to start the
    auto-sync loop at boot, before any API call — v5.22.14).
    """
    while True:
        time.sleep(_AUTO_SYNC_INTERVAL_SECONDS)
        try:
            # Ensure state is initialized before the first real sync.
            global _LAB_STATE
            if _LAB_STATE is None:
                get_state()  # initializes + arms (redundant, but honest)
            with _LAB_LOCK:
                if _LAB_STATE is not None:
                    _sync()
        except Exception:  # noqa: BLE001 -- a background refresh never kills the app
            pass


def start_auto_sync() -> None:
    """Start the background GitHub auto-sync once (idempotent)."""
    global _auto_sync_started
    with _LAB_LOCK:
        if _auto_sync_started:
            return
        _auto_sync_started = True
    threading.Thread(target=_auto_sync_loop, daemon=True, name="atlas-lab-auto-sync").start()


def get_strategy_detail(strategy_id: str) -> dict | None:
    """Get full detail for one strategy."""
    state = get_state()
    s = state.strategies.get(strategy_id)
    if s is None:
        return None
    return asdict(s)


def get_reports() -> dict[str, str]:
    """Get the raw report markdowns."""
    state = get_state()
    return state.recent_reports


# --------------------------------------------------------------------------- #
# LLM prompt → strategy → backtest
# --------------------------------------------------------------------------- #

def _llm_chat(prompt: str, system: str = "") -> str:
    """Call the NVIDIA API (via the OpenAI-compatible SDK) with a prompt.

    Reuses the app's existing NVIDIA_API_KEY and config. Returns the response
    text, or raises RuntimeError with a clear message on failure.
    """
    from openai import OpenAI

    api_key = NVIDIA_API_KEY or os.environ.get("NVIDIA_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "NVIDIA_API_KEY is not set. The LLM-powered backtesting needs it. "
            "Add it to .env (see start.command's first-run onboarding)."
        )

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = OpenAI(api_key=api_key, base_url=NVIDIA_BASE_URL, timeout=60)
        response = client.chat.completions.create(
            model=NVIDIA_MODEL,
            messages=messages,
            temperature=0.3,
            # v13.7 (2026-09-03, user directive "maximize context windows
            # of everything"): was 2000, the smallest response cap left in
            # the codebase and the one most exposed to this repo's
            # recurring truncation landmine. NVIDIA_MODEL defaults to
            # nvidia/llama-3.1-nemotron-ultra-253b-v1, a REASONING model:
            # like every other backend that has bitten this project (see
            # dispatch.py's _DEFAULT_MAX_TOKENS comment for the three prior
            # occurrences), it spends the cap thinking inside ``content``
            # before the answer starts, so 2000 does not shorten a reply,
            # it deletes one. This function's real job is emitting whole
            # strategy JSON documents and report prose — both routinely
            # longer than 2000 tokens on their own, before any reasoning
            # preamble. 8000 matches the value atlas_proposals._llm_json
            # now uses against the SAME model, so the two ATLAS LLM paths
            # stop disagreeing. Overridable via ATLAS_LLM_MAX_TOKENS.
            max_tokens=_atlas_llm_max_tokens(),
        )
        text = response.choices[0].message.content or ""
        return text.strip()
    except Exception as exc:
        raise RuntimeError(f"LLM call failed: {exc}") from exc


_STRATEGY_SYSTEM_PROMPT = """You are a quantitative strategy analyst. Given a user's trading idea in natural language, you output a structured strategy specification in JSON format. The output must be ONLY valid JSON, no other text.

The JSON schema:
{
  "strategy_name": "short descriptive name",
  "strategy_type": "momentum" | "mean_reversion" | "breakout" | "calendar" | "cross_asset" | "volatility" | "seasonal" | "news" | "custom",
  "pair": "EURUSD or similar FX pair",
  "direction": "LONG" | "SHORT",
  "entry_condition": "concise description of the entry signal",
  "exit_condition": "concise description of the exit signal",
  "parameters": {
    "lookback": 20,
    "entry_threshold": 1.5,
    "exit_threshold": 0.5,
    "stop_loss_pct": 2.0,
    "take_profit_pct": 5.0,
    "max_holding_bars": 48
  },
  "explanation": "A 2-3 sentence plain-English explanation of what the strategy does and why it might work, suitable for a non-expert to understand."
}

Use reasonable defaults for parameters. The pair should be a major FX pair (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD) unless the user specifies otherwise.
"""


def prompt_to_strategy(prompt: str) -> dict[str, Any]:
    """Convert a natural-language prompt into a structured strategy via LLM.

    Returns a dict with the strategy specification, or raises RuntimeError.
    """
    raw = _llm_chat(prompt, system=_STRATEGY_SYSTEM_PROMPT)
    # Strip markdown code fences if the LLM wraps the JSON.
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"LLM output was not valid JSON. Raw response:\n{raw[:1000]}"
        ) from exc
    # Validate required fields.
    for key in ("strategy_name", "strategy_type", "pair", "explanation"):
        if key not in spec:
            raise RuntimeError(
                f"LLM output missing required field '{key}'. "
                f"Raw response:\n{raw[:1000]}"
            )
    return spec


# --------------------------------------------------------------------------- #
# Backtest execution
# --------------------------------------------------------------------------- #

def _make_backtest_id() -> str:
    """Generate a unique backtest ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"bt_{ts}_{os.urandom(2).hex()}"


def _run_backtest_worker(req_id: str) -> None:
    """Background worker: run the backtest and store the result.

    This runs the ATLAS engine's fx-research command via the CLI bridge,
    then parses the output into a readable report.
    """
    state = get_state()
    with _LAB_LOCK:
        req = state.backtest_requests.get(req_id)
        if req is None:
            return
        req.status = "running"
        req.progress = 0.05

    try:
        # Step 1: LLM parse the prompt into a strategy spec.
        spec = prompt_to_strategy(req.prompt)
        with _LAB_LOCK:
            if req_id in state.backtest_requests:
                state.backtest_requests[req_id].progress = 0.15

        # Step 2: Run the atlas fx-research with the strategy parameters.
        pair = spec.get("pair", req.pair)
        # We'll use the atlas CLI to run fx-research on the pair.
        from dourmouse.atlas_cli import run_atlas_cli

        with _LAB_LOCK:
            if req_id in state.backtest_requests:
                state.backtest_requests[req_id].progress = 0.25

        # Build a simple strategy description for the CLI.
        strategy_type = spec.get("strategy_type", "momentum")
        params = spec.get("parameters", {})
        lookback = params.get("lookback", 20)
        direction = spec.get("direction", "LONG")
        entry = spec.get("entry_condition", "momentum signal")
        exit_cond = spec.get("exit_condition", "reversal signal")

        explanation = spec.get("explanation", "Strategy description not provided.")

        # Run the research command. We use the managed CLI runner.
        code, stdout, stderr = run_atlas_cli(
            ["fx-research", "--pair", pair, "--strategy", strategy_type, "--windows", "3"],
            timeout=600,
        )

        with _LAB_LOCK:
            if req_id in state.backtest_requests:
                state.backtest_requests[req_id].progress = 0.85

        # Step 3: Parse the output into a report.
        report = _build_report(
            spec=spec,
            pair=pair,
            exit_code=code,
            raw_output=stdout,
            raw_error=stderr,
            explanation=explanation,
        )

        with _LAB_LOCK:
            if req_id in state.backtest_requests:
                state.backtest_requests[req_id].status = "done"
                state.backtest_requests[req_id].progress = 1.0
                state.backtest_requests[req_id].result = report

        # Broadcast the completed backtest to the SSE hub.
        try:
            if _hub is not None:
                _hub.broadcast({
                    "type": "backtest_completed",
                    "id": req_id,
                    "strategy_name": report.get("strategy_name", "?"),
                    "verdict": report.get("verdict", "?"),
                    "pair": pair,
                })
        except Exception:  # noqa: BLE001 -- broadcast never kills the worker
            pass

    except RuntimeError as exc:
        with _LAB_LOCK:
            if req_id in state.backtest_requests:
                state.backtest_requests[req_id].status = "failed"
                state.backtest_requests[req_id].error = str(exc)

    except Exception as exc:
        with _LAB_LOCK:
            if req_id in state.backtest_requests:
                state.backtest_requests[req_id].status = "failed"
                state.backtest_requests[req_id].error = f"Unexpected error: {exc}"


def _build_report(
    spec: dict,
    pair: str,
    exit_code: int,
    raw_output: str,
    raw_error: str,
    explanation: str,
) -> dict:
    """Build a structured backtest report from the raw CLI output.

    Parses the JSON output (if any) and produces a human-readable report
    with all the metrics a trader actually reads.
    """
    # Try to parse the CLI output as JSON (fx-research outputs JSON).
    parsed = {}
    try:
        # Find the JSON block in the output.
        lines = raw_output.strip().split("\n")
        for i, line in enumerate(lines):
            if line.strip().startswith("{"):
                candidate = "\n".join(lines[i:])
                parsed = json.loads(candidate)
                break
    except (json.JSONDecodeError, IndexError):
        pass

    # Extract metrics from the parsed output.
    validation = parsed.get("validation", {}) if isinstance(parsed, dict) else {}
    windows = parsed.get("windows", []) if isinstance(parsed, list) else []
    # If the output is a dict with pair keys, it's a multi-pair result.
    if isinstance(parsed, dict) and not validation:
        for key, val in parsed.items():
            if isinstance(val, dict) and "validation" in val:
                validation = val["validation"]
                break

    sharpe = validation.get("sharpe_periodic", validation.get("sharpe", None))
    mean_ret = validation.get("mean_return", validation.get("mean_pct", None))
    std_val = validation.get("std", validation.get("std_dev", None))
    win_rate = validation.get("win_rate", None)
    n_trades = validation.get("n_trades", validation.get("n", 0))

    # Compute t-stat from Sharpe if not directly available.
    t_stat = validation.get("t_stat", None)
    if t_stat is None and sharpe is not None and n_trades:
        t_stat = sharpe * (n_trades ** 0.5)

    # Compute approximate p-value from t-stat.
    p_value = None
    if t_stat is not None:
        # Crude normal approximation (t ≈ N(0,1) for large df).
        from math import erfc, sqrt
        # Two-tailed p-value: 2 * (1 - Φ(|t|))
        p_value = erfc(abs(t_stat) / sqrt(2))

    # Determine verdict.
    verdict = "HOLD"
    if exit_code != 0:
        verdict = "FAILED"
    elif sharpe is not None and sharpe > 0.5 and p_value is not None and p_value < 0.05:
        verdict = "PAPER TRADE"
    elif sharpe is not None and sharpe > 0.3 and p_value is not None and p_value < 0.10:
        verdict = "CANDIDATE"
    elif sharpe is not None:
        verdict = "HOLD"

    # Build the report.
    report = {
        "strategy_name": spec.get("strategy_name", "Custom strategy"),
        "strategy_type": spec.get("strategy_type", "custom"),
        "pair": pair,
        "direction": spec.get("direction", "LONG"),
        "entry_condition": spec.get("entry_condition", ""),
        "exit_condition": spec.get("exit_condition", ""),
        "parameters": spec.get("parameters", {}),
        "explanation": explanation,
        "metrics": {
            "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
            "mean_return_pct": round(mean_ret * 100, 2) if mean_ret is not None else None,
            "median_return_pct": None,
            "std_dev_pct": round(std_val * 100, 2) if std_val is not None else None,
            "t_statistic": round(t_stat, 3) if t_stat is not None else None,
            "p_value": round(p_value, 5) if p_value is not None else None,
            "win_rate_pct": round(win_rate * 100, 1) if win_rate is not None else None,
            "n_trades": n_trades,
            "max_drawdown_pct": None,
        },
        "verdict": verdict,
        "worth_paper_trading": verdict in ("PAPER TRADE", "CANDIDATE"),
        "claude_exit_code": exit_code,
        "raw_output_snippet": (raw_output.strip() or "")[:1000],
        "raw_error": (raw_error.strip() or "")[:500],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Add a human-readable summary.
    lines = [f"## {report['strategy_name']} — {pair} ({direction})"]
    lines.append(f"\n**Verdict: {verdict}**")
    if report["worth_paper_trading"]:
        lines.append("✅ *This strategy is worth paper trading.*")
    else:
        lines.append("⏸ *More observation needed before paper trading.*")

    lines.append(f"\n### What the strategy does")
    lines.append(explanation)

    lines.append(f"\n### Entry condition")
    lines.append(entry if entry else "See parameters.")
    lines.append(f"\n### Exit condition")
    lines.append(exit_cond if exit_cond else "See parameters.")

    lines.append(f"\n### Performance metrics")
    metrics = report["metrics"]
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    if metrics["sharpe_ratio"] is not None:
        lines.append(f"| Sharpe Ratio | {metrics['sharpe_ratio']} |")
    if metrics["mean_return_pct"] is not None:
        lines.append(f"| Mean Return | {metrics['mean_return_pct']}% |")
    if metrics["t_statistic"] is not None:
        lines.append(f"| t-statistic | {metrics['t_statistic']} |")
    if metrics["p_value"] is not None:
        lines.append(f"| p-value | {metrics['p_value']} |")
    if metrics["win_rate_pct"] is not None:
        lines.append(f"| Win Rate | {metrics['win_rate_pct']}% |")
    if metrics["n_trades"]:
        lines.append(f"| Number of Trades | {metrics['n_trades']} |")

    report["summary_markdown"] = "\n".join(lines)
    return report


def submit_backtest(prompt: str, pair: str = "EURUSD") -> dict[str, Any]:
    """Submit a new backtest request. Returns the request ID for polling."""
    state = get_state()
    req_id = _make_backtest_id()
    req = BacktestRequest(
        id=req_id,
        prompt=prompt,
        pair=pair,
        status="pending",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with _LAB_LOCK:
        state.backtest_requests[req_id] = req

    # Start the background worker.
    thread = threading.Thread(target=_run_backtest_worker, args=(req_id,), daemon=True)
    thread.start()

    return {
        "id": req_id,
        "status": "pending",
        "created_at": req.created_at,
    }


def get_backtest_status(req_id: str) -> dict[str, Any] | None:
    """Poll the status of a backtest request."""
    state = get_state()
    with _LAB_LOCK:
        req = state.backtest_requests.get(req_id)
        if req is None:
            return None
        return {
            "id": req.id,
            "status": req.status,
            "progress": req.progress,
            "result": req.result,
            "error": req.error,
            "created_at": req.created_at,
        }


def list_backtests(limit: int = 10) -> list[dict]:
    """List recent backtest requests."""
    state = get_state()
    with _LAB_LOCK:
        items = []
        for req_id, req in state.backtest_requests.items():
            items.append({
                "id": req.id,
                "prompt": req.prompt[:100],
                "pair": req.pair,
                "status": req.status,
                "progress": req.progress,
                "created_at": req.created_at,
            })
        items.sort(key=lambda x: x["created_at"], reverse=True)
        return items[:limit]


def get_latest_backtest() -> dict[str, Any] | None:
    """v5.22.15: the most recently COMPLETED backtest, or None.

    Used by the daily briefing to include the latest backtest results
    (sharpe, t-stat, p-value, mean/median return, std dev). Returns None
    when no backtest has ever completed — honest, never fabricated."""
    state = get_state()
    with _LAB_LOCK:
        best: BacktestRequest | None = None
        for req in state.backtest_requests.values():
            if req.status != "done" or req.result is None:
                continue
            if best is None or req.created_at > best.created_at:
                best = req
        if best is None:
            return None
        m = best.result.get("metrics", {}) if isinstance(best.result, dict) else {}
        return {
            "id": best.id,
            "prompt": (best.prompt or "")[:150],
            "pair": best.pair,
            "strategy_name": best.result.get("strategy_name", "?") if isinstance(best.result, dict) else "?",
            "verdict": best.result.get("verdict", "?") if isinstance(best.result, dict) else "?",
            "created_at": best.created_at,
            "sharpe_ratio": m.get("sharpe_ratio"),
            "mean_return_pct": m.get("mean_return_pct"),
            "median_return_pct": m.get("median_return_pct"),
            "std_dev_pct": m.get("std_dev_pct"),
            "t_statistic": m.get("t_statistic"),
            "p_value": m.get("p_value"),
            "win_rate_pct": m.get("win_rate_pct"),
            "n_trades": m.get("n_trades"),
        }