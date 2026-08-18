"""Every model call must be measured, and measurement must never cost a call.

`_call_with_retry` is the single chokepoint for inference, so it is where
latency data comes from. The timing deliberately wraps the whole retry
sequence: what a user waits for includes backoff and any fallback attempt,
not just the attempt that happened to succeed.
"""

from __future__ import annotations

import pytest

from dourmouse import dispatch, obs


class _Usage:
    def __init__(self, prompt=11, completion=22, total=33):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class _Response:
    def __init__(self, usage=None):
        self.usage = usage


class _FakeCompletions:
    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self._behaviour(self.calls, kwargs)


class _FakeClient:
    def __init__(self, behaviour):
        self.chat = type("chat", (), {"completions": _FakeCompletions(behaviour)})()


@pytest.fixture(autouse=True)
def _logs_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("DOURMOUSE_OBS_DISABLED", raising=False)
    return tmp_path


def _call(client, **overrides):
    kwargs = dict(
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        config=None,
    )
    kwargs.update(overrides)
    return dispatch._call_with_retry(client, **kwargs)


def test_successful_call_is_recorded_with_model_and_duration():
    client = _FakeClient(lambda n, kw: _Response(_Usage()))

    _call(client)

    (row,) = obs.read_recent("perf.log")
    assert row["op"] == "inference"
    assert row["duration_ms"] >= 0
    assert row["extra"]["model"] == "test-model"
    assert row["extra"]["ok"] is True


def test_token_usage_is_captured_when_the_backend_reports_it():
    client = _FakeClient(lambda n, kw: _Response(_Usage(prompt=11, completion=22)))

    _call(client)

    extra = obs.read_recent("perf.log")[0]["extra"]
    assert extra["prompt_tokens"] == 11
    assert extra["completion_tokens"] == 22
    assert extra["total_tokens"] == 33


def test_missing_usage_is_omitted_rather_than_faked():
    """Local backends often omit usage; inventing zeros would poison tok/s."""
    client = _FakeClient(lambda n, kw: _Response(usage=None))

    _call(client)

    extra = obs.read_recent("perf.log")[0]["extra"]
    assert "completion_tokens" not in extra
    assert "total_tokens" not in extra


def test_failed_call_is_still_recorded_and_marked_not_ok():
    """A slow failure is exactly the case worth measuring."""
    def always_fails(n, kw):
        raise ValueError("nope")

    client = _FakeClient(always_fails)

    with pytest.raises(ValueError):
        _call(client)

    (row,) = obs.read_recent("perf.log")
    assert row["extra"]["ok"] is False
    assert row["duration_ms"] >= 0


def test_shape_of_the_request_is_recorded():
    client = _FakeClient(lambda n, kw: _Response(_Usage()))

    _call(
        client,
        messages=[{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
        tools=[{"type": "function"}, {"type": "function"}, {"type": "function"}],
    )

    extra = obs.read_recent("perf.log")[0]["extra"]
    assert extra["n_messages"] == 2
    assert extra["n_tools"] == 3
    assert extra["streamed"] is False


def test_one_record_per_call_not_per_attempt(monkeypatch):
    """Retries are one user-visible wait, so they are one measurement."""
    monkeypatch.setattr(dispatch, "_is_transient_error", lambda exc: True)

    def fail_then_succeed(n, kw):
        if n < 3:
            raise RuntimeError("transient")
        return _Response(_Usage())

    client = _FakeClient(fail_then_succeed)

    class _Cfg:
        max_retries = 3
        retry_backoff = 0.0
        fallback_model = ""

    call_log: list = []
    _call(client, config=_Cfg(), call_log=call_log)

    rows = obs.read_recent("perf.log")
    assert len(rows) == 1
    assert rows[0]["extra"]["ok"] is True
    assert rows[0]["extra"]["attempts"] == 3  # the retries are visible as a count


def test_measurement_failure_never_breaks_the_call(monkeypatch):
    def exploding(**_k):
        raise OSError("disk full")

    monkeypatch.setattr(obs, "log_perf", exploding)
    client = _FakeClient(lambda n, kw: _Response(_Usage()))

    result = _call(client)  # must not raise
    assert result is not None


def test_usage_of_tolerates_odd_shapes():
    class Weird:
        usage = object()  # has no token fields

    assert dispatch._usage_of(Weird()) == {}
    assert dispatch._usage_of(_Response(usage=None)) == {}
    assert dispatch._usage_of(_Response(_Usage()))["total_tokens"] == 33
