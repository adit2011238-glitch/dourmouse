"""Tests for env-driven config loading (config.py)."""

from __future__ import annotations

from pathlib import Path

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
    workspace_dir,
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

    def test_per_agent_key_env_is_scanned(self, monkeypatch):
        """2026-09-14, user-directed: "a different api key for each agent
        since claude code is supposed to be orchestrating not doing the
        work." Same shape as per-agent models, one level down."""
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-default-key")
        monkeypatch.setenv("DOURMOUSE_API_KEY_CODE_NVIDIA", "nvapi-code-key")
        monkeypatch.setenv("DOURMOUSE_API_KEY_RESEARCH_INFO", "nvapi-research-key")
        monkeypatch.delenv("DOURMOUSE_API_KEY_MARKETS", raising=False)
        cfg = load_nvidia_config()
        assert cfg.agent_keys == {
            "CODE_NVIDIA": "nvapi-code-key",
            "RESEARCH_INFO": "nvapi-research-key",
        }
        # Case-insensitive lookup, same as key_for_agent's sibling.
        assert cfg.key_for_agent("code_nvidia") == "nvapi-code-key"
        assert cfg.key_for_agent("CODE_NVIDIA") == "nvapi-code-key"
        assert cfg.key_for_agent("research_info") == "nvapi-research-key"
        # An agent with no override falls back to the run's default key.
        assert cfg.key_for_agent("markets") == "nvapi-default-key"
        assert cfg.key_for_agent("") == "nvapi-default-key"
        assert cfg.key_for_agent(None) == "nvapi-default-key"

    def test_no_per_agent_key_overrides_uses_default_for_all(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-only-key")
        monkeypatch.delenv("DOURMOUSE_API_KEY_CODE_NVIDIA", raising=False)
        cfg = load_nvidia_config()
        assert cfg.agent_keys == {}
        assert cfg.key_for_agent("code_nvidia") == "nvapi-only-key"

    def test_per_agent_keys_never_leak_into_per_agent_models_or_vice_versa(self, monkeypatch):
        """A real, live-relevant guard: DOURMOUSE_MODEL_<AGENT> and
        DOURMOUSE_API_KEY_<AGENT> share the same agent-name suffix
        convention but must never be confused with each other -- an agent
        with a model override and no key override keeps the default key,
        and vice versa."""
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-default-key")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/default-model")
        monkeypatch.setenv("DOURMOUSE_MODEL_CODE_NVIDIA", "nvidia/special-model")
        monkeypatch.delenv("DOURMOUSE_API_KEY_CODE_NVIDIA", raising=False)
        cfg = load_nvidia_config()
        assert cfg.model_for_agent("code_nvidia") == "nvidia/special-model"
        assert cfg.key_for_agent("code_nvidia") == "nvapi-default-key"


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


class TestSkipPersistedOrchestratorChoice:
    """Real bug, found live this session: force_local=True was built to
    make a config fully local (is_cloud=False, api_key="", no cloud base
    URL) — but model_for_agent("orchestrator") still unconditionally
    honored a persisted orchestrator-model choice regardless of that
    flag. On this machine the persisted choice was "gpt-oss:20b" (a
    cloud-only model, saved back when Ollama Cloud was the active
    backend) — so a force_local config still resolved to a model that
    does not exist on the local daemon, and every "apps"/"mail"/etc.
    call (privacy-pinned to force_local per _LOCAL_ONLY_AGENTS) 404'd.
    Confirmed live via a real GET to http://127.0.0.1:11434/api/tags:
    "gpt-oss:20b" is not among the locally-pulled models.

    skip_persisted_orchestrator_choice (set by load_ollama_config's
    force_local=True) closes this: a force_local config skips the
    persisted-choice lookup entirely and falls through to
    _OLLAMA_FAST_DISPATCH's local pin ("qwen2.5:7b", confirmed present
    locally) instead."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_force_local_skips_persisted_choice_and_uses_fast_dispatch_pin(
        self, monkeypatch, tmp_path
    ):
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.delenv("DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR", raising=False)
        from dourmouse.config import load_ollama_config, save_orchestrator_model_setting

        # Simulate this machine's real persisted state: a cloud-only
        # model saved while Ollama Cloud was active.
        save_orchestrator_model_setting("gpt-oss:20b", backend="ollama")

        cfg = load_ollama_config(force_local=True)
        assert cfg.skip_persisted_orchestrator_choice is True
        assert cfg.model_for_agent("orchestrator") == "qwen2.5:7b"
        assert cfg.model_for_agent("orchestrator") != "gpt-oss:20b"

    def test_non_force_local_still_honors_persisted_choice_unaffected(
        self, monkeypatch, tmp_path
    ):
        """The fix must be scoped to force_local only — normal (cloud)
        configs keep today's existing, working behavior exactly."""
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.delenv("DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR", raising=False)
        from dourmouse.config import load_ollama_config, save_orchestrator_model_setting

        save_orchestrator_model_setting("gpt-oss:20b", backend="ollama")

        cfg = load_ollama_config(force_local=False)
        assert cfg.skip_persisted_orchestrator_choice is False
        assert cfg.model_for_agent("orchestrator") == "gpt-oss:20b"

    def test_plain_construction_defaults_flag_to_false(self):
        """A bare OllamaConfig() (no force_local involved at all) must
        default to the old, unconditional-honor behavior — this flag is
        opt-in, never a silent behavior change for existing callers."""
        assert OllamaConfig().skip_persisted_orchestrator_choice is False

    def test_flag_does_not_affect_non_orchestrator_agents(self, monkeypatch, tmp_path):
        """skip_persisted_orchestrator_choice only ever gates the
        persisted-setting lookup, which only ever applies to the
        "orchestrator" key — a non-orchestrator agent's resolution path
        (agent_models override, then the fast-dispatch pin, then the
        plain default) must be identical either way."""
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.delenv("DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR", raising=False)
        from dourmouse.config import load_ollama_config, save_orchestrator_model_setting

        save_orchestrator_model_setting("gpt-oss:20b", backend="ollama")

        local_cfg = load_ollama_config(force_local=True)
        cloud_cfg = load_ollama_config(force_local=False)
        # "apps" has no dedicated fast-dispatch entry, so both fall
        # through to the plain per-config default model either way.
        assert local_cfg.model_for_agent("apps") == cloud_cfg.model_for_agent("apps")


class TestClaudeFrontModeSetting:
    """Persisted, ON-by-default Claude-front-mode toggle (the mirror image
    of Grounded Mode below — opt-OUT, not opt-in, per the user's explicit
    ask that Claude-front be the default). See
    config.claude_front_mode_enabled / save_claude_front_mode_setting."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_on_by_default(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import claude_front_mode_enabled

        assert claude_front_mode_enabled() is True

    def test_save_false_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import claude_front_mode_enabled, save_claude_front_mode_setting

        result = save_claude_front_mode_setting(False)
        assert result["ok"] is True
        assert claude_front_mode_enabled() is False

    def test_save_true_after_false_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import claude_front_mode_enabled, save_claude_front_mode_setting

        save_claude_front_mode_setting(False)
        save_claude_front_mode_setting(True)
        assert claude_front_mode_enabled() is True

    def test_save_merges_with_existing_file(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import save_claude_front_mode_setting, user_env_path

        path = user_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NVIDIA_API_KEY=nvapi-existing\n", encoding="utf-8")
        save_claude_front_mode_setting(False)
        contents = path.read_text(encoding="utf-8")
        assert "NVIDIA_API_KEY=nvapi-existing" in contents
        assert "DOURMOUSE_CLAUDE_FRONT_MODE=off" in contents

    def test_key_is_distinct_from_orchestrator_backend_setting_key(self):
        """Real bug this guards against: this key must NEVER collide with
        ORCHESTRATOR_BACKEND_SETTING_KEY (an unrelated, already-shipped
        setting — "which backend a persisted orchestrator MODEL belongs
        to" — that a user picking e.g. "ollama" would have set to a value
        this feature doesn't recognize)."""
        from dourmouse.config import CLAUDE_FRONT_MODE_SETTING_KEY, ORCHESTRATOR_BACKEND_SETTING_KEY

        assert CLAUDE_FRONT_MODE_SETTING_KEY != ORCHESTRATOR_BACKEND_SETTING_KEY


class TestGoogleOAuthFullScopesSetting:
    """Real friction, live-caught (2026-09-13): turning on Gmail/Calendar/
    Drive via real Google OAuth scopes required manually editing .env
    (GOOGLE_OAUTH_FULL_SCOPES=1) and restarting the server — undiscoverable
    unless you already knew the env var existed. Same real, persisted,
    no-restart-needed toggle shape as Claude-front-mode above, but OFF by
    default (opt-in — Google's restricted scopes 500 on an unverified
    OAuth app). See config.google_oauth_full_scopes_enabled /
    save_google_oauth_full_scopes_setting."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )
        monkeypatch.delenv("GOOGLE_OAUTH_FULL_SCOPES", raising=False)

    def test_off_by_default(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import google_oauth_full_scopes_enabled

        assert google_oauth_full_scopes_enabled() is False

    def test_save_true_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import google_oauth_full_scopes_enabled, save_google_oauth_full_scopes_setting

        result = save_google_oauth_full_scopes_setting(True)
        assert result["ok"] is True
        assert google_oauth_full_scopes_enabled() is True

    def test_save_false_after_true_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import google_oauth_full_scopes_enabled, save_google_oauth_full_scopes_setting

        save_google_oauth_full_scopes_setting(True)
        save_google_oauth_full_scopes_setting(False)
        assert google_oauth_full_scopes_enabled() is False

    def test_a_real_env_var_wins_even_when_the_persisted_setting_is_off(self, monkeypatch, tmp_path):
        """An operator who already set the raw env var directly in their
        own real .env must keep working exactly as before this toggle
        existed — the new Settings switch is additive, never a
        regression for the existing manual path."""
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import google_oauth_full_scopes_enabled, save_google_oauth_full_scopes_setting

        save_google_oauth_full_scopes_setting(False)
        monkeypatch.setenv("GOOGLE_OAUTH_FULL_SCOPES", "1")
        assert google_oauth_full_scopes_enabled() is True

    def test_save_merges_with_existing_file(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import save_google_oauth_full_scopes_setting, user_env_path

        path = user_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NVIDIA_API_KEY=nvapi-existing\n", encoding="utf-8")
        save_google_oauth_full_scopes_setting(True)
        contents = path.read_text(encoding="utf-8")
        assert "NVIDIA_API_KEY=nvapi-existing" in contents
        assert "GOOGLE_OAUTH_FULL_SCOPES=on" in contents

    def test_key_is_distinct_from_claude_front_mode_setting_key(self):
        from dourmouse.config import CLAUDE_FRONT_MODE_SETTING_KEY, GOOGLE_OAUTH_FULL_SCOPES_SETTING_KEY

        assert GOOGLE_OAUTH_FULL_SCOPES_SETTING_KEY != CLAUDE_FRONT_MODE_SETTING_KEY

    def test_google_auth_requested_scopes_reflects_the_toggle(self, monkeypatch, tmp_path):
        """The real point of this setting: google_auth.requested_scopes()
        must actually change when it's flipped, not just report a status
        nobody reads."""
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import save_google_oauth_full_scopes_setting
        from dourmouse.google_auth import requested_scopes

        assert "gmail" not in requested_scopes()
        save_google_oauth_full_scopes_setting(True)
        assert "gmail" in requested_scopes()
        assert "calendar" in requested_scopes()
        assert "drive" in requested_scopes()


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


class TestAutoApproveSetting:
    """Persisted, off-by-default "skip confirmations" toggle (2026-09-14,
    live-caught: "approval keeps failing, remove the need for approval,
    make this a toggle in settings"). Same shape as TestGroundedModeSetting
    above, byte for byte — see config.auto_approve_enabled /
    save_auto_approve_setting."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_off_by_default(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import auto_approve_enabled

        assert auto_approve_enabled() is False

    def test_save_true_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import auto_approve_enabled, save_auto_approve_setting

        result = save_auto_approve_setting(True)
        assert result["ok"] is True
        assert auto_approve_enabled() is True

    def test_save_false_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import auto_approve_enabled, save_auto_approve_setting

        save_auto_approve_setting(True)
        save_auto_approve_setting(False)
        assert auto_approve_enabled() is False

    def test_garbage_value_in_file_reads_as_off(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import auto_approve_enabled, user_env_path

        path = user_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DOURMOUSE_AUTO_APPROVE=maybe\n", encoding="utf-8")
        assert auto_approve_enabled() is False


class TestAppControlDryRunSetting:
    """v14 (user-directed, 2026-09-08): "Consider adding a 'dry run'
    mode where it shows what would be clicked without actually
    clicking." Persisted (not just env), OFF-by-default toggle — same
    exact shape as TestGroundedModeSetting above. See
    config.app_control_dry_run_enabled / save_app_control_dry_run_setting."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_off_by_default(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import app_control_dry_run_enabled

        assert app_control_dry_run_enabled() is False

    def test_save_true_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import app_control_dry_run_enabled, save_app_control_dry_run_setting

        result = save_app_control_dry_run_setting(True)
        assert result["ok"] is True
        assert app_control_dry_run_enabled() is True

    def test_save_false_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import app_control_dry_run_enabled, save_app_control_dry_run_setting

        save_app_control_dry_run_setting(True)
        save_app_control_dry_run_setting(False)
        assert app_control_dry_run_enabled() is False

    def test_save_merges_with_existing_file(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import save_app_control_dry_run_setting, user_env_path

        path = user_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NVIDIA_API_KEY=nvapi-existing\n", encoding="utf-8")
        save_app_control_dry_run_setting(True)
        contents = path.read_text(encoding="utf-8")
        assert "NVIDIA_API_KEY=nvapi-existing" in contents
        assert "DOURMOUSE_APP_CONTROL_DRY_RUN=1" in contents

    def test_key_is_distinct_from_grounded_mode(self):
        """Real, easy mistake this guards against: copy-pasting
        GROUNDED_MODE_SETTING_KEY's pattern without changing the env var
        name would silently make the two toggles control each other."""
        from dourmouse.config import (
            APP_CONTROL_DRY_RUN_SETTING_KEY,
            GROUNDED_MODE_SETTING_KEY,
        )

        assert APP_CONTROL_DRY_RUN_SETTING_KEY != GROUNDED_MODE_SETTING_KEY


class TestByokApiKeySetting:
    """v14 (user-directed, 2026-09-12): "commercial, for other people to
    use" — BYOK (bring your own key) Settings backend, replacing "open
    .env in a text editor" with a real save/clear path. One generic
    pair of functions over an explicit allowlist (BYOK_API_KEY_NAMES),
    not one hand-copied function per key name."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_unset_key_reads_as_empty(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import api_key_setting

        assert api_key_setting("OLLAMA_API_KEY") == ""

    def test_save_then_read_round_trips(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import api_key_setting, save_api_key_setting

        result = save_api_key_setting("OLLAMA_API_KEY", "real-test-key-123")
        assert result["ok"] is True
        assert result["configured"] is True
        assert api_key_setting("OLLAMA_API_KEY") == "real-test-key-123"

    def test_saving_an_empty_value_clears_the_key(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import api_key_setting, save_api_key_setting

        save_api_key_setting("GEMINI_API_KEY", "some-key")
        result = save_api_key_setting("GEMINI_API_KEY", "")
        assert result["ok"] is True
        assert result["configured"] is False
        assert api_key_setting("GEMINI_API_KEY") == ""

    def test_the_two_keys_are_independent(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import api_key_setting, save_api_key_setting

        save_api_key_setting("OLLAMA_API_KEY", "ollama-value")
        save_api_key_setting("GEMINI_API_KEY", "gemini-value")
        assert api_key_setting("OLLAMA_API_KEY") == "ollama-value"
        assert api_key_setting("GEMINI_API_KEY") == "gemini-value"

    def test_save_merges_with_existing_file(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import save_api_key_setting, user_env_path

        path = user_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DOURMOUSE_GROUNDED_MODE=1\n", encoding="utf-8")
        save_api_key_setting("OLLAMA_API_KEY", "real-key")
        contents = path.read_text(encoding="utf-8")
        assert "DOURMOUSE_GROUNDED_MODE=1" in contents
        assert "OLLAMA_API_KEY=real-key" in contents

    def test_a_name_outside_the_allowlist_is_refused(self, monkeypatch, tmp_path):
        """Rule 2.8: never write an arbitrary env-var name a request
        happens to name — real, deliberate scoping, not an oversight."""
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import save_api_key_setting, user_env_path

        result = save_api_key_setting("NVIDIA_API_KEY", "sneaky")
        assert result["ok"] is False
        path = user_env_path()
        assert not path.exists() or "NVIDIA_API_KEY" not in path.read_text(encoding="utf-8")

    def test_an_unknown_name_reads_as_empty_not_an_error(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.config import api_key_setting

        assert api_key_setting("SOME_RANDOM_ENV_VAR") == ""


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

    def test_ollama_cloud_is_not_local(self):
        """Real bug found live-testing this session: every OllamaConfig
        used to report local=True unconditionally, including a real
        Ollama Cloud config (OLLAMA_API_KEY set, base_url=ollama.com,
        is_cloud=True) — confirmed live via the server's own /api/backend
        endpoint reporting base_url="https://ollama.com/v1" while the
        brain event claimed local:true for the same request. is_cloud
        already existed on OllamaConfig specifically to answer this; it
        was simply never checked."""
        cfg = OllamaConfig(
            api_key="real-key", base_url="https://ollama.com/v1",
            model="gpt-oss:20b", is_cloud=True,
        )
        assert backend_identity(cfg) == ("ollama", False)

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


class TestWorkspaceDir:
    """workspace_dir() is the single source of truth this test guards
    against re-drifting: google_auth.default_auth_store, chat.
    _default_sessions_dir, desktop._webview_storage_path, general_roster.
    _workspace_root and design_3d_ops._manifest_path all resolve the
    workspace root through this one function now (previously five
    independent reimplementations of the same env-fallback logic)."""

    def test_env_var_wins_and_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "custom_ws"))
        assert workspace_dir() == tmp_path / "custom_ws"

    def test_env_var_supports_tilde_expansion(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", "~/dourmouse_ws_test_marker")
        assert workspace_dir() == Path.home() / "dourmouse_ws_test_marker"

    def test_blank_env_var_falls_back_to_project_workspace(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", "   ")
        result = workspace_dir()
        assert result.name == "workspace"
        assert result.parent == Path(__file__).resolve().parent.parent.parent

    def test_unset_env_var_falls_back_to_project_workspace(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_WORKSPACE", raising=False)
        result = workspace_dir()
        assert result.name == "workspace"
        # <project_root>/workspace, i.e. two levels above dourmouse/config.py
        assert result.parent == Path(__file__).resolve().parent.parent.parent

    def test_does_not_create_the_directory(self, monkeypatch, tmp_path):
        target = tmp_path / "not_yet_created"
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(target))
        result = workspace_dir()
        assert result == target
        assert not target.exists()

    def test_five_call_sites_share_this_resolution(self, monkeypatch, tmp_path):
        """Regression guard for the path-drift bug: AuthStore's db path and
        every other workspace-rooted path must come from the SAME root."""
        ws = tmp_path / "shared_ws"
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))

        from dourmouse.chat import _default_sessions_dir
        from dourmouse.desktop import _webview_storage_path
        from dourmouse.general_roster import _workspace_root
        from dourmouse.google_auth import default_auth_store

        assert _default_sessions_dir().parent == ws
        assert _webview_storage_path().parent == ws
        assert _workspace_root() == ws
        assert default_auth_store().path.parent.parent == ws
