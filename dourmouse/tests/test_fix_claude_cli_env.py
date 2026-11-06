"""Finding #191: a Claude Code pointed at another provider must not reach Dourmouse's `claude` child.

Live repro (owner test): Dourmouse started from a shell whose environment held ANTHROPIC_BASE_URL (a
gateway), ANTHROPIC_API_KEY=ollama and ANTHROPIC_DEFAULT_*_MODEL=deepseek-r1:14b[1m]. `claude auth status`
then said loggedIn true (authMethod api_key) for a CLI with no login of its own, so the signed-out fallback of
finding #183 never ran, and every Claude-front turn answered
"claude exited 1: [claude-code:unrecognized_model] ... Invalid API key".
"""

from __future__ import annotations

import os
import subprocess

import pytest

from dourmouse import code_backends, connections, general_roster
from dourmouse.tests.conftest import REAL_CLAUDE_CLI_SIGNED_IN

LEAKED = {
    "ANTHROPIC_BASE_URL": "https://gateway.invalid/anthropic",
    "ANTHROPIC_API_KEY": "ollama",
    "ANTHROPIC_AUTH_TOKEN": "placeholder",
    "ANTHROPIC_MODEL": "deepseek-r1:14b[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-r1:14b[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-r1:14b[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-r1:14b[1m]",
    "CLAUDE_CODE_USE_BEDROCK": "1",
}


@pytest.fixture()
def leaked_env(monkeypatch):
    for name, value in LEAKED.items():
        monkeypatch.setenv(name, value)
    return LEAKED


def test_every_routing_variable_is_in_the_scrub_list():
    assert set(LEAKED) <= set(code_backends._CLAUDE_ROUTING_ENV)


def test_the_child_environment_loses_the_routing_variables_and_keeps_the_rest(leaked_env, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_ENV_INHERITANCE_PROBE", "kept")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "a-sign-in-not-a-route")
    env = code_backends._cli_env("/usr/bin/true")
    for name in code_backends._CLAUDE_ROUTING_ENV:
        assert name not in env
    assert env["DOURMOUSE_ENV_INHERITANCE_PROBE"] == "kept"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "a-sign-in-not-a-route"
    assert "PATH" in env and "HOME" in env


def test_only_the_copy_is_scrubbed_this_process_keeps_its_environment(leaked_env):
    code_backends._cli_env("/usr/bin/true")
    for name, value in LEAKED.items():
        assert os.environ[name] == value


def test_the_sign_in_probe_runs_with_the_cleaned_environment(leaked_env, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout='{"loggedIn": false, "authMethod": "none"}', stderr="")

    monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/fake/claude")
    monkeypatch.setattr(subprocess, "run", fake_run)
    code_backends._SIGNIN_CACHE["value"] = None
    try:
        assert REAL_CLAUDE_CLI_SIGNED_IN(ttl=0) is False
    finally:
        code_backends._SIGNIN_CACHE["value"] = None
    assert seen["argv"][-2:] == ["auth", "status"]
    assert seen["env"] is not None, "the probe must hand the child the cleaned environment, not inherit by default"
    for name in LEAKED:
        assert name not in seen["env"]


def test_claude_p_runs_with_the_cleaned_environment(leaked_env, monkeypatch):
    seen = {}

    class _Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kw):
        seen["env"] = kw.get("env")
        return _Done()

    monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(code_backends.subprocess, "run", fake_run)
    assert code_backends.run_code_task("claude", "say ok") == "ok"
    for name in LEAKED:
        assert name not in seen["env"]


def test_the_streaming_claude_child_runs_with_the_cleaned_environment(leaked_env, monkeypatch):
    seen = {}

    class _Stdin:
        def write(self, _text):
            return None

        def close(self):
            return None

    class _Pipe:
        def __init__(self, lines=()):
            self._lines = list(lines)

        def __iter__(self):
            return iter(self._lines)

        def read(self):
            return ""

    class _Proc:
        returncode = 0

        def __init__(self, **kw):
            seen["env"] = kw.get("env")
            self.stdin = _Stdin()
            self.stdout = _Pipe(['{"type": "result", "result": "ok"}\n'])
            self.stderr = _Pipe()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(code_backends.subprocess, "Popen", lambda argv, **kw: _Proc(**kw))
    code_backends.stream_claude("say ok", cwd=None, timeout=5, on_delta=lambda _t: None)
    assert seen["env"] is not None
    for name in LEAKED:
        assert name not in seen["env"]


class TestTheConnectionsReportFollowsTheCliItself:
    def _report(self, monkeypatch, *, keychain: str, cli_says):
        monkeypatch.setattr(connections, "_cli_version", lambda name: "2.1.220" if name == "claude" else None)
        monkeypatch.setattr(connections, "_claude_signin", lambda: keychain)
        monkeypatch.setattr(code_backends, "claude_cli_signed_in", lambda **k: cli_says)
        return connections.check_connections()["claude"]

    def test_an_expired_login_is_not_a_green_tick(self, monkeypatch):
        """The Keychain still holds the old entry ('yes'); `claude auth status` says loggedIn false."""
        report = self._report(monkeypatch, keychain="yes", cli_says=False)
        assert report["ok"] is False
        assert "signed out" in report["detail"]
        assert "claude auth login" in report["hint"]
        assert "configured cloud model" in report["hint"]

    def test_signed_in_stays_signed_in(self, monkeypatch):
        report = self._report(monkeypatch, keychain="yes", cli_says=True)
        assert report["ok"] is True and "signed in" in report["detail"]

    def test_a_probe_that_cannot_tell_changes_nothing(self, monkeypatch):
        assert self._report(monkeypatch, keychain="yes", cli_says=None)["ok"] is True
        unknown = self._report(monkeypatch, keychain="unknown", cli_says=None)
        assert unknown["ok"] is False and "not verified" in unknown["detail"]

    def test_a_probe_that_raises_never_breaks_the_report(self, monkeypatch):
        def boom(**_k):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(connections, "_cli_version", lambda name: "2.1.220" if name == "claude" else None)
        monkeypatch.setattr(connections, "_claude_signin", lambda: "yes")
        monkeypatch.setattr(code_backends, "claude_cli_signed_in", boom)
        assert connections.check_connections()["claude"]["ok"] is True
