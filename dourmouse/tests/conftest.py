"""Shared test fixtures (v5.6)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _workspace_isolated(tmp_path_factory, monkeypatch):
    """v5.22.14 (audit fix): redirect the default workspace to a per-test
    tmp dir so NO test can write sessions/facts into the REAL workspace
    (Rule 2.1 hermetic — pre-fix, HTTP-based tests leaked stub sessions
    like "draft the quarterly report" into workspace/sessions/ on live
    runs). Tests that need a specific workspace set DOURMOUSE_WORKSPACE
    themselves, which overrides this fixture's value.

    v5.x: uses tmp_path_factory.mktemp("ws") instead of tmp_path — the
    per-test tmp_path is NAMED AFTER THE TEST FUNCTION, and that name is
    embedded into every sandboxed tool description via _sandbox_path_note
    ("'path' is RELATIVE to the workspace root <path>"), which leaks the
    test name as searchable tokens into the registry. A query containing a
    word from the test name (e.g. "run a terminal command" vs
    test_run_terminal_ranks_system_first) then scored a spurious haystack
    hit and flipped agent ranking. A fixed short basename has no query-
    meaningful tokens; mktemp still returns a unique dir per test.
    """
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path_factory.mktemp("ws")))


@pytest.fixture(autouse=True)
def _neuro_off(monkeypatch):
    """v5.6: keep the neural orchestrator hermetic.

    No test may read or write the REAL workspace/neuro store (learned state
    is runtime state, and planner/dispatch blend in live predictions once
    the net is trained). Tests that exercise the net opt in by setting
    DOURMOUSE_NET=1 + DOURMOUSE_NET_DIR=<tmp> inside the test.
    """
    monkeypatch.setenv("DOURMOUSE_NET", "0")


@pytest.fixture(autouse=True)
def _user_config_isolated(tmp_path_factory, monkeypatch):
    """v13 (hermetic-test-caught, real bug): every test touching
    orchestrator-model settings, Grounded Mode, or (new) the MCP bridge's
    config file was silently reading/writing the REAL developer's
    ``~/Library/Application Support/Dourmouse/.env`` via
    config.user_config_dir() — no isolation existed for it at all, unlike
    DOURMOUSE_WORKSPACE above. Concretely caught: DOURMOUSE_GROUNDED_MODE=1,
    persisted during Grounded Mode's own earlier live verification on this
    machine, leaked into unrelated dispatch tests and silently added an
    extra grounded-mode nudge turn, exhausting fake clients sized for the
    setting-off case (test_planner.py::TestPlanEventInTranscript). Same
    fixed-short-basename reasoning as _workspace_isolated above: a bare
    "cfg" avoids leaking the test name as a query-meaningful token should
    anything ever embed this path into a tool description the way
    _sandbox_path_note does for the workspace root.
    """
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
