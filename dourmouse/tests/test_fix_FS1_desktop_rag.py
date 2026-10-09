"""FS1 P4-20: a TIMEOUT already used the whole budget, so it is not retried
(a retry doubled the worst-case wait a chat turn could block)."""

from __future__ import annotations

import subprocess

import pytest

from dourmouse.desktop_rag import DesktopRagError, query_desktop_rag
from dourmouse.tests.test_desktop_rag import _no_real_retry_delay, configured_env  # noqa: F401


def test_timeout_is_reported_after_one_attempt(configured_env):  # noqa: F811
    calls = {"n": 0}

    def timing_out(cmd, timeout, stdin_path=None):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd, timeout)

    with pytest.raises(DesktopRagError) as exc:
        query_desktop_rag("x", runner=timing_out)
    assert exc.value.kind == "TIMEOUT"
    assert calls["n"] == 1
