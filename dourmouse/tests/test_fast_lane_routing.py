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
