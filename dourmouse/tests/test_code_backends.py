"""Multi-backend coding tests (v2.4) — NVIDIA / Freebuff DeepSeek / Claude.

Exercises the REAL code_backends module: backend resolution with honest
NOT CONFIGURED when unset, the OpenAI-compatible completion path via an
injected fake client (no network in tests), the Claude CLI path via a fake
binary, the preloaded subagent tool wiring, and the roster shape.
"""

from __future__ import annotations

import os

import pytest

from dourmouse import code_backends
from dourmouse.general_roster import _make_code_tool, build_general_registry


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
# Roster wiring
# --------------------------------------------------------------------------- #

class TestCodingSubagents:
    def test_three_coding_agents_registered(self):
        registry = build_general_registry()
        names = set(registry.subagent_names)
        assert {"code_nvidia", "code_deepseek", "code_claude"} <= names

    def test_each_coding_agent_has_its_backend_tool(self):
        registry = build_general_registry()
        assert {t.name for t in registry.get_subagent("code_nvidia").tools} == {"code_nvidia"}
        assert {t.name for t in registry.get_subagent("code_deepseek").tools} == {"code_deepseek"}
        assert {t.name for t in registry.get_subagent("code_claude").tools} == {"code_claude"}

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
