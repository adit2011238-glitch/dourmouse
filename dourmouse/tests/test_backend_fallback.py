"""dourmouse/backend_fallback.py -- pre-flight reachability fallback
(load_llm_config_with_fallback). The mid-call local fallback (probe_ollama_fallback) was removed by FIX-P
H-PRIV-5: cloud models only.
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

    def test_nvidia_unreachable_does_not_fall_back_to_a_local_model(self, monkeypatch):
        """FIX-P H-PRIV-5: cloud models only. The old test pinned a silent switch to local Ollama (phi:2b)."""
        monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")
        monkeypatch.setenv("NVIDIA_API_KEY", "key1")
        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
        _unreachable(monkeypatch)
        cfg = backend_fallback.load_llm_config_with_fallback()
        assert "127.0.0.1" not in cfg.base_url and "11434" not in cfg.base_url
        assert not hasattr(backend_fallback, "probe_ollama_fallback")

    def test_omniroute_is_unaffected(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "omniroute")
        _unreachable(monkeypatch)  # must not even be consulted
        cfg = backend_fallback.load_llm_config_with_fallback()
        assert cfg.base_url != "http://127.0.0.1:11434"
