"""Tests for env-driven config loading (config.py)."""

from __future__ import annotations

import pytest

from dourmouse.config import load_guardrail_config, load_nvidia_config


class TestConfigLoading:
    def test_defaults_when_env_absent(self, monkeypatch):
        for name in (
            "DOURMOUSE_MAX_POSITION_PCT",
            "DOURMOUSE_MAX_SECTOR_PCT",
            "DOURMOUSE_DAILY_LOSS_LIMIT_PCT",
            "DOURMOUSE_TRADE_CONFIRM_USD",
        ):
            monkeypatch.delenv(name, raising=False)
        cfg = load_guardrail_config()
        assert cfg.max_position_pct == 0.10
        assert cfg.max_sector_concentration_pct == 0.30
        assert cfg.daily_loss_limit_pct == 0.03
        assert cfg.trade_confirmation_threshold_usd == 1000.0

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MAX_POSITION_PCT", "0.05")
        monkeypatch.setenv("DOURMOUSE_TRADE_CONFIRM_USD", "500")
        cfg = load_guardrail_config()
        assert cfg.max_position_pct == 0.05
        assert cfg.trade_confirmation_threshold_usd == 500.0

    def test_invalid_float_raises(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MAX_POSITION_PCT", "not-a-number")
        with pytest.raises(ValueError):
            load_guardrail_config()

    def test_out_of_range_env_rejected_by_config(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MAX_POSITION_PCT", "2.0")
        with pytest.raises(ValueError):
            load_guardrail_config()


class TestNvidiaConfigLoading:
    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        with pytest.raises(ValueError, match="NVIDIA_API_KEY is not set"):
            load_nvidia_config()

    def test_present_key_uses_defaults_for_rest(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.delenv("NVIDIA_BASE_URL", raising=False)
        monkeypatch.delenv("NVIDIA_MODEL", raising=False)
        cfg = load_nvidia_config()
        assert cfg.api_key == "nvapi-fake-test-key"
        assert cfg.base_url == "https://integrate.api.nvidia.com/v1"
        assert cfg.model == "nvidia/nemotron-3-super-120b-a12b"

    def test_env_overrides_base_url_and_model(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("NVIDIA_BASE_URL", "https://example.test/v1")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1")
        cfg = load_nvidia_config()
        assert cfg.base_url == "https://example.test/v1"
        assert cfg.model == "nvidia/llama-3.3-nemotron-super-49b-v1"

    def test_per_agent_model_env_is_scanned(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/default-120b")
        monkeypatch.setenv("DOURMOUSE_MODEL_RESEARCH_INFO", "nvidia/r1-70b")
        monkeypatch.setenv("DOURMOUSE_MODEL_CODE_NVIDIA", "nvidia/code-llama-70b")
        monkeypatch.delenv("DOURMOUSE_MODEL_NEWS", raising=False)
        cfg = load_nvidia_config()
        assert cfg.agent_models == {
            "RESEARCH_INFO": "nvidia/r1-70b",
            "CODE_NVIDIA": "nvidia/code-llama-70b",
        }
        # Case-insensitive lookup on the agent name; unknown falls to default.
        assert cfg.model_for_agent("research_info") == "nvidia/r1-70b"
        assert cfg.model_for_agent("RESEARCH_INFO") == "nvidia/r1-70b"
        assert cfg.model_for_agent("code_nvidia") == "nvidia/code-llama-70b"
        assert cfg.model_for_agent("news") == "nvidia/default-120b"
        assert cfg.model_for_agent("") == "nvidia/default-120b"
        assert cfg.model_for_agent(None) == "nvidia/default-120b"

    def test_no_per_agent_overrides_uses_default_for_all(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/one-model")
        monkeypatch.delenv("DOURMOUSE_MODEL_RESEARCH_INFO", raising=False)
        cfg = load_nvidia_config()
        assert cfg.agent_models == {}
        assert cfg.model_for_agent("markets") == "nvidia/one-model"
