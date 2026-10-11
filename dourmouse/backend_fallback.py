"""Transparent backend failover: NVIDIA → local Ollama if unreachable.

Wraps load_llm_config() to probe the primary backend and fall back silently.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

from dourmouse.config import OLLAMA_DEFAULT_BASE_URL, OllamaConfig, llm_backend, load_llm_config

_LOCAL_OLLAMA_ROOT = "http://127.0.0.1:11434"


def _is_local_ollama(base_url: str) -> bool:
    """True when ``base_url`` is the default local Ollama, with or without the /v1 suffix."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root == _LOCAL_OLLAMA_ROOT


def _probe_backend(base_url: str, timeout: float = 3.0) -> bool:
    """Probe a backend's OpenAI-compatible /v1/models endpoint. Return True if reachable.

    Every backend serves its models under ``/v1``: the configured base URLs already end in it and a bare
    root (``http://127.0.0.1:11434``) gets it added, because ``/models`` on its own does not exist (404).
    An HTTP answer below 500 (a 401 or 404 included) means the host is up; urlopen raises HTTPError for
    those, so it is read here instead of being treated as a transport failure.
    """
    root = base_url.rstrip("/")
    if not root.endswith("/v1"):
        root += "/v1"
    try:
        req = urllib.request.Request(root + "/models")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
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
        return OllamaConfig(
            base_url=OLLAMA_DEFAULT_BASE_URL,
            model=os.environ.get("OLLAMA_MODEL", "phi:2b").strip() or "phi:2b",
        )
    elif backend == "ollama":
        if _probe_backend(cfg.base_url):
            return cfg
        # Configured Ollama unreachable; try default local.
        if not _is_local_ollama(cfg.base_url):
            print(
                f"[BACKEND] {cfg.base_url} unreachable, trying default "
                "127.0.0.1:11434"
            )
            if _probe_backend(OLLAMA_DEFAULT_BASE_URL):
                return OllamaConfig(
                    base_url=OLLAMA_DEFAULT_BASE_URL, model=cfg.model
                )
        # All Ollama attempts failed; return what we have (will error on use).
        return cfg

    # omniroute or auto: use as-is (omniroute is a gateway, fallback is implicit).
    return cfg


def probe_ollama_fallback(timeout: float = 3.0) -> "OllamaConfig | None":
    """Mid-call counterpart to ``load_llm_config_with_fallback()``'s
    pre-flight probe above -- that one only ever runs once, at config-load,
    against the /models endpoint of the configured primary backend. It has
    no way to react to a backend that answered fine at startup and then
    started rate-limiting every account mid-conversation.

    This is that reaction: given a primary backend that just ran out of
    usable accounts (see ``model_router.pool_exhausted`` /
    dispatch.py's ``_nvidia_rotation_factory``), probe local Ollama right
    now and, if it answers, return the config to switch the REST OF THIS
    TURN to. Returns None if Ollama is not reachable either, or fallback is
    disabled for tests -- the caller then keeps serving its current
    (exhausted) client and lets its own retry/fallback_model machinery
    surface the real error (Rule 2.2: never pretend a dead pool is fine).

    Deliberately does NOT construct a client -- that stays dispatch.py's
    job (``_build_client`` / ``OllamaNativeClient``), so this module never
    needs to import dispatch (which already imports this module) and stays
    free of any circular-import risk.
    """
    if os.environ.get("DOURMOUSE_FALLBACK_DISABLED", "").strip().lower() in (
        "1", "true", "yes"
    ):
        return None
    local_url = OLLAMA_DEFAULT_BASE_URL
    if not _probe_backend(local_url, timeout=timeout):
        return None
    model = os.environ.get("OLLAMA_MODEL", "phi:2b").strip() or "phi:2b"
    return OllamaConfig(base_url=local_url, model=model)
