"""Shared fixtures for the top-level tests/ tree (v5.22.14 audit fix).

These tests start REAL webui servers via run_server(); without workspace
isolation their ChatSessions wrote stub records (e.g. "hello", "draft the
quarterly report") into the REAL <project>/workspace/sessions/ on every
suite run. Rule 2.1 (hermetic) — redirect the default workspace to tmp for
every test here. Any test that needs a specific workspace sets
DOURMOUSE_WORKSPACE itself, overriding this fixture.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _workspace_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
