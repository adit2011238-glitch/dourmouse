"""Research Agent tests — runnable in complete isolation, no SDK/orchestrator
needed (Integration Rule 7.3), no network access, no ATLAS repo required.

Verifies the Rule 2.2 guarantee: with ATLAS not configured, the agent raises
loudly / reports an explicit error — it never fabricates research output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dourmouse import research_agent
from dourmouse.research_agent import (
    AtlasNotConfiguredError,
    call_research_tool,
    get_atlas_repo_path,
    get_atlas_venv_python,
    run_atlas_research,
)


class TestConfigResolution:
    def test_missing_repo_path_raises(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        with pytest.raises(AtlasNotConfiguredError, match="ATLAS_REPO_PATH is not set"):
            get_atlas_repo_path()

    def test_nonexistent_repo_path_raises(self, monkeypatch):
        monkeypatch.setenv("ATLAS_REPO_PATH", "/no/such/directory/anywhere")
        with pytest.raises(AtlasNotConfiguredError, match="does not exist"):
            get_atlas_repo_path()

    def test_valid_repo_path_resolves(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATLAS_REPO_PATH", str(tmp_path))
        assert get_atlas_repo_path() == tmp_path

    def test_missing_venv_path_raises(self, monkeypatch):
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)
        with pytest.raises(AtlasNotConfiguredError, match="ATLAS_VENV_PATH is not set"):
            get_atlas_venv_python()

    def test_venv_without_python_binary_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATLAS_VENV_PATH", str(tmp_path))
        with pytest.raises(AtlasNotConfiguredError, match="No python interpreter"):
            get_atlas_venv_python()

    def test_valid_venv_path_resolves(self, monkeypatch, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "python").write_text("#!/bin/sh\n")
        monkeypatch.setenv("ATLAS_VENV_PATH", str(tmp_path))
        assert get_atlas_venv_python() == bin_dir / "python"


class TestBundledAtlasResolution:
    """Personal-dist behavior (v5.22): the ATLAS engine ships next to the
    package at ``<dist>/atlas``, so the env vars are optional there."""

    def test_bundled_repo_fallback(self, monkeypatch, tmp_path):
        bundled = tmp_path / "atlas"
        bundled.mkdir()
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.setattr(research_agent, "_BUNDLED_ATLAS_DIR", bundled)
        assert get_atlas_repo_path() == bundled

    def test_bundled_venv_reuses_running_interpreter(self, monkeypatch, tmp_path):
        bundled = tmp_path / "atlas"
        bundled.mkdir()
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)
        monkeypatch.setattr(research_agent, "_BUNDLED_ATLAS_DIR", bundled)
        monkeypatch.setattr(research_agent, "_bundled_venv_can_run_atlas", lambda: True)
        assert get_atlas_venv_python() == Path(sys.executable)

    def test_bundled_venv_without_deps_raises_honestly(self, monkeypatch, tmp_path):
        bundled = tmp_path / "atlas"
        bundled.mkdir()
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)
        monkeypatch.setattr(research_agent, "_BUNDLED_ATLAS_DIR", bundled)
        monkeypatch.setattr(research_agent, "_bundled_venv_can_run_atlas", lambda: False)
        with pytest.raises(AtlasNotConfiguredError, match="ATLAS_VENV_PATH is not set"):
            get_atlas_venv_python()

    def test_env_still_wins_over_bundle(self, monkeypatch, tmp_path):
        bundled = tmp_path / "atlas"
        bundled.mkdir()
        external = tmp_path / "external-repo"
        external.mkdir()
        monkeypatch.setenv("ATLAS_REPO_PATH", str(external))
        monkeypatch.setattr(research_agent, "_BUNDLED_ATLAS_DIR", bundled)
        assert get_atlas_repo_path() == external


class TestRunAtlasResearchGuardsFabrication:
    def test_no_repo_path_raises_before_any_subprocess(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)
        with pytest.raises(AtlasNotConfiguredError):
            run_atlas_research(symbols=["SPY"])

    def test_no_venv_path_raises_even_if_repo_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATLAS_REPO_PATH", str(tmp_path))
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)
        with pytest.raises(AtlasNotConfiguredError):
            run_atlas_research(symbols=["SPY"])


class TestToolWrapper:
    def test_tool_reports_not_configured_as_error_not_fake_data(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)

        text = call_research_tool({"symbols": ["SPY"]})

        assert "NOT CONFIGURED" in text
        assert "champions" not in text.lower()  # never leaks fabricated fields
