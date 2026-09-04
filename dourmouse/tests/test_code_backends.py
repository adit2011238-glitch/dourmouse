"""Multi-backend coding tests (v2.4) — NVIDIA / Freebuff DeepSeek / Claude.
from pathlib import Path

Exercises the REAL code_backends module: backend resolution with honest
NOT CONFIGURED when unset, the OpenAI-compatible completion path via an
injected fake client (no network in tests), the Claude CLI path via a fake
binary, the preloaded subagent tool wiring, and the roster shape.
"""

from __future__ import annotations

import os
import uuid

import pytest

from dourmouse import code_backends
from dourmouse.general_roster import _make_code_tool, build_general_registry


@pytest.fixture(autouse=True)
def _reset_claude_sessions():
    """The Claude CLI session-continuity map (code_backends._CLAUDE_SESSIONS)
    is module-level, process-lifetime state by design (see its docstring) —
    but that means it must NOT leak between tests, several of which share
    the same cwd key ("." when no cwd is passed) and would otherwise see a
    stale --resume from a previous test instead of a fresh --session-id."""
    code_backends._CLAUDE_SESSIONS.clear()
    yield
    code_backends._CLAUDE_SESSIONS.clear()


@pytest.fixture(autouse=True)
def _reset_mcp_config_cache():
    """_ensure_mcp_config_path() caches its result for the LIFE OF THE
    PROCESS by design (the file's content never changes mid-run — see its
    own docstring) — but that same caching would leak a path from one
    test's monkeypatched user_config_dir() into a later test that expects
    a fresh write. Reset around every test."""
    code_backends._mcp_config_path_cache = None
    yield
    code_backends._mcp_config_path_cache = None


# --------------------------------------------------------------------------- #
# Fake OpenAI-compatible client (no network)
# --------------------------------------------------------------------------- #

class _FakeMessage:
    content = "def add(a, b):\n    return a + b\n"


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices = [_FakeChoice()]


class _FakeCompletions:
    def __init__(self, response=_FakeResponse()):
        self._response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAI:
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url
        self.chat = _FakeChat()


def _install_fake_openai(monkeypatch):
    fake = _FakeOpenAI("k", "https://example.invalid/v1")
    monkeypatch.setattr(code_backends, "_openai_client_factory", lambda *a, **k: fake)
    return fake.chat.completions


# --------------------------------------------------------------------------- #
# Backend resolution
# --------------------------------------------------------------------------- #

class TestLoadBackend:
    def test_nvidia_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            code_backends.load_backend("nvidia")

    def test_nvidia_resolves_from_env(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/some-model")
        base, key, model = code_backends.load_backend("nvidia")
        assert key == "nvapi-test-key"
        assert model == "nvidia/some-model"
        assert base.startswith("https://")

    def test_deepseek_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("FREEBUFF_DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        # v5.1: NVIDIA_API_KEY also powers deepseek (NIM hosts DeepSeek) —
        # clear it so the no-key path is actually tested.
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            code_backends.load_backend("deepseek")

    def test_deepseek_prefers_freebuff_env(self, monkeypatch):
        monkeypatch.setenv("FREEBUFF_DEEPSEEK_API_KEY", "fb-key")
        monkeypatch.setenv("FREEBUFF_DEEPSEEK_MODEL", "freebuff/model")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        _b, key, model = code_backends.load_backend("deepseek")
        assert key == "fb-key"
        assert model == "freebuff/model"

    def test_deepseek_falls_back_to_nvidia_key(self, monkeypatch):
        """v5.1: no DeepSeek key, but the user's NVIDIA key powers DeepSeek
        models hosted on NVIDIA NIM (the single-key setup the user has)."""
        monkeypatch.delenv("FREEBUFF_DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-nvidia-key")
        base, key, model = code_backends.load_backend("deepseek")
        assert key == "nvapi-nvidia-key"
        assert base == "https://integrate.api.nvidia.com/v1"
        assert model == "deepseek-ai/deepseek-v4-flash-0731"

    def test_deepseek_nvidia_model_override(self, monkeypatch):
        monkeypatch.delenv("FREEBUFF_DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-nvidia-key")
        monkeypatch.setenv("DEEPSEEK_NVIDIA_MODEL", "deepseek-ai/deepseek-r1")
        _b, _k, model = code_backends.load_backend("deepseek")
        assert model == "deepseek-ai/deepseek-r1"

    def test_deepseek_explicit_key_wins_over_nvidia(self, monkeypatch):
        """A real DeepSeek key always beats the NVIDIA fallback."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-real-key")
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-nvidia-key")
        _b, key, model = code_backends.load_backend("deepseek")
        assert key == "ds-real-key"
        assert model == "deepseek-chat"

    def test_deepseek_falls_back_to_plain_env(self, monkeypatch):
        monkeypatch.delenv("FREEBUFF_DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
        base, key, model = code_backends.load_backend("deepseek")
        assert key == "ds-key"
        assert model == "deepseek-chat"
        assert base == "https://api.deepseek.com/v1"

    def test_deepseek_uses_defaults_when_partial(self, monkeypatch):
        monkeypatch.delenv("FREEBUFF_DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
        base, key, model = code_backends.load_backend("deepseek")
        assert key == "ds-key"
        assert base == "https://api.deepseek.com/v1"  # documented default
        assert model == "deepseek-chat"

    def test_unknown_backend_raises(self):
        with pytest.raises(RuntimeError, match="unknown code backend"):
            code_backends.load_backend("gpt-somewhere")


# --------------------------------------------------------------------------- #
# run_code_task
# --------------------------------------------------------------------------- #

class TestRunCodeTask:
    def test_empty_task_raises(self, monkeypatch):
        with pytest.raises(RuntimeError, match="non-empty 'task'"):
            code_backends.run_code_task("nvidia", "   ")

    def test_nvidia_happy_path_via_fake_client(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/some-model")
        completions = _install_fake_openai(monkeypatch)
        out = code_backends.run_code_task("nvidia", "write an add function", timeout=30)
        assert "def add" in out
        assert completions.last_kwargs["model"] == "nvidia/some-model"
        assert completions.last_kwargs["messages"][-1]["content"] == "write an add function"

    def test_deepseek_happy_path_via_fake_client(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
        completions = _install_fake_openai(monkeypatch)
        out = code_backends.run_code_task("deepseek", "write an add function", timeout=30)
        assert "def add" in out
        assert completions.last_kwargs["model"] == "deepseek-chat"

    def test_deepseek_via_nvidia_happy_path(self, monkeypatch):
        """v5.1: deepseek backend through the user's NVIDIA key hits the
        NVIDIA DeepSeek model id end-to-end (fake client, no network)."""
        monkeypatch.delenv("FREEBUFF_DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-nvidia-key")
        completions = _install_fake_openai(monkeypatch)
        out = code_backends.run_code_task("deepseek", "write an add function", timeout=30)
        assert "def add" in out
        assert completions.last_kwargs["model"] == "deepseek-ai/deepseek-v4-flash-0731"

    def test_api_failure_is_honest(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")

        class _BoomCompletions:
            def create(self, **kwargs):
                raise RuntimeError("connection reset by peer")

        class _BoomChat:
            completions = _BoomCompletions()

        class _BoomOpenAI:
            def __init__(self, api_key, base_url):
                self.chat = _BoomChat()

        monkeypatch.setattr(code_backends, "_openai_client_factory", lambda *a, **k: _BoomOpenAI("k", "u"))
        with pytest.raises(RuntimeError, match="API call failed"):
            code_backends.run_code_task("nvidia", "task", timeout=30)

    def test_empty_response_is_honest(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")

        class _EmptyCompletions:
            def create(self, **kwargs):
                class _EmptyMsg:
                    content = "   "

                class _EmptyChoice:
                    message = _EmptyMsg()

                class _EmptyResp:
                    choices = [_EmptyChoice()]

                return _EmptyResp()

        class _EmptyChat:
            completions = _EmptyCompletions()

        class _EmptyOpenAI:
            def __init__(self, api_key, base_url):
                self.chat = _EmptyChat()

        monkeypatch.setattr(code_backends, "_openai_client_factory", lambda *a, **k: _EmptyOpenAI("k", "u"))
        with pytest.raises(RuntimeError, match="empty response"):
            code_backends.run_code_task("nvidia", "task", timeout=30)


# --------------------------------------------------------------------------- #
# Claude CLI path
# --------------------------------------------------------------------------- #

def _write_fake_cli(tmp_path, script: str) -> str:
    if os.name == "nt":
        p = tmp_path / "fake-claude.cmd"
        if ">&2" in script:
            body = ["@echo off", "echo boom 1>&2", "exit /b 3"]
        else:
            body = ["@echo off", 'echo CLAUDE SAYS: print("hello")']
        p.write_text("\r\n".join(body) + "\r\n", encoding="utf-8")
        return str(p)
    p = tmp_path / "fake-claude"
    p.write_text("#!/bin/bash\n" + script)
    p.chmod(0o755)
    return str(p)


class TestClaudeBackend:
    def test_not_configured_without_cli(self, monkeypatch):
        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: None
        )
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            code_backends.run_code_task("claude", "write code", timeout=30)

    def test_real_subprocess_roundtrip_fake_cli(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(
            tmp_path,
            'echo "CLAUDE SAYS: print(\\"hello\\")"',
        )
        monkeypatch.setattr("dourmouse.general_roster._find_claude_cli", lambda: fake)
        out = code_backends.run_code_task("claude", "write code", cwd=str(tmp_path), timeout=30)
        assert "CLAUDE SAYS" in out

    def test_nonzero_exit_surfaces_stderr(self, tmp_path, monkeypatch):
        fake = _write_fake_cli(tmp_path, 'echo "boom" >&2; exit 3')
        monkeypatch.setattr("dourmouse.general_roster._find_claude_cli", lambda: fake)
        with pytest.raises(RuntimeError, match="exited 3"):
            code_backends.run_code_task("claude", "task", cwd=str(tmp_path), timeout=30)


# --------------------------------------------------------------------------- #
# MCP bridge wiring (v13) — Claude gets --mcp-config/--allowedTools on every
# real invocation from this module; Codex gets a one-time `codex mcp add`
# registration instead (no per-call flag exists — verified live via
# `codex exec --help`/`codex --help`, see mcp_bridge.ensure_codex_mcp_registered's
# own docstring).
# --------------------------------------------------------------------------- #

class TestClaudeMcpWiring:
    def test_ensure_mcp_config_path_writes_a_real_config_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(code_backends, "user_config_dir", lambda: tmp_path)
        path = code_backends._ensure_mcp_config_path()
        assert os.path.isfile(path)
        assert path == str(tmp_path / "mcp-config.json")

    def test_ensure_mcp_config_path_is_cached_not_rewritten_every_call(self, tmp_path, monkeypatch):
        calls = []
        real_dir = tmp_path

        def _tracked_dir():
            calls.append(1)
            return real_dir

        monkeypatch.setattr(code_backends, "user_config_dir", _tracked_dir)
        first = code_backends._ensure_mcp_config_path()
        second = code_backends._ensure_mcp_config_path()
        assert first == second
        assert calls == [1]  # user_config_dir() consulted once, not twice

    def test_claude_invocation_carries_real_mcp_flags(self, tmp_path, monkeypatch):
        monkeypatch.setattr(code_backends, "user_config_dir", lambda: tmp_path)
        fake = _write_fake_cli(tmp_path, 'echo "ARGV: $*"')
        monkeypatch.setattr("dourmouse.general_roster._find_claude_cli", lambda: fake)
        out = code_backends.run_code_task("claude", "write code", cwd=str(tmp_path), timeout=30)
        assert "--mcp-config" in out
        assert str(tmp_path / "mcp-config.json") in out
        assert "--allowedTools mcp__dourmouse__*" in out

    def test_a_broken_mcp_config_never_blocks_the_coding_task(self, tmp_path, monkeypatch):
        """Best-effort by design (see _run_claude_once's own comment): if
        building the MCP config raises for any reason, claude still runs —
        just without Dourmouse tool access for that one call."""
        def _boom():
            raise RuntimeError("disk full")

        monkeypatch.setattr(code_backends, "user_config_dir", _boom)
        fake = _write_fake_cli(tmp_path, 'echo "ARGV: $*"')
        monkeypatch.setattr("dourmouse.general_roster._find_claude_cli", lambda: fake)
        out = code_backends.run_code_task("claude", "write code", cwd=str(tmp_path), timeout=30)
        assert "--mcp-config" not in out
        assert "ARGV:" in out  # the call itself still happened


class TestCodexMcpRegistration:
    def test_ensure_codex_mcp_registered_skips_add_when_already_listed(self, monkeypatch):
        from dourmouse import mcp_bridge

        calls = []

        class _Listed:
            stdout = "dourmouse   /usr/bin/python -m dourmouse.mcp_bridge  enabled\n"

        def _fake_run(argv, **kwargs):
            calls.append(argv)
            return _Listed()

        monkeypatch.setattr(mcp_bridge.subprocess, "run", _fake_run)
        mcp_bridge.ensure_codex_mcp_registered("/usr/bin/codex")
        assert calls == [["/usr/bin/codex", "mcp", "list"]]  # never re-added

    def test_ensure_codex_mcp_registered_adds_when_missing(self, monkeypatch):
        from dourmouse import mcp_bridge

        calls = []

        class _Empty:
            stdout = "Name  Command\n"

        def _fake_run(argv, **kwargs):
            calls.append(argv)
            return _Empty()

        monkeypatch.setattr(mcp_bridge.subprocess, "run", _fake_run)
        mcp_bridge.ensure_codex_mcp_registered("/usr/bin/codex")
        assert calls[0] == ["/usr/bin/codex", "mcp", "list"]
        assert calls[1][:3] == ["/usr/bin/codex", "mcp", "add"]
        assert "dourmouse" in calls[1]
        assert "-m" in calls[1] and "dourmouse.mcp_bridge" in calls[1]

    def test_a_broken_codex_cli_never_raises(self, monkeypatch):
        from dourmouse import mcp_bridge

        def _boom(argv, **kwargs):
            raise OSError("no such file")

        monkeypatch.setattr(mcp_bridge.subprocess, "run", _boom)
        mcp_bridge.ensure_codex_mcp_registered("/usr/bin/codex")  # must not raise

    def test_codex_code_tool_registers_before_running(self, tmp_path, monkeypatch):
        from dourmouse import general_roster

        registered = []
        monkeypatch.setattr(
            "dourmouse.mcp_bridge.ensure_codex_mcp_registered",
            lambda cli: registered.append(cli),
        )
        fake = _write_fake_cli(tmp_path, "echo ok")
        monkeypatch.setattr(general_roster, "_find_codex_cli", lambda: fake)
        general_roster._codex_code_tool({"task": "x", "cwd": str(tmp_path)})
        assert registered == [fake]

    def test_run_code_task_codex_also_registers(self, tmp_path, monkeypatch):
        registered = []
        monkeypatch.setattr(
            "dourmouse.mcp_bridge.ensure_codex_mcp_registered",
            lambda cli: registered.append(cli),
        )
        fake = _write_fake_cli(tmp_path, "echo ok")
        monkeypatch.setattr("dourmouse.general_roster._find_codex_cli", lambda: fake)
        code_backends.run_code_task("codex", "x", cwd=str(tmp_path), timeout=30)
        assert registered == [fake]


# --------------------------------------------------------------------------- #
# Claude CLI session continuity — the CODE screen's real gap
# --------------------------------------------------------------------------- #
#
# Verified live against the installed CLI (2.1.250) before writing this fix:
# two back-to-back code_backends.run_code_task("claude", ...) calls with no
# session threading genuinely lost context (the second call, asked "what
# variable name did I just ask you to remember?", got "No variable given.
# This first message."). `claude --help` documents -r/--resume <session_id>
# and --session-id <uuid>, and a live round-trip through those two flags in
# separate subprocess invocations genuinely recalled a fact from the first
# call. These tests pin the resulting behavior with a fake subprocess.run
# (no network / no real CLI needed).

class TestClaudeSessionContinuity:
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

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(
            code_backends.subprocess, "run", self._fake_run_factory(seen, lambda a: _Proc())
        )
        code_backends.run_code_task("claude", "write add", cwd="/tmp/proj")
        argv = seen[0]
        assert argv[0] == "/usr/bin/claude"
        assert argv[1] == "-p"
        assert "--session-id" in argv
        sid = argv[argv.index("--session-id") + 1]
        # a real UUID4, not a placeholder
        assert uuid.UUID(sid).version == 4
        # v13: task lands right after the session args, not necessarily
        # last — real --mcp-config/--allowedTools flags now ride after it
        # (see _run_claude_once). They must never ride BEFORE it:
        # --allowedTools takes a variadic value list and a trailing prompt
        # would be silently swallowed into it (live-caught on the real
        # CLI: `claude -p --allowedTools "mcp__dourmouse__*" "say hello"`
        # really does error "Input must be provided either through stdin
        # or as a prompt argument" — it ate "say hello" as another tool
        # name).
        assert argv[argv.index("--session-id") + 2] == "write add"
        assert code_backends._CLAUDE_SESSIONS["/tmp/proj"] == sid

    def test_second_call_same_cwd_resumes_the_same_session(self, monkeypatch):
        seen: list = []

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(
            code_backends.subprocess, "run", self._fake_run_factory(seen, lambda a: _Proc())
        )
        code_backends.run_code_task("claude", "first turn", cwd="/tmp/proj")
        code_backends.run_code_task("claude", "second turn", cwd="/tmp/proj")
        first_sid = seen[0][seen[0].index("--session-id") + 1]
        assert "--resume" in seen[1]
        assert seen[1][seen[1].index("--resume") + 1] == first_sid
        # v13: task lands right after --resume's value — see the sibling
        # test above for why order (not "last") is what matters here.
        assert seen[1][seen[1].index("--resume") + 2] == "second turn"

    def test_different_cwd_gets_a_different_session(self, monkeypatch):
        seen: list = []

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(
            code_backends.subprocess, "run", self._fake_run_factory(seen, lambda a: _Proc())
        )
        code_backends.run_code_task("claude", "task a", cwd="/tmp/proj-a")
        code_backends.run_code_task("claude", "task b", cwd="/tmp/proj-b")
        # both calls are FIRST calls for their respective cwd — both mint,
        # neither resumes the other's conversation.
        assert "--session-id" in seen[0]
        assert "--session-id" in seen[1]
        sid_a = seen[0][seen[0].index("--session-id") + 1]
        sid_b = seen[1][seen[1].index("--session-id") + 1]
        assert sid_a != sid_b

    def test_stale_session_id_recovers_with_one_fresh_retry(self, monkeypatch):
        """The CLI's real error text when a tracked session id no longer
        resolves (verified live: `claude -p ... --resume <bogus-uuid>` exits
        1 with stderr "No conversation found with session ID: <uuid>").
        Losing that id honestly (e.g. the user pruned local session
        history) must not hard-fail the whole task — it should forget the
        dead id and run once more on a fresh conversation."""
        code_backends._CLAUDE_SESSIONS["/tmp/proj"] = "11111111-1111-1111-1111-111111111111"
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

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", _fake_run)
        out = code_backends.run_code_task("claude", "task", cwd="/tmp/proj")
        assert out == "recovered"
        assert len(seen) == 2
        assert "--resume" in seen[0]  # the stale id was tried first, honestly
        assert "--session-id" in seen[1]  # then a real fresh one, not a guess
        # the dead id is gone; a real new one replaces it
        assert code_backends._CLAUDE_SESSIONS["/tmp/proj"] != "11111111-1111-1111-1111-111111111111"

    def test_stale_session_retries_exactly_once_not_forever(self, monkeypatch):
        """If the retry ALSO fails, the real error surfaces — no infinite
        retry loop chasing a CLI that keeps saying no."""
        code_backends._CLAUDE_SESSIONS["/tmp/proj"] = "11111111-1111-1111-1111-111111111111"
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

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", _fake_run)
        with pytest.raises(RuntimeError, match="exited 1"):
            code_backends.run_code_task("claude", "task", cwd="/tmp/proj")
        # exactly one retry: the original --resume attempt, then one fresh
        # --session-id attempt — never a third call.
        assert len(seen) == 2

    def test_no_cwd_uses_a_stable_default_key(self, monkeypatch):
        """cwd=None (the run_code_task default) must still get real
        continuity across calls, not silently skip session threading."""
        seen: list = []

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(
            code_backends.subprocess, "run", self._fake_run_factory(seen, lambda a: _Proc())
        )
        code_backends.run_code_task("claude", "first")
        code_backends.run_code_task("claude", "second")
        assert "--session-id" in seen[0]
        assert "--resume" in seen[1]


# --------------------------------------------------------------------------- #
# Roster wiring
# --------------------------------------------------------------------------- #

class TestCodingSubagents:
    def test_three_coding_agents_registered(self):
        registry = build_general_registry()
        names = set(registry.subagent_names)
        assert {"code_nvidia", "code_deepseek", "code_claude"} <= names

    def test_each_coding_agent_has_its_backend_tool(self):
        registry = build_general_registry()
        # query_shared_memory (shared_rag.py) rides every non-orchestrator
        # subagent — see build_general_registry's own comment.
        assert {t.name for t in registry.get_subagent("code_nvidia").tools} == {"code_nvidia", "query_shared_memory"}
        assert {t.name for t in registry.get_subagent("code_deepseek").tools} == {"code_deepseek", "query_shared_memory"}
        assert {t.name for t in registry.get_subagent("code_claude").tools} == {"code_claude", "query_shared_memory"}

    def test_tool_names_globally_unique_across_roster(self):
        """A tool NAME maps to exactly ONE spec object across the roster.

        v5.8 extend_subagent deliberately shares the SAME spec object across
        report-producing agents (publish_artifact); that is not a collision.
        A DIFFERENT object claiming the same name would be — and the
        registry rejects it. This checks the post-construction invariant:
        each name owns a single object.
        """
        registry = build_general_registry()
        owners: dict[str, int] = {}
        for agent in registry.all_subagents():
            for tool in agent.tools:
                key = id(tool)
                prev = owners.get(tool.name)
                assert prev is None or prev == key, f"collision: {tool.name}"
                owners[tool.name] = key

    def test_make_code_tool_empty_task_errors(self):
        tool = _make_code_tool("nvidia")
        out = tool.handler({"task": "  "})
        assert "ERROR" in out

    def test_make_code_tool_reports_backend_not_configured(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        tool = _make_code_tool("nvidia")
        out = tool.handler({"task": "write code"})
        assert "NOT CONFIGURED" in out
        assert "CODE NVIDIA" in out


# --------------------------------------------------------------------------- #
# v8.7 — Codex routes through the CLI, and Claude reports sign-in honestly
# --------------------------------------------------------------------------- #

class TestCodexCliFirst:
    """``code_codex`` must use the SAME thing its status light measures.

    The CODEX connection probe reports on the Codex CLI + ~/.codex/auth.json
    (the user's ChatGPT login). The backend previously went straight to the
    OpenAI API, so a user who ran ``codex login`` saw a green light and
    still got "needs CODEX_API_KEY". These pin the CLI as the primary path.
    """

    def test_codex_prefers_cli_over_api_key(self, monkeypatch):
        """With a CLI present, the API key path is NOT used."""
        seen: dict[str, object] = {}

        class _Proc:
            returncode = 0
            stdout = "def fib(n): ..."
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _Proc()

        monkeypatch.setattr(
            "dourmouse.general_roster._find_codex_cli", lambda: "/usr/bin/codex"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", _fake_run)
        # A key IS set — if the API path were taken this would still pass,
        # so assert on the argv to prove the CLI actually ran.
        monkeypatch.setenv("CODEX_API_KEY", "sk-should-not-be-used")
        out = code_backends.run_code_task("codex", "write fib")
        assert out == "def fib(n): ..."
        assert seen["argv"][:2] == ["/usr/bin/codex", "exec"]
        assert "--skip-git-repo-check" in seen["argv"]

    def test_codex_falls_back_to_api_when_no_cli(self, monkeypatch):
        """No CLI + a key set → the OpenAI-compatible path still works."""
        completions = _install_fake_openai(monkeypatch)
        monkeypatch.setattr(
            "dourmouse.general_roster._find_codex_cli", lambda: None
        )
        monkeypatch.setenv("CODEX_API_KEY", "sk-test")
        out = code_backends.run_code_task("codex", "write add")
        assert "def add" in out
        assert completions.last_kwargs is not None

    def test_codex_unconfigured_error_names_both_routes(self, monkeypatch):
        """No CLI and no key → the error must mention the CLI login too.

        Naming only the API key contradicts the CODEX status light, which
        reports on the CLI — that mismatch is the bug this pins shut.
        """
        monkeypatch.setattr(
            "dourmouse.general_roster._find_codex_cli", lambda: None
        )
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as exc:
            code_backends.run_code_task("codex", "write add")
        msg = str(exc.value)
        assert "NOT CONFIGURED" in msg
        assert "codex login" in msg
        assert "CODEX_API_KEY" in msg

    def test_codex_auth_failure_suggests_login(self, monkeypatch):
        """A non-zero exit with no stderr points at sign-in, not a shrug."""

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = ""

        monkeypatch.setattr(
            "dourmouse.general_roster._find_codex_cli", lambda: "/usr/bin/codex"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", lambda *a, **k: _Proc())
        with pytest.raises(RuntimeError) as exc:
            code_backends.run_code_task("codex", "write add")
        assert "codex login" in str(exc.value)


class TestClaudeSignInError:
    def test_empty_stderr_exit_names_sign_in(self, monkeypatch):
        """`claude -p` exits 1 with empty stderr when not signed in.

        "exited 1: (no stderr)" is useless to the user; the message must
        name the likely cause WITHOUT asserting it as verified fact.
        """

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = ""

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", lambda *a, **k: _Proc())
        with pytest.raises(RuntimeError) as exc:
            code_backends.run_code_task("claude", "write add")
        msg = str(exc.value)
        assert "NOT SIGNED IN" in msg
        assert "/login" in msg

    def test_real_stderr_is_preserved_verbatim(self, monkeypatch):
        """A genuine error must NOT be replaced by the sign-in guess."""

        class _Proc:
            returncode = 2
            stdout = ""
            stderr = "rate limit exceeded"

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", lambda *a, **k: _Proc())
        with pytest.raises(RuntimeError) as exc:
            code_backends.run_code_task("claude", "write add")
        msg = str(exc.value)
        assert "rate limit exceeded" in msg
        assert "NOT SIGNED IN" not in msg


# --------------------------------------------------------------------------- #
# v8.31 — shared-memory context injection for the CLI-shelled-out backends
# --------------------------------------------------------------------------- #

class TestSharedContextInjection:
    """The CLI-shelled-out backends (code_claude, code_codex) don't speak
    the ToolSpec protocol, so ``_inject_shared_context`` prepends real
    retrieved shared-memory context onto the task string BEFORE the CLI
    subprocess runs, mirroring ``dispatch._append_memory_context``'s own
    prepend pattern. See ``code_backends._inject_shared_context``."""

    def test_not_configured_injects_nothing_for_claude(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_GLOBAL_MEMORY", raising=False)
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_PATH", raising=False)
        seen: dict[str, object] = {}

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _Proc()

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", _fake_run)
        code_backends.run_code_task("claude", "write a fib function")
        # argv == [cli, "-p", *session_args, task, *mcp_args] — task rides
        # right after the session args, not necessarily last (v13: real
        # --mcp-config/--allowedTools flags ride after it — see
        # TestClaudeSessionContinuity below for why they can't ride
        # before).
        argv = seen["argv"]
        assert argv[argv.index("--session-id") + 2] == "write a fib function"

    def test_hits_get_prepended_to_the_claude_task(self, monkeypatch):
        from dourmouse import shared_rag

        fake_result = shared_rag.MergedResult(
            hits=[{"score": 0.9, "source": "local", "text": "prior note: use pytest"}],
            sources_used=["local"],
            warnings=[],
        )
        monkeypatch.setattr(shared_rag, "merged_search", lambda q, **k: fake_result)
        seen: dict[str, object] = {}

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _Proc()

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", _fake_run)
        code_backends.run_code_task("claude", "write a fib function")
        argv = seen["argv"]
        sent_task = argv[argv.index("--session-id") + 2]
        assert "prior note: use pytest" in sent_task
        assert sent_task.endswith("write a fib function")
        # real formatting came from format_merged_result, not a hand-rolled dupe
        assert "SHARED MEMORY SEARCH" in sent_task

    def test_hits_get_prepended_to_the_codex_task(self, monkeypatch):
        from dourmouse import shared_rag

        fake_result = shared_rag.MergedResult(
            hits=[{"score": 0.8, "source": "spatial_vault", "text": "desktop vault note"}],
            sources_used=["spatial_vault"],
            warnings=[],
        )
        monkeypatch.setattr(shared_rag, "merged_search", lambda q, **k: fake_result)
        seen: dict[str, object] = {}

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _Proc()

        monkeypatch.setattr(
            "dourmouse.general_roster._find_codex_cli", lambda: "/usr/bin/codex"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", _fake_run)
        code_backends.run_code_task("codex", "write add function")
        # argv == [cli, "exec", task, "--skip-git-repo-check"]
        sent_task = seen["argv"][2]
        assert "desktop vault note" in sent_task
        assert sent_task.endswith("write add function")

    def test_lookup_failure_is_swallowed_task_unchanged(self, monkeypatch):
        from dourmouse import shared_rag

        def _boom(q, **k):
            raise RuntimeError("vault exploded")

        monkeypatch.setattr(shared_rag, "merged_search", _boom)
        seen: dict[str, object] = {}

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _Proc()

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", _fake_run)
        out = code_backends.run_code_task("claude", "write a fib function")
        assert out == "ok"
        argv = seen["argv"]
        assert argv[argv.index("--session-id") + 2] == "write a fib function"

    def test_empty_hits_injects_nothing(self, monkeypatch):
        from dourmouse import shared_rag

        empty_result = shared_rag.MergedResult(hits=[], sources_used=["local"], warnings=[])
        monkeypatch.setattr(shared_rag, "merged_search", lambda q, **k: empty_result)
        seen: dict[str, object] = {}

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _Proc()

        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )
        monkeypatch.setattr(code_backends.subprocess, "run", _fake_run)
        code_backends.run_code_task("claude", "write a fib function")
        argv = seen["argv"]
        assert argv[argv.index("--session-id") + 2] == "write a fib function"


# --------------------------------------------------------------------------- #
# stream_claude — the CODE screen's real "talking to Claude directly" path
# (v13.2, explicit user request). A fake subprocess.Popen stands in for the
# real CLI: .stdout is an iterator of real stream-json NDJSON lines
# (verified live against the actual installed CLI — see stream_claude's own
# module docstring), .stderr has a .read(), .wait()/.kill() are no-ops.
# --------------------------------------------------------------------------- #

class _FakePopenStream:
    def __init__(self, lines, returncode=0, stderr_text=""):
        self.stdout = iter(lines)
        self._stderr_text = stderr_text
        self.returncode = returncode
        self.stderr = self

    def read(self):
        return self._stderr_text

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def _sse_line(d):
    import json as _json
    return _json.dumps(d) + "\n"


class TestStreamClaude:
    def _patch(self, monkeypatch, lines, returncode=0, stderr_text="", seen=None):
        monkeypatch.setattr(
            "dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude"
        )

        def _fake_popen(argv, **kwargs):
            if seen is not None:
                seen.append(argv)
            return _FakePopenStream(lines, returncode=returncode, stderr_text=stderr_text)

        monkeypatch.setattr(code_backends.subprocess, "Popen", _fake_popen)

    def test_text_deltas_stream_live_and_final_result_returned(self, monkeypatch):
        lines = [
            _sse_line({"type": "stream_event", "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hel"},
            }}),
            _sse_line({"type": "stream_event", "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "lo."},
            }}),
            _sse_line({"type": "result", "result": "Hello."}),
        ]
        self._patch(monkeypatch, lines)
        deltas = []
        out = code_backends.stream_claude(
            "say hello", cwd="/tmp/proj", timeout=30, on_delta=deltas.append
        )
        assert deltas == ["Hel", "lo."]
        assert out == "Hello."

    def test_thinking_deltas_go_to_their_own_callback_not_on_delta(self, monkeypatch):
        lines = [
            _sse_line({"type": "stream_event", "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "Let me think..."},
            }}),
            _sse_line({"type": "stream_event", "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Answer."},
            }}),
            _sse_line({"type": "result", "result": "Answer."}),
        ]
        self._patch(monkeypatch, lines)
        deltas, thinks = [], []
        out = code_backends.stream_claude(
            "task", cwd="/tmp/proj", timeout=30,
            on_delta=deltas.append, on_thinking=thinks.append,
        )
        assert thinks == ["Let me think..."]
        assert deltas == ["Answer."]
        assert out == "Answer."

    def test_tool_use_fires_on_start_empty_then_again_with_full_args(self, monkeypatch):
        lines = [
            _sse_line({"type": "stream_event", "event": {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "id": "t1", "name": "Bash"},
            }}),
            _sse_line({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"command":'},
            }}),
            _sse_line({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"ls"}'},
            }}),
            _sse_line({"type": "stream_event", "event": {
                "type": "content_block_stop", "index": 0,
            }}),
            _sse_line({"type": "result", "result": "done"}),
        ]
        self._patch(monkeypatch, lines)
        calls = []
        code_backends.stream_claude(
            "task", cwd="/tmp/proj", timeout=30,
            on_delta=lambda t: None, on_tool_use=lambda n, a: calls.append((n, a)),
        )
        assert calls[0] == ("Bash", "")
        assert calls[1] == ("Bash", '{"command":"ls"}')

    def test_tool_result_relayed_from_user_role_message(self, monkeypatch):
        lines = [
            _sse_line({"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "file1.py\nfile2.py"},
            ]}}),
            _sse_line({"type": "result", "result": "Listed the files."}),
        ]
        self._patch(monkeypatch, lines)
        results = []
        code_backends.stream_claude(
            "task", cwd="/tmp/proj", timeout=30,
            on_delta=lambda t: None, on_tool_result=results.append,
        )
        assert results == ["file1.py\nfile2.py"]

    def test_on_usage_receives_real_result_event_fields(self, monkeypatch):
        """v13.6, real usage bar: field names/shape here are transcribed
        EXACTLY from a real `claude -p --output-format stream-json
        --include-partial-messages --verbose` invocation run live this
        session, not guessed."""
        lines = [
            _sse_line({
                "type": "result", "result": "OK", "total_cost_usd": 0.0446764,
                "usage": {
                    "input_tokens": 2, "output_tokens": 4,
                    "cache_creation_input_tokens": 10010, "cache_read_input_tokens": 22962,
                    "output_tokens_details": {"thinking_tokens": 0},  # a real, irrelevant field -- must be ignored, not crash
                },
            }),
        ]
        self._patch(monkeypatch, lines)
        usages = []
        code_backends.stream_claude(
            "task", cwd="/tmp/proj", timeout=30,
            on_delta=lambda t: None, on_usage=usages.append,
        )
        assert usages == [{
            "cost_usd": 0.0446764, "input_tokens": 2, "output_tokens": 4,
            "cache_creation_input_tokens": 10010, "cache_read_input_tokens": 22962,
        }]

    def test_on_usage_not_called_when_result_carries_no_usage_fields(self, monkeypatch):
        lines = [_sse_line({"type": "result", "result": "ok"})]
        self._patch(monkeypatch, lines)
        usages = []
        code_backends.stream_claude(
            "task", cwd="/tmp/proj", timeout=30,
            on_delta=lambda t: None, on_usage=usages.append,
        )
        assert usages == []

    def test_on_usage_is_optional_and_a_raising_callback_never_breaks_the_reply(self, monkeypatch):
        lines = [
            _sse_line({"type": "result", "result": "done", "total_cost_usd": 0.01, "usage": {"input_tokens": 1}}),
        ]
        self._patch(monkeypatch, lines)

        def _raise(usage):
            raise RuntimeError("usage tracker down")

        out = code_backends.stream_claude(
            "task", cwd="/tmp/proj", timeout=30,
            on_delta=lambda t: None, on_usage=_raise,
        )
        assert out == "done"  # the real reply is unaffected

    def test_session_continuity_same_as_run_claude(self, monkeypatch):
        seen: list = []
        lines = [_sse_line({"type": "result", "result": "ok"})]
        self._patch(monkeypatch, lines, seen=seen)
        code_backends.stream_claude("first", cwd="/tmp/proj-stream", timeout=30, on_delta=lambda t: None)
        code_backends.stream_claude("second", cwd="/tmp/proj-stream", timeout=30, on_delta=lambda t: None)
        assert "--session-id" in seen[0]
        assert "--resume" in seen[1]

    def test_nonzero_exit_raises_with_real_stderr(self, monkeypatch):
        self._patch(monkeypatch, [], returncode=1, stderr_text="boom: bad flag")
        with pytest.raises(RuntimeError, match="boom: bad flag"):
            code_backends.stream_claude("task", cwd="/tmp/proj", timeout=30, on_delta=lambda t: None)

    def test_empty_stderr_nonzero_exit_gives_the_not_signed_in_hint(self, monkeypatch):
        self._patch(monkeypatch, [], returncode=1, stderr_text="")
        with pytest.raises(RuntimeError, match="NOT SIGNED IN"):
            code_backends.stream_claude("task", cwd="/tmp/proj", timeout=30, on_delta=lambda t: None)

    def test_no_result_event_is_an_honest_error(self, monkeypatch):
        self._patch(monkeypatch, [_sse_line({"type": "system", "subtype": "init"})])
        with pytest.raises(RuntimeError, match="no output"):
            code_backends.stream_claude("task", cwd="/tmp/proj", timeout=30, on_delta=lambda t: None)

    def test_cli_not_found_is_not_configured(self, monkeypatch):
        monkeypatch.setattr("dourmouse.general_roster._find_claude_cli", lambda: None)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            code_backends.stream_claude("task", cwd="/tmp/proj", timeout=30, on_delta=lambda t: None)

    def test_uses_bypass_permissions_mode(self, monkeypatch):
        """v13.5: full terminal-parity permission mode, not the old
        acceptEdits (which left Bash and every non-file-edit tool category
        asking a permission question this headless subprocess has no TTY
        to answer)."""
        seen: list = []
        self._patch(monkeypatch, [_sse_line({"type": "result", "result": "ok"})], seen=seen)
        code_backends.stream_claude("task", cwd="/tmp/proj", timeout=30, on_delta=lambda t: None)
        argv = seen[0]
        assert "--permission-mode" in argv
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
        assert "--output-format" in argv
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--include-partial-messages" in argv


class TestCliResolutionSurvivesAGuiLaunch:
    """Regression tests for the "Dourmouse can't access my Google Workspace"
    report, which turned out to be two separate real bugs on the Claude
    route. Neither was an auth problem: dourmouse's own gmail_search was
    working and returning real inbox rows the whole time.
    """

    def test_cli_is_found_without_the_users_shell_path(self, monkeypatch, tmp_path):
        """A macOS app launched from the Dock does not inherit the shell
        PATH. The dourmouse2.app server was measured running with
        PATH=/usr/bin:/bin:/usr/sbin:/sbin, so shutil.which("claude")
        returned None and every Claude request reported NOT CONFIGURED --
        while the binary sat at ~/.local/bin/claude, installed and logged in.
        """
        from dourmouse import general_roster

        fake_home = tmp_path / "home"
        (fake_home / ".local" / "bin").mkdir(parents=True)
        cli = fake_home / ".local" / "bin" / "claude"
        cli.write_text("#!/bin/sh\nexit 0\n")
        cli.chmod(0o755)

        monkeypatch.delenv("CLAUDE_CODE_CLI", raising=False)
        # The exact PATH the GUI-launched server process was measured with.
        monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        # Path.expanduser() resolves "~" from HOME on POSIX, so pointing HOME
        # at the fixture is enough -- no need to patch pathlib itself.
        monkeypatch.setenv("HOME", str(fake_home))

        assert general_roster._find_claude_cli() == str(cli)

    def test_child_env_keeps_the_parent_environment_and_widens_path(self):
        """The CLI resolves the user's subscription session itself, from the
        macOS Keychain ("Claude Code-credentials") or
        ~/.claude/.credentials.json. Handing it a stripped or hand-built
        environment is what would break that, so the parent environment must
        be inherited whole -- PATH is added to, never replaced.
        """
        import os

        from dourmouse.code_backends import _cli_env

        os.environ["DOURMOUSE_ENV_INHERITANCE_PROBE"] = "kept"
        try:
            env = _cli_env("/usr/bin/true")
        finally:
            os.environ.pop("DOURMOUSE_ENV_INHERITANCE_PROBE", None)

        assert env["DOURMOUSE_ENV_INHERITANCE_PROBE"] == "kept"
        # Everything the parent had on PATH is still reachable.
        for part in (os.environ.get("PATH") or "").split(os.pathsep):
            if part:
                assert part in env["PATH"].split(os.pathsep)
        # And the resolved CLI's own directory leads, so its helpers win.
        assert env["PATH"].split(os.pathsep)[0] == "/usr/bin"

    def test_strict_mcp_config_is_passed_so_only_dourmouse_tools_load(self):
        """Without --strict-mcp-config the CLI also loads the user's own
        claude.ai connectors, and Claude preferred those: asked for the
        latest email it called mcp__claude_ai_Gmail__search_threads and
        returned "Gmail auth insufficient scope. Need reauthorize
        connector". --allowedTools did not prevent it, because that gates
        which tools may RUN, not which MCP servers are loaded and offered.
        """
        import inspect

        from dourmouse import code_backends

        source = inspect.getsource(code_backends)
        # Both spawn sites (blocking _run_claude and streaming stream_claude).
        assert source.count('"--strict-mcp-config"') == 2, (
            "every claude invocation that loads Dourmouse's MCP config must "
            "also pass --strict-mcp-config, or the broken claude.ai Gmail "
            "connector comes back and wins again"
        )
