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

world-monitor-expansion: two additions to the ``DOURMOUSE_MODEL_<AGENT>``
per-agent routing mechanism, which existed but shipped with nothing
actually plugged into it (every agent silently ran on the one default
model unless a user hand-set an env var):

1. Real default per-agent models for the NVIDIA backend
   (``_NVIDIA_AGENT_DEFAULTS`` below) — orchestrator/research_info/
   dev_coding/comms/mail/news/worldmonitor now route to genuinely
   different NVIDIA NIM models out of the box, still fully overridable
   per-agent via ``DOURMOUSE_MODEL_<AGENT>`` (checked first) or en masse
   via ``NVIDIA_MODEL``. See the dict's own docstring for exactly which
   model was chosen for which agent and why. Ollama already had a
   (narrower) version of this for its ORCHESTRATOR fast-dispatch case
   (``_OLLAMA_FAST_DISPATCH``, below); OmniRoute deliberately gets no
   baked-in defaults — its ``auto`` model already does dynamic per-request
   routing, so a hardcoded per-agent override would just be guessing at
   gateway-specific model tags with no way to verify them from here.
2. ``orchestrator_model_setting()`` / ``save_orchestrator_model_setting()``:
   a persisted (not just env-var) choice of orchestrator model, read fresh
   from the user's config file on every ``model_for_agent("orchestrator")``
   call — so a change made through the Settings UI's
   ``/api/settings/orchestrator-model`` endpoint (webui.py) takes effect on
   the orchestrator's next turn, no process restart required. Sits between
   the env-var override and the NVIDIA default tier: env var still wins
   when set (an operator's explicit env config should never be silently
   shadowed by a UI click), the persisted setting wins over the built-in
   default otherwise.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .guardrails import GuardrailConfig

def user_config_dir() -> Path:
    """Where an INSTALLED Dourmouse keeps its configuration.

    v8.9. The packaged app must not read config from beside the package:
    in a frozen build that path is inside the read-only bundle, and the
    bundle deliberately ships no ``.env`` (shipping one would hand the
    builder's own API keys to everyone who installs it). Config therefore
    lives in a per-user directory that survives updates and uninstalls.

    v13 (hermetic-test-caught, real bug): with no override, every test on
    this machine that touched orchestrator-model settings or Grounded Mode
    was silently reading the REAL developer's ``.env`` — live settings
    toggled ON during earlier manual verification (DOURMOUSE_GROUNDED_MODE=1
    from Grounded Mode's own live test) leaked into unrelated hermetic
    tests, causing spurious extra dispatch turns and exhausting fake
    clients sized for the untouched-setting case. DOURMOUSE_CONFIG_DIR lets
    tests redirect this the same way DOURMOUSE_WORKSPACE already isolates
    the workspace root (see conftest.py's ``_workspace_isolated``) — unset
    in production, so real installs are unaffected.
    """
    override = os.environ.get("DOURMOUSE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Dourmouse"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Dourmouse"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "dourmouse"


def user_env_path() -> Path:
    return user_config_dir() / ".env"


def workspace_dir() -> Path:
    """The DourMouse workspace root: ``DOURMOUSE_WORKSPACE`` env var when
    set, else ``<project_root>/workspace`` (this file's grandparent dir).

    Single source of truth for this resolution. Before this helper existed
    it was independently reimplemented in five places (google_auth.py's
    ``default_auth_store``, chat.py's ``_default_sessions_dir``, desktop.py's
    ``_webview_storage_path``, general_roster.py's ``_workspace_root``,
    design_3d_ops.py's ``_manifest_path``) — a real path-drift risk: if any
    copy diverged, AuthStore's ``dourmouse_auth.db`` and the rest of the app
    would resolve different workspace roots and login would silently break.
    All five now call this instead.

    Does not create the directory — callers that need it to exist still
    call ``.mkdir(parents=True, exist_ok=True)`` on the result themselves.
    """
    raw = os.environ.get("DOURMOUSE_WORKSPACE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent.parent / "workspace"


def is_configured() -> bool:
    """True when SOME working backend is reachable.

    Deliberately not "a key exists": a local Ollama install is a complete,
    valid configuration with no key at all, and first-run setup treats it
    as the default. Used to decide whether to show the setup wizard.
    """
    if os.environ.get("NVIDIA_API_KEY", "").strip():
        return True
    if os.environ.get("DOURMOUSE_SERVER_URL", "").strip():
        return True
    backend = os.environ.get("DOURMOUSE_LLM_BACKEND", "").strip().lower()
    if backend == "ollama":
        return True
    return False


# Load config in precedence order: the USER config dir first (an installed
# app), then the project root (a source checkout / dev machine). Real
# secrets live only in these files or the shell env — never hardcoded
# (Rule 2.6). override=False so the first file found wins and a dev
# checkout never silently overrides an installed user's settings.
load_dotenv(user_env_path(), override=False)
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


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
    # 2026-09-14, user-directed: "a different api key for each agent since
    # claude code is supposed to be orchestrating not doing the work" --
    # the same idea as agent_models, one level down: agent name
    # (uppercased) -> its own NVIDIA API key, from DOURMOUSE_API_KEY_<AGENT>
    # env vars. An agent without an override runs on ``api_key``. This is
    # deliberately a SEPARATE concept from the multi-account FALLBACK pool
    # (model_router.AccountPool / NVIDIA_API_KEY_2, _3, ...) generalized
    # earlier this session -- that pool rotates between accounts on
    # rate-limit for ANY agent; this assigns a SPECIFIC, fixed account to a
    # SPECIFIC agent on purpose (e.g. billing/quota separation per team
    # member), and the two compose: an agent's own assigned key still goes
    # through the same retry/rotation machinery if IT hits a rate limit.
    agent_keys: dict[str, str] = field(default_factory=dict)

    def key_for_agent(self, agent: str | None) -> str:
        """The NVIDIA API key a specific subagent runs on (deterministic).

        An explicit ``DOURMOUSE_API_KEY_<AGENT>`` override wins; every
        other agent (and any caller with no agent name at all -- tools,
        scheduled jobs) uses the run's own default ``api_key``, exactly
        the same shape as ``model_for_agent`` above, one level down.
        """
        key = (agent or "").strip().upper()
        if key and key in self.agent_keys:
            return self.agent_keys[key]
        return self.api_key

    def model_for_agent(self, agent: str | None) -> str:
        """The NVIDIA model a specific subagent runs on (deterministic).

        Precedence, highest first:
        1. ``DOURMOUSE_MODEL_<AGENT_UPPERCASE>`` env override for that exact
           agent (``agent_models``) — an operator's explicit env config
           always wins.
        2. For ``orchestrator`` only: the persisted orchestrator-model
           setting (``orchestrator_model_setting()``), read fresh from disk
           so a change made through the Settings UI applies on the very
           next turn.
        3. The built-in per-agent default (``_NVIDIA_AGENT_DEFAULTS``).
        4. The run's default ``model``.

        Case-insensitive on the agent name (env keys are normalized to
        uppercase at load).
        """
        key = (agent or "").strip().upper()
        if key and key in self.agent_models:
            return self.agent_models[key]
        if key == "ORCHESTRATOR":
            persisted = _persisted_model_for_backend("nvidia")
            if persisted:
                return persisted
        if key and key in _NVIDIA_AGENT_DEFAULTS:
            return _NVIDIA_AGENT_DEFAULTS[key]
        return self.model


_NVIDIA_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_NVIDIA_DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"

# Public aliases so other modules (e.g. key_check.py) reuse the SAME defaults
# instead of duplicating them and drifting.
NVIDIA_DEFAULT_BASE_URL = _NVIDIA_DEFAULT_BASE_URL
NVIDIA_DEFAULT_MODEL = _NVIDIA_DEFAULT_MODEL

# world-monitor-expansion: REAL default per-agent NVIDIA models. Until this,
# every agent silently ran on NVIDIA_MODEL because DOURMOUSE_MODEL_<AGENT>
# was a real, tested mechanism (test_config.py) nobody had populated with
# defaults. Still fully overridable — DOURMOUSE_MODEL_<AGENT> (agent_models,
# checked first in model_for_agent below) always wins, and any agent not
# listed here keeps running on NVIDIA_MODEL exactly as before.
#
# Every id below is a REAL model id, live-checked against
# integrate.api.nvidia.com/v1/models on 2026-08-29 with the real
# NVIDIA_API_KEY from this repo's .env — none invented, none merely
# "referenced somewhere else in the codebase" (that was the bug: several
# ids below WERE only that, and never actually existed on NVIDIA's side):
#   - orchestrator: "nvidia/llama-3.3-nemotron-super-49b-v1" was RETIRED —
#     confirmed absent from the live catalog (systematic backend
#     verification, world-monitor-expansion). NVIDIA no longer publishes a
#     smaller "Nemotron-Super" sibling to sit next to NVIDIA_MODEL's 120B
#     default, so the family-sibling reasoning from the original choice no
#     longer applies either. Replaced with "nvidia/nemotron-3-nano-30b-a3b"
#     — a real, live-confirmed id, and the current Nemotron-3 generation's
#     purpose-built small/fast tier ("nano", "a3b" = ~3B active params via
#     MoE), which fits the looping dispatch brain's "pays its model cost
#     every turn" requirement at least as well as the retired 49B pick did.
#   - research_info: "nvidia/llama-3.1-nemotron-ultra-253b-v1" — confirmed
#     present in the live catalog. Not a new choice: this is the exact
#     NVIDIA_MODEL atlas_lab.py and atlas_proposals.py already default to
#     for real ATLAS research work, reused here as the "strong model" tier.
#   - dev_coding: "nvidia/code-llama-70b" never existed as an NVIDIA-owned
#     id — confirmed absent from the live catalog. The real model is
#     Meta's CodeLlama, served under NVIDIA NIM as "meta/codellama-70b"
#     (confirmed present) — likely what was actually meant when this default
#     (and the matching .env.example line) was first written with the wrong
#     vendor prefix. NOTE: this covers only the general dev_coding
#     subagent's own reasoning calls. The dedicated code_nvidia /
#     code_deepseek / code_claude / code_codex / code_ollama agents do NOT
#     go through model_for_agent at all — each resolves its own model via
#     code_backends.py's load_backend() (its own per-backend default +
#     honest NOT CONFIGURED handling), so they are deliberately left OUT of
#     this dict rather than given a second, competing default.
#   - comms / mail / news / worldmonitor: "deepseek-ai/deepseek-v4-flash-0731"
#     — confirmed present in the live catalog. The ONE model id in this
#     entire codebase marked "verified live on the user's key"
#     (code_backends.py's _DEEPSEEK_NVIDIA_MODEL) before this pass, and
#     re-confirmed present now. These are the lighter, higher-volume
#     screens (chat/mail triage, headline summarizing, map pulse text) — a
#     cheap/fast "flash" model is the right tier, and reusing an id this
#     codebase has already verified beats guessing at another one.
#   - companion (Vision workspace chat panel, world-monitor-expansion): the
#     same "deepseek-ai/deepseek-v4-flash-0731" flash tier as comms/mail/
#     news/worldmonitor, for the same reasoning — a conversational,
#     casual-tone companion answering live in a chat panel is exactly the
#     "lighter, higher-volume" turn-taking shape those agents already run
#     on, not a heavy multi-step research/reasoning workload. It reuses the
#     one id this codebase has already verified live rather than guessing
#     at a new one for what is, underneath the persona, the same
#     delegate_task/delegate_parallel self-dispatch tool pair the
#     orchestrator runs (see general_roster.py's "companion" registration)
#     — only the system prompt and the model differ.
#
# CAVEAT carried forward honestly (systematic backend verification,
# 2026-08-29): /v1/models listing this key's real catalog works (HTTP 200),
# but every real chat-completion call against this key currently returns
# HTTP 403 "Authorization failed" — for every model tried, including ones
# confirmed present above. That is an external, account-side NVIDIA
# problem (the key can list models but cannot invoke inference right now),
# not a stale-id bug and not fixable in this code. "Present in the live
# catalog" below means exactly that — presence, not a successful live
# completion — until that 403 clears. See test_live_model_catalogs.py.
_NVIDIA_AGENT_DEFAULTS = {
    "ORCHESTRATOR": "nvidia/nemotron-3-nano-30b-a3b",
    "RESEARCH_INFO": "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "DEV_CODING": "meta/codellama-70b",
    "COMMS": "deepseek-ai/deepseek-v4-flash-0731",
    "MAIL": "deepseek-ai/deepseek-v4-flash-0731",
    "NEWS": "deepseek-ai/deepseek-v4-flash-0731",
    "WORLDMONITOR": "deepseek-ai/deepseek-v4-flash-0731",
    "COMPANION": "deepseek-ai/deepseek-v4-flash-0731",
}


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
    # 2026-09-14: same shape, one level down -- every DOURMOUSE_API_KEY_
    # <AGENT> env var maps that agent to its own NVIDIA account/key (e.g.
    # DOURMOUSE_API_KEY_CODE_CLAUDE=nvapi-... to bill a specific team
    # member's coding work to their own account). A DIFFERENT prefix than
    # the fallback pool's NVIDIA_API_KEY_2/_3/... (a numbered ACCOUNT
    # rotation, keyed by index, tried in order on rate-limit) -- this is a
    # FIXED assignment keyed by AGENT NAME, and the two compose rather than
    # conflict.
    agent_keys = {}
    key_prefix = "DOURMOUSE_API_KEY_"
    for env_name, value in os.environ.items():
        if env_name.startswith(key_prefix) and value.strip():
            agent_name = env_name[len(key_prefix):].strip().upper()
            if agent_name:
                agent_keys[agent_name] = value.strip()
    return NvidiaConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        fallback_model=fallback_model,
        agent_models=agent_models,
        agent_keys=agent_keys,
    )


# --------------------------------------------------------------------------- #
# v4.0 — Local LLM backend (Ollama). Keyless, OpenAI-compatible, default.
# --------------------------------------------------------------------------- #

# Ollama Cloud's real OpenAI-compatible endpoint.
#
# v13.8 (real, live-reproduced bug): this constant used to be a bare
# "https://ollama.com", copied from dispatch.py's own _ollama_cloud_config()
# on the assumption it was "an already-established, presumably-verified real
# endpoint" — true for THAT copy, but misleading here: dispatch.py's version
# feeds OllamaNativeClient, which calls Ollama's NATIVE `/api/chat` route
# directly (bare "https://ollama.com" + "/api/chat" is correct there). THIS
# constant feeds code_backends._run_openai_compat's plain `OpenAI(base_url=
# ...)` SDK client, which always appends "/chat/completions" to base_url
# itself — so it needs the "/v1" prefix Ollama's OpenAI-COMPATIBLE route
# actually lives under, exactly like _OLLAMA_DEFAULT_BASE_URL below already
# has for the local case. Without it, every real call through this path
# (code_backends.py, remote_server.py) posted to
# "https://ollama.com/chat/completions" — not a real API route — and
# ollama.com's own catch-all served back its plain marketing homepage HTML
# as if it were a normal response. Live-reproduced via an actual /all
# ALL HANDS run: the synthesis step's nvidia-then-ollama fallback failed
# with "ollama fallback (gpt-oss:20b API call failed: <!doctype html>...
# <title>Ollama</title>...)" — the exact homepage markup, not a JSON error.
# Kept as its own copy here (not imported from dispatch.py) because
# config.py sits BELOW dispatch.py in this codebase's import graph
# (dispatch.py imports FROM config.py) — importing the other direction
# would be circular. A future cleanup could have dispatch.py's copy import
# these instead of duplicating the literals; not attempted here to keep
# this fix's blast radius to the actual reported bug.
_OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
_OLLAMA_CLOUD_DEFAULT_MODEL = "gpt-oss:20b"
_OLLAMA_DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
# world-monitor-expansion (systematic backend verification, 2026-08-29): was
# "qwen3:8b" — same never-pulled-model bug class as DOURMOUSE_FAST_MODEL's
# old "qwen3:4b" default (see model_check.py's docstring). Confirmed live
# via `curl 127.0.0.1:11434/api/tags` on this machine: no "qwen3:*" model is
# installed at all, and the real .env doesn't set OLLAMA_MODEL to it either
# — this box's actual OLLAMA_MODEL=qwen2.5:7b. Replaced with "qwen2.5:7b":
# already the one Ollama model this codebase has verified live elsewhere
# (_OLLAMA_FAST_DISPATCH below), and re-confirmed live again in this pass
# (a real /api/chat call returned a real answer, not a 404).
_OLLAMA_DEFAULT_MODEL = "qwen2.5:7b"

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
    """Ollama (OpenAI-compatible) backend config — local by default, real
    Ollama Cloud when ``OLLAMA_API_KEY`` is set (v13.5, see
    ``load_ollama_config``'s own docstring for the live bug this fixes:
    that key used to be read from env and then silently discarded,
    ``api_key`` was unconditionally hardcoded to ``""`` here regardless).

    ``api_key`` is empty for a genuinely local daemon (nothing leaves the
    machine, Rule 2.6) and real for Ollama Cloud. Per-agent overrides come
    from ``DOURMOUSE_OLLAMA_MODEL_<AGENT>`` (mirroring the NVIDIA
    ``DOURMOUSE_MODEL_`` convention), resolved deterministically (Rule 2.8).
    """

    api_key: str = ""
    base_url: str = _OLLAMA_DEFAULT_BASE_URL
    model: str = _OLLAMA_DEFAULT_MODEL
    max_retries: int = 2
    retry_backoff: float = 0.5
    fallback_model: str = ""
    agent_models: dict[str, str] = field(default_factory=dict)
    # v13.5: True when this config is actually pointed at Ollama Cloud
    # (api_key set, no explicit local OLLAMA_BASE_URL override). Gates the
    # built-in _OLLAMA_FAST_DISPATCH orchestrator pin below — that pin
    # exists to trade a smaller LOCAL model for a faster first token on
    # THIS machine's own compute, a rationale that doesn't hold once the
    # request is already leaving the machine over the network, and the
    # pinned name ("qwen2.5:7b") is a local-only model unlikely to even
    # exist in Ollama Cloud's real hosted catalog.
    is_cloud: bool = False
    # Real, live-caught bug (2026-09-13), the same class _persisted_model_
    # for_backend's own docstring already names for the NVIDIA/Ollama
    # cross-backend case ("a model id from one backend silently applied
    # to a different backend's config... a real 404, the orchestrator
    # going dead with no visible error"), one layer deeper: a model id
    # the user persisted while running Ollama CLOUD (this exact machine
    # has "gpt-oss:20b" persisted as its chosen orchestrator model) is
    # NOT the same thing as a model that exists on the LOCAL daemon --
    # live-verified, this config's own force_local=True path (built
    # specifically so mail/docs/google_workspace/etc. never leave the
    # machine even with a real cloud key present) still had
    # model_for_agent("orchestrator") return that persisted CLOUD model
    # name, which the local Ollama server then 404'd on (it was never
    # pulled there — confirmed via a real GET /api/tags). True only on
    # the config load_ollama_config(force_local=True) builds; never set
    # by anything else, so no other caller's behavior changes.
    skip_persisted_orchestrator_choice: bool = False

    def model_for_agent(self, agent: str | None) -> str:
        """The Ollama model a specific subagent runs on (deterministic).

        Same precedence as NvidiaConfig.model_for_agent: env override, then
        (orchestrator only) the persisted orchestrator-model setting, then
        the built-in fast-dispatch default (local only — see ``is_cloud``
        above), then the run's default model.
        """
        key = (agent or "").strip().upper()
        if key and key in self.agent_models:
            return self.agent_models[key]
        if key == "ORCHESTRATOR" and not self.skip_persisted_orchestrator_choice:
            persisted = _persisted_model_for_backend("ollama")
            if persisted:
                return persisted
        if not self.is_cloud and key and key in _OLLAMA_FAST_DISPATCH:
            return _OLLAMA_FAST_DISPATCH[key]
        return self.model


def load_ollama_config(force_local: bool = False) -> OllamaConfig:
    """Build the Ollama backend config from env (defaults when unset).

    ``force_local=True`` (2026-09-13, real live-caught privacy bug): a
    plain ``OLLAMA_API_KEY`` in the environment makes this function
    return ``is_cloud=True`` for EVERY caller, with no per-agent
    exception — dispatch.py's own privacy-pinned-agent fallthrough
    (``_build_client``'s "mode == local" branch, used for mail/docs/
    google_workspace/etc.) reused that SAME global config, so setting a
    real Ollama Cloud key silently sent private Gmail/Drive/Calendar
    content to Ollama's cloud API the very next real request — verified
    live: ``_build_client(load_ollama_config(), forced_agent="mail")``
    returned an ``OllamaNativeClient`` with ``_is_cloud=True`` the moment
    a real key was stored, with no code change needed to trigger it.
    ``force_local=True`` is the fix's other half: same model/retry/
    fallback/agent-model resolution as the normal path, but the
    cloud-routing decision is skipped entirely — always local, always
    keyless, regardless of what's in the environment.

    v13.5 (live-diagnosed, explicit user request — "why is routing requests
    to qwen local instead of the Ollama api key"): OLLAMA_API_KEY was being
    read into this function's local scope by NOTHING — the returned
    OllamaConfig hardcoded ``api_key=""`` unconditionally, so a real,
    already-provisioned Ollama Cloud key sat in the user's own .env and was
    never once used. Confirmed this wasn't a case of Ollama Cloud being
    entirely unsupported by this codebase: dispatch.py's own
    ``_ollama_cloud_config()`` (built for a separate feature, the roster's
    agent-split backend) already does real Bearer-auth Ollama Cloud calls
    against ``https://ollama.com`` — this reuses that same real, already-
    established endpoint and default cloud model
    (``_OLLAMA_CLOUD_DEFAULT_MODEL``), not a newly-guessed one.

    Precedence: an explicit ``OLLAMA_BASE_URL`` always wins (forces local
    behavior even with a key present — an operator who set both clearly
    wants that exact endpoint). Otherwise, a set ``OLLAMA_API_KEY`` routes
    to Ollama Cloud. The MODEL used once cloud is active is
    ``OLLAMA_CLOUD_MODEL`` if set, else the known-real cloud default —
    deliberately NOT a silent fallback to ``OLLAMA_MODEL``: that env var is
    very often a small local-only model name (this codebase's own default
    is "qwen2.5:7b", not a real Ollama Cloud catalog entry), and sending it
    to the cloud endpoint would just trade one confusing wrong-model bug
    for another. Set OLLAMA_CLOUD_MODEL explicitly to pick a specific real
    cloud model.
    """
    api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
    explicit_base_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
    # 2026-09-14, user-directed: "Ollama should only use cloud models from
    # the api key, never local models." force_local used to unconditionally
    # strip a real, already-configured cloud key -- built for the privacy
    # case (mail/docs/study must never leave this machine even when a
    # cloud key exists elsewhere on it). Per this explicit instruction,
    # force_local no longer suppresses a real key: it only means "genuinely
    # local" in the one case that's unavoidable anyway -- no key configured
    # at all, so there is no cloud option to use.
    if force_local and not api_key:
        base_url = explicit_base_url or _OLLAMA_DEFAULT_BASE_URL
        is_cloud = False
    elif explicit_base_url:
        base_url = explicit_base_url
        is_cloud = False
    elif api_key:
        base_url = _OLLAMA_CLOUD_BASE_URL
        is_cloud = True
    else:
        base_url = _OLLAMA_DEFAULT_BASE_URL
        is_cloud = False
    if is_cloud:
        model = os.environ.get("OLLAMA_CLOUD_MODEL", "").strip() or _OLLAMA_CLOUD_DEFAULT_MODEL
    else:
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
        api_key=api_key if is_cloud else "",
        base_url=base_url,
        model=model,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        fallback_model=fallback_model,
        agent_models=agent_models,
        is_cloud=is_cloud,
        skip_persisted_orchestrator_choice=force_local,
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
        looping brain). The persisted orchestrator-model setting (see
        NvidiaConfig.model_for_agent) still applies here — same precedence,
        env override first, persisted setting second, ``self.model`` last.
        """
        key = (agent or "").strip().upper()
        if key and key in self.agent_models:
            return self.agent_models[key]
        if key == "ORCHESTRATOR":
            persisted = _persisted_model_for_backend("omniroute")
            if persisted:
                return persisted
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


# 2026-09-14, user-directed: a real third HOME composer backend option,
# https://github.com/tashfeenahmed/freellmapi -- a self-hosted,
# OpenAI-compatible router that aggregates ~34 providers' free tiers
# behind one local endpoint (real project, not a guess: its own README
# documents `base_url="http://localhost:3001/v1"`, a unified
# `freellmapi-...` Bearer key from its own Keys page, and
# `model="auto"` letting the router itself pick a working free
# provider). Same shape as OmniRouteConfig immediately above (a
# self-hosted OpenAI-compatible gateway) -- the difference is FreeLLMAPI
# issues a real per-install unified key from its own web UI rather than
# being fully keyless, so this carries that key instead of hardcoding
# an empty string.
_FREELLMAPI_DEFAULT_BASE_URL = "http://localhost:3001/v1"
_FREELLMAPI_DEFAULT_MODEL = "auto"

FREELLMAPI_DEFAULT_BASE_URL = _FREELLMAPI_DEFAULT_BASE_URL
FREELLMAPI_DEFAULT_MODEL = _FREELLMAPI_DEFAULT_MODEL


@dataclass(frozen=True)
class FreeLLMAPIConfig:
    """FreeLLMAPI self-hosted free-tier router backend (v14).

    Requires a real unified key (``FREELLMAPI_API_KEY``) from the
    user's own running FreeLLMAPI instance's Keys page -- unlike
    OmniRoute this is not keyless, since FreeLLMAPI gates its endpoint
    on that per-install key even though every provider behind it is
    itself free. Per-agent overrides come from
    ``DOURMOUSE_FREELLMAPI_MODEL_<AGENT>`` (mirroring the NVIDIA/Ollama/
    OmniRoute conventions), resolved deterministically (Rule 2.8).

    Honesty (Rule 2.2): this is a routing layer over real third-party
    free tiers -- failures (rate limits, a provider being down) surface
    as real errors, never a fabricated success.
    """

    api_key: str = ""
    base_url: str = _FREELLMAPI_DEFAULT_BASE_URL
    model: str = _FREELLMAPI_DEFAULT_MODEL
    max_retries: int = 2
    retry_backoff: float = 0.5
    fallback_model: str = ""
    agent_models: dict[str, str] = field(default_factory=dict)

    def model_for_agent(self, agent: str | None) -> str:
        """The FreeLLMAPI model a specific subagent runs on (deterministic).

        Same precedence as OmniRouteConfig.model_for_agent: an explicit
        per-agent override first, the persisted orchestrator-model
        setting second (ORCHESTRATOR only), ``self.model`` last.
        """
        key = (agent or "").strip().upper()
        if key and key in self.agent_models:
            return self.agent_models[key]
        if key == "ORCHESTRATOR":
            persisted = _persisted_model_for_backend("freellmapi")
            if persisted:
                return persisted
        return self.model


def load_freellmapi_config() -> FreeLLMAPIConfig:
    """Build the FreeLLMAPI backend config from env (defaults when unset)."""
    api_key = os.environ.get("FREELLMAPI_API_KEY", "").strip()
    base_url = os.environ.get(
        "FREELLMAPI_BASE_URL", _FREELLMAPI_DEFAULT_BASE_URL
    ).strip() or _FREELLMAPI_DEFAULT_BASE_URL
    model = os.environ.get(
        "FREELLMAPI_MODEL", _FREELLMAPI_DEFAULT_MODEL
    ).strip() or _FREELLMAPI_DEFAULT_MODEL
    max_retries = int(os.environ.get("FREELLMAPI_MAX_RETRIES", "2"))
    retry_backoff = float(os.environ.get("FREELLMAPI_RETRY_BACKOFF", "0.5"))
    fallback_model = os.environ.get("FREELLMAPI_FALLBACK_MODEL", "").strip()
    agent_models = {}
    prefix = "DOURMOUSE_FREELLMAPI_MODEL_"
    for env_name, value in os.environ.items():
        if env_name.startswith(prefix) and value.strip():
            agent_name = env_name[len(prefix):].strip().upper()
            if agent_name:
                agent_models[agent_name] = value.strip()
    return FreeLLMAPIConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        fallback_model=fallback_model,
        agent_models=agent_models,
    )


def freellmapi_available(timeout: float = 1.0) -> bool:
    """Probe the local FreeLLMAPI gateway (deterministic, Rule 2.8).

    Honors ``FREELLMAPI_BASE_URL`` so tests and non-default deploys are
    never hardcoded to the default port. A real GET to the gateway's
    own root is enough to know it's up; never fabricates availability.
    """
    import urllib.error
    import urllib.request

    base_url = os.environ.get(
        "FREELLMAPI_BASE_URL", _FREELLMAPI_DEFAULT_BASE_URL
    ).strip() or _FREELLMAPI_DEFAULT_BASE_URL
    root = base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url
    try:
        with urllib.request.urlopen(root, timeout=timeout) as resp:  # noqa: S310
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return False


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
# world-monitor-expansion — real backend identity for the console's per-
# response model/local indicator (UX pass, live-demo feedback item 1).
# --------------------------------------------------------------------------- #

def backend_identity(config: Any) -> tuple[str, bool]:
    """(backend_name, is_local) for a loaded LLM backend config object.

    ``is_local`` is True ONLY for a genuinely local Ollama daemon — self-
    hosted, keyless, nothing leaves this machine (Rule 2.6). Every other
    backend, including OmniRoute (its gateway process happens to listen
    on 127.0.0.1, but it exists to forward requests to REMOTE free-tier
    providers — see ``OmniRouteConfig``'s own docstring — so it is
    honestly cloud, not local), reports False.

    Real bug found live-testing this session (feature sweep, 2026-09-07):
    this used to return True for EVERY ``OllamaConfig``, unconditionally
    — including one built by ``load_ollama_config()`` for real Ollama
    Cloud (``OLLAMA_API_KEY`` set, ``base_url`` = ollama.com,
    ``is_cloud=True``, a field that already existed specifically to
    answer this question and was simply never checked here). Confirmed
    live via the server's own ``/api/backend`` endpoint reporting
    ``base_url: "https://ollama.com/v1"`` while every "brain" event for
    that same session claimed ``local: true`` — privacy-relevant, since
    every agent this labeled local-only NEVER meant "route to whatever
    OllamaConfig happens to be configured", it meant "never leave this
    machine".

    Classification is the config object's real TYPE — exactly the object
    ``_build_client`` (dispatch.py) already switches on to decide whether
    to build the native Ollama client or a generic OpenAI-compatible one —
    never a guess from a model-name string pattern (a "qwen3:8b"-shaped
    name is an Ollama convention, but nothing stops an operator from
    naming a DOURMOUSE_MODEL_<AGENT> override that way on another
    backend, so the string alone is not proof of anything).

    ``config`` is ``None`` for the rare caller that supplies its own
    ``client`` without a config (mostly tests) — reported as
    ``("unknown", False)`` rather than guessed.
    """
    if isinstance(config, OllamaConfig):
        return "ollama", not config.is_cloud
    if isinstance(config, NvidiaConfig):
        return "nvidia", False
    if isinstance(config, OmniRouteConfig):
        return "omniroute", False
    if isinstance(config, FreeLLMAPIConfig):
        return "freellmapi", False
    return "unknown", False


# --------------------------------------------------------------------------- #
# world-monitor-expansion — persisted orchestrator model setting (backend
# half; a Settings UI is being built separately to call this).
#
# This is a RUNTIME-CHANGEABLE setting, not just another env var: a user
# picks a model through /api/settings/orchestrator-model (webui.py) and it
# applies on the orchestrator's next turn, no process restart. It reuses the
# EXACT storage format firstrun.py already established (a flat KEY=VALUE
# ``.env`` file at ``user_env_path()``, merge-on-write) rather than inventing
# a second settings format — the two read/write functions below duplicate
# firstrun.save_config's small merge-and-write loop instead of importing it,
# because firstrun.save_config's key allowlist is deliberately narrow (only
# the first-run wizard's own keys) and this key does not belong in it.
#
# Read is done FRESH FROM DISK on every call (not from os.environ, which is
# only populated once at process start by the ``load_dotenv`` calls at the
# top of this module) — that is what makes the setting apply live. The
# tradeoff, same one firstrun.py accepts for the same file: a disk read per
# call to model_for_agent("orchestrator"). That runs once per orchestrator
# turn, not per token, so the cost is negligible.
# --------------------------------------------------------------------------- #

#: The key this setting is stored under in the user's config .env file.
#: Deliberately distinct from DOURMOUSE_MODEL_ORCHESTRATOR (the existing
#: env-only per-agent override, which still takes precedence over this —
#: see model_for_agent's docstrings above) so the two mechanisms never
#: collide or silently overwrite one another.
ORCHESTRATOR_MODEL_SETTING_KEY = "DOURMOUSE_ORCHESTRATOR_MODEL"

#: Real bug found and fixed live: the model string alone is not enough —
#: "qwen3:8b" is meaningless to NvidiaConfig and "nvidia/llama-3.3-..." is
#: meaningless to OllamaConfig, but the old code applied whatever was
#: persisted to WHICHEVER backend config object happened to be active at
#: read time, with no cross-check. Concretely: the Settings picker was used
#: while testing the "ollama" chip, persisting model="qwen3:8b"; later the
#: active backend was switched to nvidia (DOURMOUSE_LLM_BACKEND=nvidia) —
#: NvidiaConfig then sent "qwen3:8b" to NVIDIA's real API, which correctly
#: 404'd on a model id it has never heard of, and the orchestrator went
#: silently dead. This key records WHICH backend the persisted model
#: belongs to, so each backend's model_for_agent can refuse to apply a
#: persisted value that isn't its own (see _persisted_model_for_backend
#: below) rather than trusting a bare string blindly.
ORCHESTRATOR_BACKEND_SETTING_KEY = "DOURMOUSE_ORCHESTRATOR_BACKEND"


def _read_user_config_file() -> dict[str, str]:
    """Parse the user's config .env file into a dict. Never raises."""
    path = user_env_path()
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    except OSError:
        return {}
    return values


def orchestrator_model_setting() -> str:
    """The persisted orchestrator model choice, read fresh from disk.

    Returns "" (honest empty, never a guess) when nothing has been saved
    yet — callers fall through to whatever default applies next. See
    ``ORCHESTRATOR_MODEL_SETTING_KEY`` for the storage key.

    NOTE: this alone is not safe to apply to an arbitrary backend config —
    see ``orchestrator_backend_setting()`` and ``_persisted_model_for_backend``
    below. Kept for backward compatibility (webui.py's GET endpoint still
    reports the raw persisted model regardless of backend, honestly, as
    "persisted" rather than "active").
    """
    return _read_user_config_file().get(ORCHESTRATOR_MODEL_SETTING_KEY, "").strip()


def orchestrator_backend_setting() -> str:
    """Which backend ('nvidia'/'ollama'/'omniroute') the persisted
    orchestrator model actually belongs to. Empty when unset — a value
    saved before this fix existed (or written by hand) has no backend tag,
    and is treated as untrustworthy by ``_persisted_model_for_backend``
    rather than guessed at.
    """
    return _read_user_config_file().get(ORCHESTRATOR_BACKEND_SETTING_KEY, "").strip().lower()


def _persisted_model_for_backend(backend_name: str) -> str:
    """The persisted orchestrator model, but ONLY if it was actually saved
    FOR this backend. Real bug this exists to prevent: a model id from one
    backend (e.g. Ollama's "qwen3:8b") silently applied to a totally
    different backend's config (NVIDIA), which then sends it to that
    backend's real API and gets a real 404 — the orchestrator going dead
    with no visible error to the user. Returns "" (never a guess) when the
    persisted backend tag doesn't match, is missing (untagged legacy
    value), or nothing is persisted at all.
    """
    model = orchestrator_model_setting()
    if not model:
        return ""
    saved_backend = orchestrator_backend_setting()
    if saved_backend != backend_name:
        return ""
    return model


def save_orchestrator_model_setting(model: str, backend: str = "") -> dict[str, Any]:
    """Persist the orchestrator's chosen model (and which backend it
    belongs to) to the user's config file.

    ``backend`` should be the real backend identity ('nvidia'/'ollama'/
    'omniroute') the model was resolved against — the webui handler always
    has this (it's how it resolved the model in the first place via the
    backend catalog). Left empty only for a manual raw-model-id override
    with no known backend — in that case the value is persisted but
    ``_persisted_model_for_backend`` will never apply it to any backend
    automatically (an untagged value is untrustworthy by the same rule
    that protects against the cross-backend bug), so
    ``DOURMOUSE_MODEL_ORCHESTRATOR`` (the plain env override) is the right
    tool for a genuinely backend-agnostic manual pin.

    Merges with whatever is already in that file (same rule
    firstrun.save_config follows) so this never clobbers other saved
    settings — including a first-run-wizard NVIDIA_API_KEY sitting right
    next to it. Returns ``{"ok": False, "detail": ...}`` on a bad input or
    a write failure — never raises, so a webui handler can send it straight
    back as JSON.
    """
    model = (model or "").strip()
    backend = (backend or "").strip().lower()
    if not model:
        return {"ok": False, "detail": "no model given"}
    path = user_env_path()
    try:
        user_config_dir().mkdir(parents=True, exist_ok=True)
        existing = _read_user_config_file()
        existing[ORCHESTRATOR_MODEL_SETTING_KEY] = model
        if backend:
            existing[ORCHESTRATOR_BACKEND_SETTING_KEY] = backend
        else:
            # A raw manual override with no known backend invalidates any
            # PREVIOUS backend tag too — an untagged model must never keep
            # riding a stale tag from an earlier, different save.
            existing.pop(ORCHESTRATOR_BACKEND_SETTING_KEY, None)
        body = [
            "# Dourmouse configuration — written by first-run setup / settings.",
            "# This file holds credentials. Keep it to yourself; it is never",
            "# bundled into a build or uploaded anywhere.",
            "",
        ]
        body += [f"{k}={v}" for k, v in sorted(existing.items())]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"could not write config: {exc}"}
    return {"ok": True, "detail": "saved", "model": model, "backend": backend or None, "path": str(path)}


#: The user's own explicit ask: Claude is the default front-end for every
#: tab; behind the scenes, non-heavy work splits across Ollama/Gemini
#: (see dispatch.py's _agent_split_backend). Deliberately a DIFFERENT key
#: than ORCHESTRATOR_BACKEND_SETTING_KEY above — that one already means
#: "which backend a persisted orchestrator MODEL belongs to" and is
#: loaded into real os.environ via load_dotenv(); reusing it for this
#: unrelated on/off flag would have silently collided the two features
#: (a user picking "ollama" as their orchestrator model would have set
#: this same key to "ollama", an unrecognized value here — caught before
#: shipping, not live).
CLAUDE_FRONT_MODE_SETTING_KEY = "DOURMOUSE_CLAUDE_FRONT_MODE"


def claude_front_mode_setting() -> str:
    """The persisted Claude-front-mode choice, read fresh from disk.
    "" (nothing saved) means the real default applies — ON — this is
    an opt-OUT setting, not opt-in, per the user's explicit ask that
    Claude-front be the default. "off"/"0"/"false"/"no" means the user
    explicitly disabled it; anything else truthy is an explicit ON
    (redundant with the default, but a real signal the user affirmed it
    after having turned it off before).
    """
    return _read_user_config_file().get(CLAUDE_FRONT_MODE_SETTING_KEY, "").strip().lower()


def claude_front_mode_enabled() -> bool:
    """True unless the user has explicitly turned it off. Real default-
    on/opt-out semantics, not default-off/opt-in — see the setting key's
    own docstring for why."""
    return claude_front_mode_setting() not in ("off", "0", "false", "no")


def save_claude_front_mode_setting(enabled: bool) -> dict[str, Any]:
    """Persist the Claude-front-mode on/off choice. Same merge-with-
    existing-file discipline as save_orchestrator_model_setting above —
    never clobbers unrelated saved settings."""
    path = user_env_path()
    try:
        user_config_dir().mkdir(parents=True, exist_ok=True)
        existing = _read_user_config_file()
        existing[CLAUDE_FRONT_MODE_SETTING_KEY] = "on" if enabled else "off"
        body = [
            "# Dourmouse configuration — written by first-run setup / settings.",
            "# This file holds credentials. Keep it to yourself; it is never",
            "# bundled into a build or uploaded anywhere.",
            "",
        ]
        body += [f"{k}={v}" for k, v in sorted(existing.items())]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"could not write config: {exc}"}
    return {"ok": True, "detail": "saved", "enabled": enabled, "path": str(path)}


# --------------------------------------------------------------------------- #
# Google OAuth full scopes (Gmail/Calendar/Drive via OAuth, not just identity)
# --------------------------------------------------------------------------- #
#
# Real friction, live-caught (2026-09-13): turning this on required manually
# editing .env and restarting the server -- undiscoverable unless you already
# knew the env var existed, since nothing in the UI ever mentioned it. Same
# three-function shape as Claude-front-mode above (setting/enabled/save), so
# it's a real toggle read fresh from disk on every request, no restart.

GOOGLE_OAUTH_FULL_SCOPES_SETTING_KEY = "GOOGLE_OAUTH_FULL_SCOPES"


def google_oauth_full_scopes_setting() -> str:
    """The persisted choice, read fresh from disk. An explicit env var
    (an operator who set it directly in their own real .env) always
    wins over this setting -- see google_oauth_full_scopes_enabled."""
    return _read_user_config_file().get(GOOGLE_OAUTH_FULL_SCOPES_SETTING_KEY, "").strip().lower()


def google_oauth_full_scopes_enabled() -> bool:
    """True if EITHER a real environment variable OR the persisted
    Settings toggle turns this on. Off by default (Google's restricted
    scopes 500 on an unverified OAuth app -- see google_auth.py's own
    requested_scopes docstring), so this is opt-in, unlike Claude-front-
    mode above."""
    env_val = os.environ.get("GOOGLE_OAUTH_FULL_SCOPES", "").strip().lower()
    if env_val in ("1", "true", "yes", "on"):
        return True
    return google_oauth_full_scopes_setting() in ("1", "true", "yes", "on")


def save_google_oauth_full_scopes_setting(enabled: bool) -> dict[str, Any]:
    """Persist the full-scopes on/off choice. Same merge-with-existing-
    file discipline as every other save_*_setting here."""
    path = user_env_path()
    try:
        user_config_dir().mkdir(parents=True, exist_ok=True)
        existing = _read_user_config_file()
        existing[GOOGLE_OAUTH_FULL_SCOPES_SETTING_KEY] = "on" if enabled else "off"
        body = [
            "# Dourmouse configuration — written by first-run setup / settings.",
            "# This file holds credentials. Keep it to yourself; it is never",
            "# bundled into a build or uploaded anywhere.",
            "",
        ]
        body += [f"{k}={v}" for k, v in sorted(existing.items())]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"could not write config: {exc}"}
    return {"ok": True, "detail": "saved", "enabled": enabled, "path": str(path)}


# --------------------------------------------------------------------------- #
# v13 — Grounded Mode: user-controllable "must actually use a tool" strictness
# --------------------------------------------------------------------------- #
#
# Real gap this closes, found live on 29 August 2026 by actually typing into
# the running console rather than reading code: asked through RESEARCH (no
# forced_agent pinned, an ordinary conversational turn), the orchestrator
# answered "the current version of Python 3 is 3.11" — wrong for the date,
# and answered after 56.6 real seconds with ZERO tool calls, despite the
# RESEARCH screen's own UI text promising "searches the live web and cites
# sources." dispatch.py already HAS an honest fabrication-guard for this
# shape of problem (the "[DOURMOUSE: plan step(s) not executed via tools]"
# caveat) — but it only fires `if plan:`, and a plain conversational turn
# routed at a specific agent frequently has no `plan` object at all, so
# nothing verifies a "research" answer actually used a real tool before
# being presented as one.
#
# Deliberately NOT auto-enabled and NOT scoped to specific agent names: a
# blanket "you used zero tools, are you sure?" nudge fired unconditionally
# would produce real false positives on every genuinely tool-free
# conversational reply sharing this same code path (not just RESEARCH) —
# "what's 2+2" pinned to any agent doesn't need a tool call, and nudging it
# anyway wastes a real LLM round-trip for nothing. Grounded Mode is instead
# a user-facing, off-by-default SETTING (mirrors the orchestrator-model
# picker's own persisted-setting pattern above): when ON, a focus_agent
# turn whose pinned agent genuinely HAS at least one real tool available
# but returns a text-only final answer with ZERO tool_use events gets ONE
# honest nudge asking the model to either call a tool now or say plainly
# why none was needed (see dispatch.py's own use of this flag for the exact
# mechanism) — never a silent block, never a fabricated retry, just the
# same "give it one honest chance to correct itself" pattern the existing
# plan-reminder mechanism already uses.

GROUNDED_MODE_SETTING_KEY = "DOURMOUSE_GROUNDED_MODE"


def grounded_mode_enabled() -> bool:
    """Whether Grounded Mode is currently on, read fresh from disk (same
    live-without-restart contract as orchestrator_model_setting()). Off by
    default — an explicit "1"/"true"/"yes"/"on" (case-insensitive) is the
    only way to enable it; anything else, including unset, is honestly off.
    """
    raw = _read_user_config_file().get(GROUNDED_MODE_SETTING_KEY, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def save_grounded_mode_setting(enabled: bool) -> dict[str, Any]:
    """Persist the Grounded Mode toggle. Same merge-on-write .env file as
    save_orchestrator_model_setting() — never clobbers other saved
    settings. Never raises; a write failure is reported honestly."""
    path = user_env_path()
    try:
        user_config_dir().mkdir(parents=True, exist_ok=True)
        existing = _read_user_config_file()
        existing[GROUNDED_MODE_SETTING_KEY] = "1" if enabled else "0"
        body = [
            "# Dourmouse configuration — written by first-run setup / settings.",
            "# This file holds credentials. Keep it to yourself; it is never",
            "# bundled into a build or uploaded anywhere.",
            "",
        ]
        body += [f"{k}={v}" for k, v in sorted(existing.items())]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"could not write config: {exc}"}
    return {"ok": True, "detail": "saved", "enabled": enabled, "path": str(path)}


# 2026-09-14 (live-caught, user-directed): "approval keeps failing, remove
# the need for approval, make this a toggle in settings" — a real, live
# gap converges here with an explicit ask. Claude Front Mode turns
# (mcp_bridge.py's _handle_tools_call) hardcode confirmation_gate=None, so
# a REQUIRES_CONFIRMATION tool called that way is refused outright rather
# than paused — a real architectural gap, but the user's own practical
# fix is simpler than re-architecting that bridge: a real, explicit,
# off-by-default toggle that skips confirmation entirely for every gated
# tool everywhere, not just the Claude Front Mode case. Same settings
# pattern as GROUNDED_MODE above, byte for byte.
AUTO_APPROVE_SETTING_KEY = "DOURMOUSE_AUTO_APPROVE"


def auto_approve_enabled() -> bool:
    """Whether every confirmation gate should be skipped automatically,
    read fresh from disk (same live-without-restart contract as
    grounded_mode_enabled()). Off by default — an explicit "1"/"true"/
    "yes"/"on" (case-insensitive) is the only way to enable it."""
    raw = _read_user_config_file().get(AUTO_APPROVE_SETTING_KEY, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def save_auto_approve_setting(enabled: bool) -> dict[str, Any]:
    """Persist the auto-approve toggle. Same merge-on-write .env file as
    save_grounded_mode_setting() — never clobbers other saved settings.
    Never raises; a write failure is reported honestly."""
    path = user_env_path()
    try:
        user_config_dir().mkdir(parents=True, exist_ok=True)
        existing = _read_user_config_file()
        existing[AUTO_APPROVE_SETTING_KEY] = "1" if enabled else "0"
        body = [
            "# Dourmouse configuration — written by first-run setup / settings.",
            "# This file holds credentials. Keep it to yourself; it is never",
            "# bundled into a build or uploaded anywhere.",
            "",
        ]
        body += [f"{k}={v}" for k, v in sorted(existing.items())]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"could not write config: {exc}"}
    return {"ok": True, "detail": "saved", "enabled": enabled, "path": str(path)}


# v14 (user-directed, 2026-09-08): "Consider adding a 'dry run' mode
# where it shows what would be clicked without actually clicking" — a
# real safety feature for app_control.py's real AppleScript actions
# (activate_app, quit_app, send_keystrokes, press_key, click_menu_item).
# Same settings pattern as GROUNDED_MODE above. Opt-IN, off by default:
# app-control actions already require per-action human confirmation
# (Permission.REQUIRES_CONFIRMATION), so dry-run is an extra safety net
# for someone who wants to preview a whole sequence before any of it
# runs for real, not a replacement for that existing gate.
APP_CONTROL_DRY_RUN_SETTING_KEY = "DOURMOUSE_APP_CONTROL_DRY_RUN"


def app_control_dry_run_setting() -> bool:
    """Whether App Control Dry Run is currently on, read fresh from disk.
    Off by default — an explicit "1"/"true"/"yes"/"on" (case-insensitive)
    is the only way to enable it; anything else, including unset, is
    honestly off."""
    raw = _read_user_config_file().get(APP_CONTROL_DRY_RUN_SETTING_KEY, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def app_control_dry_run_enabled() -> bool:
    """Public name used by the actual dry-run check site (general_roster.py's
    _app_control_dry_run) — a thin alias over app_control_dry_run_setting()
    so the call site reads as "is dry run enabled" rather than "what does
    the raw setting say", matching grounded_mode_enabled()'s own naming."""
    return app_control_dry_run_setting()


def save_app_control_dry_run_setting(enabled: bool) -> dict[str, Any]:
    """Persist the App Control Dry Run toggle. Same merge-on-write .env
    file as save_grounded_mode_setting() — never clobbers other saved
    settings. Never raises; a write failure is reported honestly."""
    path = user_env_path()
    try:
        user_config_dir().mkdir(parents=True, exist_ok=True)
        existing = _read_user_config_file()
        existing[APP_CONTROL_DRY_RUN_SETTING_KEY] = "1" if enabled else "0"
        body = [
            "# Dourmouse configuration — written by first-run setup / settings.",
            "# This file holds credentials. Keep it to yourself; it is never",
            "# bundled into a build or uploaded anywhere.",
            "",
        ]
        body += [f"{k}={v}" for k, v in sorted(existing.items())]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"could not write config: {exc}"}
    return {"ok": True, "detail": "saved", "enabled": enabled, "path": str(path)}


# v14 (user-directed, 2026-09-12): "commercial, for other people to use" —
# a real, distributable desktop app means each install is a stranger's
# own machine, not this developer's. Sharing ONE Ollama Cloud / Gemini
# API key across every installer would mean every stranger's usage bills
# and rate-limits against the SAME account — a real abuse and billing
# problem, not a hypothetical one. BYOK (bring your own key) is the
# correct, standard shape for a distributed AI app (same model as any
# "paste your OpenAI key" tool) — this is the real Settings-UI backend
# for it, replacing "open .env in a text editor" with an actual form.
# Deliberately ONE generic pair of functions rather than one hand-copied
# pair per key name — every current and future BYOK key (Ollama, Gemini,
# whatever comes next) reads/writes through the exact same real path,
# so the two can never quietly drift in behavior.
#: Keys this app currently lets a user BYOK through Settings. A real
#: allowlist (Rule 2.8 — never write an arbitrary env-var name a request
#: happens to name) rather than accepting any key name a POST body sends.
BYOK_API_KEY_NAMES = ("OLLAMA_API_KEY", "GEMINI_API_KEY")


def api_key_setting(env_name: str) -> str:
    """The current value of one BYOK key, read fresh from disk — same
    live-without-restart contract every other setting in this module
    already has. Empty string, never None, when unset."""
    if env_name not in BYOK_API_KEY_NAMES:
        return ""
    return _read_user_config_file().get(env_name, "").strip()


def save_api_key_setting(env_name: str, value: str) -> dict[str, Any]:
    """Persist one BYOK key. Same merge-on-write .env file as every other
    save_*_setting above — never clobbers other saved settings. An empty
    ``value`` clears the key (the honest way to "remove my key" — never
    a special sentinel string). Never raises; a write failure is
    reported honestly."""
    if env_name not in BYOK_API_KEY_NAMES:
        return {"ok": False, "detail": f"{env_name!r} is not a BYOK key this app accepts"}
    path = user_env_path()
    try:
        user_config_dir().mkdir(parents=True, exist_ok=True)
        existing = _read_user_config_file()
        value = value.strip()
        if value:
            existing[env_name] = value
        else:
            existing.pop(env_name, None)
        body = [
            "# Dourmouse configuration — written by first-run setup / settings.",
            "# This file holds credentials. Keep it to yourself; it is never",
            "# bundled into a build or uploaded anywhere.",
            "",
        ]
        body += [f"{k}={v}" for k, v in sorted(existing.items())]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"could not write config: {exc}"}
    return {"ok": True, "detail": "saved" if value else "cleared", "configured": bool(value), "path": str(path)}


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
    to a ONE-completion, no-tool-loop path with a compact system prompt
    (no 21-agent roster) instead of the full agentic loop. Default on. Set
    to 0/off to disable — every turn (even "2+2") gets the full loop.
    """
    raw = value if value is not None else os.environ.get("DOURMOUSE_FAST_LANE", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def fast_lane_model_swap_enabled(value: str | None = None) -> bool:
    """DOURMOUSE_FAST_LANE_MODEL_SWAP: whether the fast lane is ALSO allowed
    to swap the model itself to ``fast_lane_model()`` (a smaller model name,
    default "qwen2.5:7b" on Ollama) instead of answering on the turn's own
    real resolved brain model. Default on, matching the historical behavior
    (dispatch._fast_lane_model_is_servable's docstring: "get the first token
    out sooner" on a local Ollama backend, where a smaller model really is
    faster).

    v13.5 (live-diagnosed, explicit user request): this is a SEPARATE knob
    from ``fast_lane_enabled`` above on purpose. The user's actual complaint
    ("why is routing requests to qwen local instead of the Ollama api key or
    claude code") traced to this exact swap: DOURMOUSE_FAST_MODEL defaults
    to "qwen2.5:7b" and _fast_lane_model_is_servable() treats ANY Ollama-
    backed client as eligible for it, so every simple chat turn got
    hardcoded to that one small model regardless of what OLLAMA_MODEL (or a
    real Ollama Cloud model, once OLLAMA_API_KEY is actually wired — see
    load_ollama_config's own OLLAMA_API_KEY comment) was actually configured
    to answer on. Turning this off keeps the OTHER fast-lane benefit (no
    tool loop, compact prompt, still fast) while every turn answers on the
    real configured model, not a silent swap-in. Deliberately a new opt-out
    rather than changing fast_lane_model_is_servable's own default for
    everyone: other deployments (a genuinely local-only setup with no cloud
    key) still benefit from the documented speed win unless they ask not to.
    """
    raw = value if value is not None else os.environ.get("DOURMOUSE_FAST_LANE_MODEL_SWAP", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def fast_lane_model() -> str:
    """DOURMOUSE_FAST_MODEL: the small model used for simple responses.

    world-monitor-expansion (systematic backend verification, 2026-08-29):
    the hardcoded fallback here was still "qwen3:4b" — the EXACT model id
    model_check.py's own regression test (test_model_check.py,
    "test_detects_the_qwen3_4b_mismatch") documents as the original
    never-pulled-model incident that module exists to catch. The incident
    was only ever patched by setting DOURMOUSE_FAST_MODEL in .env; this
    fallback default itself was never fixed, so a machine with the env var
    unset (or a fresh install) would hit the identical bug again. Default
    is now "qwen2.5:7b" — confirmed installed on this machine and
    re-verified live in this pass via a real /api/chat call.
    """
    return os.environ.get("DOURMOUSE_FAST_MODEL", "qwen2.5:7b").strip() or "qwen2.5:7b"


def fast_lane_server_enabled(value: str | None = None) -> bool:
    """DOURMOUSE_FAST_LANE_SERVER: route pure-chat fast-lane turns to the
    compute node (Dell) when it is online, falling back to the local fast
    model on ANY failure. Default on — the real gate is that the operator
    explicitly set DOURMOUSE_SERVER_URL (a dead/unconfigured node must
    never add probe latency to every reply).
    """
    raw = value if value is not None else os.environ.get("DOURMOUSE_FAST_LANE_SERVER", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def brief_mode_enabled(value: str | None = None) -> bool:
    """DOURMOUSE_BRIEF: hold LOOKUP-shaped turns to a short answer.

    A lookup ("what is X", "how do I Y", "how much free disk space") is
    answered correctly today and then padded with sections the question
    never asked for. Default on. Set to 0/off to restore the old length.
    """
    raw = value if value is not None else os.environ.get("DOURMOUSE_BRIEF", "1")
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


# --------------------------------------------------------------------------- #
# Google AI Studio (Gemini) — third real LLM backend (2026-09-04).
#
# Same dataclass + loader shape as the NVIDIA / Ollama / OmniRoute blocks
# above; the transport lives in dourmouse/gemini_backend.py, which imports
# the names below so there is exactly one definition of the model id and
# endpoint (Integration Rule 7).
#
# Two deliberate, documented divergences from those blocks — both because
# copying a field that nothing implements would advertise behaviour that
# does not exist (Rule 2.2):
#   * no ``max_retries`` / ``retry_backoff`` / ``fallback_model``. Those
#     exist on the other configs because ``dispatch._call_with_retry``
#     actually reads them. Nothing retries the Gemini path today.
#   * ``load_gemini_config()`` does NOT raise on a missing key, unlike
#     ``load_nvidia_config``. It mirrors ``load_ollama_config`` instead and
#     returns ``api_key=""``, because ``gemini_backend.gemini_status()`` is
#     the honest gate here and a status PROBE must be able to report "no
#     key" without catching an exception. Callers that need a key get the
#     NOT CONFIGURED error from ``gemini_backend._require_key`` at the
#     moment a request would otherwise be sent.
#
# Not wired into ``load_llm_config`` / ``llm_backend`` / ``backend_identity``
# — this pass adds the backend and its config, not a fourth value for
# DOURMOUSE_LLM_BACKEND. ``backend_identity(GeminiConfig(...))`` therefore
# honestly returns ("unknown", False) rather than a wrong guess.
# --------------------------------------------------------------------------- #

_GEMINI_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Verified against Google's own current published docs on 2026-09-04
# (ai.google.dev/gemini-api/docs/models and the gemini-3.5-flash model
# page), NOT against a live API call — this machine has no Gemini key, so
# ListModels could not be run. What the docs said, recorded so the next
# person can tell evidence from assumption:
#   * "gemini-2.0-flash" is listed as SHUT DOWN. It was this task's
#     suggested default and would have 404'd on the first real request.
#     Not used.
#   * "gemini-3.5-flash" is stable/GA, input 1,048,576 tokens, output
#     65,536 tokens. Chosen: it is the longest-standing current stable
#     Flash model, which is what a default should be.
#   * "gemini-3.8-flash" is also stable and is the newest ("most
#     intelligent Flash model, engineered for long-horizon software
#     engineering", 1M in / 64k out). A reasonable GEMINI_MODEL override
#     for coding work; not the default only because "newest" and "safest
#     default" are different jobs.
#   * "gemini-flash-latest" exists as a moving alias. Deliberately NOT the
#     default: a floating alias silently changes the model under a running
#     deployment, which is exactly the class of surprise the NVIDIA
#     per-agent block above was written to stop.
_GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"

# Public aliases (same convention as the NVIDIA/Ollama/OmniRoute defaults).
GEMINI_DEFAULT_BASE_URL = _GEMINI_DEFAULT_BASE_URL
GEMINI_DEFAULT_MODEL = _GEMINI_DEFAULT_MODEL

#: Env var names checked for the key, in order. GEMINI_API_KEY is the name
#: Google's own docs and SDKs use; GOOGLE_AI_STUDIO_KEY is accepted as a
#: fallback so an operator who named it after the console does not get a
#: silent "not configured". Order is precedence: first non-empty wins.
GEMINI_ENV_KEYS = ("GEMINI_API_KEY", "GOOGLE_AI_STUDIO_KEY")


@dataclass(frozen=True)
class GeminiConfig:
    """Google AI Studio (Gemini) backend config.

    ``api_key`` is "" when no key is set anywhere — the honest
    not-configured state, never a placeholder that would be sent to the
    real endpoint. Per-agent overrides come from
    ``DOURMOUSE_GEMINI_MODEL_<AGENT>``, mirroring the
    ``DOURMOUSE_MODEL_`` (NVIDIA) and ``DOURMOUSE_OLLAMA_MODEL_``
    conventions, resolved deterministically (Rule 2.8).
    """

    api_key: str = ""
    base_url: str = _GEMINI_DEFAULT_BASE_URL
    model: str = _GEMINI_DEFAULT_MODEL
    agent_models: dict[str, str] = field(default_factory=dict)

    def model_for_agent(self, agent: str | None) -> str:
        """The Gemini model a specific subagent runs on (deterministic).

        Same precedence as the other backends' ``model_for_agent``: the
        per-agent env override, then (orchestrator only) the persisted
        orchestrator-model setting — which ``_persisted_model_for_backend``
        only returns when it was actually saved FOR "gemini", so an Ollama
        or NVIDIA model id can never leak onto this backend — then the
        run's default model.
        """
        key = (agent or "").strip().upper()
        if key and key in self.agent_models:
            return self.agent_models[key]
        if key == "ORCHESTRATOR":
            persisted = _persisted_model_for_backend("gemini")
            if persisted:
                return persisted
        return self.model


def load_gemini_config() -> GeminiConfig:
    """Build the Gemini backend config from env (defaults when unset).

    Never raises on a missing key — see the section comment above for why
    this mirrors ``load_ollama_config`` rather than ``load_nvidia_config``.
    """
    api_key = ""
    for name in GEMINI_ENV_KEYS:
        value = os.environ.get(name, "").strip()
        if value:
            api_key = value
            break
    base_url = os.environ.get(
        "GEMINI_BASE_URL", _GEMINI_DEFAULT_BASE_URL
    ).strip() or _GEMINI_DEFAULT_BASE_URL
    model = os.environ.get(
        "GEMINI_MODEL", _GEMINI_DEFAULT_MODEL
    ).strip() or _GEMINI_DEFAULT_MODEL
    agent_models = {}
    prefix = "DOURMOUSE_GEMINI_MODEL_"
    for env_name, value in os.environ.items():
        if env_name.startswith(prefix) and value.strip():
            agent_name = env_name[len(prefix):].strip().upper()
            if agent_name:
                agent_models[agent_name] = value.strip()
    return GeminiConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        agent_models=agent_models,
    )
