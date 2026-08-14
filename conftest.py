"""Repo-root fixtures — the LLM-backend half of Rule 2.1 (hermetic).

Both test trees already isolate the workspace and the neural store, but
nothing isolated the INFERENCE BACKEND. Because the suite imports the real
config, a developer machine with a populated ``.env`` and a running Ollama
sent real completions from tests that meant to use a fake client. Symptoms,
all observed live on 2026-08-14 before this file existed:

* ``test_builds_client_from_env_config_when_none_injected`` asserted
  ``final_text == "ok"`` and got ``"Hello! How can I assist you today?"`` —
  a genuine model reply.
* Several dispatch/chat tests raised StopIteration because the real model
  answered without calling a tool, so no ``tool_result`` was ever recorded.
* The full run took 6m13s and reported 17 failures. Hermetically the SAME
  tree is 2m41s and green — the failures were environmental, not defects.

That made the suite's pass count a property of the developer's machine,
which is exactly what a release gate must never be. Neutralising the
backend here keeps every test deterministic on any machine, CI included.

Opt out for a deliberate live-backend run:

    DOURMOUSE_TEST_LIVE_BACKEND=1 pytest ...
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _llm_backend_isolated(monkeypatch):
    if os.environ.get("DOURMOUSE_TEST_LIVE_BACKEND", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return
    # No backend selected, and any code path that ignores that and dials out
    # anyway hits a closed port instead of the real daemon — it fails fast
    # and loudly rather than silently succeeding against a live model.
    monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "none")
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:1")
    monkeypatch.setenv("DOURMOUSE_SERVER_URL", "")
    # Disable backend fallback in tests — tests expect the config they set up,
    # not a fallback decision. Production code still gets fallback in load_llm_config_with_fallback().
    monkeypatch.setenv("DOURMOUSE_FALLBACK_DISABLED", "1")
