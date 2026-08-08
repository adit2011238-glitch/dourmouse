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
from pathlib import Path
from types import SimpleNamespace

import pytest

from dourmouse import atlas_cli
from dourmouse.atlas_cli import AtlasNotConfiguredError
from dourmouse.general_roster import build_general_registry

_ECHO_SCRIPT = "#!/usr/bin/env bash\necho \"FAKE_RAN:$*\"\nexit 0\n"
_FAIL_SCRIPT = "#!/usr/bin/env bash\necho 'boom on stderr' >&2\nexit 3\n"


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
            tmp_path, monkeypatch,
            script="#!/usr/bin/env bash\nsleep 5\n",
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
