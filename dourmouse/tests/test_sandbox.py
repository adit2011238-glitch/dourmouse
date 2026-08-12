"""Tests for the Phase 1 kernel-enforced sandbox (dourmouse/sandbox.py).

Proves the sandbox is the REAL boundary, not the regex: the three bypass
patterns from the Phase 0 audit (cat ~/.ssh/id_rsa, python3 -c os.remove,
find -delete) are actually executed and shown to FAIL at the OS level, while
ordinary work in the allowed workspace still succeeds and network is denied
by default. The honest NOT CONFIGURED fallback is tested everywhere via
monkeypatch; the real-sandbox tests skip when sandbox-exec is unavailable
(non-macOS) so the suite stays green cross-platform.
"""

from __future__ import annotations

import os

import pytest

from dourmouse.sandbox import (
    build_sandbox_profile,
    run_sandboxed,
    sandbox_available,
)

# Only the tests that need a REAL sandbox-exec skip on non-macOS.
# TestHonestFallback monkeypatches availability away and MUST run everywhere
# — the "never silently unsandboxed" property matters most where sandbox-exec
# is absent.
_NEEDS_SANDBOX = pytest.mark.skipif(
    not sandbox_available(),
    reason="sandbox-exec unavailable (non-macOS or removed) — fallback is tested separately",
)


@pytest.fixture
def sandbox_env(tmp_path, monkeypatch):
    """Fake HOME (so ~/.ssh is ours to stage) + workspace + outside dir.

    HOME is set via the env var so both the profile's deny paths
    (Path.home()) and the shell's `~` expansion agree on the fake home.
    """
    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    outside = tmp_path / "outside"
    for d in (home / ".ssh", ws, outside):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))
    return {"home": home, "ws": ws, "outside": outside}


@_NEEDS_SANDBOX
class TestAuditBypassesNowBlocked:
    """The EXACT three bypass patterns from the Phase 0 audit, run for real."""

    def test_cat_ssh_key_blocked(self, sandbox_env):
        key = sandbox_env["home"] / ".ssh" / "id_rsa"
        key.write_text("TOP-SECRET-KEY-MATERIAL")
        result = run_sandboxed("cat ~/.ssh/id_rsa", cwd=str(sandbox_env["ws"]), timeout=15)
        assert "TOP-SECRET-KEY-MATERIAL" not in result
        assert "NOT CONFIGURED" not in result  # ran for real, didn't fake-fallback
        assert "EXIT CODE:" in result

    def test_python3_os_remove_blocked(self, sandbox_env):
        victim = sandbox_env["outside"] / "victim.txt"
        victim.write_text("secret")
        cmd = f'python3 -c "import os; os.remove(\\"{victim}\\")"'
        result = run_sandboxed(cmd, cwd=str(sandbox_env["ws"]), timeout=20)
        assert victim.exists(), "file outside the sandbox must survive"
        assert "operation not permitted" in result.lower() or "denied" in result.lower()

    def test_find_delete_blocked(self, sandbox_env):
        victim = sandbox_env["outside"] / "victim.txt"
        victim.write_text("secret")
        result = run_sandboxed(
            f"find {sandbox_env['outside']} -delete",
            cwd=str(sandbox_env["ws"]),
            timeout=20,
        )
        assert victim.exists(), "find -delete must not escape the sandbox"
        assert "operation not permitted" in result.lower()

    def test_os_unlink_via_interpreter_blocked(self, sandbox_env):
        victim = sandbox_env["outside"] / "v2.txt"
        victim.write_text("x")
        cmd = f'python3 -c "import os; os.unlink(\\"{victim}\\")"'
        run_sandboxed(cmd, cwd=str(sandbox_env["ws"]), timeout=20)
        assert victim.exists()


@_NEEDS_SANDBOX
class TestSandboxPositiveAndNetwork:
    def test_ordinary_work_in_workspace_succeeds(self, sandbox_env):
        result = run_sandboxed(
            "echo works > proof.txt && cat proof.txt",
            cwd=str(sandbox_env["ws"]),
            timeout=15,
        )
        assert "works" in result
        assert "EXIT CODE: 0" in result
        assert (sandbox_env["ws"] / "proof.txt").exists()

    def test_read_own_workspace_file_allowed(self, sandbox_env):
        (sandbox_env["ws"] / "notes.txt").write_text("hello sandbox")
        result = run_sandboxed("cat notes.txt", cwd=str(sandbox_env["ws"]), timeout=15)
        assert "hello sandbox" in result
        assert "EXIT CODE: 0" in result

    def test_network_denied_by_default(self, sandbox_env):
        # A REAL curl attempt inside the sandbox must fail at the OS level.
        result = run_sandboxed(
            "curl -s --max-time 3 https://example.com; echo CURL_EXIT=$?",
            cwd=str(sandbox_env["ws"]),
            timeout=20,
        )
        assert "CURL_EXIT=" in result
        assert "CURL_EXIT=0" not in result, "network must be denied by default"

    def test_secret_filename_blocked_even_inside_workspace(self, sandbox_env):
        # .env in the WORKSPACE itself is still unreadable inside the sandbox
        # (regex deny, defense in depth with Phase 0).
        (sandbox_env["ws"] / ".env").write_text("NVIDIA_API_KEY=nvapi-secret")
        result = run_sandboxed("cat .env", cwd=str(sandbox_env["ws"]), timeout=15)
        assert "nvapi-secret" not in result

    def test_profile_denies_resolved_fake_home_ssh(self, sandbox_env, tmp_path):
        profile = build_sandbox_profile(cwd=str(tmp_path))
        # macOS /tmp is a symlink to /private/tmp — Seatbelt matches RESOLVED
        # paths, so the deny must reference the RESOLVED fake-home .ssh.
        resolved_ssh = (sandbox_env["home"].resolve() / ".ssh")
        assert f'(deny file-read* (subpath "{resolved_ssh}"))' in profile
        assert "(deny default)" in profile
        assert "(deny network*)" in profile

    def test_run_command_tool_is_sandboxed_network(self, sandbox_env):
        """Wiring-level: the run_command TOOL (not just run_sandboxed) has
        network denied by default — a real curl through the tool fails."""
        from dourmouse.system_access import build_system_subagent

        spec = next(t for t in build_system_subagent().tools if t.name == "run_command")
        result = spec.handler(
            {"command": "curl -s --max-time 3 https://example.com; echo EXIT=$?"}
        )
        assert "EXIT=0" not in result

    def test_find_delete_through_run_command_tool(self, sandbox_env):
        """Wiring-level: a classifier-dodging command (find -delete on a
        credential dir) fails inside the sandbox even via the real tool."""
        from dourmouse.system_access import build_system_subagent

        victim = sandbox_env["home"] / ".ssh" / "id_rsa"
        victim.write_text("TOP-SECRET")
        spec = next(t for t in build_system_subagent().tools if t.name == "run_command")
        result = spec.handler({"command": "find ~/.ssh -delete"})
        assert victim.exists(), "credential key must survive"
        assert "operation not permitted" in result.lower()


class TestHonestFallback:
    """run_sandboxed NEVER silently falls back to unsandboxed execution."""

    def test_not_configured_when_sandbox_exec_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr("dourmouse.sandbox.shutil.which", lambda _n: None)
        marker = tmp_path / "must-not-run.txt"
        result = run_sandboxed(
            f"touch {marker}",
            cwd=str(tmp_path),
            timeout=10,
        )
        assert "NOT CONFIGURED" in result
        assert "sandbox-exec" in result
        assert not marker.exists(), "command must NOT run unsandboxed"

    def test_sandbox_available_flag_is_bool_and_consistent(self):
        import shutil

        flag = sandbox_available()
        assert isinstance(flag, bool)
        # Consistency with the real lookup, without reimplementing the logic:
        exe = shutil.which("sandbox-exec")
        assert flag == (exe is not None and os.access(exe, os.X_OK))
