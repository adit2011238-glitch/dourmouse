"""FR fixes P2-15, P2-16, P2-17 (claude_code / codex_code) and H-FS1-2."""

from __future__ import annotations

import subprocess

import pytest

from dourmouse import code_backends
from dourmouse import general_roster as gr
from dourmouse.general_roster import build_general_registry


def _spec(name):
    return build_general_registry().lookup(name)


class _Done:
    returncode = 0
    stdout = "ok"
    stderr = ""


@pytest.fixture()
def fake_cli(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(gr, "_find_claude_cli", lambda: "/opt/homebrew/bin/claude")
    monkeypatch.setattr(gr, "_find_codex_cli", lambda: "/opt/homebrew/bin/codex")

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return _Done()

    monkeypatch.setattr(gr.subprocess, "run", fake_run)
    monkeypatch.setattr(gr, "_claude_code_session_key", lambda cwd: "k")
    monkeypatch.setattr(gr, "_claude_code_session_args", lambda key: ["--session-id", "11111111-1111-1111-1111-111111111111"])
    monkeypatch.setattr(code_backends, "_ensure_mcp_config_path", lambda: str(tmp_path / "mcp.json"))
    return calls


def test_description_no_longer_says_default_permissions():
    text = _spec("claude_code").description
    assert "default permissions" not in text and "typically declined" not in text
    assert "bypassPermissions" in text and "without asking" in text


def test_approval_prompt_shows_the_whole_task_and_what_it_can_do():
    task = "do the thing " * 400  # ~5200 characters
    prompt = _spec("claude_code").confirm_prompt({"task": task, "cwd": "/Users/me/proj"})
    assert task in prompt
    assert "permission prompts OFF" in prompt and "/Users/me/proj" in prompt and f"{len(task)} characters" in prompt
    codex_prompt = _spec("codex_code").confirm_prompt({"task": task})
    assert task in codex_prompt and "sandbox policy" in codex_prompt


def test_a_task_too_long_to_show_is_refused_not_cut(fake_cli):
    out = gr._claude_code_tool({"task": "x" * (gr._MAX_CLI_TASK_CHARS + 1)})
    assert out.startswith("REFUSED") and fake_cli == []
    out = gr._codex_code_tool({"task": "x" * (gr._MAX_CLI_TASK_CHARS + 1)})
    assert out.startswith("REFUSED") and fake_cli == []


def test_claude_code_runs_with_strict_mcp_config_and_keeps_native_tools(fake_cli):
    gr._claude_code_tool({"task": "refactor"})
    argv, _kw = fake_cli[0]
    assert "--strict-mcp-config" in argv  # the owner's claude.ai connectors are not loaded
    assert "--mcp-config" in argv and "--allowedTools" in argv
    assert "--permission-mode" in argv and "bypassPermissions" in argv
    assert "--disallowedTools" not in argv  # native Bash/Write/Edit stay on (the owner's open decision)


def test_a_failing_mcp_config_does_not_drop_strict_mode(fake_cli, monkeypatch):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(code_backends, "_ensure_mcp_config_path", boom)
    gr._claude_code_tool({"task": "refactor"})
    argv, _kw = fake_cli[0]
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv


def test_the_cli_child_gets_the_cli_environment(fake_cli, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    gr._claude_code_tool({"task": "t"})
    gr._codex_code_tool({"task": "t"})
    runs = [(argv, kw) for argv, kw in fake_cli if argv[1] in ("-p", "exec")]  # not the codex mcp registration
    assert len(runs) == 2
    for _argv, kw in runs:
        assert "env" in kw
        assert "/opt/homebrew/bin" in kw["env"]["PATH"].split(":")  # the CLI's own directory is on PATH


def test_a_timeout_says_the_process_was_stopped(monkeypatch):
    monkeypatch.setattr(gr, "_find_claude_cli", lambda: "/opt/homebrew/bin/claude")

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 20)

    monkeypatch.setattr(gr.subprocess, "run", hang)
    out = gr._claude_code_tool({"task": "t", "timeout_seconds": 20})
    assert "still running" not in out and "was stopped" in out and "may have" in out
