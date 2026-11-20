"""T5 step 5 (finding 213): a redirect from a public address into the local network is not read."""

from __future__ import annotations

import asyncio

import pytest

import dourmouse.browser_agent as ba
from dourmouse.tests.test_fix_SEC2_5_own_server_redirect import _Page


@pytest.mark.parametrize("requested,landed,expected", [
    ("https://example.com/a", "http://127.0.0.1:8799/x", True),
    ("https://example.com/a", "http://192.168.1.5/router", True),
    ("https://example.com/a", "http://localhost:3000/", True),
    ("https://example.com/a", "http://[::1]:8000/", True),
    ("https://example.com/a", "http://2130706433/", True),
    ("https://example.com/a", "https://example.org/b", False),
    ("https://example.com/a", "about:blank", False),
    ("https://example.com/a", "", False),
    ("http://127.0.0.1:8799/ok", "http://127.0.0.1:8799/ok2", False),  # the owner confirmed a local address
    ("http://192.168.1.5/a", "http://127.0.0.1:8799/x", False),  # a local request is judged at the gate
])
def test_decision(requested, landed, expected):
    assert ba._redirected_into_local_network(requested, landed) is expected


def _open(monkeypatch, landing):
    page = _Page("about:blank")

    async def _raw(quiet=False):
        return page

    async def _goto(url, **kw):
        page.visited.append(url)
        page.url = url if url == "about:blank" else landing

    page.goto = _goto

    class _Acting:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ba, "_ensure_browser_raw", _raw)
    monkeypatch.setattr(ba, "_acting", _Acting)
    monkeypatch.setattr(ba, "_call", lambda coro_fn, **kw: asyncio.run(coro_fn()))

    async def _summary(p, n=60):
        return "SUMMARY " + p.url

    monkeypatch.setattr(ba, "_page_summary", _summary)
    return page


def test_open_redirected_to_a_local_service_blanks_the_tab_and_reads_nothing(monkeypatch):
    page = _open(monkeypatch, "http://192.168.1.5:8080/router-admin")
    out = ba.browser_open({"url": "https://example.com/go"})
    assert out.startswith("REFUSED") and "192.168.1.5" in out
    assert page.visited[-1] == "about:blank" and "SUMMARY" not in out


def test_open_that_lands_on_a_public_page_is_read_as_before(monkeypatch):
    _open(monkeypatch, "https://example.org/page")
    assert ba.browser_open({"url": "https://example.com/go"}) == "SUMMARY https://example.org/page"
