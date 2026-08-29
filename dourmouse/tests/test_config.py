"""Tests for env-driven config loading (config.py)."""

from __future__ import annotations

import pytest

from dourmouse.config import (
    NvidiaConfig,
    OllamaConfig,
    OmniRouteConfig,
    backend_identity,
    llm_backend,
    load_guardrail_config,
    load_llm_config,
    load_nvidia_config,
    load_omniroute_config,
    omniroute_available,
)


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
        # Case-insensitive lookup on the agent name; env override wins even
        # over the agent's own built-in default (research_info has one —
        # see TestNvidiaAgentDefaults below — and the env value still wins).
        assert cfg.model_for_agent("research_info") == "nvidia/r1-70b"
        assert cfg.model_for_agent("RESEARCH_INFO") == "nvidia/r1-70b"
        assert cfg.model_for_agent("code_nvidia") == "nvidia/code-llama-70b"
        # "markets" has no env override AND no built-in default -> falls
        # all the way through to NVIDIA_MODEL. ("news" no longer belongs
        # here: it now has a real built-in default, world-monitor-expansion,
        # tested in TestNvidiaAgentDefaults.)
        assert cfg.model_for_agent("markets") == "nvidia/default-120b"
        assert cfg.model_for_agent("") == "nvidia/default-120b"
        assert cfg.model_for_agent(None) == "nvidia/default-120b"

    def test_no_per_agent_overrides_uses_default_for_all(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/one-model")
        monkeypatch.delenv("DOURMOUSE_MODEL_RESEARCH_INFO", raising=False)
        cfg = load_nvidia_config()
        assert cfg.agent_models == {}
        # "markets" has no built-in default either (see TestNvidiaAgentDefaults) -
        # a genuinely unmapped agent, unlike research_info/orchestrator/etc.
        assert cfg.model_for_agent("markets") == "nvidia/one-model"


# --------------------------------------------------------------------------- #
# world-monitor-expansion — real per-agent NVIDIA defaults +
# persisted orchestrator-model setting.
# --------------------------------------------------------------------------- #
class TestNvidiaAgentDefaults:
    """Every agent used to silently fall back to NVIDIA_MODEL; these pin
    the real built-in defaults now in place (config._NVIDIA_AGENT_DEFAULTS)
    and their precedence under DOURMOUSE_MODEL_<AGENT> env overrides."""

    def test_builtin_defaults_apply_without_any_override(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/one-model")
        for name in (
            "DOURMOUSE_MODEL_ORCHESTRATOR",
            "DOURMOUSE_MODEL_RESEARCH_INFO",
            "DOURMOUSE_MODEL_DEV_CODING",
            "DOURMOUSE_MODEL_COMMS",
            "DOURMOUSE_MODEL_MAIL",
            "DOURMOUSE_MODEL_NEWS",
            "DOURMOUSE_MODEL_WORLDMONITOR",
            "DOURMOUSE_ORCHESTRATOR_MODEL",
        ):
            monkeypatch.delenv(name, raising=False)
        cfg = load_nvidia_config()
        # world-monitor-expansion (systematic backend verification,
        # 2026-08-29): the two old ids here ("nvidia/llama-3.3-nemotron-
        # super-49b-v1", "nvidia/code-llama-70b") were confirmed RETIRED /
        # NEVER-REAL against a live integrate.api.nvidia.com/v1/models call
        # — see config._NVIDIA_AGENT_DEFAULTS' docstring for the full
        # cross-check and replacement reasoning.
        assert cfg.model_for_agent("orchestrator") == "nvidia/nemotron-3-nano-30b-a3b"
        assert cfg.model_for_agent("research_info") == "nvidia/llama-3.1-nemotron-ultra-253b-v1"
        assert cfg.model_for_agent("dev_coding") == "meta/codellama-70b"
        for agent in ("comms", "mail", "news", "worldmonitor", "companion"):
            assert cfg.model_for_agent(agent) == "deepseek-ai/deepseek-v4-flash-0731"
        # code_* family is NOT in the defaults dict — resolved via
        # code_backends.py instead, so it stays on the plain default here.
        assert cfg.model_for_agent("code_nvidia") == "nvidia/one-model"

    def test_env_override_wins_over_builtin_default(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("DOURMOUSE_MODEL_ORCHESTRATOR", "nvidia/custom-orchestrator")
        monkeypatch.delenv("DOURMOUSE_ORCHESTRATOR_MODEL", raising=False)
        cfg = load_nvidia_config()
        assert cfg.model_for_agent("orchestrator") == "nvidia/custom-orchestrator"


class TestOrchestratorModelSetting:
    """Persisted (not just env) orchestrator model choice — the backend
    half of the Settings UI's orchestrator-model picker. See
    config.orchestrator_model_setting / save_orchestrator_model_setting."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_nothing_saved_returns_empty(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import orchestrator_model_setting

        assert orchestrator_model_setting() == ""

    def test_save_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import orchestrator_model_setting, save_orchestrator_model_setting

        result = save_orchestrator_model_setting("nvidia/my-chosen-model")
        assert result["ok"] is True
        assert orchestrator_model_setting() == "nvidia/my-chosen-model"

    def test_save_merges_with_existing_file(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import save_orchestrator_model_setting, user_env_path

        path = user_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NVIDIA_API_KEY=nvapi-existing\n", encoding="utf-8")
        save_orchestrator_model_setting("nvidia/my-chosen-model")
        contents = path.read_text(encoding="utf-8")
        assert "NVIDIA_API_KEY=nvapi-existing" in contents
        assert "DOURMOUSE_ORCHESTRATOR_MODEL=nvidia/my-chosen-model" in contents

    def test_empty_model_rejected(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import save_orchestrator_model_setting

        result = save_orchestrator_model_setting("   ")
        assert result["ok"] is False

    def test_model_for_agent_reads_persisted_setting(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.delenv("DOURMOUSE_MODEL_ORCHESTRATOR", raising=False)
        from dourmouse.config import load_nvidia_config, save_orchestrator_model_setting

        save_orchestrator_model_setting("nvidia/persisted-choice", backend="nvidia")
        cfg = load_nvidia_config()
        assert cfg.model_for_agent("orchestrator") == "nvidia/persisted-choice"

    def test_persisted_setting_without_backend_tag_never_auto_applies(self, monkeypatch, tmp_path):
        """Real bug, found live: a bare model string with no backend tag
        used to be applied to WHATEVER backend was active at read time —
        e.g. an Ollama model id ("qwen3:8b") silently handed to NVIDIA's
        real API, which 404'd and killed the orchestrator with no visible
        error. An untagged save must never auto-apply anywhere; it falls
        through to that backend's normal default instead."""
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.delenv("DOURMOUSE_MODEL_ORCHESTRATOR", raising=False)
        from dourmouse.config import load_nvidia_config, save_orchestrator_model_setting

        save_orchestrator_model_setting("qwen3:8b")  # no backend= given
        cfg = load_nvidia_config()
        assert cfg.model_for_agent("orchestrator") != "qwen3:8b"

    def test_persisted_setting_from_a_different_backend_never_leaks_across(self, monkeypatch, tmp_path):
        """The exact cross-backend leak this whole fix exists to close:
        a model persisted FOR ollama must never be read back by nvidia's
        (or omniroute's) model_for_agent, even though all three share the
        same underlying storage file."""
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.delenv("DOURMOUSE_MODEL_ORCHESTRATOR", raising=False)
        monkeypatch.delenv("DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR", raising=False)
        from dourmouse.config import load_nvidia_config, save_orchestrator_model_setting

        save_orchestrator_model_setting("qwen3:8b", backend="ollama")
        cfg = load_nvidia_config()
        assert cfg.model_for_agent("orchestrator") != "qwen3:8b"

    def test_env_override_still_wins_over_persisted_setting(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("DOURMOUSE_MODEL_ORCHESTRATOR", "nvidia/env-wins")
        from dourmouse.config import load_nvidia_config, save_orchestrator_model_setting

        save_orchestrator_model_setting("nvidia/persisted-choice")
        cfg = load_nvidia_config()
        assert cfg.model_for_agent("orchestrator") == "nvidia/env-wins"

    def test_persisted_setting_applies_only_to_its_own_saved_backend(self, monkeypatch, tmp_path):
        """Each backend's persisted choice is independent — saving one
        does not affect, and is not affected by, the others. This is the
        corrected version of the old (buggy) "applies everywhere" test:
        that behavior is exactly the cross-backend leak this fix closes."""
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import (
            load_ollama_config,
            load_omniroute_config,
            save_orchestrator_model_setting,
        )

        monkeypatch.delenv("DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR", raising=False)
        monkeypatch.delenv("DOURMOUSE_OMNIROUTE_MODEL_ORCHESTRATOR", raising=False)

        save_orchestrator_model_setting("ollama/persisted-choice", backend="ollama")
        assert load_ollama_config().model_for_agent("orchestrator") == "ollama/persisted-choice"
        # Saving for ollama must not make omniroute pick it up too.
        assert load_omniroute_config().model_for_agent("orchestrator") != "ollama/persisted-choice"

        save_orchestrator_model_setting("omniroute/persisted-choice", backend="omniroute")
        assert load_omniroute_config().model_for_agent("orchestrator") == "omniroute/persisted-choice"
        # The later omniroute save supersedes ollama's — only the most
        # recent backend tag is trusted, matching the single-storage-slot
        # design (one persisted choice at a time, tagged with its backend).
        assert load_ollama_config().model_for_agent("orchestrator") != "ollama/persisted-choice"


class TestGroundedModeSetting:
    """Persisted (not just env), off-by-default Grounded Mode toggle — the
    backend half of the Settings UI's grounded-mode switch. See
    config.grounded_mode_enabled / save_grounded_mode_setting."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_off_by_default(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import grounded_mode_enabled

        assert grounded_mode_enabled() is False

    def test_save_true_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import grounded_mode_enabled, save_grounded_mode_setting

        result = save_grounded_mode_setting(True)
        assert result["ok"] is True
        assert grounded_mode_enabled() is True

    def test_save_false_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import grounded_mode_enabled, save_grounded_mode_setting

        save_grounded_mode_setting(True)
        save_grounded_mode_setting(False)
        assert grounded_mode_enabled() is False

    def test_save_merges_with_existing_file(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import save_grounded_mode_setting, user_env_path

        path = user_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NVIDIA_API_KEY=nvapi-existing\n", encoding="utf-8")
        save_grounded_mode_setting(True)
        contents = path.read_text(encoding="utf-8")
        assert "NVIDIA_API_KEY=nvapi-existing" in contents
        assert "DOURMOUSE_GROUNDED_MODE=1" in contents

    def test_garbage_value_in_file_reads_as_off(self, monkeypatch, tmp_path):
        """Honest degrade, matching this module's own rule elsewhere: an
        unrecognized value is never guessed true, only an explicit
        1/true/yes/on counts."""
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import grounded_mode_enabled, user_env_path

        path = user_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DOURMOUSE_GROUNDED_MODE=maybe\n", encoding="utf-8")
        assert grounded_mode_enabled() is False


# --------------------------------------------------------------------------- #
# v5.10 — OmniRoute free-tier gateway backend
# --------------------------------------------------------------------------- #
class TestOmniRouteConfig:
    def test_defaults_are_keyless_and_local(self, monkeypatch):
        monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
        monkeypatch.delenv("OMNIROUTE_MODEL", raising=False)
        cfg = load_omniroute_config()
        assert cfg.api_key == ""
        assert cfg.base_url == "http://127.0.0.1:20128/v1"
        assert cfg.model == "auto"
        assert cfg.model_for_agent("orchestrator") == "auto"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:9999/v1")
        monkeypatch.setenv("OMNIROUTE_MODEL", "auto/best-fast")
        cfg = load_omniroute_config()
        assert cfg.base_url == "http://127.0.0.1:9999/v1"
        assert cfg.model == "auto/best-fast"

    def test_per_agent_model_env(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_OMNIROUTE_MODEL_DEV_CODING", "auto/coding:free")
        cfg = load_omniroute_config()
        assert cfg.model_for_agent("dev_coding") == "auto/coding:free"
        assert cfg.model_for_agent("markets") == "auto"  # no override

    def test_omniroute_available_probes_gateway(self, monkeypatch):
        # Point the probe at a dead port so the test is hermetic regardless
        # of whether the real gateway is running on this machine.
        monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:1/v1")
        assert omniroute_available(timeout=0.3) is False

    def test_llm_backend_accepts_omniroute(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "omniroute")
        assert llm_backend() == "omniroute"

    def test_llm_backend_rejects_unknown(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "mystery")
        with pytest.raises(ValueError, match="DOURMOUSE_LLM_BACKEND"):
            llm_backend()

    def test_load_llm_config_returns_omniroute_config(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "omniroute")
        cfg = load_llm_config()
        assert isinstance(cfg, OmniRouteConfig)
        assert cfg.api_key == ""

    def test_load_llm_config_explicit_ollama_wins_over_auto(self, monkeypatch):
        """Explicit backend selection is honored even when another probe
        would answer — deterministic (Rule 2.8)."""
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")
        cfg = load_llm_config()
        assert isinstance(cfg, OllamaConfig)

    def test_load_llm_config_auto_ollama_first(self, monkeypatch):
        """auto: local Ollama wins when it answers; never a network guess."""
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "auto")
        monkeypatch.setattr(
            "dourmouse.config.ollama_available", lambda **_: True
        )
        cfg = load_llm_config()
        assert isinstance(cfg, OllamaConfig)

    def test_load_llm_config_auto_omniroute_second(self, monkeypatch):
        """auto: no Ollama -> free OmniRoute gateway when it answers AND the
        user opted in (DOURMOUSE_OMNIROUTE_AUTO=1, v5.10 privacy gate)."""
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "auto")
        monkeypatch.setenv("DOURMOUSE_OMNIROUTE_AUTO", "1")
        monkeypatch.setattr(
            "dourmouse.config.ollama_available", lambda **_: False
        )
        monkeypatch.setattr(
            "dourmouse.config.omniroute_available", lambda **_: True
        )
        cfg = load_llm_config()
        assert isinstance(cfg, OmniRouteConfig)

    def test_load_llm_config_auto_skips_omniroute_without_optin(self, monkeypatch):
        """auto: the third-party gateway is NEVER chosen implicitly — even
        when it answers, without DOURMOUSE_OMNIROUTE_AUTO=1 the chain goes
        Ollama -> NVIDIA (Rule 2.6 local-first privacy)."""
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "auto")
        monkeypatch.delenv("DOURMOUSE_OMNIROUTE_AUTO", raising=False)
        monkeypatch.setattr(
            "dourmouse.config.ollama_available", lambda **_: False
        )
        monkeypatch.setattr(
            "dourmouse.config.omniroute_available", lambda **_: True
        )
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        cfg = load_llm_config()
        assert isinstance(cfg, NvidiaConfig)

    def test_load_llm_config_auto_falls_back_to_nvidia(self, monkeypatch):
        """auto: neither local backend -> NVIDIA (needs its key, honestly)."""
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "auto")
        monkeypatch.setattr(
            "dourmouse.config.ollama_available", lambda **_: False
        )
        monkeypatch.setattr(
            "dourmouse.config.omniroute_available", lambda **_: False
        )
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        cfg = load_llm_config()
        assert isinstance(cfg, NvidiaConfig)


class TestBackendIdentity:
    """world-monitor-expansion (UX pass item 1): the console's per-response
    model/local indicator classifies by the config object's real TYPE —
    never guessed from a model-name string — so these pin the contract
    backend_identity() promises."""

    def test_ollama_is_local(self):
        assert backend_identity(OllamaConfig()) == ("ollama", True)

    def test_nvidia_is_cloud(self):
        cfg = NvidiaConfig(api_key="k", base_url="https://integrate.api.nvidia.com/v1", model="m")
        assert backend_identity(cfg) == ("nvidia", False)

    def test_omniroute_is_cloud_despite_localhost_gateway(self):
        """OmniRoute's gateway process listens on 127.0.0.1, but it exists
        to forward requests to REMOTE free-tier providers — it must not be
        misclassified as local just because its own base_url looks local."""
        cfg = OmniRouteConfig()
        assert "127.0.0.1" in cfg.base_url  # the gateway really is local...
        assert backend_identity(cfg) == ("omniroute", False)  # ...but generation isn't

    def test_none_config_is_honestly_unknown(self):
        """A caller with no config attached (mostly tests / a bare client)
        reports unknown rather than guessing local or cloud."""
        assert backend_identity(None) == ("unknown", False)

    def test_unrecognized_object_is_honestly_unknown(self):
        assert backend_identity(object()) == ("unknown", False)
