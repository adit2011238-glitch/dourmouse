"""dourmouse/backend_fallback.py -- pre-flight reachability fallback
(load_llm_config_with_fallback) plus the mid-call pool-exhaustion path
(probe_ollama_fallback) that closes the gap: the pre-flight probe only ever
runs once, at config-load, and never reacts to a backend that answered fine
at startup and then rate-limited every account mid-conversation.
"""

from __future__ import annotations

import urllib.error

from dourmouse import backend_fallback


class _Resp:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reachable(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(200))


def _unreachable(monkeypatch):
    def _raise(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)


class TestProbeBackend:
    def test_reachable_returns_true(self, monkeypatch):
        _reachable(monkeypatch)
        assert backend_fallback._probe_backend("https://x") is True

    def test_unreachable_returns_false(self, monkeypatch):
        _unreachable(monkeypatch)
        assert backend_fallback._probe_backend("https://x") is False

    def test_404_still_counts_as_reachable(self, monkeypatch):
        # the endpoint answered (it's up), it just doesn't like this route --
        # that's a config problem, not a "switch backends" problem. Per
        # _probe_backend's own range check (200 <= status < 500), only a
        # 5xx (the server itself failing) counts as unreachable.
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(404))
        assert backend_fallback._probe_backend("https://x") is True

    def test_5xx_counts_as_unreachable(self, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(500))
        assert backend_fallback._probe_backend("https://x") is False


class TestLoadLlmConfigWithFallback:
    """Existing reachability-probe path, config-load time only."""

    def test_disabled_flag_skips_probing_entirely(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_FALLBACK_DISABLED", "1")
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")
        monkeypatch.setenv("NVIDIA_API_KEY", "key1")

        def _boom(*a, **k):  # pragma: no cover - must never be called
            raise AssertionError("probe ran despite DOURMOUSE_FALLBACK_DISABLED=1")

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        cfg = backend_fallback.load_llm_config_with_fallback()
        assert cfg.base_url != "http://127.0.0.1:11434"

    def test_nvidia_reachable_stays_on_nvidia(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")
        monkeypatch.setenv("NVIDIA_API_KEY", "key1")
        _reachable(monkeypatch)
        cfg = backend_fallback.load_llm_config_with_fallback()
        assert cfg.base_url != "http://127.0.0.1:11434"

    def test_nvidia_unreachable_falls_back_to_local_ollama(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")
        monkeypatch.setenv("NVIDIA_API_KEY", "key1")
        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
        _unreachable(monkeypatch)
        cfg = backend_fallback.load_llm_config_with_fallback()
        assert cfg.base_url == "http://127.0.0.1:11434"
        assert cfg.model == "phi:2b"

    def test_omniroute_is_unaffected(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "omniroute")
        _unreachable(monkeypatch)  # must not even be consulted
        cfg = backend_fallback.load_llm_config_with_fallback()
        assert cfg.base_url != "http://127.0.0.1:11434"


class TestProbeOllamaFallback:
    """The NEW mid-call path: dispatch.py's _nvidia_rotation_factory calls
    this when model_router.pool_exhausted() says every NVIDIA account is
    cooling down mid-conversation."""

    def test_disabled_flag_returns_none_without_probing(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_FALLBACK_DISABLED", "1")

        def _boom(*a, **k):  # pragma: no cover - must never be called
            raise AssertionError("probe ran despite DOURMOUSE_FALLBACK_DISABLED=1")

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        assert backend_fallback.probe_ollama_fallback() is None

    def test_ollama_reachable_returns_its_config(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
        _reachable(monkeypatch)
        cfg = backend_fallback.probe_ollama_fallback()
        assert cfg is not None
        assert cfg.base_url == "http://127.0.0.1:11434"
        assert cfg.model == "phi:2b"

    def test_honors_ollama_model_env_override(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
        _reachable(monkeypatch)
        cfg = backend_fallback.probe_ollama_fallback()
        assert cfg is not None
        assert cfg.model == "qwen3:8b"

    def test_ollama_unreachable_returns_none(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
        _unreachable(monkeypatch)
        assert backend_fallback.probe_ollama_fallback() is None

    def test_does_not_construct_a_client(self, monkeypatch):
        """Deliberate: client construction stays dispatch.py's job
        (_build_client/OllamaNativeClient) so this module never needs to
        import dispatch, which already imports this module."""
        monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
        _reachable(monkeypatch)
        cfg = backend_fallback.probe_ollama_fallback()
        from dourmouse.config import OllamaConfig

        assert isinstance(cfg, OllamaConfig)
