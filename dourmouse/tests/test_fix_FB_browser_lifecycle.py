"""Fix agent FB: P4-11 (popup following after 50 pages), P4-12 (a timed-out call keeps running),
P4-13 (a relaunched headless Chrome leaks the old one; close_browser leaves the browser and Playwright)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from dourmouse import browser_agent as ba


class _FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self._closed = False
        self.loads = 0

    def is_closed(self):
        return self._closed

    async def wait_for_load_state(self, *a, **k):
        self.loads += 1


class _FakeContext:
    def __init__(self):
        self.handler = None

    def on(self, name, cb):
        assert name == "page"
        self.handler = cb

    async def route(self, *a, **k):
        return None


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
    monkeypatch.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
    monkeypatch.setattr(ba, "_PAGE_MODE", "headless")
    monkeypatch.setattr(ba, "_NEW_PAGES", [])
    monkeypatch.setattr(ba, "_NEW_PAGES_TOTAL", 0, raising=False)
    monkeypatch.setattr(ba, "_NOTES", [])
    monkeypatch.setattr(ba, "_PREPARED_CONTEXTS", set())


# --------------------------------------------------------------------------- #
# P4-11
# --------------------------------------------------------------------------- #


def test_a_popup_is_still_followed_after_more_than_fifty_pages(clean, monkeypatch):
    ctx = _FakeContext()
    asyncio.run(ba._prepare_context(ctx, filter_requests=False))
    for _ in range(120):  # a long session: far past the 50 the list keeps
        ctx.handler(_FakePage())
    start = _FakePage("http://a/")
    monkeypatch.setattr(ba, "_PAGE", start)
    mark = ba._action_mark()
    popup = _FakePage("http://a/popup")
    ctx.handler(popup)  # the action opened a tab
    got = asyncio.run(ba._settle_after_action(start, mark))
    assert got is popup
    assert ba._PAGE is popup
    assert any("opened a new tab" in n for n in ba._NOTES)


def test_no_popup_means_no_switch_even_when_the_list_is_full(clean, monkeypatch):
    ctx = _FakeContext()
    asyncio.run(ba._prepare_context(ctx, filter_requests=False))
    for _ in range(80):
        ctx.handler(_FakePage())
    start = _FakePage("http://a/")
    monkeypatch.setattr(ba, "_PAGE", start)
    mark = ba._action_mark()
    assert asyncio.run(ba._settle_after_action(start, mark)) is start


def test_the_list_itself_stays_bounded(clean):
    ctx = _FakeContext()
    asyncio.run(ba._prepare_context(ctx, filter_requests=False))
    for _ in range(500):
        ctx.handler(_FakePage())
    assert len(ba._NEW_PAGES) <= 50


# --------------------------------------------------------------------------- #
# P4-12
# --------------------------------------------------------------------------- #


def test_a_call_that_times_out_is_cancelled_and_never_acts_later():
    acted = threading.Event()
    cancelled = threading.Event()

    async def slow():
        try:
            await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        acted.set()  # the late click / submit
        return "done"

    with pytest.raises(RuntimeError) as err:
        ba._call(slow, timeout=0.2)
    assert "BROWSER TIMEOUT" in str(err.value)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not cancelled.is_set():
        time.sleep(0.02)
    assert cancelled.is_set(), "the coroutine kept running after the caller was told it timed out"
    time.sleep(1.8)
    assert not acted.is_set()


def test_a_timeout_message_tells_the_model_the_action_was_stopped():
    async def slow():
        await asyncio.sleep(5)

    with pytest.raises(RuntimeError) as err:
        ba._call(slow, timeout=0.1)
    assert "stopped" in str(err.value).lower() or "cancel" in str(err.value).lower()


# --------------------------------------------------------------------------- #
# P4-13
# --------------------------------------------------------------------------- #


class _FakeBrowser:
    def __init__(self, tag):
        self.tag = tag
        self.closed = False
        self.contexts_made = []

    async def new_context(self, **k):
        ctx = _FakeBrowserContext(self)
        self.contexts_made.append(ctx)
        return ctx

    async def close(self):
        self.closed = True

    def is_connected(self):
        return not self.closed


class _FakeBrowserContext(_FakeContext):
    def __init__(self, browser):
        super().__init__()
        self.browser = browser
        self.pages = []
        self.closed = False

    async def new_page(self):
        p = _FakePage("about:blank")
        self.pages.append(p)
        return p

    async def close(self):
        self.closed = True
        for p in self.pages:
            p._closed = True


class _FakeChromium:
    def __init__(self):
        self.launched = []

    async def launch(self, **k):
        b = _FakeBrowser(len(self.launched))
        self.launched.append(b)
        return b


class _FakePW:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.fixture
def fake_engine(monkeypatch, clean):
    pw = _FakePW()
    for name, val in (("_PW", pw), ("_PAGE", None), ("_CONTEXT", None), ("_BROWSER", None)):
        monkeypatch.setattr(ba, name, val)
    monkeypatch.setattr(ba, "_PAGE_MODE", None)
    monkeypatch.setattr(ba, "_HEADLESS_BROWSER", None, raising=False)
    return pw


def test_relaunching_closes_the_previous_headless_chrome(fake_engine):
    first = asyncio.run(ba._ensure_browser())
    browser1 = fake_engine.chromium.launched[0]
    assert ba._PAGE_MODE == "headless"
    # every tab goes away (the owner or a page closed the last one) and the context with it
    ba._CONTEXT.pages.clear()
    first._closed = True
    asyncio.run(ba._ensure_browser())
    assert len(fake_engine.chromium.launched) == 2
    assert browser1.closed, "the first Chrome was left running"
    assert not fake_engine.chromium.launched[1].closed


def test_close_browser_closes_the_browser_and_stops_playwright(fake_engine):
    asyncio.run(ba._ensure_browser())
    browser = fake_engine.chromium.launched[0]
    # close_browser works through the module's own loop
    loop = ba._ensure_loop()
    ba._PAGE = None
    ba.close_browser()
    assert browser.closed
    assert fake_engine.stopped
    assert ba._PW is None
    assert ba._HEADLESS_BROWSER is None
    del loop
