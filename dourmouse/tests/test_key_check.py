"""Live NVIDIA key validation tests (v2.6, dourmouse/key_check.py).

The real module is exercised with an injectable fake client factory — the
module makes a REAL 1-token call in production, but tests must not hit the
network (Rule 2.1 discipline, same as every other layer in this repo). The
exception classes are the REAL openai ones (AuthenticationError etc.), so the
401/403/429 mapping is tested against genuine openai v2 exceptions, not
lookalikes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dourmouse import key_check
from dourmouse.config import NVIDIA_DEFAULT_BASE_URL, NVIDIA_DEFAULT_MODEL

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_VALID_KEY = "nvapi-" + "a" * 30


# --------------------------------------------------------------------------- #
# Fake client + real openai exceptions (same shape as test_governance.py)
# --------------------------------------------------------------------------- #

class _FakeRequest:
    pass


class _FakeResp:
    request = _FakeRequest()
    status_code = 200
    headers = {"x-request-id": "req_1"}  # plain dict has .get

    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _FakeCompletions:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return {"choices": [{"message": {"content": "ok"}}]}


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeClient:
    def __init__(self, completions: _FakeCompletions):
        self.chat = _FakeChat(completions)
        self.api_key: str | None = None
        self.base_url: str | None = None


def _factory_for(client: _FakeClient):
    def _factory(api_key: str, base_url: str):
        client.api_key = api_key
        client.base_url = base_url
        return client

    return _factory


# --------------------------------------------------------------------------- #
# Happy path — a real 1-token call through the fake client
# --------------------------------------------------------------------------- #

class TestHappyPath:
    def test_valid_key_returns_ok_and_calls_create(self):
        client = _FakeClient(_FakeCompletions())
        ok, message = key_check.validate_key_live(_VALID_KEY, client_factory=_factory_for(client))
        assert ok is True
        assert "VALID" in message
        assert _VALID_KEY not in message  # never echo the full key
        # It really went through the OpenAI-compatible path with 1 token.
        assert len(client.chat.completions.calls) == 1
        call = client.chat.completions.calls[0]
        assert call["model"] == NVIDIA_DEFAULT_MODEL
        assert call["max_tokens"] == 1
        assert call["messages"][0]["role"] == "user"
        # Client was built with the key + default base URL.
        assert client.api_key == _VALID_KEY
        assert client.base_url == NVIDIA_DEFAULT_BASE_URL

    def test_env_overrides_model_and_base_url(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_MODEL", "custom/model-1")
        monkeypatch.setenv("NVIDIA_BASE_URL", "https://example.invalid/v1")
        client = _FakeClient(_FakeCompletions())
        ok, _ = key_check.validate_key_live(_VALID_KEY, client_factory=_factory_for(client))
        assert ok is True
        call = client.chat.completions.calls[0]
        assert call["model"] == "custom/model-1"
        assert client.base_url == "https://example.invalid/v1"

    def test_explicit_model_and_base_url_override_env(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_MODEL", "env-model")
        client = _FakeClient(_FakeCompletions())
        ok, _ = key_check.validate_key_live(
            _VALID_KEY,
            model="explicit-model",
            client_factory=_factory_for(client),
        )
        assert ok is True
        assert client.chat.completions.calls[0]["model"] == "explicit-model"


# --------------------------------------------------------------------------- #
# Format rejection (before any network call)
# --------------------------------------------------------------------------- #

class TestFormatRejection:
    @pytest.mark.parametrize(
        "key,needle",
        [
            ("", "No API key"),
            ("short", "too short"),
            ("sk-abcdefghijklmnop", "does not start with 'nvapi-'"),
        ],
    )
    def test_bad_key_rejected_without_network(self, key, needle):
        client = _FakeClient(_FakeCompletions())
        ok, message = key_check.validate_key_live(key, client_factory=_factory_for(client))
        assert ok is False
        assert needle in message
        assert client.chat.completions.calls == []  # no call was ever made


# --------------------------------------------------------------------------- #
# Real openai exception classes -> clear messages
# --------------------------------------------------------------------------- #

class TestFailureMapping:
    def test_401_authentication_error(self):
        import openai

        client = _FakeClient(
            _FakeCompletions(openai.AuthenticationError("bad key", response=_FakeResp(401), body=None))
        )
        ok, message = key_check.validate_key_live(_VALID_KEY, client_factory=_factory_for(client))
        assert ok is False
        assert "401" in message
        assert "invalid, expired, or revoked" in message
        assert _VALID_KEY not in message

    def test_403_permission_denied_names_the_model(self):
        import openai

        client = _FakeClient(
            _FakeCompletions(
                openai.PermissionDeniedError("forbidden", response=_FakeResp(403), body=None)
            )
        )
        ok, message = key_check.validate_key_live(_VALID_KEY, client_factory=_factory_for(client))
        assert ok is False
        assert "403" in message
        assert "NO access to model" in message
        assert NVIDIA_DEFAULT_MODEL in message  # the specific trap we fixed

    def test_429_rate_limit(self):
        import openai

        client = _FakeClient(
            _FakeCompletions(openai.RateLimitError("rl", response=_FakeResp(429), body=None))
        )
        ok, message = key_check.validate_key_live(_VALID_KEY, client_factory=_factory_for(client))
        assert ok is False
        assert "429" in message
        assert "rate limited" in message

    def test_network_connection_error(self):
        import httpx
        import openai

        exc = openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
        client = _FakeClient(_FakeCompletions(exc))
        ok, message = key_check.validate_key_live(_VALID_KEY, client_factory=_factory_for(client))
        assert ok is False
        assert "could not reach NVIDIA" in message

    def test_other_api_status_error(self):
        import openai

        client = _FakeClient(
            _FakeCompletions(openai.InternalServerError("boom", response=_FakeResp(500), body=None))
        )
        ok, message = key_check.validate_key_live(_VALID_KEY, client_factory=_factory_for(client))
        assert ok is False
        assert "500" in message

    def test_unexpected_exception_surfaced_honestly(self):
        client = _FakeClient(_FakeCompletions(RuntimeError("something broke")))
        ok, message = key_check.validate_key_live(_VALID_KEY, client_factory=_factory_for(client))
        assert ok is False
        assert "unexpected failure" in message
        assert "something broke" in message  # the real error, never masked


# --------------------------------------------------------------------------- #
# Masking — the full key must never leak
# --------------------------------------------------------------------------- #

class TestMasking:
    def test_mask_never_contains_the_full_key(self):
        masked = key_check._mask(_VALID_KEY)
        assert masked != _VALID_KEY
        assert _VALID_KEY not in masked
        assert masked.endswith(_VALID_KEY[-4:])
        assert masked.startswith("nvapi-")

    def test_short_key_mask_is_safe(self):
        # 10-char key -> "nvap…1234" (9 chars): shorter than the input and
        # never the full key.
        short = "nvapi-1234"
        masked = key_check._mask(short)
        assert len(masked) < len(short)
        assert short not in masked
        assert masked.startswith(short[:4]) and masked.endswith(short[-4:])
        assert "***" in key_check._mask("tiny")


# --------------------------------------------------------------------------- #
# CLI contract — stdin key, exit codes, no echo
# --------------------------------------------------------------------------- #

class TestCli:
    def test_cli_exits_zero_and_prints_valid(self, monkeypatch, capsys):
        monkeypatch.setattr(key_check, "_default_client_factory", lambda k, b: _FakeClient(_FakeCompletions()))
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(_VALID_KEY + "\n"))
        assert key_check.main() == 0
        out = capsys.readouterr().out
        assert "VALID" in out
        assert _VALID_KEY not in out  # the key is never echoed anywhere

    def test_cli_exits_one_on_rejection(self, monkeypatch, capsys):
        import openai

        client = _FakeClient(
            _FakeCompletions(openai.AuthenticationError("bad", response=_FakeResp(401), body=None))
        )
        monkeypatch.setattr(key_check, "_default_client_factory", lambda k, b: client)
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(_VALID_KEY + "\n"))
        assert key_check.main() == 1
        out = capsys.readouterr().out
        assert "REJECTED" in out
        assert "401" in out

    def test_cli_empty_stdin_exits_one(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("  \n\n"))
        assert key_check.main() == 1
        assert "No API key" in capsys.readouterr().out

    def test_cli_whitespace_around_key_is_stripped(self, monkeypatch):
        import io

        seen = {}
        client = _FakeClient(_FakeCompletions())

        def _factory(k, b):
            seen["key"] = k
            return client

        monkeypatch.setattr(key_check, "_default_client_factory", _factory)
        monkeypatch.setattr("sys.stdin", io.StringIO(f"  {_VALID_KEY}  \n"))
        assert key_check.main() == 0
        assert seen["key"] == _VALID_KEY

    def test_cli_check_existing_uses_env_key(self, monkeypatch, capsys):
        """--check-existing validates the key already in .env (the stale-key
        trap from the earlier 401), reusing the engine's loader."""
        seen = {}
        client = _FakeClient(_FakeCompletions())

        def _factory(k, b):
            seen["key"] = k
            return client

        monkeypatch.setenv("NVIDIA_API_KEY", _VALID_KEY)
        monkeypatch.setenv("NVIDIA_MODEL", "custom/model")
        monkeypatch.setattr(key_check, "_default_client_factory", _factory)
        assert key_check.main(["--check-existing"]) == 0
        assert seen["key"] == _VALID_KEY
        assert "VALID" in capsys.readouterr().out

    def test_cli_check_existing_without_key_exits_one(self, monkeypatch, capsys):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        monkeypatch.delenv("NVIDIA_BASE_URL", raising=False)
        assert key_check.main(["--check-existing"]) == 1
        assert "NVIDIA_API_KEY is not set" in capsys.readouterr().out

    def test_cli_dlp_redacts_credential_shaped_exception_text(self, monkeypatch, capsys):
        """Even if an exception echoed a credential, DLP redacts it from the
        printed message (Rule 2.6 defense in depth)."""
        import openai

        client = _FakeClient(
            _FakeCompletions(openai.AuthenticationError("nvapi-leakedsecretvalue12345", response=_FakeResp(401), body=None))
        )
        monkeypatch.setattr(key_check, "_default_client_factory", lambda k, b: client)
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(_VALID_KEY + "\n"))
        assert key_check.main() == 1
        out = capsys.readouterr().out
        assert "nvapi-leakedsecretvalue12345" not in out
        assert "REDACTED" in out


# --------------------------------------------------------------------------- #
# Wiring into start.command
# --------------------------------------------------------------------------- #

class TestStartCommandWiring:
    def test_onboarding_runs_live_check_before_writing_env(self):
        launcher = (_PROJECT_ROOT / "start.command").read_text()
        # The live check must come BEFORE the .env write, and reject loudly.
        live_idx = launcher.index("dourmouse.key_check")
        write_idx = launcher.index("printf 'NVIDIA_API_KEY=")
        assert live_idx < write_idx
        assert "Live validation FAILED" in launcher
        assert "key NOT saved" in launcher
        assert "3 attempts" in launcher

    def test_bash_n_passes(self):
        script = _PROJECT_ROOT / "start.command"
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
