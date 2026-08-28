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
import json
import threading
import time
from typing import Any

import pytest

import dourmouse.dispatch as dispatch_module

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
        # world-monitor-expansion (systematic backend verification): was
        # "qwen3:8b" — never pulled on the real dev machine, same bug class
        # as DOURMOUSE_FAST_MODEL's old qwen3:4b default (model_check.py).
        # config._OLLAMA_DEFAULT_MODEL is now "qwen2.5:7b" (confirmed
        # installed + live-verified); pinning the literal here on purpose,
        # same as before, so a future silent default drift still fails loud.
        assert cfg.model == "qwen2.5:7b"
        assert cfg.model_for_agent("news") == "qwen2.5:7b"
        assert cfg.model_for_agent(None) == "qwen2.5:7b"
        assert cfg.model_for_agent("") == "qwen2.5:7b"

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

    def test_auto_falls_back_to_nvidia_when_no_local_backend(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "auto")
        monkeypatch.setattr(config_module, "ollama_available", lambda: False)
        # v5.10: the auto chain is Ollama -> OmniRoute -> NVIDIA. The probe
        # is mocked so the test stays hermetic (Rule 2.8 — no network).
        monkeypatch.setattr(config_module, "omniroute_available", lambda: False)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        from dourmouse.config import NvidiaConfig

        cfg = load_llm_config()
        assert isinstance(cfg, NvidiaConfig)
        assert cfg.api_key == "nvapi-test"

    def test_auto_prefers_omniroute_when_no_ollama(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "auto")
        # v5.10 privacy gate: the third-party gateway needs the explicit
        # opt-in even in auto mode (Rule 2.6 local-first).
        monkeypatch.setenv("DOURMOUSE_OMNIROUTE_AUTO", "1")
        monkeypatch.setattr(config_module, "ollama_available", lambda: False)
        monkeypatch.setattr(config_module, "omniroute_available", lambda: True)
        from dourmouse.config import OmniRouteConfig

        cfg = load_llm_config()
        assert isinstance(cfg, OmniRouteConfig)
        assert cfg.api_key == ""  # keyless — the free-tier guarantee

    def test_auto_never_uses_omniroute_without_optin(self, monkeypatch):
        """auto with the gateway up but NO opt-in must fall through to
        NVIDIA — prompts never leave for a third-party implicitly."""
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "auto")
        monkeypatch.delenv("DOURMOUSE_OMNIROUTE_AUTO", raising=False)
        monkeypatch.setattr(config_module, "ollama_available", lambda: False)
        monkeypatch.setattr(config_module, "omniroute_available", lambda: True)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        from dourmouse.config import NvidiaConfig

        cfg = load_llm_config()
        assert isinstance(cfg, NvidiaConfig)

    def test_explicit_omniroute_returns_omniroute_config(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "omniroute")
        monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128/v1")
        cfg = load_llm_config()
        from dourmouse.config import OmniRouteConfig

        assert isinstance(cfg, OmniRouteConfig)
        assert cfg.base_url == "http://127.0.0.1:20128/v1"
        assert cfg.model == "auto"

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

        # dispatch now calls load_llm_config_with_fallback (which calls the real
        # load_llm_config internally), so mock the fallback function instead.
        monkeypatch.setattr(dispatch, "load_llm_config_with_fallback", _fake_resolver)
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

    def test_ollama_uses_native_client_with_speed_fixes(self):
        """The local path must NOT use the OpenAI-compat endpoint (this Ollama
        build ignores think/enable_thinking there — measured live: 57-73s
        thinking traces per answer). It uses the native adapter instead, which
        disables thinking, keeps the model warm, and raises the context
        window past the 4096 truncation default."""
        from dourmouse.config import NvidiaConfig
        from dourmouse.dispatch import OllamaNativeClient, _build_client

        # NVIDIA keeps the OpenAI SDK, with the keyless sentinel.
        nvidia = NvidiaConfig(api_key="nvapi-x", base_url="http://x", model="m")
        assert isinstance(_build_client(nvidia), dispatch_module.OpenAI)

        # Ollama gets the native adapter — no API key at all.
        ollama = OllamaConfig()
        assert ollama.api_key == ""  # the contract: keyless by design
        client = _build_client(ollama)
        assert isinstance(client, OllamaNativeClient)

        captured: dict = {}

        def fake_post(payload):
            captured.update(payload)
            return json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True})

        native = OllamaNativeClient(ollama, _post=fake_post)
        native.chat.completions.create(
            model="qwen3:8b", messages=[{"role": "user", "content": "hi"}], stream=False
        )
        assert captured["think"] is False
        assert captured["enable_thinking"] is False
        assert captured["keep_alive"] == dispatch_module._OLLAMA_KEEP_ALIVE
        assert captured["options"]["num_ctx"] == dispatch_module._OLLAMA_NUM_CTX
        assert captured["options"]["num_predict"] == dispatch_module._DEFAULT_MAX_TOKENS
