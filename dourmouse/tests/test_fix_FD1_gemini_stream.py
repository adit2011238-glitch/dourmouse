"""Fix agent FD1: P5-17, mid-stream failures become RuntimeError and the timeout is a wall-clock deadline."""

from __future__ import annotations

import http.client
import urllib.request

import pytest

from dourmouse import gemini_backend as gb


class _Resp:
    def __init__(self, lines, then=None, delay=0.0):
        self.lines = lines
        self.then = then
        self.delay = delay

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        import time

        for ln in self.lines:
            if self.delay:
                time.sleep(self.delay)
            yield ln
        if self.then is not None:
            raise self.then


def _run(monkeypatch, resp, timeout=5.0):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)
    return gb._urllib_transport("https://x/y", data=b"{}", headers={}, timeout=timeout)


@pytest.mark.parametrize("exc", [ConnectionResetError("reset"), TimeoutError("read timed out"),
                                 http.client.IncompleteRead(b"par", 10), OSError("broken pipe")])
def test_mid_stream_failures_are_wrapped(monkeypatch, exc):
    stream = _run(monkeypatch, _Resp([b"data: {}\n"], then=exc))
    assert next(stream) == "data: {}"
    with pytest.raises(RuntimeError, match="interrupted"):
        next(stream)


def test_a_trickling_server_hits_the_wall_clock_deadline(monkeypatch):
    # each line arrives well inside the per-read idle timeout, but the whole stream may not outlive `timeout`
    stream = _run(monkeypatch, _Resp([b": keepalive\n"] * 50, delay=0.05), timeout=0.2)
    with pytest.raises(RuntimeError, match="deadline|wall-clock|exceeded"):
        for _ in stream:
            pass


def test_stream_gemini_surfaces_the_wrapped_error_to_its_caller(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    got = []
    resp = _Resp([b'data: {"candidates":[{"content":{"parts":[{"text":"hel"}]}}]}\n'], then=ConnectionResetError("reset"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)
    with pytest.raises(RuntimeError, match="interrupted"):
        gb.stream_gemini("hi", on_delta=got.append)
    assert got == ["hel"], "text delivered before the failure was already streamed to the caller"
