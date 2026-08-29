"""Claude Code bridge tests (dourmouse/general_roster.py claude_code tool).

The dev_coding subagent can delegate coding work to the user's REAL Claude
Code CLI in headless mode (`claude -p <task>`). All real-execution tests
point CLAUDE_CODE_CLI at tiny FAKE scripts so no live Claude Code / API
credits are consumed; the missing-CLI path proves the honest NOT CONFIGURED
behavior (Rule 2.2 — no silent stub, no fabricated result).
"""

from __future__ import annotations

import os
import re
import textwrap
import uuid

import pytest

from dourmouse import general_roster
from dourmouse.general_roster import build_general_registry
from dourmouse.general_roster import _claude_code_tool as run_tool
from dourmouse.general_roster import _find_claude_cli as find_cli


@pytest.fixture
def registry():
    return build_general_registry()


@pytest.fixture(autouse=True)
def _reset_claude_code_sessions():
    """general_roster._CLAUDE_CODE_SESSIONS is module-level, process-lifetime
    state by design (see its docstring) — but that means it must NOT leak
    between tests, several of which share the same cwd key and would
    otherwise see a stale --resume from a previous test instead of a fresh
    --session-id."""
    general_roster._CLAUDE_CODE_SESSIONS.clear()
    yield
    general_roster._CLAUDE_CODE_SESSIONS.clear()


def _write_fake_cli(tmp_path, script: str) -> str:
    """Write an executable fake claude CLI; return its path.

    POSIX: a bash script with the exec bit. Windows: a .cmd shim — bash
    cannot execute there, and subprocess can launch .cmd files natively.
    """
    if os.name == "nt":
        path = tmp_path / "fake-claude.cmd"
        path.write_text(_bash_to_cmd(script), encoding="utf-8")
        return str(path)
    path = tmp_path / "fake-claude"
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
    elif "while" in s and "line-" in s:
        body = ["@echo off", "for /L %%i in (1,1,400) do echo line-%%i-0123456789"]
    else:
        body = ["@echo off", "echo ok"]
    return "\r\n".join(body) + "\r\n"


class TestToolRegistration:
    def test_claude_code_registered_on_dev_coding(self, registry):
        sub = registry.get_subagent("dev_coding")
        assert sub is not None
        names = [t.name for t in sub.tools]
        assert "claude_code" in names

    def test_claude_code_is_regular_tier(self, registry):
        sub = registry.get_subagent("dev_coding")
        tool = next(t for t in sub.tools if t.name == "claude_code")
        assert tool.permission.value == "regular"

    def test_claude_code_in_roster_payload(self, registry):
        from dourmouse.webui import build_roster_payload

        payload = build_roster_payload(registry)
        dev = next(a for a in payload["subagents"] if a["name"] == "dev_coding")
        assert "claude_code" in [t["name"] for t in dev["tools"]]


class TestCliDiscovery:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path, "#!/bin/sh\necho 'ok'\n"
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        monkeypatch.delenv("PATH", raising=False)
        assert find_cli() == fake

    def test_bare_name_resolves_via_path(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_CLI", "")
        monkeypatch.delenv("CLAUDE_CODE_CLI")
        # PATH lookup for a real-ish command must at least not crash; on any
        # machine 'sh' or 'true' resolves.
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/true" if name == "claude" else None)
        assert find_cli() == "/usr/bin/true"

    def test_not_found_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_CLI", str(tmp_path / "does-not-exist"))
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert find_cli() is None


class TestToolBehavior:
    def test_missing_cli_is_honest_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_CLI", str(tmp_path / "nope"))
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = run_tool({"task": "refactor this"})
        assert result.startswith("NOT CONFIGURED")
        assert "claude" in result.lower()
        assert "Nothing was run" in result

    def test_empty_task_errors(self, monkeypatch):
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
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "explain this bug", "cwd": str(tmp_path)})
        assert "EXIT CODE: 0" in result
        # First call for this cwd mints a real session id via --session-id
        # (see TestSessionContinuity below) — assert the shape rather than
        # a fixed string since the uuid is random each run. v13: real
        # --mcp-config/--allowedTools args now ride along too (see
        # _claude_code_mcp_args) — tolerate whatever lands between the
        # session args and the task text rather than asserting their exact
        # absence.
        match = re.search(
            r"ARGV: -p --session-id ([0-9a-f-]{36}) .*explain this bug", result
        )
        assert match, result
        assert uuid.UUID(match.group(1)).version == 4
        assert f"CWD: {tmp_path}" in result

    def test_nonzero_exit_surfaces_stderr(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            echo "boom" >&2
            exit 3
            """,
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "do the thing"})
        assert "EXIT CODE: 3" in result
        assert "boom" in result
        assert "non-zero" in result

    def test_timeout_reports_honestly(self, tmp_path, monkeypatch):
        # v13: the real floor is now 20s (see _MIN_CLI_DELEGATE_TIMEOUT's
        # own docstring for why) — lowered here just for this test so it
        # stays fast; the floor itself is covered by
        # test_timeout_seconds_floored_at_twenty below without any real
        # sleep at all.
        monkeypatch.setattr(general_roster, "_MIN_CLI_DELEGATE_TIMEOUT", 1)
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            sleep 5
            """,
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "slow task", "timeout_seconds": 1})
        assert "timed out" in result
        assert "1s" in result

    def test_timeout_capped_at_600(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path, "#!/bin/sh\necho ok\n"
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "x", "timeout_seconds": 9999})
        assert "EXIT CODE: 0" in result

    def test_zero_timeout_clamped_to_the_real_floor(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path, "#!/bin/sh\necho ok\n"
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "x", "timeout_seconds": 0})
        assert "EXIT CODE: 0" in result

    def test_timeout_seconds_floored_at_twenty(self, monkeypatch):
        """v13: a real bug fixed — the old floor was 1 second, so a weak
        orchestrator model retrying with "a shorter timeout" could hand
        this tool a value guaranteed to fail before any real CLI could
        possibly cold-start, let alone finish — live-observed: it then gave
        up and fabricated the final answer instead of honestly reporting
        the failure. No real sleep here: subprocess.run itself is faked so
        the test is instant regardless of what timeout value reaches it."""
        seen_timeouts: list = []

        class _OkProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen_timeouts.append(kwargs.get("timeout"))
            return _OkProc()

        monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/usr/bin/claude")
        monkeypatch.setattr(general_roster.subprocess, "run", _fake_run)

        run_tool({"task": "x", "timeout_seconds": 1})
        assert seen_timeouts == [20]

        run_tool({"task": "x", "timeout_seconds": 45})
        assert seen_timeouts == [20, 45]  # a real, sane value passes through unchanged

    def test_non_integer_timeout_errors(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path, "#!/bin/sh\necho ok\n"
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "x", "timeout_seconds": "soon"})
        assert result.startswith("ERROR")
        assert "integer" in result

    def test_output_truncated_at_cap(self, tmp_path, monkeypatch):
        # Shrink the cap so a small fake output crosses it deterministically.
        import dourmouse.general_roster as roster_module

        monkeypatch.setattr(roster_module, "_CLAUDE_OUTPUT_CAP", 200)
        fake = _write_fake_cli(
            tmp_path,
            """#!/bin/sh
            i=0
            while [ $i -lt 400 ]; do echo "line-$i-0123456789"; i=$((i+1)); done
            """,
        )
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "big output"})
        assert "output truncated" in result
        # Only the last 200 chars survive + header; ensure it stayed small.
        assert len(result) < 1_000

    def test_oserror_reported(self, tmp_path, monkeypatch):
        # A CLI file whose shebang interpreter doesn't exist -> exec fails ->
        # subprocess raises FileNotFoundError (an OSError) at run time.
        fake = tmp_path / "broken-claude"
        fake.write_text("#!/nonexistent/interpreter\n")
        fake.chmod(0o755)
        monkeypatch.setenv("CLAUDE_CODE_CLI", str(fake))
        result = run_tool({"task": "x"})
        assert "ERROR" in result


class TestSessionContinuity:
    """Mirrors code_backends.py's TestClaudeSessionContinuity — the same
    real fix (mint a session id via --session-id on the first call for a
    key, --resume it on every later call, forget-and-retry-once on a stale
    id), live-verified against the same installed CLI, applied to this
    SEPARATE call path: dev_coding's claude_code tool via _run_cli_delegate,
    not code_backends._run_claude. Monkeypatches subprocess.run directly
    (rather than this file's usual fake-CLI-script fixtures) so the exact
    argv shape and the multi-call session bookkeeping can be asserted
    precisely, the same way test_code_backends.py does it."""

    def _fake_run_factory(self, seen: list, proc_factory):
        def _fake_run(argv, **kwargs):
            seen.append(argv)
            return proc_factory(argv)

        return _fake_run

    def test_first_call_mints_a_fresh_session_id(self, monkeypatch):
        seen: list = []

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/usr/bin/claude")
        monkeypatch.setattr(
            general_roster.subprocess, "run", self._fake_run_factory(seen, lambda a: _Proc())
        )
        run_tool({"task": "write add", "cwd": "/tmp/proj"})
        argv = seen[0]
        assert argv[0] == "/usr/bin/claude"
        assert argv[1] == "-p"
        assert "--session-id" in argv
        sid = argv[argv.index("--session-id") + 1]
        # a real UUID4, not a placeholder
        assert uuid.UUID(sid).version == 4
        # v13: the task must land immediately after the session args — NOT
        # necessarily last, since real --mcp-config/--allowedTools flags
        # (see _claude_code_mcp_args) now ride after it. They must never
        # ride BEFORE it: --allowedTools takes a variadic value list and a
        # trailing prompt would be silently swallowed into it (live-caught:
        # `claude -p --allowedTools "mcp__dourmouse__*" "say hello"` really
        # does error "Input must be provided either through stdin or as a
        # prompt argument" — the CLI ate "say hello" as another tool name).
        assert argv[argv.index("--session-id") + 2] == "write add"
        assert general_roster._CLAUDE_CODE_SESSIONS["/tmp/proj"] == sid

    def test_second_call_same_cwd_resumes_the_same_session(self, monkeypatch):
        seen: list = []

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/usr/bin/claude")
        monkeypatch.setattr(
            general_roster.subprocess, "run", self._fake_run_factory(seen, lambda a: _Proc())
        )
        run_tool({"task": "first turn", "cwd": "/tmp/proj"})
        run_tool({"task": "second turn", "cwd": "/tmp/proj"})
        first_sid = seen[0][seen[0].index("--session-id") + 1]
        assert "--resume" in seen[1]
        assert seen[1][seen[1].index("--resume") + 1] == first_sid
        # v13: task lands right after --resume's value, not necessarily
        # last — see the sibling test above for why order matters here.
        assert seen[1][seen[1].index("--resume") + 2] == "second turn"

    def test_different_cwd_gets_a_different_session(self, monkeypatch):
        seen: list = []

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/usr/bin/claude")
        monkeypatch.setattr(
            general_roster.subprocess, "run", self._fake_run_factory(seen, lambda a: _Proc())
        )
        run_tool({"task": "task a", "cwd": "/tmp/proj-a"})
        run_tool({"task": "task b", "cwd": "/tmp/proj-b"})
        # both calls are FIRST calls for their respective cwd — both mint,
        # neither resumes the other's conversation.
        assert "--session-id" in seen[0]
        assert "--session-id" in seen[1]
        sid_a = seen[0][seen[0].index("--session-id") + 1]
        sid_b = seen[1][seen[1].index("--session-id") + 1]
        assert sid_a != sid_b

    def test_stale_session_id_recovers_with_one_fresh_retry(self, monkeypatch):
        """The CLI's real error text when a tracked session id no longer
        resolves (verified live in code_backends.py's original diagnosis:
        `claude -p ... --resume <bogus-uuid>` exits 1 with stderr "No
        conversation found with session ID: <uuid>"). Losing that id
        honestly (e.g. the user pruned local session history) must not
        hard-fail the whole task — it should forget the dead id and run
        once more on a fresh conversation."""
        general_roster._CLAUDE_CODE_SESSIONS["/tmp/proj"] = (
            "11111111-1111-1111-1111-111111111111"
        )
        seen: list = []

        class _StaleProc:
            returncode = 1
            stdout = ""
            stderr = (
                "No conversation found with session ID: "
                "11111111-1111-1111-1111-111111111111"
            )

        class _OkProc:
            returncode = 0
            stdout = "recovered"
            stderr = ""

        calls = {"n": 0}

        def _fake_run(argv, **kwargs):
            seen.append(argv)
            calls["n"] += 1
            return _StaleProc() if calls["n"] == 1 else _OkProc()

        monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/usr/bin/claude")
        monkeypatch.setattr(general_roster.subprocess, "run", _fake_run)
        result = run_tool({"task": "task", "cwd": "/tmp/proj"})
        assert "recovered" in result
        assert len(seen) == 2
        assert "--resume" in seen[0]  # the stale id was tried first, honestly
        assert "--session-id" in seen[1]  # then a real fresh one, not a guess
        # the dead id is gone; a real new one replaces it
        assert (
            general_roster._CLAUDE_CODE_SESSIONS["/tmp/proj"]
            != "11111111-1111-1111-1111-111111111111"
        )

    def test_stale_session_retries_exactly_once_not_forever(self, monkeypatch):
        """If the retry ALSO fails, the real error surfaces — no infinite
        retry loop chasing a CLI that keeps saying no."""
        general_roster._CLAUDE_CODE_SESSIONS["/tmp/proj"] = (
            "11111111-1111-1111-1111-111111111111"
        )
        seen: list = []

        class _StaleProc:
            returncode = 1
            stdout = ""
            stderr = (
                "No conversation found with session ID: "
                "11111111-1111-1111-1111-111111111111"
            )

        def _fake_run(argv, **kwargs):
            seen.append(argv)
            return _StaleProc()

        monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/usr/bin/claude")
        monkeypatch.setattr(general_roster.subprocess, "run", _fake_run)
        result = run_tool({"task": "task", "cwd": "/tmp/proj"})
        assert "EXIT CODE: 1" in result
        # exactly one retry: the original --resume attempt, then one fresh
        # --session-id attempt — never a third call.
        assert len(seen) == 2

    def test_no_cwd_uses_a_stable_default_key(self, monkeypatch):
        """The tool's default cwd (str(_PROJECT_ROOT), not None) must still
        get real continuity across calls, not silently skip session
        threading just because the caller omitted 'cwd'."""
        seen: list = []

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/usr/bin/claude")
        monkeypatch.setattr(
            general_roster.subprocess, "run", self._fake_run_factory(seen, lambda a: _Proc())
        )
        run_tool({"task": "first"})
        run_tool({"task": "second"})
        assert "--session-id" in seen[0]
        assert "--resume" in seen[1]


# --------------------------------------------------------------------------- #
# MCP bridge wiring (v13) — claude_code gets the same --mcp-config/
# --allowedTools access as code_backends._run_claude, via the shared,
# process-cached config file (_claude_code_mcp_args reads it from
# code_backends._ensure_mcp_config_path so both callers agree).
# --------------------------------------------------------------------------- #

class TestClaudeCodeMcpWiring:
    def test_argv_carries_real_mcp_flags(self, tmp_path, monkeypatch):
        from dourmouse import code_backends

        monkeypatch.setattr(code_backends, "user_config_dir", lambda: tmp_path)
        code_backends._mcp_config_path_cache = None
        fake = _write_fake_cli(tmp_path, '#!/bin/sh\necho "ARGV: $*"')
        monkeypatch.setenv("CLAUDE_CODE_CLI", fake)
        result = run_tool({"task": "explain this bug", "cwd": str(tmp_path)})
        assert "--mcp-config" in result
        assert str(tmp_path / "mcp-config.json") in result
        assert "--allowedTools mcp__dourmouse__*" in result
        code_backends._mcp_config_path_cache = None

    def test_a_broken_mcp_setup_never_blocks_claude_code(self, monkeypatch):
        """_claude_code_mcp_args itself swallows errors (see its own
        docstring) — this pins that it degrades to an empty list rather
        than raising, so a broken MCP setup never stops claude_code's own
        core job."""
        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr("dourmouse.code_backends._ensure_mcp_config_path", _boom)
        from dourmouse.general_roster import _claude_code_mcp_args

        assert _claude_code_mcp_args() == []
