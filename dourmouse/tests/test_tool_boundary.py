"""A failing tool must not take the conversation down with it.

`_execute_tool` invoked `spec.handler(arguments)` unguarded, so any
exception from any of the roster's tools propagated out and aborted the whole
dispatch turn. One bad fetch cost the user the conversation. These tests pin
the containment: the turn survives, the model gets a usable sentence, and the
traceback lands in logs/errors.log.
"""

from __future__ import annotations

import urllib.error

import pytest

from dourmouse import obs
from dourmouse.dispatch import Permission, ToolSpec, _execute_tool


@pytest.fixture(autouse=True)
def _logs_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("DOURMOUSE_OBS_DISABLED", raising=False)
    return tmp_path


def _spec(handler, name="explodes"):
    return ToolSpec(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        permission=Permission.REGULAR,
    )


BOOMS = [
    pytest.param(ValueError("bad int"), id="ValueError"),
    pytest.param(KeyError("missing"), id="KeyError"),
    pytest.param(TypeError("wrong shape"), id="TypeError"),
    pytest.param(AttributeError("no attr"), id="AttributeError"),
    pytest.param(ZeroDivisionError("div"), id="ZeroDivisionError"),
    pytest.param(RuntimeError("HTTP 404 from https://x.test/y"), id="RuntimeError-404"),
    pytest.param(urllib.error.HTTPError("https://x.test", 500, "e", None, None), id="HTTPError-500"),
    pytest.param(TimeoutError("slow"), id="TimeoutError"),
]


@pytest.mark.parametrize("exc", BOOMS)
def test_any_handler_exception_is_contained(exc):
    def boom(_args):
        raise exc

    result = _execute_tool(_spec(boom), {}, None)

    assert isinstance(result, str)
    assert result.strip()
    assert "Traceback" not in result


@pytest.mark.parametrize("exc", BOOMS)
def test_contained_result_leaks_no_url_or_status(exc):
    def boom(_args):
        raise exc

    result = _execute_tool(_spec(boom), {}, None)

    assert "https://" not in result
    assert "404" not in result
    assert "500" not in result


def test_traceback_is_preserved_in_the_error_log():
    def boom(_args):
        raise ValueError("the real cause")

    _execute_tool(_spec(boom, name="my_tool"), {"a": 1}, None)

    (row,) = obs.read_recent("errors.log")
    assert row["source"] == "tool:my_tool"
    assert "ValueError: the real cause" in row["detail"]
    assert "Traceback" in row["detail"]
    assert row["extra"]["arguments"] == {"a": 1}


def test_failure_is_recorded_as_a_failed_agent_call():
    def boom(_args):
        raise ValueError("x")

    _execute_tool(_spec(boom, name="my_tool"), {}, None)

    (row,) = obs.read_recent("agents.log")
    assert row["tool"] == "my_tool"
    assert row["ok"] is False
    assert row["duration_ms"] >= 0


def test_success_path_is_unchanged_and_logged():
    result = _execute_tool(_spec(lambda _a: "REAL RESULT", name="ok_tool"), {}, None)

    assert result == "REAL RESULT"
    assert obs.read_recent("errors.log") == []
    (row,) = obs.read_recent("agents.log")
    assert row["tool"] == "ok_tool"
    assert row["ok"] is True


def test_keyboard_interrupt_still_stops_the_process():
    """Containment must not swallow the user's Ctrl-C."""
    def boom(_args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _execute_tool(_spec(boom), {}, None)


def test_system_exit_still_propagates():
    def boom(_args):
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        _execute_tool(_spec(boom), {}, None)


def test_observability_failure_does_not_break_a_good_tool(monkeypatch):
    """Logging is best-effort; it must never cost a successful result."""
    def exploding_log(**_k):
        raise OSError("disk full")

    monkeypatch.setattr(obs, "log_agent_call", exploding_log)

    result = _execute_tool(_spec(lambda _a: "STILL FINE"), {}, None)
    assert result == "STILL FINE"
