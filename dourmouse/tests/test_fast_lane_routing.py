"""The fast lane must not send a local model name to a hosted backend.

Reported symptom: every short question came back "404 page not found" in
about a second. Cause: the fast lane unconditionally swapped the model to
DOURMOUSE_FAST_MODEL (default qwen3:4b, a local Ollama model) even when the
configured client was NVIDIA's hosted API, which has never heard of it.

The hosted backend does not need the lane — its measured p50 is ~1.1s,
faster than the local small model — so the correct behaviour is to keep the
primary model there.
"""

from __future__ import annotations

import pytest

from dourmouse.dispatch import _fast_lane_model_is_servable


class _Client:
    def __init__(self, base_url):
        self.base_url = base_url


LOCAL = [
    "http://127.0.0.1:11434/v1",
    "http://localhost:11434/v1",
    "http://127.0.0.1:11434",
    "HTTP://LOCALHOST:11434/V1",
]

HOSTED = [
    "https://integrate.api.nvidia.com/v1",
    "https://api.openai.com/v1",
    "https://api.deepseek.com/v1",
    "https://some-vendor.example.com/v1",
]


@pytest.mark.parametrize("url", LOCAL)
def test_local_clients_may_use_the_local_fast_model(url):
    assert _fast_lane_model_is_servable(_Client(url)) is True


@pytest.mark.parametrize("url", HOSTED)
def test_hosted_clients_must_not_get_a_local_model_name(url):
    """This is the 404: qwen3:4b sent to integrate.api.nvidia.com."""
    assert _fast_lane_model_is_servable(_Client(url)) is False


def test_nvidia_is_rejected_even_on_a_nonstandard_port():
    assert _fast_lane_model_is_servable(_Client("https://integrate.api.nvidia.com:443/v1")) is False


def test_ollama_native_client_is_always_local():
    from dourmouse.dispatch import OllamaNativeClient

    client = OllamaNativeClient.__new__(OllamaNativeClient)
    assert _fast_lane_model_is_servable(client) is True


def test_client_without_a_base_url_keeps_historical_behaviour():
    """Engine-test doubles assert the swap; they must keep passing."""
    assert _fast_lane_model_is_servable(object()) is True


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


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeLocalClient(_Client):
    """A LOCAL-shaped client (fast-lane-servable, per _fast_lane_model_is_
    servable above) that actually answers a call, so the model it was
    ACTUALLY asked for is inspectable."""

    def __init__(self):
        super().__init__("http://127.0.0.1:11434/v1")
        self.completions = _FakeCompletions()
        self.chat = _FakeChat(self.completions)


class TestFastLaneModelSwapGate:
    """v13.5 (live-diagnosed, explicit user request — "why is routing
    requests to qwen local instead of the Ollama api key or claude code"):
    a SEPARATE opt-out from fast_lane_enabled — the fast lane's other real
    benefit (no tool loop, compact prompt) stays on, but the MODEL swap to
    DOURMOUSE_FAST_MODEL ("qwen2.5:7b") can be turned off so a pure-chat
    turn still answers on the turn's own real resolved brain model instead
    of a hardcoded swap-in. See config.fast_lane_model_swap_enabled's own
    docstring for the full diagnosis."""

    def _pure_chat_registry_and_client(self):
        from dourmouse.dispatch import DispatchRegistry, Subagent

        registry = DispatchRegistry()
        registry.register_subagent(Subagent(name="x", domain="x", description="x", tools=()))
        return registry, _FakeLocalClient()

    def test_default_on_still_swaps_to_the_fast_model(self, monkeypatch):
        from dourmouse.dispatch import run_dispatch

        monkeypatch.delenv("DOURMOUSE_FAST_LANE_MODEL_SWAP", raising=False)
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        registry, client = self._pure_chat_registry_and_client()
        run_dispatch("just say hi", registry, client=client)
        assert client.completions.calls[0]["model"] == "qwen2.5:7b"

    def test_disabled_keeps_the_turns_own_resolved_model(self, monkeypatch):
        # No explicit `model=` here on purpose: an explicit override already
        # bypasses the fast lane entirely on its own (fast_lane requires
        # _explicit_model is None) -- that would prove nothing about THIS
        # gate. With no override and a pre-supplied client, the turn's own
        # resolved model is the deterministic "test-model" fallback
        # (run_dispatch_messages: `model = model or (config.model if
        # config is not None else "test-model")`) -- the real point is just
        # that it must NOT be silently swapped to "qwen2.5:7b".
        from dourmouse.dispatch import run_dispatch

        monkeypatch.setenv("DOURMOUSE_FAST_LANE_MODEL_SWAP", "0")
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "1")
        registry, client = self._pure_chat_registry_and_client()
        run_dispatch("just say hi", registry, client=client)
        assert client.completions.calls[0]["model"] != "qwen2.5:7b"
        assert client.completions.calls[0]["model"] == "test-model"


class TestFastLaneModelSwapEnabledConfig:
    def test_default_on(self, monkeypatch):
        from dourmouse.config import fast_lane_model_swap_enabled

        monkeypatch.delenv("DOURMOUSE_FAST_LANE_MODEL_SWAP", raising=False)
        assert fast_lane_model_swap_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_off_values(self, monkeypatch, value):
        from dourmouse.config import fast_lane_model_swap_enabled

        monkeypatch.setenv("DOURMOUSE_FAST_LANE_MODEL_SWAP", value)
        assert fast_lane_model_swap_enabled() is False


def test_a_remote_ollama_on_the_lan_counts_as_local():
    """An Ollama daemon is an Ollama daemon, wherever it runs.

    The distinction that matters is *model naming*, not network location: a
    LAN Ollama uses the same 'qwen3:4b' namespace, whereas a hosted vendor
    API uses its own entirely. Excluding the LAN would disable the fast lane
    for a legitimate setup (a dedicated compute node on the same network),
    which is the configuration this project actually runs.

    If a LAN daemon turns out to lack the model, that is the missing-model
    case covered by `model_check`, not a routing bug.
    """
    assert _fast_lane_model_is_servable(_Client("http://192.168.1.50:11434/v1")) is True
