"""Shared test fixtures (v5.6)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _neuro_off(monkeypatch):
    """v5.6: keep the neural orchestrator hermetic.

    No test may read or write the REAL workspace/neuro store (learned state
    is runtime state, and planner/dispatch blend in live predictions once
    the net is trained). Tests that exercise the net opt in by setting
    DOURMOUSE_NET=1 + DOURMOUSE_NET_DIR=<tmp> inside the test.
    """
    monkeypatch.setenv("DOURMOUSE_NET", "0")
