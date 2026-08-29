"""Tests for dourmouse/gods_eye.py — the HTTP client side of the real
God's Eye View globe-control bridge.

Hermetic (Rule 2.1): urllib.request.urlopen is faked; no real network, no
real gods-eye-view dev server. The live, real end-to-end round trip
(Python -> vite.config.js's dourmouseActionBridgeProxy -> a real browser
tab running gevActions.js -> back) was verified manually once against the
actual running dev server — see this module's own docstring for the
architecture that proved out.
"""

from __future__ import annotations

import json

import pytest

from dourmouse import gods_eye


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class TestGodsEyeUrl:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(gods_eye._URL_ENV, raising=False)
        assert gods_eye.gods_eye_url() == "http://localhost:4173"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(gods_eye._URL_ENV, "http://localhost:9999/")
        assert gods_eye.gods_eye_url() == "http://localhost:9999"


class TestRunGlobeAction:
    def test_requires_a_name(self):
        with pytest.raises(RuntimeError, match="requires a non-empty"):
            gods_eye.run_globe_action("")

    def test_real_round_trip_shape(self, monkeypatch):
        seen = {}

        def _fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["body"] = json.loads(req.data.decode("utf-8"))
            seen["timeout"] = timeout
            return _Resp(json.dumps({"ok": True, "action": "zoom_to_globe"}).encode())

        monkeypatch.setattr(gods_eye.urllib.request, "urlopen", _fake_urlopen)
        result = gods_eye.run_globe_action("zoom_to_globe", {"foo": "bar"})
        assert result == {"ok": True, "action": "zoom_to_globe"}
        assert seen["url"] == "http://localhost:4173/api/dourmouse/action"
        assert seen["method"] == "POST"
        assert seen["body"] == {"name": "zoom_to_globe", "args": {"foo": "bar"}}
        assert seen["timeout"] == gods_eye._HTTP_TIMEOUT

    def test_args_defaults_to_empty_dict(self, monkeypatch):
        seen = {}

        def _fake_urlopen(req, timeout=None):  # noqa: ARG001
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp(b'{"ok": true}')

        monkeypatch.setattr(gods_eye.urllib.request, "urlopen", _fake_urlopen)
        gods_eye.run_globe_action("stop_tracking")
        assert seen["body"]["args"] == {}

    def test_connection_refused_is_honest_not_configured(self, monkeypatch):
        def _boom(req, timeout=None):  # noqa: ARG001
            raise gods_eye.urllib.error.URLError("Connection refused")

        monkeypatch.setattr(gods_eye.urllib.request, "urlopen", _boom)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            gods_eye.run_globe_action("zoom_to_globe")

    def test_not_configured_names_the_real_start_command(self, monkeypatch):
        def _boom(req, timeout=None):  # noqa: ARG001
            raise gods_eye.urllib.error.URLError("Connection refused")

        monkeypatch.setattr(gods_eye.urllib.request, "urlopen", _boom)
        with pytest.raises(RuntimeError, match="npm run dev"):
            gods_eye.run_globe_action("zoom_to_globe")

    def test_timeout_is_reported_honestly(self, monkeypatch):
        def _boom(req, timeout=None):  # noqa: ARG001
            raise TimeoutError("timed out")

        monkeypatch.setattr(gods_eye.urllib.request, "urlopen", _boom)
        with pytest.raises(RuntimeError, match="timed out"):
            gods_eye.run_globe_action("zoom_to_globe")

    def test_non_json_response_is_honest_not_a_crash(self, monkeypatch):
        def _fake_urlopen(req, timeout=None):  # noqa: ARG001
            return _Resp(b"not json")

        monkeypatch.setattr(gods_eye.urllib.request, "urlopen", _fake_urlopen)
        with pytest.raises(RuntimeError, match="non-JSON"):
            gods_eye.run_globe_action("zoom_to_globe")

    def test_passes_through_a_real_action_failure_verbatim(self, monkeypatch):
        """A real gevActions.js {ok: false, error: ...} must reach the
        caller UNCHANGED — this module never reinterprets or swallows it."""
        failure = {"ok": False, "error": "Unknown data layer: bogus"}

        def _fake_urlopen(req, timeout=None):  # noqa: ARG001
            return _Resp(json.dumps(failure).encode())

        monkeypatch.setattr(gods_eye.urllib.request, "urlopen", _fake_urlopen)
        assert gods_eye.run_globe_action("set_layer_visibility", {"layerId": "bogus"}) == failure
