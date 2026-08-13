"""Load the guardrail configuration from environment variables.

Single source of config truth (Integration Rule 7). Defaults match the values
the user confirmed for Phase 0. Real secrets/keys live in a .env file the
user populates; this module only reads risk NUMBERS, never credentials.

v4.0: the LLM backend is no longer NVIDIA-only. ``DOURMOUSE_LLM_BACKEND``
selects ``ollama`` (local, keyless) / ``omniroute`` (free-tier gateway) /
``nvidia`` / ``auto``, and ``load_llm_config()`` returns the active backend
config behind one interface (``api_key``, ``base_url``, ``model``,
``model_for_agent``) so dispatch / orchestrator / webui treat them
identically.

v5.10: ``auto`` prefers the local Ollama server, then (only when the user
opts in via ``DOURMOUSE_OMNIROUTE_AUTO=1``) the OmniRoute free-tier gateway,
then NVIDIA. The opt-in keeps the local-first privacy guarantee (Rule 2.6):
prompts never leave the machine for a third-party free provider without an
explicit choice — the explicit ``omniroute`` backend is always available
for users who want it.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .guardrails import GuardrailConfig

# Load .env from the project root regardless of the caller's cwd. Real
# secrets live only here (or the shell env) — never hardcoded (Rule 2.6).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _migrate_legacy_env() -> None:
    """Honor legacy ``JARVIS_*`` env names under the new ``DOURMOUSE_*`` names.

    v4.0 rebrand: the codebase now reads ``DOURMOUSE_*`` vars, but an existing
    ``.env`` (and any shell export) still says ``JARVIS_*``. Copy every
    ``JARVIS_*`` var to its ``DOURMOUSE_*`` equivalent when the new name is
    unset, so nothing silently stops working after the rename. The new name
    wins when both are present (set explicitly in a newer .env).
    """

    for name, value in list(os.environ.items()):
        if name.startswith("JARVIS_") and value != "":
            new_name = "DOURMOUSE_" + name[len("JARVIS_"):]
            if os.environ.get(new_name, "") == "":
                os.environ[new_name] = value


_migrate_legacy_env()

# Defaults confirmed by the user for Phase 0 ("use defaults").
_DEFAULTS = {
    "DOURMOUSE_MAX_POSITION_PCT": 0.10,
    "DOURMOUSE_MAX_SECTOR_PCT": 0.30,
    "DOURMOUSE_DAILY_LOSS_LIMIT_PCT": 0.03,
    "DOURMOUSE_TRADE_CONFIRM_USD": 1000.0,
}


def _get_float(name: str) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return _DEFAULTS[name]
    try:
        return float(raw)
    except ValueError as exc:  # surface bad config loudly, don't guess
        raise ValueError(f"env var {name}={raw!r} is not a valid float") from exc


def load_guardrail_config() -> GuardrailConfig:
    """Build a validated GuardrailConfig from env (falling back to defaults)."""
    return GuardrailConfig(
        max_position_pct=_get_float("DOURMOUSE_MAX_POSITION_PCT"),
        max_sector_concentration_pct=_get_float("DOURMOUSE_MAX_SECTOR_PCT"),
        daily_loss_limit_pct=_get_float("DOURMOUSE_DAILY_LOSS_LIMIT_PCT"),
        trade_confirmation_threshold_usd=_get_float("DOURMOUSE_TRADE_CONFIRM_USD"),
    )


@dataclass(frozen=True)
class NvidiaConfig:
    """NVIDIA NIM (OpenAI-compatible) LLM backend config.

    Per user decision, every LLM-driven component in this system runs on
    NVIDIA NIM rather than Claude. Unlike the guardrail numbers, there is no
    sane default for api_key — it must come from the user's real .env.

    Institutional resilience (self-correction spec): ``max_retries`` +
    ``retry_backoff`` give transient API failures bounded retry with
    exponential backoff, and ``fallback_model`` (optional) is a second model
    used if the primary exhausts its retries — so a rate-limited or flaky
    endpoint degrades gracefully instead of failing the whole run.
    """

    api_key: str
    base_url: str
    model: str
    max_retries: int = 2
    retry_backoff: float = 0.5
    fallback_model: str = ""
    # v3.1 per-agent models: agent name (uppercased) -> NVIDIA model id,
    # resolved deterministically from DOURMOUSE_MODEL_<AGENT> env vars. An
    # agent without an override runs on ``model``. Pure env resolution,
    # never an LLM judgment (Rule 2.8).
    agent_models: dict[str, str] = field(default_factory=dict)

    def model_for_agent(self, agent: str | None) -> str:
        """The NVIDIA model a specific subagent runs on (deterministic).

        Precedence: a DOURMOUSE_MODEL_<AGENT_UPPERCASE> env override for that
        exact agent, else the run's default ``model``. Case-insensitive on
        the agent name (env keys are normalized to uppercase at load).
        """
        key = (agent or "").strip().upper()
        if key and key in self.agent_models:
            return self.agent_models[key]
        return self.model


_NVIDIA_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_NVIDIA_DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"

# Public aliases so other modules (e.g. key_check.py) reuse the SAME defaults
# instead of duplicating them and drifting.
NVIDIA_DEFAULT_BASE_URL = _NVIDIA_DEFAULT_BASE_URL
NVIDIA_DEFAULT_MODEL = _NVIDIA_DEFAULT_MODEL


def load_nvidia_config() -> NvidiaConfig:
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise ValueError(
            "NVIDIA_API_KEY is not set. This system's orchestrator runs "
            "entirely on NVIDIA NIM (no Claude/Anthropic fallback) — without "
            "this key it cannot make any LLM calls. Add it to .env (never "
            "hardcode it; see .env.example)."
        )
    base_url = os.environ.get("NVIDIA_BASE_URL", _NVIDIA_DEFAULT_BASE_URL)
    model = os.environ.get("NVIDIA_MODEL", _NVIDIA_DEFAULT_MODEL)
    max_retries = int(os.environ.get("NVIDIA_MAX_RETRIES", "2"))
    retry_backoff = float(os.environ.get("NVIDIA_RETRY_BACKOFF", "0.5"))
    fallback_model = os.environ.get("NVIDIA_FALLBACK_MODEL", "").strip()
    # v3.1 per-agent models: every DOURMOUSE_MODEL_<AGENT> env var maps that
    # agent to its own NVIDIA model (e.g. DOURMOUSE_MODEL_RESEARCH_INFO=...).
    # Keys normalized to uppercase; deterministic (Rule 2.8).
    agent_models = {}
    prefix = "DOURMOUSE_MODEL_"
    for env_name, value in os.environ.items():
        if env_name.startswith(prefix) and value.strip():
            agent_name = env_name[len(prefix):].strip().upper()
            if agent_name:
                agent_models[agent_name] = value.strip()
    return NvidiaConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        fallback_model=fallback_model,
        agent_models=agent_models,
    )


# --------------------------------------------------------------------------- #
# v4.0 — Local LLM backend (Ollama). Keyless, OpenAI-compatible, default.
# --------------------------------------------------------------------------- #

_OLLAMA_DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
_OLLAMA_DEFAULT_MODEL = "qwen3:8b"

# Public aliases (same convention as the NVIDIA defaults above).
OLLAMA_DEFAULT_BASE_URL = _OLLAMA_DEFAULT_BASE_URL
OLLAMA_DEFAULT_MODEL = _OLLAMA_DEFAULT_MODEL

# v5.0 fast dispatch: on the local backend the orchestrator (the looping
# dispatch brain — every turn pays its cost) defaults to a SMALLER, faster
# model, while heavy agents (research/coding) stay on the big default.
# Env DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR overrides it; set to the default
# model name to restore 8b everywhere.
# v5.2: qwen3:4b measured at 9-30 tok/s AND ignored think=False (its
# answers arrived as "Hmm, the user asked..." thinking-narration, burning
# the whole token budget before answering). qwen2.5:7b answers DIRECTLY
# (~20 tok/s, no reasoning preamble) — measured live on this machine.
_OLLAMA_FAST_DISPATCH = {"ORCHESTRATOR": "qwen2.5:7b"}

# One shared interface: dispatch/orchestrator/webui call ``model_for_agent``
# and read ``api_key``/``base_url``/``model`` on either backend config. A
# ``BackendConfig`` Protocol keeps type checkers honest without a base class.


@dataclass(frozen=True)
class OllamaConfig:
    """Local Ollama (OpenAI-compatible) backend config.

    Keyless by design — the model runs on this machine, nothing leaves it
    (Rule 2.6 / local-first). ``api_key`` is an empty string and the OpenAI
    client accepts it; Ollama ignores it. Per-agent overrides come from
    ``DOURMOUSE_OLLAMA_MODEL_<AGENT>`` (mirroring the NVIDIA ``DOURMOUSE_MODEL_``
    convention), resolved deterministically (Rule 2.8).
    """

    api_key: str = ""
    base_url: str = _OLLAMA_DEFAULT_BASE_URL
    model: str = _OLLAMA_DEFAULT_MODEL
    max_retries: int = 2
    retry_backoff: float = 0.5
    fallback_model: str = ""
    agent_models: dict[str, str] = field(default_factory=dict)

    def model_for_agent(self, agent: str | None) -> str:
        """The Ollama model a specific subagent runs on (deterministic)."""
        key = (agent or "").strip().upper()
        if key and key in self.agent_models:
            return self.agent_models[key]
        if key and key in _OLLAMA_FAST_DISPATCH:
            return _OLLAMA_FAST_DISPATCH[key]
        return self.model


def load_ollama_config() -> OllamaConfig:
    """Build the Ollama backend config from env (defaults when unset)."""
    base_url = os.environ.get("OLLAMA_BASE_URL", _OLLAMA_DEFAULT_BASE_URL).strip() or _OLLAMA_DEFAULT_BASE_URL
    model = os.environ.get("OLLAMA_MODEL", _OLLAMA_DEFAULT_MODEL).strip() or _OLLAMA_DEFAULT_MODEL
    max_retries = int(os.environ.get("OLLAMA_MAX_RETRIES", "2"))
    retry_backoff = float(os.environ.get("OLLAMA_RETRY_BACKOFF", "0.5"))
    fallback_model = os.environ.get("OLLAMA_FALLBACK_MODEL", "").strip()
    agent_models = {}
    prefix = "DOURMOUSE_OLLAMA_MODEL_"
    for env_name, value in os.environ.items():
        if env_name.startswith(prefix) and value.strip():
            agent_name = env_name[len(prefix):].strip().upper()
            if agent_name:
                agent_models[agent_name] = value.strip()
    return OllamaConfig(
        api_key="",
        base_url=base_url,
        model=model,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        fallback_model=fallback_model,
        agent_models=agent_models,
    )


def ollama_available(timeout: float = 1.0) -> bool:
    """Probe the default Ollama server (local, deterministic, Rule 2.8).

    Returns True when ``/api/tags`` answers; any failure (connection refused,
    timeout, garbage response) is honestly False. Never raises — the probe is
    the auto-backend detection seam and must not take down config loading.
    """
    probe = "http://127.0.0.1:11434/api/tags"
    try:
        with urllib.request.urlopen(probe, timeout=timeout) as resp:  # noqa: S310 (localhost)
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# v5.10 — OmniRoute free-tier gateway (keyless, OpenAI-compatible).
# --------------------------------------------------------------------------- #

_OMNIROUTE_DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1"
# "auto" lets OmniRoute pick a working free backend (zero-config, no key).
_OMNIROUTE_DEFAULT_MODEL = "auto"

# Public aliases (same convention as the NVIDIA/Ollama defaults above).
OMNIROUTE_DEFAULT_BASE_URL = _OMNIROUTE_DEFAULT_BASE_URL
OMNIROUTE_DEFAULT_MODEL = _OMNIROUTE_DEFAULT_MODEL


@dataclass(frozen=True)
class OmniRouteConfig:
    """OmniRoute free-tier gateway backend (v5.10).

    OmniRoute (MIT, self-hosted on localhost:20128) pools free-tier LLM
    providers behind one OpenAI-compatible endpoint. Keyless by design — the
    ``auto`` model routes to a working free backend with no credentials
    (Rule 2.6: nothing secret required). ``api_key`` is an empty string and
    the OpenAI client accepts it; OmniRoute ignores it. Per-agent overrides
    come from ``DOURMOUSE_OMNIROUTE_MODEL_<AGENT>`` (mirroring the NVIDIA/
    Ollama conventions), resolved deterministically (Rule 2.8).

    Honesty (Rule 2.2): the gateway is a routing layer — free providers are
    rate-limited and can change; failures surface as real errors, never a
    fabricated success.
    """

    api_key: str = ""
    base_url: str = _OMNIROUTE_DEFAULT_BASE_URL
    model: str = _OMNIROUTE_DEFAULT_MODEL
    max_retries: int = 2
    retry_backoff: float = 0.5
    fallback_model: str = ""
    agent_models: dict[str, str] = field(default_factory=dict)

    def model_for_agent(self, agent: str | None) -> str:
        """The OmniRoute model a specific subagent runs on (deterministic).

        Unlike Ollama there is no baked-in fast-dispatch override — the
        gateway's ``auto`` model already routes to a working provider per
        request, so per-agent speed tuning is done with
        ``DOURMOUSE_OMNIROUTE_MODEL_<AGENT>`` (e.g.
        ``DOURMOUSE_OMNIROUTE_MODEL_ORCHESTRATOR=auto/best-coding`` for the
        looping brain).
        """
        key = (agent or "").strip().upper()
        if key and key in self.agent_models:
            return self.agent_models[key]
        return self.model


def load_omniroute_config() -> OmniRouteConfig:
    """Build the OmniRoute backend config from env (defaults when unset)."""
    base_url = os.environ.get(
        "OMNIROUTE_BASE_URL", _OMNIROUTE_DEFAULT_BASE_URL
    ).strip() or _OMNIROUTE_DEFAULT_BASE_URL
    model = os.environ.get(
        "OMNIROUTE_MODEL", _OMNIROUTE_DEFAULT_MODEL
    ).strip() or _OMNIROUTE_DEFAULT_MODEL
    max_retries = int(os.environ.get("OMNIROUTE_MAX_RETRIES", "2"))
    retry_backoff = float(os.environ.get("OMNIROUTE_RETRY_BACKOFF", "0.5"))
    fallback_model = os.environ.get("OMNIROUTE_FALLBACK_MODEL", "").strip()
    agent_models = {}
    prefix = "DOURMOUSE_OMNIROUTE_MODEL_"
    for env_name, value in os.environ.items():
        if env_name.startswith(prefix) and value.strip():
            agent_name = env_name[len(prefix):].strip().upper()
            if agent_name:
                agent_models[agent_name] = value.strip()
    return OmniRouteConfig(
        api_key="",
        base_url=base_url,
        model=model,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        fallback_model=fallback_model,
        agent_models=agent_models,
    )


def omniroute_available(timeout: float = 1.0) -> bool:
    """Probe the local OmniRoute gateway (deterministic, Rule 2.8).

    Honors ``OMNIROUTE_BASE_URL`` (same env the loader reads) so tests and
    alternate installs probe the right host. Returns True when
    ``/v1/models`` answers; any failure (connection refused, timeout,
    garbage response) is honestly False. Never raises — the probe is the
    auto-backend detection seam and must not take down config loading.
    """
    base = os.environ.get("OMNIROUTE_BASE_URL", _OMNIROUTE_DEFAULT_BASE_URL).strip() \
        or _OMNIROUTE_DEFAULT_BASE_URL
    probe = base.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(probe, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# v4.0 — Multi-device access (spec Phase 9)
# --------------------------------------------------------------------------- #

def access_token() -> str:
    """DOURMOUSE_ACCESS_TOKEN: bearer token for non-loopback clients.

    Empty (default) = loopback-only (the current secure posture). When set,
    every route requires the token EXCEPT loopback clients (the desktop app
    and local chat stay token-free). Read from env only (Rule 2.6).
    """
    return os.environ.get("DOURMOUSE_ACCESS_TOKEN", "").strip()


def bind_host() -> str:
    """DOURMOUSE_HOST: where the UI server binds. Default 127.0.0.1 (local).

    Set to 0.0.0.0 for Tailscale/LAN reach — only safe together with
    DOURMOUSE_ACCESS_TOKEN (the serve path prints a loud warning otherwise).
    """
    raw = os.environ.get("DOURMOUSE_HOST", "").strip()
    return raw or "127.0.0.1"


def fast_lane_enabled(value: str | None = None) -> bool:
    """DOURMOUSE_FAST_LANE: route pure-chat turns (no plan, no tool match)
    to the small local fast model. Default on. Set to 0/off to disable.
    """
    raw = value if value is not None else os.environ.get("DOURMOUSE_FAST_LANE", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def fast_lane_model() -> str:
    """DOURMOUSE_FAST_MODEL: the small model used for simple responses.
    Default qwen3:4b (2.5GB, several times faster than qwen3:8b on this
    class of machine). Falls back to the default on empty/whitespace.
    """
    return os.environ.get("DOURMOUSE_FAST_MODEL", "qwen3:4b").strip() or "qwen3:4b"


def fast_lane_server_enabled(value: str | None = None) -> bool:
    """DOURMOUSE_FAST_LANE_SERVER: route pure-chat fast-lane turns to the
    compute node (Dell) when it is online, falling back to the local fast
    model on ANY failure. Default on — the real gate is that the operator
    explicitly set DOURMOUSE_SERVER_URL (a dead/unconfigured node must
    never add probe latency to every reply).
    """
    raw = value if value is not None else os.environ.get("DOURMOUSE_FAST_LANE_SERVER", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def llm_backend() -> str:
    """The active backend name from DOURMOUSE_LLM_BACKEND
    (ollama|omniroute|nvidia|auto)."""
    raw = os.environ.get("DOURMOUSE_LLM_BACKEND", "auto").strip().lower()
    if raw not in ("ollama", "omniroute", "nvidia", "auto"):
        raise ValueError(
            f"DOURMOUSE_LLM_BACKEND must be 'ollama', 'omniroute', 'nvidia' "
            f"or 'auto', got {raw!r}"
        )
    return raw


def load_llm_config() -> NvidiaConfig | OllamaConfig | OmniRouteConfig:
    """Resolve the ACTIVE LLM backend config (v4.0, deterministic, Rule 2.8).

    - ``ollama`` → OllamaConfig (no key required).
    - ``omniroute`` → OmniRouteConfig (keyless, free-tier gateway).
    - ``nvidia`` → NvidiaConfig (raises ValueError without NVIDIA_API_KEY,
      exactly as before).
    - ``auto`` (default) → Ollama when the local server answers; else
      OmniRoute when ``DOURMOUSE_OMNIROUTE_AUTO=1`` AND the free gateway is
      running; else NVIDIA.

    The OmniRoute step is gated behind ``DOURMOUSE_OMNIROUTE_AUTO=1`` (an
    explicit opt-in, v5.10 reviewer fix): ``auto`` must not silently send
    prompts to third-party free providers when Ollama hiccups — this system
    holds credentials and personal data (Rule 2.6 local-first). The explicit
    ``omniroute`` backend below needs no gate. Falling back to NVIDIA keeps
    the legacy keyed behavior.
    """
    backend = llm_backend()
    if backend == "auto":
        if ollama_available():
            return load_ollama_config()
        if (
            os.environ.get("DOURMOUSE_OMNIROUTE_AUTO", "").strip() == "1"
            and omniroute_available()
        ):
            print(  # loud, visible in the server log (Rule 2.2 honesty)
                "[CONFIG] auto: Ollama down, opting into the OmniRoute "
                "free-tier gateway (DOURMOUSE_OMNIROUTE_AUTO=1). Prompts "
                "will be sent to third-party free providers."
            )
            return load_omniroute_config()
        backend = "nvidia"
    if backend == "ollama":
        return load_ollama_config()
    if backend == "omniroute":
        return load_omniroute_config()
    return load_nvidia_config()
