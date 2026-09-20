"""dourmouse/security/reputation.py -- real, keyed AbuseIPDB lookups.
Every real network call is monkeypatched; the honest-refusal logic (non-
public targets, missing key, malformed responses) is what's under test."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from dourmouse.security import reputation as rep


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, _n=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestReputationConfigured:
    def test_no_key_is_not_configured(self, monkeypatch):
        monkeypatch.delenv(rep.REPUTATION_API_KEY_ENV, raising=False)
        assert rep.reputation_configured() is False

    def test_a_real_key_is_configured(self, monkeypatch):
        monkeypatch.setenv(rep.REPUTATION_API_KEY_ENV, "real-key-value")
        assert rep.reputation_configured() is True

    def test_whitespace_only_key_is_not_configured(self, monkeypatch):
        monkeypatch.setenv(rep.REPUTATION_API_KEY_ENV, "   ")
        assert rep.reputation_configured() is False


class TestRejectNonPublic:
    @pytest.mark.parametrize("ip", ["192.168.1.1", "10.0.0.5", "127.0.0.1", "169.254.1.1", "224.0.0.1"])
    def test_private_loopback_link_local_reserved_multicast_are_rejected(self, ip):
        assert rep._reject_non_public(ip) is not None

    def test_a_real_public_ip_is_accepted(self):
        assert rep._reject_non_public("8.8.8.8") is None

    def test_a_malformed_ip_is_honestly_rejected(self):
        assert rep._reject_non_public("not-an-ip") is not None


class TestCheckIpReputation:
    def test_a_private_target_is_refused_before_any_network_call(self, monkeypatch):
        monkeypatch.setenv(rep.REPUTATION_API_KEY_ENV, "key")

        def _boom(*a, **kw):
            raise AssertionError("must not be called for a private target")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        result = rep.check_ip_reputation("192.168.1.5")
        assert result["available"] is False
        assert "private" in result["reason"]

    def test_no_key_is_honestly_not_configured(self, monkeypatch):
        monkeypatch.delenv(rep.REPUTATION_API_KEY_ENV, raising=False)
        result = rep.check_ip_reputation("8.8.8.8")
        assert result["available"] is False
        assert "NOT CONFIGURED" in result["reason"]

    def test_a_real_successful_response_is_parsed(self, monkeypatch):
        monkeypatch.setenv(rep.REPUTATION_API_KEY_ENV, "key")
        body = json.dumps({"data": {
            "ipAddress": "8.8.8.8", "abuseConfidenceScore": 0, "totalReports": 0,
            "countryCode": "US", "isp": "Google LLC", "isTor": False,
        }}).encode("utf-8")
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body))
        result = rep.check_ip_reputation("8.8.8.8")
        assert result["available"] is True
        assert result["abuse_confidence_score"] == 0
        assert result["isp"] == "Google LLC"

    def test_an_http_error_is_honest(self, monkeypatch):
        monkeypatch.setenv(rep.REPUTATION_API_KEY_ENV, "key")

        def _raise(req, timeout=None):
            raise urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        result = rep.check_ip_reputation("8.8.8.8")
        assert result["available"] is False
        assert "401" in result["reason"]

    def test_a_network_error_is_honest(self, monkeypatch):
        monkeypatch.setenv(rep.REPUTATION_API_KEY_ENV, "key")

        def _raise(req, timeout=None):
            raise urllib.error.URLError("name resolution failed")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        result = rep.check_ip_reputation("8.8.8.8")
        assert result["available"] is False
        assert "unreachable" in result["reason"]

    def test_a_malformed_json_response_is_honest(self, monkeypatch):
        monkeypatch.setenv(rep.REPUTATION_API_KEY_ENV, "key")
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(b"not json"))
        result = rep.check_ip_reputation("8.8.8.8")
        assert result["available"] is False
        assert "could not read" in result["reason"]

    def test_a_response_missing_the_real_score_is_honest(self, monkeypatch):
        monkeypatch.setenv(rep.REPUTATION_API_KEY_ENV, "key")
        body = json.dumps({"data": {"ipAddress": "8.8.8.8"}}).encode("utf-8")
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body))
        result = rep.check_ip_reputation("8.8.8.8")
        assert result["available"] is False
        assert "confidence score" in result["reason"]
