"""Orchestrator tool-calling loop tests.

Runs entirely in isolation (Integration Rule 7.3): a fake OpenAI-shaped
client stands in for NVIDIA NIM so this exercises OUR dispatch loop logic
without a real NVIDIA_API_KEY or network call. This is a test double for an
external API's response shape, not a fabrication of ATLAS/trading results
(Rule 2.2 concerns the latter) — the Research Agent's own honesty guarantees
are tested separately in test_research_agent.py.
"""

from __future__ import annotations

import json

import dourmouse.orchestrator as orchestrator_module
from dourmouse.orchestrator import dispatch


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    """Pops canned responses in order; a single-response list repeats forever
    (used to simulate a model that never stops calling tools)."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class FakeClient:
    def __init__(self, responses: list[_FakeResponse]):
        self.chat = _FakeChat(_FakeCompletions(responses))


class TestDirectAnswerNoTools:
    def test_model_answers_without_calling_a_tool(self):
        client = FakeClient([_FakeResponse(_FakeMessage(content="Hello there."))])
        report = dispatch("Say hi", client=client)
        assert report["final_text"] == "Hello there."
        assert report["transcript"] == [{"type": "assistant_text", "text": "Hello there."}]


class TestToolCallFlow:
    def test_research_tool_call_reports_not_configured_honestly(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)

        tool_call = _FakeToolCall(
            "call_1", "run_atlas_research", json.dumps({"symbols": ["SPY"]})
        )
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ATLAS is not configured yet."))
        client = FakeClient([first, second])

        report = dispatch("Run ATLAS research on SPY.", client=client)

        assert report["final_text"] == "ATLAS is not configured yet."
        tool_use = next(t for t in report["transcript"] if t["type"] == "tool_use")
        assert tool_use["name"] == "run_atlas_research"
        tool_result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "NOT CONFIGURED" in tool_result["text"]
        assert "champions" not in tool_result["text"].lower()

    def test_unknown_tool_name_reports_error_not_crash(self):
        tool_call = _FakeToolCall("call_1", "place_live_order", "{}")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="Could not do that."))
        client = FakeClient([first, second])

        report = dispatch("Do something not in the roster.", client=client)

        tool_result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "unknown tool" in tool_result["text"]
        assert report["final_text"] == "Could not do that."

    def test_malformed_tool_arguments_reported_not_crash(self):
        tool_call = _FakeToolCall("call_1", "run_atlas_research", "{not valid json")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="Fixed."))
        client = FakeClient([first, second])

        report = dispatch("Run research.", client=client)

        tool_result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "invalid JSON" in tool_result["text"]


class TestRealClientConstructionPath:
    """Verifies dispatch() correctly loads NvidiaConfig and constructs an
    OpenAI client when no test double is injected — mocks only the OpenAI
    SDK class itself (the external API boundary), never fabricates a
    response's content."""

    def test_builds_client_from_env_config_when_none_injected(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")  # v4.0: explicit backend
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("NVIDIA_BASE_URL", "https://example.test/v1")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")

        captured = {}

        class _FakeOpenAI:
            def __init__(self, api_key, base_url):
                captured["api_key"] = api_key
                captured["base_url"] = base_url
                self.chat = _FakeChat(
                    _FakeCompletions([_FakeResponse(_FakeMessage(content="ok"))])
                )

        monkeypatch.setattr(orchestrator_module, "OpenAI", _FakeOpenAI)

        report = dispatch("hi")

        assert captured["api_key"] == "nvapi-fake-test-key"
        assert captured["base_url"] == "https://example.test/v1"
        assert report["final_text"] == "ok"

    def test_missing_api_key_raises_before_any_network_attempt(self, monkeypatch):
        # v4.0: force NVIDIA so the no-key path is deterministic (auto would
        # pick Ollama when a local server answers).
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        import pytest

        with pytest.raises(ValueError, match="NVIDIA_API_KEY is not set"):
            dispatch("hi")


class TestMaxTurnsGuard:
    def test_model_that_never_stops_calling_tools_is_bounded(self, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        monkeypatch.delenv("ATLAS_VENV_PATH", raising=False)

        tool_call = _FakeToolCall(
            "call_x", "run_atlas_research", json.dumps({"symbols": ["SPY"]})
        )
        looping_response = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        client = FakeClient([looping_response])  # single response == repeats forever

        report = dispatch("Loop forever.", client=client, max_turns=3)

        assert report["final_text"] == ""
        assert client.chat.completions.calls.__len__() == 3
        assert report["transcript"][-1] == {
            "type": "result",
            "is_error": True,
            "reason": "max_turns exceeded",
        }
