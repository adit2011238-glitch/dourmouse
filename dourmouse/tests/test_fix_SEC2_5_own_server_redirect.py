"""SEC2-5 (confirmed live 2026-10-23, security canary A13): the address gate only saw the URL the
model was handed. A page on any local port that answered with a redirect landed the model's
browser on the app's own server, and the model read and relayed the owner's security dashboard.
The check is now made on where the tab IS."""

from __future__ import annotations

import asyncio

import pytest

import dourmouse.browser_agent as ba


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/api/security_dashboard?zz=1",
    "http://localhost:8765/",
    "http://localhost.:8765/x",
    "http://LOCALHOST:8765/x",
    "http://0x7f000001:8765/x",
    "http://2130706433:8765/x",
    "http://127.1:8765/x",
    "http://[::1]:8765/x",
    "https://127.0.0.1:8765/x",
    "http://127.0.0.1:9333/json",
    "http://127.0.0.1:9334/tabs",
    "http://0.0.0.0:8765/x",
    "http://app.localhost:8765/x",
])
def test_every_spelling_of_the_apps_own_address_is_recognised(url):
    assert ba._is_own_server_url(url)


@pytest.mark.parametrize("url", [
    "https://example.com/page",
    "http://127.0.0.1:8799/a3_local_service.html",  # another local page: asks first, but is not the app
    "http://127.0.0.1/",
    "about:blank",
    "",
    "file:///etc/passwd",
    "http://example.com:8765/x",  # a public host that happens to use the port
    "http://192.168.1.5:8765/x",
])
def test_other_addresses_are_not_the_app(url):
    assert not ba._is_own_server_url(url)


class _Page:
    def __init__(self, url):
        self.url = url
        self.visited = []

    def is_closed(self):
        return False

    async def title(self):
        return "dashboard"

    async def goto(self, url, **kw):
        self.visited.append(url)
        self.url = url


def test_the_summary_shown_to_the_model_never_reads_the_app():
    page = _Page("http://127.0.0.1:8765/api/security_dashboard")
    out = asyncio.run(ba._page_summary(page))
    assert out.startswith("REFUSED")
    assert "dashboard" not in out.lower().replace("security dashboard", "")  # no page content in it
    assert "URL: http://127.0.0.1:8765" not in out


def test_tools_refuse_a_tab_that_is_the_app_but_navigation_may_leave_it(monkeypatch):
    page = _Page("http://127.0.0.1:8765/")

    async def _raw(quiet=False):
        return page

    monkeypatch.setattr(ba, "_ensure_browser_raw", _raw)
    with pytest.raises(RuntimeError, match="REFUSED"):
        asyncio.run(ba._ensure_browser())
    assert asyncio.run(ba._ensure_browser(allow_own=True)) is page  # browser_open / browser_back


def test_open_that_redirects_to_the_app_blanks_the_tab_and_reads_nothing(monkeypatch):
    page = _Page("about:blank")

    async def _raw(quiet=False):
        return page

    async def _goto_redirecting(url, **kw):
        page.visited.append(url)
        if url == "about:blank":
            page.url = url
        else:
            page.url = "http://127.0.0.1:8765/api/security_dashboard"  # the redirect landed here

    page.goto = _goto_redirecting

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
    out = ba.browser_open({"url": "http://127.0.0.1:8799/redirect"})
    assert out.startswith("REFUSED")
    assert page.visited[-1] == "about:blank"  # the tab was left blank, not showing the app
