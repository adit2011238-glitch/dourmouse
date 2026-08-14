"""Transparent backend failover: NVIDIA → local Ollama if unreachable.

Wraps load_llm_config() to probe the primary backend and fall back silently.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from dataclasses import replace

from dourmouse.config import load_llm_config, llm_backend


def _probe_backend(base_url: str, timeout: float = 3.0) -> bool:
    """Probe a backend's /models endpoint. Return True if reachable."""
    try:
        req = urllib.request.Request(base_url + "/models")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def load_llm_config_with_fallback() -> dict:
    """Load LLM config, falling back to local Ollama if primary unreachable.

    If primary backend (NVIDIA or explicit Ollama URL) doesn't answer within
    3 seconds, silently switch to local Ollama (127.0.0.1:11434) and log the
    event. This preserves user experience: requests never hang waiting for an
    offline endpoint.

    Rule: fallback is silent, not loud. The connections panel will report
    true status on next probe. No user-facing banner needed; degradation is
    expected and graceful.

    Test mode: DOURMOUSE_FALLBACK_DISABLED=1 skips probing (tests need stable
    config, not dynamic fallback).
    """
    cfg = load_llm_config()

    # Test mode: skip fallback, use configured backend as-is.
    if os.environ.get("DOURMOUSE_FALLBACK_DISABLED", "").strip().lower() in (
        "1", "true", "yes"
    ):
        return cfg

    backend = llm_backend()

    # Probe the primary endpoint. If it answers, use it.
    if backend == "nvidia":
        if _probe_backend(cfg.base_url):
            return cfg
        # NVIDIA unreachable; fall back to local Ollama.
        print(
            "[BACKEND] NVIDIA unreachable, falling back to local Ollama "
            "(127.0.0.1:11434)"
        )
        # Synthesize an OllamaConfig pointing at local.
        from dourmouse.config import OllamaConfig

        return OllamaConfig(
            base_url="http://127.0.0.1:11434",
            model=os.environ.get("OLLAMA_MODEL", "phi:2b").strip() or "phi:2b",
        )
    elif backend == "ollama":
        if _probe_backend(cfg.base_url):
            return cfg
        # Configured Ollama unreachable; try default local.
        if cfg.base_url != "http://127.0.0.1:11434":
            print(
                f"[BACKEND] {cfg.base_url} unreachable, trying default "
                "127.0.0.1:11434"
            )
            if _probe_backend("http://127.0.0.1:11434"):
                from dourmouse.config import OllamaConfig

                return OllamaConfig(
                    base_url="http://127.0.0.1:11434", model=cfg.model
                )
        # All Ollama attempts failed; return what we have (will error on use).
        return cfg

    # omniroute or auto: use as-is (omniroute is a gateway, fallback is implicit).
    return cfg
