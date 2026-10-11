"""FD2 fix P5-61: a POST whose outcome is unknown says so; paper_confirm must be literally True."""

from __future__ import annotations

import urllib.error

import pytest

from dourmouse import trading212_ops as t


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "k")
    monkeypatch.setenv("T212_ENV", "demo")
    monkeypatch.delenv("T212_ALLOW_LIVE", raising=False)
    monkeypatch.delenv("T212_CONFIRM_LIVE", raising=False)


class _TimeoutOnRead:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        raise TimeoutError("read timed out")


def test_read_timeout_after_post_is_reported_as_unknown_status(monkeypatch):
    monkeypatch.setattr(t.urllib.request, "urlopen", lambda req, timeout=None: _TimeoutOnRead())
    out = t._wrap(t.t212_order)({"ticker": "AAPL", "quantity": 1, "paper_confirm": True})
    assert out.startswith("ERROR")
    assert "UNKNOWN" in out
    assert "before retrying" in out


def test_urlerror_after_post_is_also_unknown_not_just_unreachable(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(t.urllib.request, "urlopen", boom)
    out = t._wrap(t.t212_order)({"ticker": "AAPL", "quantity": 1, "paper_confirm": True})
    assert "UNKNOWN" in out and "before retrying" in out


def test_http_error_on_post_is_a_definite_failure(monkeypatch):
    import io

    def boom(req, timeout=None):
        raise urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b"bad"))

    monkeypatch.setattr(t.urllib.request, "urlopen", boom)
    out = t._wrap(t.t212_order)({"ticker": "AAPL", "quantity": 1, "paper_confirm": True})
    assert out.startswith("ERROR") and "UNKNOWN" not in out


def test_get_timeout_is_a_clean_error_not_an_exception(monkeypatch):
    monkeypatch.setattr(t.urllib.request, "urlopen", lambda req, timeout=None: _TimeoutOnRead())
    out = t._wrap(t.t212_positions)({})
    assert out.startswith("ERROR") and "timed out" in out and "UNKNOWN" not in out


@pytest.mark.parametrize("flag", ["false", "False", "no", "0", 1, "true", None, 0])
def test_paper_confirm_must_be_literally_true(monkeypatch, flag):
    calls = []
    monkeypatch.setattr(t.urllib.request, "urlopen", lambda req, timeout=None: calls.append(req))
    with pytest.raises(ValueError):
        t.t212_order("AAPL", 1, paper_confirm=flag)
    assert calls == []
