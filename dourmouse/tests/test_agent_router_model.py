"""dourmouse/agent_router_model.py — user-directed (2026-09-15): "use
the local agent router model as your router for choosing tools". See
that module's own docstring for the real, evidence-based reasoning.
Hermetic: urllib is monkeypatched, no real Ollama daemon needed.
"""

from __future__ import annotations

import json
import urllib.error

from dourmouse import agent_router_model as router


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class TestRouteViaLocalModel:
    def test_empty_query_is_none_without_a_network_call(self, monkeypatch):
        def fail_if_called(*a, **k):
            raise AssertionError("must not call the network for an empty query")

        monkeypatch.setattr(router.urllib.request, "urlopen", fail_if_called)
        assert router.route_via_local_model("", {"mail", "docs"}) is None

    def test_empty_agent_set_is_none_without_a_network_call(self, monkeypatch):
        def fail_if_called(*a, **k):
            raise AssertionError("must not call the network with no valid agents")

        monkeypatch.setattr(router.urllib.request, "urlopen", fail_if_called)
        assert router.route_via_local_model("check my inbox", set()) is None

    def test_real_tool_call_with_a_known_agent_wins(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return _FakeResponse(
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "route_to_agent", "arguments": {"agent": "mail"}}}
                        ]
                    }
                }
            )

        monkeypatch.setattr(router.urllib.request, "urlopen", fake_urlopen)
        assert router.route_via_local_model("check my inbox", {"mail", "docs"}) == "mail"

    def test_string_encoded_arguments_are_parsed(self, monkeypatch):
        """Ollama's real /api/chat sometimes returns tool arguments as a
        JSON-encoded STRING rather than a parsed object — real shape
        observed live this session testing this exact model."""

        def fake_urlopen(req, timeout=None):
            return _FakeResponse(
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "route_to_agent",
                                    "arguments": json.dumps({"agent": "docs"}),
                                }
                            }
                        ]
                    }
                }
            )

        monkeypatch.setattr(router.urllib.request, "urlopen", fake_urlopen)
        assert router.route_via_local_model("read my drive", {"docs"}) == "docs"

    def test_unknown_agent_name_is_none(self, monkeypatch):
        """A garbled or hallucinated agent name (real, observed failure
        mode this session: 'is_prime(17)' for a coding-shaped query) must
        never be handed to the caller as if it were real."""

        def fake_urlopen(req, timeout=None):
            return _FakeResponse(
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "route_to_agent", "arguments": {"agent": "is_prime(17)"}}}
                        ]
                    }
                }
            )

        monkeypatch.setattr(router.urllib.request, "urlopen", fake_urlopen)
        assert router.route_via_local_model("write a prime checker", {"dev_coding"}) is None

    def test_no_tool_call_at_all_is_none(self, monkeypatch):
        """Real, observed failure mode: the model just answers directly
        instead of routing (4/5 coding-shaped queries in this session's
        own real comparison test)."""

        def fake_urlopen(req, timeout=None):
            return _FakeResponse({"message": {"content": "Sure, here's a function..."}})

        monkeypatch.setattr(router.urllib.request, "urlopen", fake_urlopen)
        assert router.route_via_local_model("write a prime checker", {"dev_coding"}) is None

    def test_network_failure_is_none_not_a_crash(self, monkeypatch):
        def raise_url_error(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(router.urllib.request, "urlopen", raise_url_error)
        assert router.route_via_local_model("check my inbox", {"mail"}) is None

    def test_malformed_json_response_is_none_not_a_crash(self, monkeypatch):
        class _BadResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"not json"

        monkeypatch.setattr(router.urllib.request, "urlopen", lambda req, timeout=None: _BadResponse())
        assert router.route_via_local_model("check my inbox", {"mail"}) is None

    def test_uses_the_configured_model_name(self, monkeypatch):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            return _FakeResponse({"message": {"tool_calls": []}})

        monkeypatch.setattr(router.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("DOURMOUSE_AGENT_ROUTER_MODEL", "custom-router:latest")
        router.route_via_local_model("check my inbox", {"mail"})
        assert seen["body"]["model"] == "custom-router:latest"

    def test_defaults_to_the_real_fine_tuned_model_name(self, monkeypatch):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            return _FakeResponse({"message": {"tool_calls": []}})

        monkeypatch.delenv("DOURMOUSE_AGENT_ROUTER_MODEL", raising=False)
        monkeypatch.setattr(router.urllib.request, "urlopen", fake_urlopen)
        router.route_via_local_model("check my inbox", {"mail"})
        assert seen["body"]["model"] == "agent-router:latest"

    def test_always_targets_the_local_daemon_never_cloud(self, monkeypatch):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            return _FakeResponse({"message": {"tool_calls": []}})

        monkeypatch.setattr(router.urllib.request, "urlopen", fake_urlopen)
        router.route_via_local_model("check my inbox", {"mail"})
        assert seen["url"] == "http://127.0.0.1:11434/api/chat"
