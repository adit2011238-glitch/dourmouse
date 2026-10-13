"""The fast lane never swaps the model (H-PRIV-5, FIX-P).

History: the lane used to swap in DOURMOUSE_FAST_MODEL (a small local Ollama model such as qwen2.5:7b). Against a
hosted backend that gave a 404 on every short question, and under the owner's cloud-only rule a model on this Mac
is not allowed at all. The lane now keeps what it is for (no tool loop, a compact system prompt) and always answers
on the turn's own cloud model, whatever the client looks like.
"""

from __future__ import annotations

import pytest


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(_FakeMessage(content="a real answer"))


class _FakeClient:
    def __init__(self, base_url=None):
        if base_url:
            self.base_url = base_url
        self.completions = _FakeCompletions()
        self.chat = type("Chat", (), {"completions": self.completions})()


def _registry():
    from dourmouse.dispatch import DispatchRegistry, Subagent

    registry = DispatchRegistry()
    registry.register_subagent(Subagent(name="x", domain="x", description="x", tools=()))
    return registry


@pytest.mark.parametrize("base_url", [
    None,
    "http://127.0.0.1:11434/v1",
    "http://192.168.1.50:11434/v1",
    "https://integrate.api.nvidia.com/v1",
    "https://ollama.com/v1",
])
@pytest.mark.parametrize("swap_setting", [None, "1", "0"])
def test_a_pure_chat_turn_keeps_the_turns_own_model_for_every_client(monkeypatch, base_url, swap_setting):
    from dourmouse.dispatch import run_dispatch

    monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
    monkeypatch.setenv("DOURMOUSE_FAST_MODEL", "qwen2.5:7b")
    if swap_setting is None:
        monkeypatch.delenv("DOURMOUSE_FAST_LANE_MODEL_SWAP", raising=False)
    else:
        monkeypatch.setenv("DOURMOUSE_FAST_LANE_MODEL_SWAP", swap_setting)
    client = _FakeClient(base_url)
    run_dispatch("just say hi", _registry(), client=client)
    assert client.completions.calls[0]["model"] == "test-model"


def test_the_lane_still_sends_the_compact_prompt(monkeypatch):
    from dourmouse.dispatch import _FAST_LANE_SYSTEM, run_dispatch

    monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
    client = _FakeClient()
    run_dispatch("just say hi", _registry(), client=client)
    assert client.completions.calls[0]["messages"][0]["content"] == _FAST_LANE_SYSTEM


def test_no_local_model_fallback_exists_in_the_source():
    from pathlib import Path

    import dourmouse.backend_fallback as bf
    import dourmouse.dispatch as d

    assert not hasattr(bf, "probe_ollama_fallback") and not hasattr(d, "_fast_lane_model_is_servable")
    assert "switching to local Ollama" not in Path(d.__file__).read_text(encoding="utf-8")
