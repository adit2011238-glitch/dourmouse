"""Finding #074: real local-model concurrency ceiling, fixed.

Live-observed: two simultaneous real calls against local Ollama threw a
genuine HTTP 400 (the local server's own real capacity limit). Fix: a
real, process-wide semaphore serializes real network calls that
`backend_identity(config)` identifies as local -- cloud/NVIDIA calls are
completely unaffected. These tests prove BOTH halves with real threads and
real timing, not just that the semaphore object exists.
"""

from __future__ import annotations

import threading
import time

import pytest

from dourmouse.config import NvidiaConfig, OllamaConfig
from dourmouse.dispatch import (
    _call_with_retry_inner,
    _get_local_model_semaphore,
    _is_local_backend,
    _local_model_max_concurrent,
    reset_local_model_semaphore,
)


@pytest.fixture(autouse=True)
def _clean_semaphore(monkeypatch):
    monkeypatch.delenv("DOURMOUSE_LOCAL_MODEL_MAX_CONCURRENT", raising=False)
    reset_local_model_semaphore()
    yield
    reset_local_model_semaphore()


class _SlowClient:
    """A fake OpenAI-compatible client whose create() sleeps briefly and
    records the real wall-clock window it ran in, so a test can check
    whether two calls genuinely overlapped."""

    def __init__(self, delay: float, windows: list[tuple[float, float]]):
        self._delay = delay
        self._windows = windows
        self._lock = threading.Lock()
        self.chat = self  # client.chat.completions.create(...)
        self.completions = self

    def create(self, **kwargs):
        start = time.monotonic()
        time.sleep(self._delay)
        end = time.monotonic()
        with self._lock:
            self._windows.append((start, end))

        class _Msg:
            content = "ok"
            tool_calls = None

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


def _overlaps(windows: list[tuple[float, float]]) -> bool:
    for i, (s1, e1) in enumerate(windows):
        for s2, e2 in windows[i + 1 :]:
            if s1 < e2 and s2 < e1:
                return True
    return False


class TestIsLocalBackend:
    def test_ollama_is_local(self):
        assert _is_local_backend(OllamaConfig()) is True

    def test_nvidia_is_not_local(self):
        assert _is_local_backend(NvidiaConfig(api_key="k", base_url="u", model="m")) is False

    def test_none_config_never_crashes(self):
        assert _is_local_backend(None) is False


class TestLocalModelMaxConcurrent:
    def test_default_is_one(self):
        assert _local_model_max_concurrent() == 1

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LOCAL_MODEL_MAX_CONCURRENT", "3")
        assert _local_model_max_concurrent() == 3

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LOCAL_MODEL_MAX_CONCURRENT", "not-a-number")
        assert _local_model_max_concurrent() == 1


class TestSemaphoreSerializesLocalCalls:
    def test_two_local_calls_never_overlap(self):
        windows: list[tuple[float, float]] = []
        client = _SlowClient(delay=0.2, windows=windows)
        config = OllamaConfig()

        def run():
            _call_with_retry_inner(client, model="m", messages=[], tools=[], config=config)

        threads = [threading.Thread(target=run) for _ in range(2)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        elapsed = time.monotonic() - start
        assert len(windows) == 2
        assert not _overlaps(windows), f"local calls overlapped: {windows}"
        # Real serialization means total wall time is roughly additive
        # (>= 2x one call's delay), not roughly the delay of one call.
        assert elapsed >= 0.35

    def test_two_cloud_calls_run_concurrently_unaffected(self):
        windows: list[tuple[float, float]] = []
        client = _SlowClient(delay=0.2, windows=windows)
        config = NvidiaConfig(api_key="k", base_url="u", model="m")

        def run():
            _call_with_retry_inner(client, model="m", messages=[], tools=[], config=config)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(windows) == 2
        # The overlapping call windows ARE the proof of concurrency. A wall-clock
        # upper bound (elapsed < 0.35) used to sit here too; it failed whenever
        # the machine was busy (full suite, 2026-09-24) without saying anything
        # about concurrency. A lower bound on the serial test is safe (load can
        # only slow it down); an upper bound is not.
        assert _overlaps(windows), f"cloud calls should have run concurrently: {windows}"

    def test_raised_env_limit_allows_that_many_concurrent_local_calls(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LOCAL_MODEL_MAX_CONCURRENT", "2")
        reset_local_model_semaphore()
        windows: list[tuple[float, float]] = []
        client = _SlowClient(delay=0.2, windows=windows)
        config = OllamaConfig()

        def run():
            _call_with_retry_inner(client, model="m", messages=[], tools=[], config=config)

        threads = [threading.Thread(target=run) for _ in range(2)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        elapsed = time.monotonic() - start
        assert len(windows) == 2
        assert _overlaps(windows)
        assert elapsed < 0.35

    def test_semaphore_releases_even_when_the_call_raises(self):
        class _BoomClient:
            chat = None

            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("boom, non-transient")

        _BoomClient.chat = _BoomClient
        config = OllamaConfig()
        with pytest.raises(Exception):
            _call_with_retry_inner(_BoomClient(), model="m", messages=[], tools=[], config=config)
        # The semaphore must not be left held -- a second call must not hang.
        client = _SlowClient(delay=0.01, windows=[])
        _call_with_retry_inner(client, model="m", messages=[], tools=[], config=config)


class TestGetLocalModelSemaphore:
    def test_same_semaphore_object_reused(self):
        s1 = _get_local_model_semaphore()
        s2 = _get_local_model_semaphore()
        assert s1 is s2

    def test_reset_forces_a_rebuild(self, monkeypatch):
        s1 = _get_local_model_semaphore()
        monkeypatch.setenv("DOURMOUSE_LOCAL_MODEL_MAX_CONCURRENT", "5")
        reset_local_model_semaphore()
        s2 = _get_local_model_semaphore()
        assert s1 is not s2
