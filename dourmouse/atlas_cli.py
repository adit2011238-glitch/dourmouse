"""ATLAS CLI bridge (v5.4) — real access to the current ATLAS quant engine.

The ATLAS project ships a rich CLI (``atlas/ops/cli.py``): fx-research,
fx-daily, fx-refresh, fx-verify, fx-backfill, health, fx-universe, version.
Per Integration Rule 7.1 this module NEVER reimplements ATLAS logic — it
locates the real ATLAS repo (``ATLAS_REPO_PATH``) and its own venv
(``ATLAS_VENV_PATH``) and subprocess-invokes ``python -m atlas.ops.cli``
there, returning the REAL output. Every failure (missing config, non-zero
exit, timeout, exec error) is reported honestly (Rule 2.2) — never a
fabricated result.

Also owns:

- ``atlas_read_report`` — a pure-filesystem read of the newest (or a dated)
  ``deliverables/fx/*.md`` report, so agents can actually read ATLAS's
  written research instead of only listing it.
- ``AtlasRunManager`` — the single-flight runner behind the HUD's
  ``[FX-DAILY]`` button (POST /api/atlas/run): at most one ATLAS command
  runs at a time and its live state is polled via GET /api/atlas.
- ``atlas_panel_snapshot()`` — the whole GET /api/atlas payload, kept here
  as a pure function so webui.py stays a thin shim and the panel logic is
  unit-testable.
"""

from __future__ import annotations

import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dourmouse.research_agent import (
    AtlasNotConfiguredError,
    get_atlas_repo_path,
    get_atlas_venv_python,
)

# CLI output is capped from the END (research verdicts and health alerts
# land in the tail — the head is usually a big JSON header).
_OUTPUT_CAP = 30_000
_MANAGER_TAIL_CAP = 4_000
_REPORT_SNIPPET_CHARS = 600
_DEFAULT_TIMEOUT = 300

# Commands the HUD run button may launch (safe, read-only or idempotent).
# fx-backfill / fx-research are intentionally NOT here: backfill is a
# long-running bootstrap and research needs per-run parameters — both stay
# tools on the atlas subagent.
_MANAGER_COMMANDS: dict[str, tuple[list[str], int]] = {
    "version": (["version"], 60),
    "health": (["health", "--no-providers"], 120),
    "fx-universe": (["fx-universe"], 120),
    "fx-verify": (["fx-verify"], 900),
    "fx-refresh": (["fx-refresh"], 1200),
    "fx-daily": (["fx-daily"], 1800),
    "fx-daily-no-refresh": (["fx-daily", "--no-refresh"], 1800),
}


def _cap(text: str, limit: int = _OUTPUT_CAP) -> str:
    """Tail-cap ``text``; appends an honest truncation marker when cut."""
    if len(text) <= limit:
        return text
    return "…[truncated " + str(len(text) - limit) + " chars]…\n" + text[-limit:]


# --------------------------------------------------------------------------- #
# The bridge — run one real ATLAS CLI command
# --------------------------------------------------------------------------- #

def run_atlas_cli(argv: list[str], timeout: int = _DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Run ``atlas <argv>`` in the real repo/venv; returns (exit, stdout, stderr).

    Raises ``AtlasNotConfiguredError`` when ATLAS_REPO_PATH / ATLAS_VENV_PATH
    are missing or invalid. Never falls back to fabricated output (Rule 2.2).
    """
    repo = get_atlas_repo_path()
    python = get_atlas_venv_python()
    proc = subprocess.run(
        [str(python), "-m", "atlas.ops.cli", *argv],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,  # non-zero exits are surfaced, never raised
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _format_cli_run(label: str, argv: list[str], timeout: int) -> str:
    """Run one command and format its REAL output for a tool handler."""
    try:
        code, out, err = run_atlas_cli(argv, timeout)
    except AtlasNotConfiguredError as exc:
        return f"ATLAS {label} (reported honestly): NOT CONFIGURED — {exc}"
    except subprocess.TimeoutExpired:
        # subprocess.run KILLS the child on timeout — the command is NOT
        # still running. Say so honestly (Rule 2.2): a killed backfill or
        # research run can leave partial state in the repo.
        return (
            f"ATLAS {label}: terminated after {timeout}s — the ATLAS CLI "
            "did not finish in time and was killed. Check the repo's logs/"
            "data for partial state and re-run if needed; nothing was "
            "fabricated."
        )
    except OSError as exc:
        return f"ATLAS {label}: could not run the ATLAS CLI: {exc}"
    parts = [f"ATLAS COMMAND: atlas {' '.join(argv)}", f"EXIT CODE: {code}"]
    if out:
        parts.append("STDOUT:\n" + _cap(out))
    if err:
        parts.append("STDERR:\n" + _cap(err))
    if code != 0 and not out:
        parts.append(
            "(command failed — see STDERR for the real error; nothing was fabricated.)"
        )
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Report reading (pure filesystem — no subprocess)
# --------------------------------------------------------------------------- #

def _reports_dir() -> Path:
    """deliverables/fx under the real repo (raises honestly when unset)."""
    return get_atlas_repo_path() / "deliverables" / "fx"


def _read_report_md(target: Path, snippet_chars: int = _REPORT_SNIPPET_CHARS) -> dict[str, Any]:
    """One dated report: name, modified iso, full body (capped) + snippet."""
    body = target.read_text(errors="replace")
    try:
        mtime = datetime.fromtimestamp(
            target.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
    except OSError:
        mtime = ""
    return {
        "name": target.name,
        "path": str(target.relative_to(get_atlas_repo_path())),
        "modified": mtime,
        "size": len(body),
        "body": body[: _OUTPUT_CAP],
        "snippet": body[:snippet_chars],
    }


def latest_fx_report() -> dict[str, Any] | None:
    """The newest deliverables/fx/YYYY-MM-DD.md, or None (honest)."""
    try:
        base = _reports_dir()
    except AtlasNotConfiguredError:
        return None
    if not base.is_dir():
        return None
    files = sorted(base.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return _read_report_md(files[0]) if files else None


_REPORT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def read_fx_report(date: str) -> dict[str, Any]:
    """A specific dated report deliverables/fx/<date>.md.

    ``date`` must be exactly YYYY-MM-DD — anything path-like (e.g. ``..`` or
    a slash) is REFUSED so the read can never escape deliverables/fx/
    (reviewer-caught path-traversal guard). Raises ValueError for a bad
    date, AtlasNotConfiguredError when the repo is unset, and
    FileNotFoundError (honest) when that date has no report.
    """
    date = date.strip()
    if not _REPORT_DATE_RE.match(date):
        raise ValueError(
            f"ATLAS report 'date' must be YYYY-MM-DD (e.g. 2026-08-06) — got "
            f"{date!r}; refusing a path-like date (honest)."
        )
    base = _reports_dir()
    target = base / f"{date}.md"
    if not target.is_file():
        raise FileNotFoundError(
            f"no ATLAS report for {date} at {target} (honest — nothing fabricated)"
        )
    return _read_report_md(target)


def _atlas_read_report_tool(arguments: dict[str, Any]) -> str:
    try:
        get_atlas_repo_path()  # raises honestly when ATLAS is unconfigured
    except AtlasNotConfiguredError as exc:
        return f"ATLAS REPORT (reported honestly): NOT CONFIGURED — {exc}"
    report: dict[str, Any] | None
    try:
        if arguments.get("date"):
            report = read_fx_report(str(arguments["date"]).strip())
        else:
            report = latest_fx_report()
    except ValueError as exc:
        return f"ERROR: {exc}"
    except FileNotFoundError as exc:
        return f"ATLAS REPORT: {exc}"
    if report is None:
        return "ATLAS REPORT: none yet under deliverables/fx/ (honest)."
    return (
        f"ATLAS FX REPORT: {report['name']} ({report['modified']}, {report['size']} chars)\n"
        "---\n"
        + report["body"]
    )


# --------------------------------------------------------------------------- #
# Version (cached briefly — cheap, but not per-6s-poll cheap)
# --------------------------------------------------------------------------- #

_version_cache: dict[str, Any] = {"at": 0.0, "value": None}
_VERSION_TTL_SECONDS = 60.0


def atlas_version() -> str | None:
    """The real ATLAS package version, or None honestly (never raises)."""
    now = datetime.now(timezone.utc).timestamp()
    if now - _version_cache["at"] < _VERSION_TTL_SECONDS:
        return _version_cache["value"]
    try:
        code, out, _err = run_atlas_cli(["version"], timeout=60)
        value = out.strip() if (code == 0 and out.strip()) else None
    except Exception:  # noqa: BLE001 -- version probe must never crash the panel
        return None  # do NOT cache a failure — the next poll retries honestly
    if value is not None:
        _version_cache.update({"at": now, "value": value})
    return value


# --------------------------------------------------------------------------- #
# ToolSpecs for the atlas subagent (appended by atlas_ops.build_atlas_tool_specs)
# --------------------------------------------------------------------------- #

def _spec(
    name: str,
    description: str,
    handler: Any,
    props: dict[str, Any],
    required: list[str] | None = None,
) -> Any:
    from dourmouse.dispatch import ToolSpec

    return ToolSpec(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": props,
            "required": required or [],
        },
        handler=handler,
    )


def _atlas_version_tool(arguments: dict[str, Any]) -> str:
    return _format_cli_run("VERSION", ["version"], 60)


def _atlas_health_tool(arguments: dict[str, Any]) -> str:
    include = bool(arguments.get("include_providers", False))
    argv = ["health"] if include else ["health", "--no-providers"]
    return _format_cli_run("HEALTH", argv, 120)


def _atlas_fx_universe_tool(arguments: dict[str, Any]) -> str:
    return _format_cli_run("FX-UNIVERSE", ["fx-universe"], 120)


def _atlas_fx_verify_tool(arguments: dict[str, Any]) -> str:
    return _format_cli_run("FX-VERIFY", ["fx-verify"], 900)


def _atlas_fx_refresh_tool(arguments: dict[str, Any]) -> str:
    return _format_cli_run("FX-REFRESH", ["fx-refresh"], 1200)


def _atlas_fx_research_tool(arguments: dict[str, Any]) -> str:
    pairs = (arguments.get("pairs") or "").strip()
    if not pairs:
        return "ERROR: atlas_fx_research requires a non-empty 'pairs' (comma-separated codes, e.g. EURUSD,GBPUSD)."
    strategy = (arguments.get("strategy") or "").strip()
    argv = ["fx-research", "--pairs", pairs]
    if strategy:
        argv += ["--strategy", strategy]
    elif arguments.get("all_strategies", True):
        argv += ["--all-strategies"]
    else:
        return "ERROR: atlas_fx_research needs 'strategy' (a name) or 'all_strategies' (default true)."
    for param, flag in (
        ("sessions", "--sessions"),
        ("ensemble", "--ensemble"),
        ("ensemble_method", "--ensemble-method"),
    ):
        value = (arguments.get(param) or "").strip()
        if value:
            argv += [flag, value]
    for param, flag in (("windows", "--windows"), ("commission_bps", "--commission-bps")):
        try:
            value = int(arguments.get(param, 0))
        except (TypeError, ValueError):
            return f"ERROR: {param} must be an integer."
        if value:
            argv += [flag, str(value)]
    if arguments.get("no_ic_screen"):
        argv += ["--no-ic-screen"]
    return _format_cli_run("FX-RESEARCH", argv, 1800)


def _atlas_fx_daily_tool(arguments: dict[str, Any]) -> str:
    argv = ["fx-daily"]
    if arguments.get("no_refresh"):
        argv += ["--no-refresh"]
    for param, flag in (("pairs", "--pairs"), ("strategies", "--strategies"), ("sessions", "--sessions"), ("report_dir", "--report-dir")):
        value = (arguments.get(param) or "").strip()
        if value:
            argv += [flag, value]
    try:
        windows = int(arguments.get("windows", 0))
    except (TypeError, ValueError):
        return "ERROR: windows must be an integer."
    if windows:
        argv += ["--windows", str(windows)]
    return _format_cli_run("FX-DAILY", argv, 1800)


def _atlas_fx_backfill_tool(arguments: dict[str, Any]) -> str:
    pairs = (arguments.get("pairs") or "").strip()
    start = (arguments.get("start") or "").strip()
    end = (arguments.get("end") or "").strip()
    if not pairs or not start or not end:
        return "ERROR: atlas_fx_backfill requires 'pairs', 'start' (YYYY-MM-DD) and 'end' (YYYY-MM-DD)."
    argv = ["fx-backfill", "--pairs", pairs, "--start", start, "--end", end]
    if arguments.get("force"):
        argv += ["--force"]
    return _format_cli_run("FX-BACKFILL", argv, 3600)


def build_atlas_cli_specs() -> list[Any]:
    """The v5.4 CLI-bridge ToolSpecs for the ``atlas`` subagent."""
    return [
        _spec(
            "atlas_version",
            "The REAL ATLAS package version (atlas version).",
            _atlas_version_tool,
            {},
        ),
        _spec(
            "atlas_health",
            "Run ATLAS's real health check (atlas health). Returns the JSON "
            "health report and the honest healthy/alerts verdict.",
            _atlas_health_tool,
            {
                "include_providers": {
                    "type": "boolean",
                    "default": False,
                    "description": "also probe market-data providers (network); default skips them for speed",
                }
            },
        ),
        _spec(
            "atlas_fx_universe",
            "The real FX pairs registry (atlas fx-universe): code, base, "
            "quote, decimals, pip, yahoo symbol.",
            _atlas_fx_universe_tool,
            {},
        ),
        _spec(
            "atlas_fx_verify",
            "Integrity-check the deep FX 1m archive (atlas fx-verify): raw vs "
            "parquet consistency, truncation. Read-only; can be slow on a "
            "large archive.",
            _atlas_fx_verify_tool,
            {},
        ),
        _spec(
            "atlas_fx_refresh",
            "Keep the deep FX 1m archive current (atlas fx-refresh, "
            "idempotent network refresh). Can take minutes.",
            _atlas_fx_refresh_tool,
            {},
        ),
        _spec(
            "atlas_fx_research",
            "Run ATLAS's REAL walk-forward validation of FX scalping "
            "strategies on archived 1m bars (atlas fx-research). Defaults to "
            "sweeping every registered strategy across the pair(s); pass "
            "'strategy' to focus on one. Returns the honest per-pair "
            "verdicts ATLAS itself computes.",
            _atlas_fx_research_tool,
            {
                "pairs": {"type": "string", "description": "comma-separated pair codes, e.g. EURUSD,GBPUSD"},
                "strategy": {"type": "string", "description": "one strategy name (donchian_breakout, mean_reversion, trend_follow, or a registered TA-library name)"},
                "all_strategies": {"type": "boolean", "default": True, "description": "sweep every registered strategy (used when 'strategy' is empty)"},
                "sessions": {"type": "string", "description": "comma-separated UTC sessions: tokyo,london,new_york (default 24/5)"},
                "windows": {"type": "integer", "description": "rolling walk-forward windows"},
                "commission_bps": {"type": "integer", "description": "per-side commission in bps"},
                "ensemble": {"type": "string", "description": "comma-separated constituent strategies to combine into one ensemble"},
                "ensemble_method": {"type": "string", "description": "equal_weight or ic_weighted"},
                "no_ic_screen": {"type": "boolean", "description": "skip the pre-registration IC screen (not recommended)"},
            },
            ["pairs"],
        ),
        _spec(
            "atlas_fx_daily",
            "Run the ATLAS permanent-connection loop (atlas fx-daily): "
            "refresh the archive and produce today's dated research report "
            "under deliverables/fx/. Pass 'no_refresh' to research the "
            "archive as it is (offline mode).",
            _atlas_fx_daily_tool,
            {
                "no_refresh": {"type": "boolean", "default": False, "description": "skip the network refresh; research the archive as it is"},
                "pairs": {"type": "string", "description": "comma-separated pair codes (default: the 7 majors)"},
                "strategies": {"type": "string", "description": "comma-separated strategy names (default: all)"},
                "sessions": {"type": "string", "description": "comma-separated UTC sessions: tokyo,london,new_york"},
                "windows": {"type": "integer", "description": "rolling walk-forward windows"},
                "report_dir": {"type": "string", "description": "report output directory (default deliverables/fx)"},
            },
        ),
        _spec(
            "atlas_fx_backfill",
            "Backfill the deep FX 1m archive from Dukascopy (atlas "
            "fx-backfill). LONG-RUNNING network bootstrap (minutes to hours "
            "for deep ranges) — prefer the background bootstrap runners; "
            "this tool runs it synchronously with a generous timeout.",
            _atlas_fx_backfill_tool,
            {
                "pairs": {"type": "string", "description": "comma-separated pair codes, e.g. EURUSD,USDJPY"},
                "start": {"type": "string", "description": "inclusive start date YYYY-MM-DD"},
                "end": {"type": "string", "description": "inclusive end date YYYY-MM-DD"},
                "force": {"type": "boolean", "default": False, "description": "re-download raw days that already exist"},
            },
            ["pairs", "start", "end"],
        ),
        _spec(
            "atlas_read_report",
            "Read ATLAS's newest written FX report (deliverables/fx/YYYY-MM-DD.md), "
            "or a specific date. Returns the real report text — never a summary.",
            _atlas_read_report_tool,
            {"date": {"type": "string", "description": "optional report date YYYY-MM-DD (default: newest)"}},
        ),
    ]


# --------------------------------------------------------------------------- #
# Single-flight run manager (HUD [FX-DAILY] button)
# --------------------------------------------------------------------------- #

class AtlasRunManager:
    """At most one managed ATLAS command at a time; state is polled by the HUD.

    Deterministic (Rule 2.8): a launch while one is running is REFUSED with
    ``False`` — never queued, never double-run. State carries the REAL exit
    code and a capped output tail of the last completed run so the panel can
    show what actually happened (Rule 2.1).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "command": None,
            "running": False,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "tail": "",
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def launch(self, command: str) -> bool:
        """Start ``command`` in a daemon thread; False when one is running."""
        entry = _MANAGER_COMMANDS.get(command)
        if entry is None:
            raise ValueError(
                f"unknown managed command {command!r} — allowed: {', '.join(sorted(_MANAGER_COMMANDS))}"
            )
        argv, timeout = entry
        with self._lock:
            if self._state["running"]:
                return False
            self._state.update(
                {
                    "command": command,
                    "running": True,
                    "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "finished_at": None,
                    "exit_code": None,
                    "tail": "",
                }
            )
        threading.Thread(
            target=self._run, args=(command, argv, timeout), daemon=True
        ).start()
        return True

    def _run(self, command: str, argv: list[str], timeout: int) -> None:
        try:
            code, out, err = run_atlas_cli(argv, timeout)
            tail = _cap(((out or "") + ("\n" + err if err else "")).strip(), _MANAGER_TAIL_CAP)
        except Exception as exc:  # noqa: BLE001 -- honest failure surface
            code, tail = -1, f"MANAGED RUN FAILED (reported honestly): {exc}"
        with self._lock:
            self._state.update(
                {
                    "running": False,
                    "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "exit_code": code,
                    "tail": tail,
                }
            )

    def spewrate(self) -> dict[str, Any]:
        """Real measured output rate of ATLAS work — never fabricated.

        Units are **candidates evaluated per second**, parsed from the actual
        fx-daily summary JSON (``"evaluated": N``) in the captured output,
        divided by the real wall-clock elapsed time of the run.

        Honest states (Rule 2.2):

        - ``idle`` — no managed run has happened yet; rate ``None``.
        - ``running`` — a run is in progress; rate ``None`` (no work count
          is known until it finishes), with live elapsed seconds.
        - ``done`` — last run finished; rate computed when the output
          carries a parseable evaluated count, else ``None`` with a note.
        """
        with self._lock:
            state = dict(self._state)
        started = state.get("started_at")
        finished = state.get("finished_at")
        if not started:
            return {
                "state": "idle",
                "unit": "candidates/s",
                "rate": None,
                "work": None,
                "elapsed_s": None,
                "note": "no managed ATLAS run yet — spewrate appears after the first [FX-DAILY]",
            }
        start_dt = datetime.fromisoformat(started)
        end_dt = datetime.fromisoformat(finished) if finished else datetime.now(timezone.utc)
        elapsed = max((end_dt - start_dt).total_seconds(), 1e-9)
        if state.get("running"):
            return {
                "state": "running",
                "unit": "candidates/s",
                "rate": None,
                "work": None,
                "elapsed_s": round(elapsed, 1),
                "note": "measuring… work count lands when the run finishes",
            }
        work = _evaluated_from_tail(state.get("tail") or "")
        if work is None:
            return {
                "state": "done",
                "unit": "candidates/s",
                "rate": None,
                "work": None,
                "elapsed_s": round(elapsed, 1),
                "note": "last run finished but its output carried no parseable evaluated count",
            }
        return {
            "state": "done",
            "unit": "candidates/s",
            "rate": round(work / elapsed, 3),
            "work": work,
            "elapsed_s": round(elapsed, 1),
            "note": "measured from the real run output and wall-clock time",
        }


def _evaluated_from_tail(tail: str) -> int | None:
    """Parse ATLAS's real ``"evaluated": N`` summary field from run output.

    Returns ``None`` (not 0) when absent, so callers can distinguish "no
    work count in the output" from a genuinely empty evaluation.
    """
    match = re.search(r'"evaluated"\s*:\s*(\d+)', tail)
    return int(match.group(1)) if match else None


atlas_run_manager = AtlasRunManager()


# --------------------------------------------------------------------------- #
# HUD panel payload (pure function — unit-testable without a web server)
# --------------------------------------------------------------------------- #

def atlas_panel_snapshot() -> dict[str, Any]:
    """The complete GET /api/atlas payload.

    ``configured: False`` with the exact reason when ATLAS isn't set up
    (Rule 2.2 — never a fabricated status). When configured, mixes real
    telemetry (repo status, bootstrap progress, deliverables) with the
    managed-run state and the newest written report.
    """
    try:
        repo = get_atlas_repo_path()
    except AtlasNotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}

    from dourmouse import atlas_ops

    payload: dict[str, Any] = {
        "configured": True,
        "repo": str(repo),
        "version": atlas_version(),
    }
    for key, fn in (
        ("status", atlas_ops.atlas_status),
        ("bootstrap", atlas_ops.atlas_bootstrap_status),
    ):
        try:
            payload[key] = fn()
        except Exception as exc:  # noqa: BLE001 -- one section never kills the panel
            payload[key] = {"error": f"section failed (reported honestly): {exc}"}
    try:
        payload["deliverables"] = atlas_ops.atlas_deliverables(limit=3)
    except Exception:  # noqa: BLE001
        payload["deliverables"] = []
    payload["latest_report"] = latest_fx_report()
    payload["last_run"] = atlas_run_manager.snapshot()
    payload["spewrate"] = atlas_run_manager.spewrate()
    return payload
