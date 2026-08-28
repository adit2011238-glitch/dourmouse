"""Chinese-lab coding backend tests (v1) — Qwen / GLM / Kimi.

Exercises the REAL cn_backends module: honest NOT CONFIGURED when no key
is set, env-var resolution (including the DashScope/GLM legacy-name
fallbacks), and the delegation from code_backends.load_backend() into
cn_backends.load_backend() for these three names. Mirrors
test_code_backends.py's TestLoadBackend structure.
"""

from __future__ import annotations

import pytest

from dourmouse import cn_backends, code_backends


# --------------------------------------------------------------------------- #
# cn_backends.load_backend directly
# --------------------------------------------------------------------------- #

class TestQwenBackend:
    def test_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("QWEN_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            cn_backends.load_backend("qwen")

    def test_resolves_from_qwen_env(self, monkeypatch):
        monkeypatch.setenv("QWEN_API_KEY", "qwen-test-key")
        monkeypatch.setenv("QWEN_MODEL", "qwen-max")
        base, key, model = cn_backends.load_backend("qwen")
        assert key == "qwen-test-key"
        assert model == "qwen-max"
        assert base.startswith("https://")

    def test_falls_back_to_dashscope_env(self, monkeypatch):
        """No QWEN_API_KEY, but a DASHSCOPE_API_KEY (Alibaba SDK's own env
        name) is honored so an existing DashScope setup keeps working."""
        monkeypatch.delenv("QWEN_API_KEY", raising=False)
        monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")
        _base, key, _model = cn_backends.load_backend("qwen")
        assert key == "dashscope-key"

    def test_uses_default_model_and_base_when_unset(self, monkeypatch):
        monkeypatch.setenv("QWEN_API_KEY", "qwen-test-key")
        monkeypatch.delenv("QWEN_MODEL", raising=False)
        monkeypatch.delenv("QWEN_BASE_URL", raising=False)
        base, _key, model = cn_backends.load_backend("qwen")
        assert model == "qwen-plus"
        assert base == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


class TestGlmBackend:
    def test_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        monkeypatch.delenv("GLM_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            cn_backends.load_backend("glm")

    def test_resolves_from_zhipu_env(self, monkeypatch):
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test-key")
        base, key, model = cn_backends.load_backend("glm")
        assert key == "zhipu-test-key"
        assert model == "glm-4-flash"
        assert base == "https://open.bigmodel.cn/api/paas/v4"

    def test_falls_back_to_glm_api_key_env(self, monkeypatch):
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        monkeypatch.setenv("GLM_API_KEY", "glm-key")
        _base, key, _model = cn_backends.load_backend("glm")
        assert key == "glm-key"

    def test_accepts_zhipu_and_zai_aliases(self, monkeypatch):
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test-key")
        for alias in ("zhipu", "z.ai", "zai"):
            _base, key, _model = cn_backends.load_backend(alias)
            assert key == "zhipu-test-key"


class TestKimiBackend:
    def test_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            cn_backends.load_backend("kimi")

    def test_not_configured_message_names_no_free_tier(self, monkeypatch):
        """Honest caveat (Rule 2.1): unlike qwen/glm, research found no
        standing free tier for Kimi's direct API — the error must say so
        rather than implying a free path exists."""
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="no standing free tier"):
            cn_backends.load_backend("kimi")

    def test_resolves_from_moonshot_env(self, monkeypatch):
        monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-test-key")
        base, key, model = cn_backends.load_backend("kimi")
        assert key == "moonshot-test-key"
        assert model == "kimi-k2.6"
        assert base == "https://api.moonshot.ai/v1"

    def test_accepts_moonshot_alias(self, monkeypatch):
        monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-test-key")
        _base, key, _model = cn_backends.load_backend("moonshot")
        assert key == "moonshot-test-key"


class TestUnknownCnBackend:
    def test_unknown_backend_raises(self):
        with pytest.raises(RuntimeError, match="unknown Chinese-lab code backend"):
            cn_backends.load_backend("ernie")


# --------------------------------------------------------------------------- #
# code_backends.py delegation — reachable the same way nvidia/deepseek are
# --------------------------------------------------------------------------- #

class TestCodeBackendsDelegatesToCn:
    def test_qwen_not_configured_via_code_backends(self, monkeypatch):
        monkeypatch.delenv("QWEN_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            code_backends.load_backend("qwen")

    def test_glm_not_configured_via_code_backends(self, monkeypatch):
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        monkeypatch.delenv("GLM_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            code_backends.load_backend("glm")

    def test_kimi_not_configured_via_code_backends(self, monkeypatch):
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            code_backends.load_backend("kimi")

    def test_qwen_resolves_via_code_backends(self, monkeypatch):
        monkeypatch.setenv("QWEN_API_KEY", "qwen-test-key")
        base, key, model = code_backends.load_backend("qwen")
        assert key == "qwen-test-key"
        assert model == "qwen-plus"
        assert base.startswith("https://")

    def test_run_code_task_reports_qwen_not_configured(self, monkeypatch):
        """run_code_task's generic (non-claude/codex) path already calls
        load_backend(name), so qwen/glm/kimi are reachable through it with
        no separate if-branch needed there."""
        monkeypatch.delenv("QWEN_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            code_backends.run_code_task("qwen", "write an add function")
