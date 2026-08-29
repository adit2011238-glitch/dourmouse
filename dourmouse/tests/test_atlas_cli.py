"""v5.4 ATLAS CLI-bridge tests (atlas_cli.py + webui/setup wiring).

Every test is hermetic (Rule 2.1): a fake ATLAS-shaped repo + a fake
executable venv python under tmp_path, real subprocesses that just echo
their argv — no network, no real ATLAS repo. Verifies:

- run_atlas_cli builds the correct ``python -m atlas.ops.cli <argv>`` command
- honest NOT CONFIGURED / non-zero-exit / timeout degradation (Rule 2.2)
- each tool's argv construction (health providers flag, research sweep
  defaults, daily --no-refresh, backfill required args)
- atlas_read_report: newest by default, specific date, honest missing
- AtlasRunManager single-flight semantics
- the atlas subagent actually carries the new tools
- the HUD panel payload (atlas_panel_snapshot) and the SETUP row
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dourmouse import atlas_cli
from dourmouse.atlas_cli import AtlasNotConfiguredError
from dourmouse.general_roster import build_general_registry

_ECHO_SCRIPT = "#!/usr/bin/env bash\necho \"FAKE_RAN:$*\"\nexit 0\n"
_FAIL_SCRIPT = "#!/usr/bin/env bash\necho 'boom on stderr' >&2\nexit 3\n"
_SLEEP_SCRIPT = "#!/usr/bin/env bash\nsleep 5\n"

# Windows cannot execute POSIX bash scripts. The Windows fake is a REAL
# interpreter copy plus a REAL atlas.ops.cli module in the fake repo — the
# exact subprocess path production takes (`python -m atlas.ops.cli`), with
# the same observable behavior as each POSIX bash fake.
_FAKE_CLI_MODULE = {
    _ECHO_SCRIPT: "import sys\nprint('FAKE_RAN:' + ' '.join(sys.argv[1:]))\n",
    _FAIL_SCRIPT: "import sys\nprint('boom on stderr', file=sys.stderr)\nsys.exit(3)\n",
    _SLEEP_SCRIPT: "import time\ntime.sleep(5)\n",
}


def _fake_atlas(
    tmp_path: Path, monkeypatch, script: str = _ECHO_SCRIPT
) -> Path:
    """Fake ATLAS repo + fake venv python; sets both env vars."""
    repo = tmp_path / "atlas"
    (repo / "deliverables" / "fx").mkdir(parents=True)
    (repo / "data" / "fx_archive" / "raw" / "EURUSD" / "2023" / "01").mkdir(parents=True)
    (repo / "data" / "fx_archive" / "raw" / "EURUSD" / "2023" / "01" / "01_bid.bi5").write_bytes(b"x")
    (repo / "deliverables" / "fx" / "2026-08-06.md").write_text(
        "# ATLAS report 2026-08-06\nverdict: PASS\n"
    )
    (repo / "deliverables" / "fx" / "2026-08-05.md").write_text("# older\n")
    # Deterministic "newest": 08-06 must sort AFTER 08-05 by mtime (APFS
    # can tie two same-millisecond writes, which would flake the test).
    os.utime(repo / "deliverables" / "fx" / "2026-08-06.md", (2_000_000_000, 2_000_000_000))
    os.utime(repo / "deliverables" / "fx" / "2026-08-05.md", (1_000_000_000, 1_000_000_000))
    venv = tmp_path / "venv"
    if os.name == "nt":
        # Real interpreter copy + real module: bash can't run on Windows.
        import shutil as _shutil
        import sys as _sys

        bindir = venv / "Scripts"
        bindir.mkdir(parents=True)
        base = Path(_sys._base_executable)
        _shutil.copyfile(base, bindir / "python.exe")
        for dll in base.parent.glob("*.dll"):
            _shutil.copyfile(dll, bindir / dll.name)
        ops = repo / "atlas" / "ops"
        ops.mkdir(parents=True)
        (repo / "atlas" / "__init__.py").write_text("")
        (ops / "__init__.py").write_text("")
        (ops / "cli.py").write_text(_FAKE_CLI_MODULE[script])
        # PYTHONHOME silences "Could not find platform independent
        # libraries" from a copied interpreter; -m still resolves the fake
        # module from the repo cwd.
        monkeypatch.setenv("PYTHONHOME", str(base.parent))
    else:
        bindir = venv / "bin"
        bindir.mkdir(parents=True)
        py = bindir / "python"
        py.write_text(script)
        py.chmod(0o755)
    monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
    monkeypatch.setenv("ATLAS_VENV_PATH", str(venv))
    return repo


class TestConfig:
    def test_repo_missing_env(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.setenv("ATLAS_VENV_PATH", "/tmp/venv")
        with pytest.raises(AtlasNotConfiguredError):
            atlas_cli.run_atlas_cli(["version"])

    def test_venv_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATLAS_REPO_PATH", str(tmp_path / "atlas"))
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)
        with pytest.raises(AtlasNotConfiguredError):
            atlas_cli.run_atlas_cli(["version"])


class TestRunAtlasCli:
    def test_builds_real_argv(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        code, out, err = atlas_cli.run_atlas_cli(
            ["fx-research", "--pairs", "EURUSD", "--all-strategies"]
        )
        assert code == 0
        assert err == ""
        assert "fx-research --pairs EURUSD --all-strategies" in out

    def test_nonzero_exit_is_honest(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch, script=_FAIL_SCRIPT)
        code, _out, err = atlas_cli.run_atlas_cli(["fx-universe"])
        assert code == 3
        assert "boom on stderr" in err

    def test_timeout_raises_honestly(self, tmp_path, monkeypatch):
        """subprocess.run raises TimeoutExpired; the tool formats it honestly."""
        _fake_atlas(
            tmp_path, monkeypatch, script=_SLEEP_SCRIPT,
        )
        with pytest.raises(subprocess.TimeoutExpired):
            _, _, _ = atlas_cli.run_atlas_cli(["version"], timeout=1)

    def test_timeout_tool_message_is_honest(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)

        def _boom(argv, timeout):
            raise subprocess.TimeoutExpired(cmd="atlas", timeout=timeout)

        monkeypatch.setattr(atlas_cli, "run_atlas_cli", _boom)
        out = atlas_cli._atlas_version_tool({})
        assert "terminated after" in out
        assert "was killed" in out


class TestToolHandlers:
    def test_version_tool(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_version_tool({})
        assert "ATLAS COMMAND: atlas version" in out
        assert "EXIT CODE: 0" in out

    def test_health_default_skips_providers(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_health_tool({})
        assert "atlas health --no-providers" in out

    def test_health_can_include_providers(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_health_tool({"include_providers": True})
        assert "atlas health" in out
        assert "--no-providers" not in out

    def test_fx_research_defaults_to_sweep(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_fx_research_tool({"pairs": "EURUSD,GBPUSD"})
        assert "atlas fx-research --pairs EURUSD,GBPUSD --all-strategies" in out

    def test_fx_research_focused_strategy(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_fx_research_tool(
            {"pairs": "EURUSD", "strategy": "mean_reversion", "sessions": "london", "windows": 4, "commission_bps": 1}
        )
        assert "--strategy mean_reversion" in out
        assert "--sessions london" in out
        assert "--windows 4" in out
        assert "--commission-bps 1" in out
        assert "--all-strategies" not in out

    def test_fx_research_requires_pairs(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_fx_research_tool({})
        assert "ERROR" in out and "pairs" in out

    def test_fx_daily_no_refresh(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_fx_daily_tool({"no_refresh": True, "pairs": "EURUSD"})
        assert "atlas fx-daily --no-refresh --pairs EURUSD" in out

    def test_fx_daily_default_no_network_flag(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_fx_daily_tool({})
        assert "atlas fx-daily" in out
        assert "--no-refresh" not in out

    def test_backfill_requires_dates(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_fx_backfill_tool({"pairs": "EURUSD"})
        assert "ERROR" in out

    def test_backfill_argv(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_fx_backfill_tool(
            {"pairs": "EURUSD", "start": "2024-01-01", "end": "2024-01-31", "force": True}
        )
        assert "--start 2024-01-01 --end 2024-01-31 --force" in out

    def test_read_report_newest(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_read_report_tool({})
        assert "2026-08-06.md" in out
        assert "verdict: PASS" in out

    def test_read_report_dated(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_read_report_tool({"date": "2026-08-05"})
        assert "2026-08-05.md" in out

    def test_read_report_missing_date_honest(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_read_report_tool({"date": "1999-01-01"})
        assert "no ATLAS report for 1999-01-01" in out

    def test_read_report_rejects_path_like_date(self, tmp_path, monkeypatch):
        """Reviewer-caught: the date param must never build a path (traversal)."""
        _fake_atlas(tmp_path, monkeypatch)
        for bad in ("../../README", "..", "2026-08-06/../../secret", "2026/08/06"):
            out = atlas_cli._atlas_read_report_tool({"date": bad})
            assert "must be YYYY-MM-DD" in out, bad
            assert "refusing" in out, bad

    def test_not_configured_handlers(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)
        for fn, args in (
            (atlas_cli._atlas_version_tool, {}),
            (atlas_cli._atlas_fx_research_tool, {"pairs": "EURUSD", "strategy": "mean_reversion"}),
            (atlas_cli._atlas_read_report_tool, {}),
        ):
            out = fn(args)
            assert "NOT CONFIGURED" in out

    def test_champions_default_path(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_champions_tool({})
        assert "atlas champions atlas/data/champions.json --top 10" in out

    def test_champions_custom_path_and_top(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_champions_tool({"path": "custom.json", "top": 3})
        assert "atlas champions custom.json --top 3" in out

    def test_meta_model_bare(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_meta_model_tool({})
        assert "atlas meta-model" in out
        assert "--experiment-db" not in out

    def test_meta_model_with_db_and_json(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_meta_model_tool({"experiment_db": "exp.db", "json": True})
        assert "--experiment-db exp.db" in out
        assert "--json" in out

    def test_coverage_bare(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_coverage_tool({})
        assert "atlas coverage" in out

    def test_coverage_store_dir(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_coverage_tool({"store_dir": "mystore"})
        assert "--store-dir mystore" in out

    def test_adjustments_bare(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_adjustments_tool({})
        assert "atlas adjustments" in out

    def test_verify_audit_requires_path(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_verify_audit_tool({})
        assert "ERROR" in out and "path" in out

    def test_verify_audit_argv(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_verify_audit_tool({"path": "audit.log"})
        assert "atlas verify-audit audit.log" in out

    def test_refresh_store_requires_symbols(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_refresh_store_tool({})
        assert "ERROR" in out and "symbols" in out

    def test_refresh_store_argv(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_refresh_store_tool(
            {"symbols": "SPY,EURUSD=X", "interval": "5m", "lookback_days": 30}
        )
        assert "--symbols SPY,EURUSD=X" in out
        assert "--interval 5m" in out
        assert "--lookback-days 30" in out

    def test_literature_cycle_bare(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_literature_cycle_tool({})
        assert "atlas literature-cycle" in out
        assert "--realism" not in out

    def test_literature_cycle_full_argv(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_literature_cycle_tool(
            {
                "limit": 200,
                "trial_registry": "trials.db",
                "experiment_db": "experiments.db",
                "cutoff_date": "2020-01-01",
                "lookback_years": 5,
                "realism": True,
                "capital": 10_000_000,
                "weighting": "equal_weight",
                "meta_model": True,
            }
        )
        assert "--limit 200" in out
        assert "--trial-registry trials.db" in out
        assert "--experiment-db experiments.db" in out
        assert "--cutoff-date 2020-01-01" in out
        assert "--lookback-years 5" in out
        assert "--realism" in out
        assert "--capital 10000000" in out
        assert "--weighting equal_weight" in out
        assert "--meta-model" in out

    def test_literature_cycle_bad_int(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        out = atlas_cli._atlas_literature_cycle_tool({"limit": "not-a-number"})
        assert "ERROR" in out and "limit" in out


class TestBuildSpecs:
    def test_spec_names(self):
        names = {s.name for s in atlas_cli.build_atlas_cli_specs()}
        assert {
            "atlas_version",
            "atlas_health",
            "atlas_fx_universe",
            "atlas_fx_verify",
            "atlas_fx_refresh",
            "atlas_fx_research",
            "atlas_fx_daily",
            "atlas_fx_backfill",
            "atlas_read_report",
            "atlas_champions",
            "atlas_meta_model",
            "atlas_coverage",
            "atlas_adjustments",
            "atlas_verify_audit",
            "atlas_refresh_store",
            "atlas_literature_cycle",
        } <= names

    def test_atlas_subagent_carries_cli_tools(self):
        registry = build_general_registry()
        sub = registry.get_subagent("atlas")
        assert sub is not None
        names = {t.name for t in sub.tools}
        assert {"atlas_fx_research", "atlas_fx_daily", "atlas_read_report", "atlas_health"} <= names


class TestRunManager:
    def test_single_flight(self, monkeypatch, tmp_path):
        _fake_atlas(tmp_path, monkeypatch)
        monkeypatch.setattr(atlas_cli, "atlas_run_manager", atlas_cli.AtlasRunManager())
        started = threading.Event()
        release = threading.Event()

        def fake_run(argv, timeout):
            started.set()
            assert release.wait(5)
            return 0, "managed ok", ""

        monkeypatch.setattr(atlas_cli, "run_atlas_cli", fake_run)
        mgr = atlas_cli.atlas_run_manager
        assert mgr.launch("version") is True
        assert started.wait(3)
        # second launch refused while one is running
        assert mgr.launch("fx-universe") is False
        release.set()
        for _ in range(100):
            if not mgr.snapshot()["running"]:
                break
            time.sleep(0.02)
        snap = mgr.snapshot()
        assert snap["running"] is False
        assert snap["exit_code"] == 0
        assert snap["tail"] == "managed ok"

    def test_unknown_command(self):
        with pytest.raises(ValueError):
            atlas_cli.atlas_run_manager.launch("fx-research")  # not managed (needs params)

    def test_failure_recorded_honestly(self, monkeypatch, tmp_path):
        _fake_atlas(tmp_path, monkeypatch)
        monkeypatch.setattr(atlas_cli, "atlas_run_manager", atlas_cli.AtlasRunManager())

        def fake_run(argv, timeout):
            raise RuntimeError("repo exploded")

        monkeypatch.setattr(atlas_cli, "run_atlas_cli", fake_run)
        mgr = atlas_cli.atlas_run_manager
        assert mgr.launch("health") is True
        for _ in range(100):
            if not mgr.snapshot()["running"]:
                break
            time.sleep(0.02)
        snap = mgr.snapshot()
        assert snap["exit_code"] == -1
        assert "repo exploded" in snap["tail"]


class TestSpewrate:
    """v8.2 — real measured ATLAS spewrate (candidates/sec)."""

    def test_idle_before_any_run(self):
        mgr = atlas_cli.AtlasRunManager()
        out = mgr.spewrate()
        assert out["state"] == "idle"
        assert out["rate"] is None
        assert out["unit"] == "candidates/s"

    def test_parses_evaluated_from_real_output_shape(self):
        tail = (
            '{\n  "report_path": "deliverables/fx/2026-08-09.md",\n'
            '  "pairs": 7,\n  "evaluated": 63,\n  "accepted": 4,\n'
            '  "failed": false\n}\n'
            "fx-daily: report written to deliverables/fx/2026-08-09.md\n"
        )
        assert atlas_cli._evaluated_from_tail(tail) == 63

    def test_no_evaluated_returns_none(self):
        assert atlas_cli._evaluated_from_tail("boom on stderr") is None

    def test_done_run_computes_rate(self):
        mgr = atlas_cli.AtlasRunManager()
        started = datetime.now(timezone.utc) - timedelta(seconds=30)
        mgr._state.update(
            {
                "command": "fx-daily",
                "running": False,
                "started_at": started.isoformat(timespec="seconds"),
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "exit_code": 0,
                "tail": '{"evaluated": 63, "accepted": 4}',
            }
        )
        out = mgr.spewrate()
        assert out["state"] == "done"
        assert out["work"] == 63
        assert out["elapsed_s"] is not None
        assert out["rate"] is not None
        # 63 candidates in ~30s ≈ 2.1/s — the roundtrip must stay sane.
        assert 1.0 <= out["rate"] <= 5.0

    def test_done_without_work_count_is_honest(self):
        mgr = atlas_cli.AtlasRunManager()
        started = datetime.now(timezone.utc) - timedelta(seconds=10)
        mgr._state.update(
            {
                "command": "health",
                "running": False,
                "started_at": started.isoformat(timespec="seconds"),
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "exit_code": 0,
                "tail": "health: all providers nominal",
            }
        )
        out = mgr.spewrate()
        assert out["state"] == "done"
        assert out["rate"] is None
        assert "no parseable evaluated count" in out["note"]

    def test_running_state_has_no_rate(self):
        mgr = atlas_cli.AtlasRunManager()
        mgr._state.update(
            {
                "command": "fx-daily",
                "running": True,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "finished_at": None,
                "exit_code": None,
                "tail": "",
            }
        )
        out = mgr.spewrate()
        assert out["state"] == "running"
        assert out["rate"] is None
        assert out["elapsed_s"] is not None

    def test_panel_payload_carries_spewrate(self, tmp_path, monkeypatch):
        repo = _fake_atlas(tmp_path, monkeypatch)
        monkeypatch.setattr(atlas_cli, "_version_cache", {"at": 0.0, "value": None})
        monkeypatch.setattr(atlas_cli, "atlas_run_manager", atlas_cli.AtlasRunManager())
        payload = atlas_cli.atlas_panel_snapshot()
        assert payload["configured"] is True
        assert payload["repo"] == str(repo)
        assert payload["spewrate"]["state"] == "idle"
        assert payload["spewrate"]["unit"] == "candidates/s"


class TestPanelSnapshot:
    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)
        payload = atlas_cli.atlas_panel_snapshot()
        assert payload["configured"] is False
        assert "ATLAS_REPO_PATH" in payload["error"]

    def test_configured_payload(self, tmp_path, monkeypatch):
        repo = _fake_atlas(tmp_path, monkeypatch)
        monkeypatch.setattr(atlas_cli, "_version_cache", {"at": 0.0, "value": None})
        payload = atlas_cli.atlas_panel_snapshot()
        assert payload["configured"] is True
        assert payload["repo"] == str(repo)
        assert payload["status"]["branch"] in ("(detached/unknown)", "(no commits)")
        assert payload["bootstrap"]["pair_days"] == {"EURUSD": 1}
        assert payload["latest_report"]["name"] == "2026-08-06.md"
        assert "last_run" in payload


class TestSetupStatus:
    def test_atlas_row(self, tmp_path, monkeypatch):
        _fake_atlas(tmp_path, monkeypatch)
        from dourmouse.webui import build_setup_status

        server = SimpleNamespace(config=None, memory=None, live_runtime=None)
        items = build_setup_status(server)["items"]
        assert items["atlas"]["configured"] is True
        assert "configured" in items["atlas"]["detail"]
