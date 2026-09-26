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


@_NEEDS_SANDBOX
class TestRunSandboxedS23:
    """S23: run_sandboxed passes an allowlist environment, reads default-deny,
    and writes only inside the cwd."""

    def test_api_keys_are_not_in_the_shell_environment(self, sandbox_env, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
        monkeypatch.setenv("OLLAMA_API_KEY", "ol-should-not-leak")
        result = run_sandboxed("env", cwd=str(sandbox_env["ws"]), timeout=15)
        assert "EXIT CODE: 0" in result
        assert "should-not-leak" not in result and "API_KEY" not in result

    def test_personal_data_outside_the_allowlist_is_unreadable(self, sandbox_env):
        home = sandbox_env["home"]
        for rel in ("Library/Application Support/x.db", "Library/Cookies/c.binarycookies", ".zsh_history", "notes.txt"):
            f = home / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("PERSONAL-DATA")
        cmds = " ; ".join(f'cat "{home / rel}"' for rel in (
            "Library/Application Support/x.db", "Library/Cookies/c.binarycookies", ".zsh_history", "notes.txt"))
        result = run_sandboxed(cmds, cwd=str(sandbox_env["ws"]), timeout=15)
        assert "PERSONAL-DATA" not in result

    def test_writes_are_limited_to_the_cwd_not_the_whole_workspace(self, sandbox_env):
        ws = sandbox_env["ws"]
        job = ws / "job"
        job.mkdir()
        (ws / "self_extensions" / "approved").mkdir(parents=True)
        result = run_sandboxed(
            f'echo x > "{ws}/self_extensions/approved/evil.py"; echo y > "{ws}/sibling.txt"; echo z > mine.txt',
            cwd=str(job), timeout=15,
        )
        assert not (ws / "self_extensions" / "approved" / "evil.py").exists()
        assert not (ws / "sibling.txt").exists()
        assert (job / "mine.txt").read_text().strip() == "z"
        assert "operation not permitted" in result.lower()

    def test_a_timed_out_command_kills_its_background_children(self, sandbox_env):
        import time

        pidfile = sandbox_env["ws"] / "bg.pid"
        result = run_sandboxed(f"sleep 90 & echo $! > {pidfile}; sleep 90", cwd=str(sandbox_env["ws"]), timeout=2)
        assert "timed out" in result
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        pytest.fail(f"background child {pid} survived the timeout")


class TestProfiles:
    def test_job_profile_is_default_deny_with_an_explicit_read_allowlist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        from dourmouse.sandbox import build_job_profile

        job = tmp_path / "job"
        job.mkdir()
        profile = build_job_profile(job)
        assert "(deny default)" in profile
        assert "\n(allow file-read*)" not in profile
        assert f'(allow file-write* (subpath "{job.resolve()}"))' in profile
        assert "(deny network*)" in profile and "(allow network*)" not in profile
        for needle in (".ssh", "Library/Keychains", "Library/Application Support", "Library/Cookies",
                       "Library/Messages", ".zsh_history"):
            assert needle in profile
        assert f'(allow file-read* (subpath "{os.path.realpath(__import__("sys").prefix)}"))' in profile

    def test_network_is_opt_in_only_through_the_parameter(self, tmp_path):
        from dourmouse.sandbox import build_job_profile

        assert "(allow network*)" in build_job_profile(tmp_path, allow_network=True)
        assert "(allow network*)" not in build_job_profile(tmp_path)

    def test_job_environment_is_an_allowlist(self, monkeypatch, tmp_path):
        from dourmouse.sandbox import job_environment

        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "k")
        monkeypatch.setenv("LC_CTYPE", "en_US.UTF-8")
        env = job_environment(tmp_path)
        assert set(env) <= {"PATH", "HOME", "LC_CTYPE", "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONNOUSERSITE",
                            "PYTHONDONTWRITEBYTECODE", "TMPDIR"} | {k for k in env if k.startswith("LC_")}
        assert env["HOME"] == str(tmp_path)
