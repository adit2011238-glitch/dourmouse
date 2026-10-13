"""FIX-P R5-1 and R5-2: the CLI front modes cannot read the disk by themselves.

Claude CLI: the native file readers are denied and the process runs in an empty scratch folder.
Codex CLI: runs under a permission profile that denies reading everything except the scratch folder.
Fake subprocesses only; no model call."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from dourmouse import code_backends


def _fake_cli(monkeypatch):
    import dourmouse.general_roster as gr

    monkeypatch.setattr(gr, "_find_claude_cli", lambda: "/fake/claude")
    monkeypatch.setattr(gr, "_find_codex_cli", lambda: "/fake/codex")


def test_claude_front_mode_denies_every_native_file_reader():
    denied = set(code_backends._DISALLOWED_NATIVE_TOOLS.split(","))
    for tool in ("Read", "Glob", "Grep", "LS", "Task", "Agent", "TodoWrite", "NotebookRead", "SlashCommand", "Skill",
                 "Bash", "Write", "Edit", "WebFetch", "WebSearch"):
        assert tool in denied, tool
    assert "ToolSearch" not in denied  # it loads the bridge's tools
    args, _ = code_backends._claude_tool_args()
    assert args[args.index("--disallowedTools") + 1] == code_backends._DISALLOWED_NATIVE_TOOLS


def test_claude_front_mode_runs_in_an_empty_scratch_folder_not_the_project(monkeypatch, tmp_path):
    _fake_cli(monkeypatch)
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    seen: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("KEY=1")
    code_backends._run_claude_once("/fake/claude", "hi", ["--session-id", "x"], cwd=str(project), timeout=5)
    assert seen["cwd"] != str(project)
    scratch = Path(seen["cwd"])
    assert scratch.is_dir() and list(scratch.iterdir()) == []
    assert str(scratch).startswith(str(tmp_path / "ws"))


def test_claude_code_tool_keeps_its_native_tools():
    from dourmouse.general_roster import _claude_code_mcp_args

    assert "--disallowedTools" not in _claude_code_mcp_args()


def test_codex_front_mode_runs_under_a_deny_by_default_profile_with_the_chat_handoff(monkeypatch, tmp_path):
    _fake_cli(monkeypatch)
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    seen: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["cwd"] = list(argv), kwargs.get("cwd")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from dourmouse import privacy

    privacy.PrivacyStore().set_chat_private("tab-cx", True)
    with privacy.chat_scope("tab-cx"):
        privacy.placeholder_map("tab-cx").placeholder("EMAIL", "bob@example.org")
        code_backends._run_codex("task", cwd=str(tmp_path), timeout=5)
    argv = seen["argv"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    # `-s` and default_permissions cannot be set together (codex-cli 0.144 refuses), so only the profile is used
    assert "-s" not in argv and "--sandbox" not in argv
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert 'default_permissions="dourmouse_front"' in overrides
    profile = next(o for o in overrides if o.startswith("permissions.dourmouse_front.filesystem="))
    assert '":minimal"="read"' in profile
    assert str(Path.home()) not in profile.replace(str(tmp_path), "")
    assert seen["cwd"] != str(tmp_path) and Path(seen["cwd"]).is_dir()
    assert seen["cwd"] in profile
    # the bridge Codex starts gets this chat's id, Private flag and map file for this one call
    assert 'mcp_servers.dourmouse.env.DOURMOUSE_PRIVACY_CHAT="tab-cx"' in overrides
    assert 'mcp_servers.dourmouse.env.DOURMOUSE_PRIVACY_PRIVATE="1"' in overrides
    assert any(o.startswith("mcp_servers.dourmouse.env.DOURMOUSE_PRIVACY_MAP=") for o in overrides)
    # the temp map file is removed after the call
    mapfile = next(o for o in overrides if "PRIVACY_MAP" in o).split("=", 1)[1].strip('"')
    assert not Path(mapfile).exists()
