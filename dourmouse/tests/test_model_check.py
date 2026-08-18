"""Config naming a model is a claim; verify it cheaply.

Regression origin: DOURMOUSE_FAST_MODEL=qwen3:4b was set on a machine where
that model had never been pulled, so every fast-lane question returned
"404 page not found" instantly. Nothing in the system noticed the mismatch.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from dourmouse import model_check


def _tags(*names: str) -> str:
    return json.dumps({"models": [{"name": n, "model": n} for n in names]})


@pytest.fixture
def backend(monkeypatch):
    """Point installed_models at a canned /api/tags response."""

    def install(payload=None, exc=None):
        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return payload.encode()

        def fake_urlopen(req, timeout=None):
            if exc is not None:
                raise exc
            return _Resp()

        monkeypatch.setattr(model_check.urllib.request, "urlopen", fake_urlopen)

    return install


# --------------------------------------------------------------------------- #
# installed_models
# --------------------------------------------------------------------------- #

def test_lists_installed_names(backend):
    backend(_tags("qwen2.5:7b", "qwen3:8b"))
    assert model_check.installed_models() == ["qwen2.5:7b", "qwen3:8b"]


def test_unreachable_is_none_not_empty(backend):
    """None means 'unknown'; [] means 'known to have nothing'."""
    backend(exc=urllib.error.URLError("refused"))
    assert model_check.installed_models() is None


def test_reachable_but_empty_is_a_real_empty_list(backend):
    backend(json.dumps({"models": []}))
    assert model_check.installed_models() == []


def test_malformed_payload_is_none(backend):
    backend("not json at all")
    assert model_check.installed_models() is None


def test_timeout_is_none(backend):
    backend(exc=TimeoutError("slow"))
    assert model_check.installed_models() is None


# --------------------------------------------------------------------------- #
# check_configured_models — the exact reported failure
# --------------------------------------------------------------------------- #

def test_detects_the_qwen3_4b_mismatch(backend):
    backend(_tags("qwen2.5:7b", "qwen3:8b", "qwen2.5:1.5b"))

    result = model_check.check_configured_models(
        {"fast lane": "qwen3:4b", "primary": "qwen3:8b"}
    )

    assert result["reachable"] is True
    assert result["missing"] == {"fast lane": "qwen3:4b"}
    assert "qwen3:4b" in result["detail"]


def test_no_missing_when_everything_is_present(backend):
    backend(_tags("qwen3:8b"))
    result = model_check.check_configured_models({"primary": "qwen3:8b"})
    assert result["missing"] == {}
    assert "all configured models present" in result["detail"]


def test_untagged_name_matches_a_tagged_install(backend):
    """'qwen3' should match an installed 'qwen3:8b' rather than false-alarm."""
    backend(_tags("qwen3:8b"))
    result = model_check.check_configured_models({"primary": "qwen3"})
    assert result["missing"] == {}


def test_unreachable_backend_reports_unverified_not_missing(backend):
    """A stopped daemon must not be reported as 'your model is gone'."""
    backend(exc=urllib.error.URLError("down"))
    result = model_check.check_configured_models({"fast lane": "qwen3:4b"})
    assert result["reachable"] is False
    assert result["missing"] == {}
    assert "not verified" in result["detail"]


def test_blank_configuration_entries_are_ignored(backend):
    backend(_tags("qwen3:8b"))
    result = model_check.check_configured_models({"fallback": ""})
    assert result["missing"] == {}


# --------------------------------------------------------------------------- #
# resolve_available
# --------------------------------------------------------------------------- #

def test_wanted_model_is_returned_untouched_when_present(backend):
    backend(_tags("qwen3:4b"))
    model, note = model_check.resolve_available("qwen3:4b")
    assert model == "qwen3:4b"
    assert note is None


def test_falls_back_to_a_preferred_candidate(backend):
    backend(_tags("qwen2.5:7b", "qwen2.5:1.5b"))

    model, note = model_check.resolve_available(
        "qwen3:4b", prefer=["qwen2.5:1.5b", "qwen2.5:7b"]
    )

    assert model == "qwen2.5:1.5b"
    assert "not installed" in note
    assert "ollama pull qwen3:4b" in note  # tells the user how to fix it


def test_falls_back_to_anything_installed_when_no_preference_matches(backend):
    backend(_tags("some-other-model"))
    model, note = model_check.resolve_available("qwen3:4b", prefer=["also-missing"])
    assert model == "some-other-model"
    assert note is not None


def test_unreachable_backend_does_not_trigger_a_swap(backend):
    """A timed-out health check is not evidence the model is absent."""
    backend(exc=urllib.error.URLError("down"))
    model, note = model_check.resolve_available("qwen3:4b", prefer=["qwen2.5:7b"])
    assert model == "qwen3:4b"
    assert note is None


def test_no_models_at_all_keeps_wanted_and_says_so(backend):
    backend(json.dumps({"models": []}))
    model, note = model_check.resolve_available("qwen3:4b")
    assert model == "qwen3:4b"
    assert "no other model" in note


# --------------------------------------------------------------------------- #
# host resolution
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "env,value,expected",
    [
        ("OLLAMA_BASE_URL", "http://box:11434/v1", "http://box:11434"),
        ("OLLAMA_HOST", "127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("OLLAMA_HOST", "http://box:1234", "http://box:1234"),
    ],
)
def test_host_env_vars_are_normalised(monkeypatch, env, value, expected):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv(env, value)
    assert model_check.ollama_host() == expected


def test_default_host_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert model_check.ollama_host() == model_check.OLLAMA_DEFAULT
