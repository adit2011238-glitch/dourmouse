"""Regression guard: configured model ids must exist on the REAL backend.

Origin (systematic backend verification, world-monitor-expansion,
2026-08-29): three model ids baked into config.py's built-in defaults —
the fast-lane/Ollama-default "qwen3:*" family and NVIDIA's
"nvidia/llama-3.3-nemotron-super-49b-v1" / "nvidia/code-llama-70b" — were
never pulled locally / never existed on NVIDIA's side, and nothing in the
system noticed until every request that routed to them died with a bare
404. That is the exact incident model_check.py / test_model_check.py
already document and guard for Ollama's fast lane specifically. This file
generalizes the same idea to every backend this codebase actually
defaults to:

- Ollama: reuses ``dourmouse.model_check`` (no new comparison logic — see
  that module's own docstring) against the REAL locally-pulled model list,
  cross-checked against config.py's real resolved defaults (env-aware, so
  this reflects what actually runs today, not just the hardcoded fallback).
- NVIDIA: no prior guard existed for this backend at all. Lists the live
  catalog from ``integrate.api.nvidia.com/v1/models`` with the real
  ``NVIDIA_API_KEY`` and cross-checks every id in
  ``config._NVIDIA_AGENT_DEFAULTS`` (+ ``NVIDIA_DEFAULT_MODEL``) against it.

Gating (Rule 2.1 hermetic — see repo-root ``conftest.py``'s own docstring):
the root conftest deliberately neuters every backend by default (redirects
OLLAMA_HOST to a closed port, sets DOURMOUSE_LLM_BACKEND=none) so the
suite's pass count is never a property of the machine it runs on. These
two tests need the OPPOSITE — a real, reachable backend — so, exactly like
that conftest's own documented escape hatch, both SKIP outright unless
``DOURMOUSE_TEST_LIVE_BACKEND=1`` is set. Default `pytest` runs (including
the one this repo's own CONTRIBUTING-style workflow runs) stay fully
hermetic; a deliberate

    DOURMOUSE_TEST_LIVE_BACKEND=1 pytest dourmouse/tests/test_live_model_catalogs.py

is what actually exercises the live cross-check. Within that opt-in, both
probes still degrade to an honest skip (never a failure) if the specific
resource turns out unreachable (daemon down, key missing/offline) — a
probe that fails the whole suite over an environment gap would just get
this file deleted the first time it runs somewhere without either.

NOTE: presence in NVIDIA's ``/v1/models`` listing is NOT the same claim as
"this model can serve a completion right now" — see config.py's
_NVIDIA_AGENT_DEFAULTS docstring for the real, currently-open 403
"Authorization failed" issue this key has on every inference call despite
listing succeeding. This test only verifies the id is a real, current
NVIDIA-catalog id — the cheap, always-safe-to-automate half of "is this
model actually real" — not that inference currently works end to end.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from dourmouse import config as _cfg
from dourmouse import model_check

_NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
_TIMEOUT = 5.0

_LIVE_BACKEND_OPT_IN = os.environ.get("DOURMOUSE_TEST_LIVE_BACKEND", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

pytestmark = pytest.mark.skipif(
    not _LIVE_BACKEND_OPT_IN,
    reason=(
        "live model-catalog cross-check needs a real backend; the repo-root "
        "conftest neuters backends by default (Rule 2.1 hermetic) — set "
        "DOURMOUSE_TEST_LIVE_BACKEND=1 to opt in, same as its own escape hatch."
    ),
)


def _live_nvidia_model_ids() -> set[str] | None:
    """Real model ids from a live NVIDIA catalog call, or None (unreachable/
    no key) — never raises, mirrors model_check.installed_models()'s honest
    None-vs-empty contract."""
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            _NVIDIA_MODELS_URL, headers={"Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    return {m.get("id") for m in data if isinstance(m, dict) and m.get("id")}


# --------------------------------------------------------------------------- #
# NVIDIA — no prior live guard existed for this backend.
# --------------------------------------------------------------------------- #

def test_nvidia_agent_defaults_exist_in_the_live_catalog():
    live_ids = _live_nvidia_model_ids()
    if live_ids is None:
        pytest.skip(
            "NVIDIA_API_KEY not set or integrate.api.nvidia.com unreachable "
            "— cross-check needs a live catalog, not a guess."
        )
    referenced = {_cfg.NVIDIA_DEFAULT_MODEL, *_cfg._NVIDIA_AGENT_DEFAULTS.values()}
    missing = sorted(m for m in referenced if m not in live_ids)
    assert not missing, (
        f"stale/retired NVIDIA model id(s) configured but absent from the "
        f"live catalog: {missing} — same never-real-model bug class as the "
        "qwen3:4b Ollama incident (test_model_check.py). Pick a real "
        "replacement confirmed present in a fresh /v1/models call, the way "
        "config._NVIDIA_AGENT_DEFAULTS' docstring documents doing."
    )


# --------------------------------------------------------------------------- #
# Ollama — reuses model_check.py's own comparison, just against config.py's
# REAL resolved values instead of canned test fixtures.
# --------------------------------------------------------------------------- #

def test_configured_ollama_models_are_actually_installed():
    ollama_cfg = _cfg.load_ollama_config()
    configured = {
        "fast lane (DOURMOUSE_FAST_MODEL)": _cfg.fast_lane_model(),
        "ollama default (OLLAMA_MODEL)": ollama_cfg.model,
    }
    for agent, model in ollama_cfg.agent_models.items():
        configured[f"per-agent override ({agent})"] = model

    report = model_check.check_configured_models(configured)
    if not report["reachable"]:
        pytest.skip("Ollama not reachable on 127.0.0.1:11434 — cannot verify live.")
    assert report["missing"] == {}, report["detail"]
