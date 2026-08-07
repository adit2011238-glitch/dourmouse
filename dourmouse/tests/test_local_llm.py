"""v4.0 local-LLM backend tests (Ollama) — config resolver + wiring.

Covers the DOURMOUSE_LLM_BACKEND=ollama|nvidia|auto resolver in config.py, the
shared BackendConfig interface (model_for_agent on both backends), the
honest auto-detection probe, dispatch/orchestrator default resolution, the
webui /api/backend endpoint + roster model labels, the code_backends ollama
path, and the new code_ollama roster agent. All hermetic: the Ollama probe is
monkeypatched, and no network is ever touched (Rules 2.1 / 2.8).
"""

from __future__ import annotations

import http.client
import threading
import time
from typing import Any

import pytest

from dourmouse import config as config_module
from dourmouse.config import (
    OllamaConfig,
    load_llm_config,
    load_ollama_config,
    ollama_available,
)
from dourmouse.general_roster import build_general_registry


# --------------------------------------------------------------------------- #
# OllamaConfig + load_ollama_config
# --------------------------------------------------------------------------- #

class TestOllamaConfig:
    def test_defaults(self, monkeypatch):
        for name in (
            "OLLAMA_BASE_URL",
            "OLLAMA_MODEL",
            "OLLAMA_MAX_RETRIES",
            "OLLAMA_RETRY_BACKOFF",
            "OLLAMA_FALLBACK_MODEL",
            "DOURMOUSE_OLLAMA_MODEL_DEV_CODING",
            "DOURMOUSE_OLLAMA_MODEL_NEWS",
        ):
            monkeypatch.delenv(name, raising=False)
        for name in ("DOURMOUSE_OLLAMA_MODEL_DEV_CODING", "DOURMOUSE_OLLAMA_MODEL_NEWS"):
            monkeypatch.delenv(name, raising=False)
        cfg = load_ollama_config()
        assert cfg.api_key == ""  # keyless — the local-first guarantee
        assert cfg.base_url == "http://127.0.0.1:11434/v1"
        assert cfg.model == "qwen3:8b"
        assert cfg.model_for_agent("news") == "qwen3:8b"
        assert cfg.model_for_agent(None) == "qwen3:8b"
        assert cfg.model_for_agent("") == "qwen3:8b"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9999/v1")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:14b")
        monkeypatch.setenv("OLLAMA_MAX_RETRIES", "4")
        monkeypatch.setenv("DOURMOUSE_OLLAMA_MODEL_DEV_CODING", "qwen2.5-coder:14b")
        monkeypatch.setenv("DOURMOUSE_OLLAMA_MODEL_NEWS", "glm-4.7-flash:latest")
        cfg = load_ollama_config()
        assert cfg.base_url == "http://127.0.0.1:9999/v1"
        assert cfg.model == "qwen2.5-coder:14b"
        assert cfg.max_retries == 4
        # per-agent overrides win; others fall back to the default
        assert cfg.model_for_agent("dev_coding") == "qwen2.5-coder:14b"
        assert cfg.model_for_agent("DEV_CODING") == "qwen2.5-coder:14b"  # case-insensitive
        assert cfg.model_for_agent("news") == "glm-4.7-flash:latest"
        assert cfg.model_for_agent("markets") == "qwen2.5-coder:14b"


# --------------------------------------------------------------------------- #
# ollama_available probe (monkeypatched — no network)
# --------------------------------------------------------------------------- #

class TestOllamaAvailable:
    def test_returns_true_when_ok(self, monkeypatch):
        class _Resp:
            status = 200

        class _Ctx:
            def __enter__(self):
                return _Resp()

            def __exit__(self, *a):
                return False

        def _fake_urlopen(url, timeout=1.0):  # noqa: ARG001
            assert "11434" in url
            return _Ctx()

        monkeypatch.setattr(config_module.urllib.request, "urlopen", _fake_urlopen)
        assert ollama_available() is True

    def test_returns_false_on_any_failure(self, monkeypatch):
        def _boom(url, timeout=1.0):  # noqa: ARG001
            raise OSError("connection refused")

        monkeypatch.setattr(config_module.urllib.request, "urlopen", _boom)
        assert ollama_available() is False

    def test_never_raises_on_garbage(self, monkeypatch):
        def _boom(url, timeout=1.0):  # noqa: ARG001
            raise TimeoutError("timed out")

        monkeypatch.setattr(config_module.urllib.request, "urlopen", _boom)
        assert ollama_available() is False


# --------------------------------------------------------------------------- #
# llm_backend / load_llm_config resolver
# --------------------------------------------------------------------------- #

class TestLlmBackendResolver:
    def test_invalid_backend_raises(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "banana")
        with pytest.raises(ValueError):
            config_module.llm_backend()

    def test_explicit_ollama_returns_ollama_config(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")
        cfg = load_llm_config()
        assert isinstance(cfg, OllamaConfig)
        assert cfg.api_key == ""

    def test_explicit_nvidia_requires_key(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        with pytest.raises(ValueError):
            load_llm_config()

    def test_auto_prefers_ollama_when_available(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "auto")
        monkeypatch.setattr(config_module, "ollama_available", lambda: True)
        cfg = load_llm_config()
        assert isinstance(cfg, OllamaConfig)

    def test_auto_falls_back_to_nvidia_when_no_ollama(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "auto")
        monkeypatch.setattr(config_module, "ollama_available", lambda: False)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        from dourmouse.config import NvidiaConfig

        cfg = load_llm_config()
        assert isinstance(cfg, NvidiaConfig)
        assert cfg.api_key == "nvapi-test"

    def test_default_is_auto(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_LLM_BACKEND", raising=False)
        assert config_module.llm_backend() == "auto"


# --------------------------------------------------------------------------- #
# webui: backend label + /api/backend over real HTTP
# --------------------------------------------------------------------------- #

class TestBackendEndpoint:
    def test_backend_label_ollama(self):
        from dourmouse.webui import _backend_label

        assert _backend_label(OllamaConfig()) == "ollama"
        assert _backend_label(None) == "default"

    def test_api_backend_route_live(self, monkeypatch):
        # Serve with an explicit Ollama config so no NVIDIA key / probe is
        # needed; the route must report ollama + the effective model.
        from dourmouse.webui import run_server

        registry = build_general_registry()
        cfg = OllamaConfig(model="qwen3:8b")
        server = run_server(registry, port=0, config=cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            for _ in range(50):
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=3)
                    conn.request("GET", "/api/backend")
                    resp = conn.getresponse()
                    break
                except (ConnectionRefusedError, OSError):
                    time.sleep(0.1)
                    conn.close()
            assert resp.status == 200
            import json as _json

            body = _json.loads(resp.read().decode())
            assert body["backend"] == "ollama"
            assert body["model"] == "qwen3:8b"
            assert "11434" in body["base_url"]
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_roster_payload_carries_per_agent_models(self, monkeypatch):
        from dourmouse.webui import build_roster_payload

        cfg = OllamaConfig(
            model="qwen3:8b",
            agent_models={"DEV_CODING": "qwen2.5-coder:14b"},
        )
        payload = build_roster_payload(build_general_registry(), cfg)
        by_name = {s["name"]: s for s in payload["subagents"]}
        assert by_name["dev_coding"]["model"] == "qwen2.5-coder:14b"
        assert by_name["news"]["model"] == "qwen3:8b"
        assert by_name["code_ollama"]["model"] == "qwen3:8b"


# --------------------------------------------------------------------------- #
# dispatch / orchestrator default resolution
# --------------------------------------------------------------------------- #

class TestDispatchDefaultBackend:
    def test_run_dispatch_uses_resolver_when_no_client(self, monkeypatch):
        """Without an injected client, the engine resolves the backend via
        load_llm_config — so a keyless Ollama deployment works out of the box."""
        from dourmouse import dispatch

        calls: list[str] = []

        def _fake_resolver():
            calls.append("resolved")
            return OllamaConfig()

        def _fake_build_client(config):  # noqa: ARG001
            calls.append("built")
            return type("_C", (), {})()  # never actually used — no LLM turn

        monkeypatch.setattr(dispatch, "load_llm_config", _fake_resolver)
        monkeypatch.setattr(dispatch, "_build_client", _fake_build_client)
        # A registry whose only tool errors harmlessly: we just need the
        # default-config path to run and resolve through the resolver.
        registry = build_general_registry()
        with pytest.raises(Exception):
            # no real client → the first LLM call fails, but ONLY AFTER the
            # resolver ran. We assert the resolution happened.
            dispatch.run_dispatch_messages(
                [{"role": "user", "content": "hi"}],
                registry,
                max_turns=1,
            )
        assert "resolved" in calls

    def test_code_backends_ollama(self, monkeypatch):
        from dourmouse import code_backends

        monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:14b")
        base, key, model = code_backends.load_backend("ollama")
        assert base == "http://127.0.0.1:11434/v1"
        assert key == ""
        assert model == "qwen2.5-coder:14b"

    def test_code_ollama_agent_registered(self):
        registry = build_general_registry()
        names = set(registry.subagent_names)
        assert "code_ollama" in names
        tool = registry.lookup("code_ollama")
        assert tool is not None
        assert "ollama" in tool.description.lower()

class TestLiveCaughtRegressions:
    """Both bugs were caught LIVE against real Ollama, then pinned here."""

    def test_keyless_backend_never_sends_empty_key(self):
        """The OpenAI SDK rejects api_key='' — keyless Ollama configs must
        substitute a sentinel or the local brain crashes (Missing credentials)."""
        from dourmouse.dispatch import _build_client

        cfg = OllamaConfig()
        assert cfg.api_key == ""  # the contract: keyless by design
        client = _build_client(cfg)
        assert client.api_key != ""

    def test_thinking_models_get_enable_thinking_false(self):
        """qwen3 burns its token budget on reasoning and returns EMPTY content;
        keyless (Ollama) calls must send enable_thinking=False. NVIDIA calls
        must NOT get the extra_body (it is ignored at best)."""
        from dourmouse.dispatch import _call_with_retry
        from dourmouse.config import NvidiaConfig

        captured: dict = {}

        class _Spy:
            def __init__(self, cfg: object) -> None:
                self._cfg = cfg

            # dispatch calls client.chat.completions.create(...) — an attribute
            # chain, so chat/completions must be properties, not methods.
            @property
            def chat(self):
                return self

            @property
            def completions(self):
                return self

            def create(self, **kwargs):
                captured.update(kwargs)
                return None

        nvidia = NvidiaConfig(api_key="nvapi-x", base_url="http://x", model="m")
        _call_with_retry(_Spy(nvidia), model="m", messages=[], tools=[], config=nvidia)
        assert captured.get("extra_body") is None

        ollama = OllamaConfig()
        captured.clear()
        _call_with_retry(_Spy(ollama), model="m", messages=[], tools=[], config=ollama)
        assert captured.get("extra_body") == {"enable_thinking": False}
