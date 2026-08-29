"""Codex CLI bridge tests (v5.3) — general_roster.py codex_code tool.

The dev_coding subagent can delegate coding work to the user's REAL Codex
CLI in headless mode (`codex exec <task> --skip-git-repo-check`). All
real-execution tests point CODEX_CLI at tiny FAKE scripts so no live Codex /
OpenAI credits are consumed; the missing-CLI path proves the honest
NOT CONFIGURED behavior (Rule 2.2 — no silent stub, no fabricated result).
"""

from __future__ import annotations

import os
import textwrap

import pytest

from dourmouse import general_roster
from dourmouse.general_roster import (
    _codex_code_tool as run_tool,
    _find_codex_cli as find_cli,
    build_general_registry,
)


@pytest.fixture
def registry():
    return build_general_registry()


def _write_fake_cli(tmp_path, script: str) -> str:
    """Write an executable fake codex CLI; return its path.

    POSIX: a bash script with the exec bit. Windows: a .cmd shim — bash
    cannot execute there, and subprocess can launch .cmd files natively.
    """
    if os.name == "nt":
        path = tmp_path / "fake-codex.cmd"
        path.write_text(_bash_to_cmd(script), encoding="utf-8")
        return str(path)
    path = tmp_path / "fake-codex"
    path.write_text(textwrap.dedent(script))
    path.chmod(0o755)
    return str(path)


def _bash_to_cmd(script: str) -> str:
    """Translate the fixed fake-script shapes into batch equivalents.

    The ARGV/CWD shape is emitted through the real interpreter because cmd's
    ``%*`` re-quotes args containing spaces (``-p "task"`` instead of
    ``-p task``) — Python's argv parsing unquotes them faithfully.
    """
    import sys as _sys

    s = textwrap.dedent(script)
    if "ARGV:" in s and "CWD:" in s:
        script = (
            "import sys,os; "
            "print('ARGV: ' + ' '.join(sys.argv[1:])); "
            "print('CWD: ' + os.getcwd())"
        )
        body = ["@echo off", f'"{_sys.executable}" -c "{script}" %*']
    elif "boom" in s and ">&2" in s:
        body = ["@echo off", "echo boom 1>&2", "exit /b 3"]
    elif "sleep" in s:
        body = ["@echo off", "ping -n 6 127.0.0.1 >nul"]
    else:
        body = ["@echo off", "echo ok"]
    return "\r\n".join(body) + "\r\n"


class TestToolRegistration:
    def test_codex_code_registered_on_dev_coding(self, registry):
        sub = registry.get_subagent("dev_coding")
        assert sub is not None
        names = [t.name for t in sub.tools]
        assert "codex_code" in names

    def test_codex_code_is_regular_tier(self, registry):
        sub = registry.get_subagent("dev_coding")
        tool = next(t for t in sub.tools if t.name == "codex_code")
        assert tool.permission.value == "regular"

    def test_codex_code_in_roster_payload(self, registry):
        from dourmouse.webui import build_roster_payload

        payload = build_roster_payload(registry)
        dev = next(a for a in payload["subagents"] if a["name"] == "dev_coding")
        assert "codex_code" in [t["name"] for t in dev["tools"]]


class TestCliDiscovery:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(tmp_path, "#!/bin/sh\necho 'ok'\n")
        monkeypatch.setenv("CODEX_CLI", fake)
        monkeypatch.delenv("PATH", raising=False)
        assert find_cli() == fake

    def test_bare_name_resolves_via_path(self, monkeypatch):
        monkeypatch.delenv("CODEX_CLI", raising=False)
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/true" if name == "codex" else None)
        assert find_cli() == "/usr/bin/true"

    def test_not_found_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLI", str(tmp_path / "does-not-exist"))
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert find_cli() is None


class TestToolBehavior:
    def test_missing_cli_is_honest_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLI", str(tmp_path / "nope"))
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = run_tool({"task": "refactor this"})
        assert result.startswith("NOT CONFIGURED")
        assert "codex" in result.lower()
        assert "Nothing was run" in result

    def test_empty_task_errors(self):
        result = run_tool({"task": "   "})
        assert result.startswith("ERROR")
        assert "non-empty" in result

    def test_real_subprocess_roundtrip_fake_cli(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            echo "ARGV: $*"
            echo "CWD: $(pwd)"
            """,
        )
        monkeypatch.setenv("CODEX_CLI", fake)
        result = run_tool({"task": "explain this bug", "cwd": str(tmp_path)})
        assert "EXIT CODE: 0" in result
        assert "ARGV: exec explain this bug --skip-git-repo-check" in result
        assert f"CWD: {tmp_path}" in result

    def test_nonzero_exit_surfaces_stderr(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            echo "boom" >&2
            exit 3
            """,
        )
        monkeypatch.setenv("CODEX_CLI", fake)
        result = run_tool({"task": "do the thing"})
        assert "EXIT CODE: 3" in result
        assert "boom" in result
        assert "non-zero" in result

    def test_timeout_reports_honestly(self, tmp_path, monkeypatch):
        # v13: the real floor is now 20s (see _MIN_CLI_DELEGATE_TIMEOUT's
        # own docstring in general_roster.py) — lowered here just for this
        # test so it stays fast; the floor itself is covered by
        # test_timeout_seconds_floored_at_twenty below without any real
        # sleep at all.
        monkeypatch.setattr(general_roster, "_MIN_CLI_DELEGATE_TIMEOUT", 1)
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            sleep 5
            """,
        )
        monkeypatch.setenv("CODEX_CLI", fake)
        result = run_tool({"task": "slow task", "timeout_seconds": 1})
        assert "timed out" in result
        assert "1s" in result

    def test_timeout_capped_at_600(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(tmp_path, "#!/bin/sh\necho ok\n")
        monkeypatch.setenv("CODEX_CLI", fake)
        result = run_tool({"task": "x", "timeout_seconds": 9999})
        assert "EXIT CODE: 0" in result

    def test_timeout_seconds_floored_at_twenty(self, monkeypatch):
        """v13: same fix as claude_code (see that test file's own
        docstring) applied to codex_code — no real sleep, subprocess.run
        itself is faked."""
        seen_timeouts: list = []

        class _OkProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen_timeouts.append(kwargs.get("timeout"))
            return _OkProc()

        monkeypatch.setattr(general_roster, "_find_codex_cli", lambda: "/usr/bin/codex")
        monkeypatch.setattr(general_roster.subprocess, "run", _fake_run)
        # v13: codex_code now also does a best-effort MCP registration
        # check (see ensure_codex_mcp_registered) before running the real
        # task — irrelevant to what THIS test asserts (the exec call's own
        # timeout flooring), so it's stubbed out rather than counted.
        from dourmouse import mcp_bridge

        monkeypatch.setattr(mcp_bridge, "ensure_codex_mcp_registered", lambda cli: None)

        run_tool({"task": "x", "timeout_seconds": 1})
        assert seen_timeouts == [20]

        run_tool({"task": "x", "timeout_seconds": 45})
        assert seen_timeouts == [20, 45]  # a real, sane value passes through unchanged

    def test_non_integer_timeout_errors(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(tmp_path, "#!/bin/sh\necho ok\n")
        monkeypatch.setenv("CODEX_CLI", fake)
        result = run_tool({"task": "x", "timeout_seconds": "soon"})
        assert result.startswith("ERROR")
        assert "integer" in result

    def test_oserror_reported(self, tmp_path, monkeypatch):
        fake = tmp_path / "broken-codex"
        fake.write_text("#!/nonexistent/interpreter\n")
        fake.chmod(0o755)
        monkeypatch.setenv("CODEX_CLI", str(fake))
        result = run_tool({"task": "x"})
        assert "ERROR" in result
