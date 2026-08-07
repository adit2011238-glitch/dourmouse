"""v4.0 ATLAS command-centre tests (atlas_ops.py + roster wiring).

Exercises the REAL telemetry functions against a fake ATLAS repo in a tmp
dir: git status via a stub subprocess, bootstrap state via real filesystem
reads, deliverables listing, honest NOT CONFIGURED degradation, and the
roster registration. All hermetic (no network, no real repo, Rule 2.1).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dourmouse import atlas_ops
from dourmouse.atlas_ops import (
    AtlasNotConfiguredError,
    atlas_bootstrap_status,
    atlas_deliverables,
    atlas_status,
    get_atlas_repo_path,
)
from dourmouse.general_roster import build_general_registry


def _fake_repo(tmp_path: Path) -> Path:
    """Build a minimal ATLAS-shaped repo with source/tests/deliverables."""
    repo = tmp_path / "atlas"
    (repo / "atlas").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "deliverables" / "fx" / "research").mkdir(parents=True)
    (repo / "data" / "fx_archive" / "raw" / "EURUSD" / "2023" / "01").mkdir(parents=True)
    (repo / "atlas" / "core.py").write_text("def x(): pass\n")
    (repo / "tests" / "test_core.py").write_text("def test_x(): pass\n")
    (repo / "deliverables" / "fx" / "research" / "EURUSD.json").write_text("{}")
    (repo / "deliverables" / "fx" / "2026-08-06.md").write_text("# report")
    (repo / "data" / "fx_archive" / "raw" / "EURUSD" / "2023" / "01" / "01_bid.bi5").write_bytes(b"x")
    (repo / "data" / "fx_archive" / "raw" / "EURUSD" / "2023" / "01" / "02_bid.bi5").write_bytes(b"x")
    (repo / "data" / "fx-backfill.log").write_text("line1\nline2\nline3\n")
    return repo


class TestConfig:
    def test_missing_env_raises_honestly(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        with pytest.raises(AtlasNotConfiguredError):
            get_atlas_repo_path()

    def test_missing_dir_raises_honestly(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATLAS_REPO_PATH", str(tmp_path / "nope"))
        with pytest.raises(AtlasNotConfiguredError):
            get_atlas_repo_path()

    def test_ok(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        assert get_atlas_repo_path() == repo


class TestAtlasStatus:
    def test_counts_files_and_reports_branch(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))

        def _fake_git(r, *args):  # noqa: ARG001
            if args[:1] == ("branch",):
                return "main"
            if args[:1] == ("log",):
                return "abc1234 Initial commit"
            if args[:1] == ("status",):
                return " M README.md\n?? new.txt"
            return ""

        monkeypatch.setattr(atlas_ops, "_git", _fake_git)
        status = atlas_status()
        assert status["configured"] is True
        assert status["branch"] == "main"
        assert status["last_commit"] == "abc1234 Initial commit"
        assert status["dirty_files"] == 2
        assert status["source_files"] == 1
        assert status["test_files"] == 1
        assert status["deliverable_files"] == 2

    def test_git_failure_is_honest(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        monkeypatch.setattr(
            atlas_ops, "_git", lambda *a, **k: ""
        )
        status = atlas_status()
        assert status["branch"] == "(detached/unknown)"
        assert status["last_commit"] == "(no commits)"


class TestBootstrapStatus:
    def test_counts_pair_days_and_log_tail(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        state = atlas_bootstrap_status()
        assert state["pair_days"] == {"EURUSD": 2}
        assert state["total_pair_days"] == 2
        assert state["done_marker"] is False
        assert "line3" in state["log_tail"]

    def test_done_marker_detected(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        (repo / "data" / "fx-bootstrap.done").write_text("status: complete\n")
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        assert atlas_bootstrap_status()["done_marker"] is True


class TestDeliverables:
    def test_lists_newest_first(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        items = atlas_deliverables(limit=10)
        assert len(items) == 2
        assert all("size" in it and "modified" in it for it in items)
        paths = [it["path"] for it in items]
        assert any("EURUSD.json" in p for p in paths)
        assert any("2026-08-06.md" in p for p in paths)

    def test_empty_dir(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        (repo / "deliverables" / "fx" / "research" / "EURUSD.json").unlink()
        (repo / "deliverables" / "fx" / "2026-08-06.md").unlink()
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        assert atlas_deliverables() == []


class TestHandlers:
    def test_status_handler_not_configured(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        out = atlas_ops._atlas_status_tool({})
        assert "NOT CONFIGURED" in out

    def test_status_handler_ok(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        monkeypatch.setattr(atlas_ops, "_git", lambda *a, **k: "")
        out = atlas_ops._atlas_status_tool({})
        assert "ATLAS REPO STATUS" in out
        assert "branch:" in out

    def test_bootstrap_handler_ok(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        out = atlas_ops._atlas_bootstrap_tool({})
        assert "EURUSD" in out
        assert "2 bid-days" in out

    def test_report_consolidates(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        out = atlas_ops._atlas_report_tool({})
        assert "ATLAS REPO STATUS" in out
        assert "ATLAS FX BOOTSTRAP" in out
        assert "ATLAS DELIVERABLES" in out


class TestRosterWiring:
    def test_atlas_agent_registered(self):
        registry = build_general_registry()
        assert "atlas" in registry.subagent_names
        sub = registry.get_subagent("atlas")
        assert sub is not None
        tool_names = {t.name for t in sub.tools}
        assert {"atlas_status", "atlas_bootstrap", "atlas_deliverables", "atlas_report"} <= tool_names

    def test_atlas_tool_handler_runs(self):
        """The registered handler is the real atlas_ops handler."""
        registry = build_general_registry()
        for name in ("atlas_status", "atlas_bootstrap", "atlas_deliverables"):
            spec = registry.lookup(name)
            assert spec is not None
            assert callable(spec.handler)
