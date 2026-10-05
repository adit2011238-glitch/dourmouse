"""Phase C1: the browser agent works on the tab the owner is looking at.

Before, browser_agent attached to the first about:blank tab of the Electron pane and held that Page
forever: a tab the owner switched to, a popup the agent's own click opened and a closed tab 1 were all
invisible, and a failed attach fell back silently to a separate headless Chrome. Now every call asks
the pane bridge which tab is active, maps it to the Playwright Page by CDP target id (the address
only as a fallback), tells the model when the tab changed, and never falls back to a separate Chrome
while the pane is there.

Two layers:

* fakes for the Playwright objects and the pane bridge, exercising every branch of the mapping,
  the notices, the popup follow, the fallback rules and the error paths;
* a REAL Google Chrome with a REAL DevTools port, a real Playwright connection, and a small fake pane
  bridge that reports the real CDP target ids of the real tabs. These skip when Chrome is missing.

Neither layer is the Electron shell itself: that is recorded as seen live in finding #165.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

from dourmouse import browser_agent as ba

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _stop_playwright():
    stop = getattr(ba._PW, "stop", None)  # a fake driver has nothing to stop
    if stop is not None:
        await stop()


def _reset() -> None:
    ba._call(lambda: _stop_playwright(), timeout=30)
    ba._PAGE = None
    ba._CONTEXT = None
    ba._PAGE_MODE = None
    ba._BROWSER = None
    ba._PW = None
    ba._WORLDS.clear()
    ba._TARGET_IDS.clear()
    ba._PREPARED_CONTEXTS.clear()
    ba._NEW_PAGES.clear()
    ba._ID_OWNER.clear()
    ba._NOTES.clear()
    ba._PANE_SEEN = {"active": None, "ids": frozenset()}


# --------------------------------------------------------------------------- #
# layer one: fakes
# --------------------------------------------------------------------------- #


class FakeSession:
    def __init__(self, page):
        self.page = page
        self.detached = False

    async def send(self, method, params=None):
        if method == "Target.getTargetInfo":
            if self.page.target_error:
                raise RuntimeError("Target closed")
            return {"targetInfo": {"targetId": self.page.target_id}}
        raise AssertionError(f"unexpected CDP call {method}")

    async def detach(self):
        self.detached = True


class FakeContext:
    def __init__(self):
        self.pages: list[FakePage] = []
        self.routes = 0
        self.listeners: list = []

    def on(self, event, fn):
        assert event == "page"
        self.listeners.append(fn)

    async def route(self, pattern, handler):
        self.routes += 1

    async def new_cdp_session(self, page):
        return FakeSession(page)

    async def new_page(self):
        p = FakePage(self, "about:blank", f"LAUNCHED-{len(self.pages)}")
        self.pages.append(p)
        return p


class FakePage:
    def __init__(self, context, url, target_id):
        self.context = context
        self.url = url
        self.target_id = target_id
        self.closed = False
        self.target_error = False
        self.frames = [object()]

    def is_closed(self):
        return self.closed

    async def wait_for_load_state(self, *a, **k):
        return None


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]

    def is_connected(self):
        return True


class FakeBridge:
    """The pane bridge's /tabs, driven by the test."""

    def __init__(self):
        self.tabs: list[dict] = []
        self.stale_until_refresh: dict[int, str] = {}  # tab id -> the real target id, shown only after ?refresh=1
        self.active = 0
        self.calls: list[str] = []
        self.down = False
        self.report_target_ids = True

    def add(self, tab_id, url, target_id, title=""):
        self.tabs.append({"id": tab_id, "url": url, "title": title, "targetId": target_id})

    def request(self, pane_port, method, path):
        self.calls.append(f"{method} {path}")
        if self.down:
            raise urllib.error.URLError("connection refused")
        if path.startswith("/tabs"):
            tabs = []
            for t in self.tabs:
                row = {**t, "active": t["id"] == self.active}
                if t["id"] in self.stale_until_refresh and "refresh=1" not in path:
                    row["targetId"] = "T-STALE"
                if not self.report_target_ids:
                    row["targetId"] = ""
                tabs.append(row)
            return {"ok": True, "tabs": tabs, "active": self.active, "closedTabs": 0}
        return {"ok": True}


@pytest.fixture
def world(monkeypatch):
    """A fake browser holding fake pages, a fake bridge, and the module state reset around it."""
    ba._ensure_loop()
    _reset()
    monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "9")
    monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", "10")
    ctx = FakeContext()
    browser = FakeBrowser(ctx)
    bridge = FakeBridge()
    monkeypatch.setattr(ba, "_pane_bridge_request", bridge.request)

    async def fake_connect(cdp, pane):
        return browser

    monkeypatch.setattr(ba, "_connect_pane", fake_connect)
    monkeypatch.setattr(ba, "_POPUP_POLL_SECONDS", (0.0, 0.01, 0.02))
    monkeypatch.setattr(ba, "_PANE_DISCOVERY_TIMEOUT", 0.6)

    class W:
        pass

    w = W()
    w.ctx, w.browser, w.bridge = ctx, browser, bridge

    def page(url, target_id, tab_id=None, title=""):
        p = FakePage(ctx, url, target_id)
        ctx.pages.append(p)
        if tab_id is not None:
            bridge.add(tab_id, "" if url == "about:blank" else url, target_id, title)
        return p

    w.page = page
    yield w
    _reset()


def _ensure():
    return ba._call(lambda: ba._ensure_browser())


class TestUrlMatching:
    def test_blank_tab_matches_about_blank(self):
        assert ba._url_matches("about:blank", "")
        assert ba._url_matches("", "")
        assert not ba._url_matches("https://a.example/", "")

    def test_exact_and_fragment_insensitive(self):
        assert ba._url_matches("https://a.example/x", "https://a.example/x")
        assert ba._url_matches("https://a.example/x#top", "https://a.example/x")
        assert not ba._url_matches("https://a.example/y", "https://a.example/x")

    def test_tab_label_is_readable(self):
        assert ba._tab_label({"id": 3, "title": "Docs", "url": "https://d.example/"}) == "tab 3 (Docs, https://d.example/)"
        assert ba._tab_label({"id": 4, "url": ""}) == "tab 4 (a new tab)"


class TestFollowTheActiveTab:
    def test_attaches_to_the_active_tab_by_target_id(self, world):
        a = world.page("https://a.example/", "T-A", 1)
        b = world.page("https://b.example/", "T-B", 2)
        world.bridge.active = 2
        assert _ensure() is b
        assert ba._PAGE is b and ba._PAGE_MODE == "pane"
        assert ba._TARGET_IDS[b] == "T-B"
        assert a not in ba._TARGET_IDS  # tab 1 was never even asked: the address already told them apart

    def test_two_tabs_with_the_same_address_are_told_apart_by_target_id(self, world):
        a = world.page("https://same.example/", "T-A", 1)
        b = world.page("https://same.example/", "T-B", 2)
        world.bridge.active = 1
        assert _ensure() is a
        world.bridge.active = 2
        assert _ensure() is b
        assert "could not tell them apart" not in ba._drain_notes()

    def test_the_owner_switching_tabs_is_followed_and_told(self, world):
        a = world.page("https://a.example/", "T-A", 1, "Alpha")
        b = world.page("https://b.example/", "T-B", 2, "Beta")
        world.bridge.active = 1
        assert _ensure() is a
        assert ba._drain_notes() == ""  # the first attach is not a switch
        world.bridge.active = 2
        assert _ensure() is b
        notes = ba._drain_notes()
        assert "following the tab the owner is viewing" in notes
        assert "tab 2 (Beta, https://b.example/)" in notes and "was tab 1" in notes
        assert "take a new browser_snapshot" in notes
        # back again
        world.bridge.active = 1
        assert _ensure() is a
        assert "tab 1" in ba._drain_notes()

    def test_no_notice_when_the_tab_did_not_change(self, world):
        world.page("https://a.example/", "T-A", 1)
        world.bridge.active = 1
        _ensure()
        _ensure()
        _ensure()
        assert ba._drain_notes() == ""

    def test_tab_one_closed_reattaches_to_the_active_tab(self, world):
        a = world.page("https://a.example/", "T-A", 1)
        b = world.page("https://b.example/", "T-B", 2)
        world.bridge.active = 1
        assert _ensure() is a
        # the owner closes tab 1: the page is gone, tab 2 becomes active
        a.closed = True
        world.ctx.pages.remove(a)
        world.bridge.tabs = [t for t in world.bridge.tabs if t["id"] != 1]
        world.bridge.active = 2
        assert _ensure() is b
        assert ba._PAGE is b and not ba._PAGE.is_closed()
        assert "was tab 1" in ba._drain_notes()

    def test_closing_every_tab_leaves_a_blank_one_the_agent_attaches_to(self, world):
        a = world.page("https://a.example/", "T-A", 1)
        world.bridge.active = 1
        assert _ensure() is a
        a.closed = True
        world.ctx.pages.remove(a)
        world.bridge.tabs = []
        blank = world.page("about:blank", "T-BLANK", 2)
        world.bridge.active = 2
        assert _ensure() is blank

    def test_a_page_that_shows_up_late_is_waited_for(self, world):
        world.page("https://a.example/", "T-A", 1)
        world.bridge.active = 1
        _ensure()
        world.bridge.add(2, "https://late.example/", "T-LATE")
        world.bridge.active = 2

        def appear():
            time.sleep(0.25)
            world.page("https://late.example/", "T-LATE")

        threading.Thread(target=appear, daemon=True).start()
        assert _ensure().url == "https://late.example/"

    def test_a_stale_bridge_target_id_is_refreshed_before_guessing_between_same_address_tabs(self, world):
        world.page("https://same.example/", "T-A", 1)
        b = world.page("https://same.example/", "T-B", 2)
        world.bridge.stale_until_refresh.update({1: "T-A", 2: "T-B"})
        world.bridge.active = 2
        assert _ensure() is b
        assert any("refresh=1" in c for c in world.bridge.calls)
        assert "could not tell them apart" not in ba._drain_notes()

    def test_a_stale_bridge_target_id_with_a_unique_address_needs_no_refresh(self, world):
        b = world.page("https://b.example/", "T-REAL", 1)
        world.bridge.stale_until_refresh[1] = "T-REAL"
        world.bridge.active = 1
        assert _ensure() is b
        assert not any("refresh=1" in c for c in world.bridge.calls)

    def test_without_target_ids_the_address_decides_and_ambiguity_is_disclosed(self, world):
        world.bridge.report_target_ids = False
        a = world.page("https://same.example/", "T-A", 1)
        world.page("https://same.example/", "T-B", 2)
        world.bridge.active = 2
        got = _ensure()
        assert got in (a, world.ctx.pages[1])
        assert "could not tell them apart" in ba._drain_notes()

    def test_without_target_ids_a_unique_address_is_exact(self, world):
        world.bridge.report_target_ids = False
        world.page("https://a.example/", "T-A", 1)
        b = world.page("https://b.example/", "T-B", 2)
        world.bridge.active = 2
        assert _ensure() is b
        assert "could not tell them apart" not in ba._drain_notes()

    def test_other_windows_of_the_app_are_never_mistaken_for_a_tab(self, world):
        world.page("http://127.0.0.1:8765/", "T-CONSOLE")  # the app's own window, same Chromium
        pane = world.page("https://a.example/", "T-A", 1)
        world.bridge.active = 1
        assert _ensure() is pane

    def test_a_page_whose_target_id_cannot_be_read_does_not_break_the_lookup(self, world):
        broken = world.page("https://a.example/", "T-X")
        broken.target_error = True
        good = world.page("https://a.example/", "T-A", 1)
        world.bridge.active = 1
        assert _ensure() is good


class TestNeverSilentlyFallsBack:
    def test_pane_present_but_no_page_found_is_an_error_not_a_second_chrome(self, world, monkeypatch):
        world.bridge.add(1, "https://ghost.example/", "T-GHOST")
        world.bridge.active = 1
        launched = []

        class NoLaunch:
            class chromium:  # noqa: N801
                @staticmethod
                async def launch(**kw):
                    launched.append(kw)
                    raise AssertionError("must not launch a separate Chrome while the pane exists")

        ba._PW = NoLaunch
        with pytest.raises(RuntimeError) as err:
            _ensure()
        text = str(err.value)
        assert "BROWSER PANE" in text and "did NOT fall back" in text and "tab 1" in text
        assert launched == []
        assert ba._PAGE_MODE != "headless"

    def test_a_pane_with_no_active_tab_says_so(self, world):
        with pytest.raises(RuntimeError, match="no active tab"):
            _ensure()

    def test_devtools_port_refused_while_the_bridge_answers_is_an_error(self, world, monkeypatch):
        async def refused(cdp, pane):
            raise RuntimeError("BROWSER PANE: the pane bridge answers but the DevTools port 9 does not accept a connection (x). The agent did not fall back to a separate Chrome, because the pane exists.")

        monkeypatch.setattr(ba, "_connect_pane", refused)
        with pytest.raises(RuntimeError, match="did not fall back"):
            _ensure()

    def test_bridge_gone_means_no_pane_so_a_separate_chrome_is_allowed_but_disclosed(self, world, monkeypatch):
        async def gone(cdp, pane):
            raise ba._PaneUnreachable("could not reach the Electron pane bridge")

        monkeypatch.setattr(ba, "_connect_pane", gone)
        launched = FakeContext()

        class FakeLaunched:
            async def new_context(self, **kw):
                return launched

        class FakePW:
            class chromium:  # noqa: N801
                @staticmethod
                async def launch(**kw):
                    return FakeLaunched()

        ba._PW = FakePW
        page = _ensure()
        assert page.url == "about:blank" and ba._PAGE_MODE == "headless"
        notes = ba._drain_notes()
        assert "embedded browser pane is not reachable" in notes and "separate headless Chrome" in notes

    def test_when_the_pane_comes_back_the_agent_returns_to_it(self, world, monkeypatch):
        real_connect = ba._connect_pane
        state = {"down": True}

        async def flaky(cdp, pane):
            if state["down"]:
                raise ba._PaneUnreachable("down")
            return await real_connect(cdp, pane)

        monkeypatch.setattr(ba, "_connect_pane", flaky)
        launched = FakeContext()

        class FakeLaunched:
            async def new_context(self, **kw):
                return launched

        class FakePW:
            class chromium:  # noqa: N801
                @staticmethod
                async def launch(**kw):
                    return FakeLaunched()

        ba._PW = FakePW
        first = _ensure()
        assert ba._PAGE_MODE == "headless"
        assert _ensure() is first  # still down: the open separate page keeps working
        state["down"] = False
        pane_page = world.page("https://a.example/", "T-A", 1)
        world.bridge.active = 1
        assert _ensure() is pane_page and ba._PAGE_MODE == "pane"


class TestPopupsAndActionFollowing:
    def _on(self, world):
        a = world.page("https://a.example/", "T-A", 1, "Alpha")
        world.bridge.active = 1
        assert _ensure() is a
        ba._drain_notes()
        return a

    def test_an_action_that_opens_a_foreground_tab_is_followed(self, world):
        a = self._on(world)

        async def act():
            mark = ba._action_mark()
            # the click: the shell opens tab 2 and makes it active
            popup = world.page("https://pop.example/", "T-POP", 2, "Popup")
            world.bridge.active = 2
            return await ba._settle_after_action(a, mark), popup

        got, popup = ba._call(act)
        assert got is popup and ba._PAGE is popup
        notes = ba._drain_notes()
        assert "your action opened tab 2 (Popup, https://pop.example/)" in notes and "switched to it" in notes
        assert "following the tab the owner is viewing" not in notes  # one specific notice, not two

    def test_an_action_that_opens_a_background_tab_stays_put(self, world):
        a = self._on(world)

        async def act():
            mark = ba._action_mark()
            world.page("https://bg.example/", "T-BG", 2, "Background")
            return await ba._settle_after_action(a, mark)

        assert ba._call(act) is a
        notes = ba._drain_notes()
        assert "in the background" in notes and "stays on its tab" in notes and "tab 2" in notes

    def test_an_action_that_closes_its_own_tab_reattaches(self, world):
        a = world.page("https://a.example/", "T-A", 1, "Alpha")
        b = world.page("https://b.example/", "T-B", 2, "Beta")
        world.bridge.active = 1
        assert _ensure() is a
        ba._drain_notes()

        async def act():
            mark = ba._action_mark()
            a.closed = True
            world.ctx.pages.remove(a)
            world.bridge.tabs = [t for t in world.bridge.tabs if t["id"] != 1]
            world.bridge.active = 2
            return await ba._settle_after_action(a, mark)

        assert ba._call(act) is b
        notes = ba._drain_notes()
        assert "closed the tab it was on" in notes
        assert "following the tab the owner is viewing" not in notes  # one specific notice, not two

    def test_an_action_that_changes_nothing_returns_the_same_page_quickly(self, world):
        a = self._on(world)

        async def act():
            mark = ba._action_mark()
            start = time.monotonic()
            got = await ba._settle_after_action(a, mark)
            return got, time.monotonic() - start

        got, took = ba._call(act)
        assert got is a and took < 1.0
        assert ba._drain_notes() == ""

    def test_a_bridge_that_drops_mid_action_does_not_break_the_action(self, world):
        a = self._on(world)

        async def act():
            mark = ba._action_mark()
            world.bridge.down = True
            return await ba._settle_after_action(a, mark)

        assert ba._call(act) is a


class TestNotesRideOnTheResult:
    def test_a_note_is_appended_once(self, monkeypatch):
        ba._NOTES.clear()

        async def work():
            ba._note("something changed")
            return "RESULT"

        out = ba._call(work)
        assert out == "RESULT\nNOTE: something changed"
        assert ba._call(lambda: _return("PLAIN")) == "PLAIN"

    def test_a_note_survives_an_error_for_the_next_call(self):
        ba._NOTES.clear()

        async def boom():
            ba._note("tab followed")
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            ba._call(boom)
        assert ba._call(lambda: _return("NEXT")) == "NEXT\nNOTE: tab followed"

    def test_duplicate_notes_are_collapsed(self):
        ba._NOTES.clear()
        ba._note("a")
        ba._note("a")
        ba._note("b")
        assert ba._drain_notes() == "\nNOTE: a\nNOTE: b"

    def test_non_text_results_are_left_alone(self):
        ba._NOTES.clear()
        ba._note("x")
        assert ba._call(lambda: _return(42)) == 42
        ba._NOTES.clear()


async def _return(value):
    return value


# --------------------------------------------------------------------------- #
# layer two: a real Chrome, a real DevTools port, a fake pane bridge with real target ids
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    profile = tempfile.mkdtemp(prefix="dm-c1-chrome-")
    cdp_port = _free_port()
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={cdp_port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1).read()
                break
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.2)
        else:
            pytest.skip("Chrome did not open its DevTools port")

        site_root = Path(profile) / "site"
        site_root.mkdir()
        (site_root / "a.html").write_text("<title>Page A</title><input id=f aria-label='Field A'><a id=l href='/pop.html' target=_blank>open popup</a><button onclick=\"window.close()\">Close me</button>")
        (site_root / "b.html").write_text("<title>Page B</title><input id=f aria-label='Field B'>")
        (site_root / "pop.html").write_text("<title>Popup</title><input id=f aria-label='Popup field'>")

        from http.server import SimpleHTTPRequestHandler

        class Site(SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(site_root), **k)

            def log_message(self, *args):
                pass

        site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{site.server_address[1]}"

        rig = _Rig(cdp_port, base)
        bridge = HTTPServer(("127.0.0.1", 0), rig.handler())
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        rig.bridge_port = bridge.server_address[1]
        yield rig
        bridge.shutdown()
        site.shutdown()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15)


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Google Chrome is not installed")
class TestRealChromeWithRealTargetIds:
    @pytest.fixture(autouse=True)
    def _env(self, rig, monkeypatch):
        ba._ensure_loop()
        _reset()
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", str(rig.cdp_port))
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(rig.bridge_port))
        monkeypatch.setattr(ba, "_POPUP_POLL_SECONDS", (0.0, 0.15, 0.4))
        rig.reset()
        yield
        _reset()

    def test_follows_the_owner_between_real_tabs_with_the_same_address(self, rig):
        t1 = rig.open(rig.base + "/b.html")
        t2 = rig.open(rig.base + "/b.html")
        rig.active = t1
        first = ba.browser_snapshot({})
        assert "TITLE: Page B" in first and "TAB: tab 1" in first
        field = re.search(r"- (e\d+) \[input\] 'Field B'", first).group(1)
        ba.browser_fill({"target": field, "value": "typed in tab one"})
        rig.active = t2
        second = ba.browser_snapshot({})
        assert "NOTE: following the tab the owner is viewing" in second and "tab 2" in second
        assert ba._TARGET_IDS[ba._PAGE] == rig.target_of(t2)
        # the field of tab 2 is empty: the earlier fill went to tab 1 only
        assert "value='typed in tab one'" not in second
        rig.active = t1
        third = ba.browser_snapshot({})
        assert "value='typed in tab one'" in third

    def test_an_id_from_one_tab_is_refused_on_another(self, rig):
        t1 = rig.open(rig.base + "/a.html")
        t2 = rig.open(rig.base + "/b.html")
        rig.active = t1
        snap = ba.browser_snapshot({})
        field = re.search(r"- (e\d+) \[input\] 'Field A'", snap).group(1)
        rig.active = t2
        with pytest.raises(RuntimeError, match="different tab"):
            ba.browser_fill({"target": field, "value": "x"})

    def test_a_target_blank_click_opens_a_tab_the_agent_follows(self, rig):
        t1 = rig.open(rig.base + "/a.html")
        rig.active = t1
        snap = ba.browser_snapshot({})
        link = re.search(r"- (e\d+) \[a\] 'open popup'", snap).group(1)
        rig.auto_activate_new = True
        out = ba.browser_click({"target": link})
        assert "TITLE: Popup" in out
        assert "NOTE: your action opened tab 2" in out and "switched to it" in out
        assert ba._TARGET_IDS[ba._PAGE] == rig.target_of(rig.active)

    def test_tab_one_closed_reattaches_and_says_so(self, rig):
        t1 = rig.open(rig.base + "/a.html")
        t2 = rig.open(rig.base + "/b.html")
        rig.active = t1
        assert "TITLE: Page A" in ba.browser_snapshot({})
        rig.close(t1)
        rig.active = t2
        out = ba.browser_snapshot({})
        assert "TITLE: Page B" in out and "NOTE:" in out and "was tab" in out
        assert not ba._PAGE.is_closed()

    def test_the_page_closing_itself_is_followed(self, rig):
        t1 = rig.open(rig.base + "/b.html")
        t2 = rig.open(rig.base + "/a.html")
        rig.active = t2
        snap = ba.browser_snapshot({})
        button = re.search(r"- (e\d+) \[button\] 'Close me'", snap).group(1)
        rig.on_page_closed = lambda: setattr(rig, "active", t1)
        out = ba.browser_click({"target": button})
        assert "TITLE: Page B" in out and "closed the tab it was on" in out

    def test_without_target_ids_from_the_bridge_the_address_is_used(self, rig):
        t1 = rig.open(rig.base + "/a.html")
        rig.open(rig.base + "/b.html")
        rig.active = t1
        rig.report_target_ids = False
        assert "TITLE: Page A" in ba.browser_snapshot({})
        rig.report_target_ids = True

    def test_nothing_is_launched_when_the_pane_page_is_missing(self, rig):
        rig.fake_tab(99, "https://not-in-this-chrome.example/")
        rig.active = 99
        with pytest.raises(RuntimeError, match="did NOT fall back"):
            ba.browser_snapshot({})
        assert ba._PAGE_MODE != "headless"


class _Rig:
    """A real Chrome plus a fake pane bridge: tab ids are the order of opening, the target ids and
    addresses come from Chrome's own /json/list, and ``active`` is whatever the test says."""

    def __init__(self, cdp_port, base):
        self.cdp_port = cdp_port
        self.base = base
        self.bridge_port = 0
        self.lock = threading.Lock()
        self.tab_targets: dict[int, str] = {}
        self.fake: dict[int, str] = {}
        self.active = 0
        self.next_id = 1
        self.report_target_ids = True
        self.auto_activate_new = False
        self.on_page_closed = None
        self._known: set[str] = set()

    def _json(self, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.cdp_port}{path}", method=method)
        raw = urllib.request.urlopen(req, timeout=10).read().decode()
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    def pages(self):
        return [p for p in self._json("/json/list") if p.get("type") == "page"]

    def reset(self):
        for p in self.pages():
            self._json(f"/json/close/{p['id']}")
        time.sleep(0.2)
        with self.lock:
            self.tab_targets.clear()
            self.fake.clear()
            self.active = 0
            self.next_id = 1
            self.auto_activate_new = False
            self.on_page_closed = None
            self.report_target_ids = True
            self._known = set()

    def open(self, url):
        page = self._json(f"/json/new?{url}", method="PUT")
        with self.lock:
            tab = self.next_id
            self.next_id += 1
            self.tab_targets[tab] = page["id"]
            self._known.add(page["id"])
        time.sleep(0.4)
        return tab

    def close(self, tab):
        self._json(f"/json/close/{self.tab_targets[tab]}")
        with self.lock:
            self.tab_targets.pop(tab, None)
        time.sleep(0.3)

    def fake_tab(self, tab, url):
        with self.lock:
            self.fake[tab] = url

    def target_of(self, tab):
        return self.tab_targets[tab]

    def _sync_new_pages(self):
        """Chrome opened a tab by itself (a popup): give it the next tab id, like the shell does."""
        live = {p["id"]: p for p in self.pages()}
        for tid in list(live):
            if tid not in self._known:
                tab = self.next_id
                self.next_id += 1
                self.tab_targets[tab] = tid
                self._known.add(tid)
                if self.auto_activate_new:
                    self.active = tab
        for tab, tid in list(self.tab_targets.items()):
            if tid not in live:
                del self.tab_targets[tab]
                if self.on_page_closed and self.active == tab:
                    self.on_page_closed()

    def tabs_view(self):
        with self.lock:
            self._sync_new_pages()
            live = {p["id"]: p for p in self.pages()}
            rows = []
            for tab, tid in sorted(self.tab_targets.items()):
                page = live.get(tid)
                if page is None:
                    continue
                url = page.get("url", "")
                rows.append({
                    "id": tab, "url": "" if url == "about:blank" else url, "title": page.get("title", ""),
                    "active": tab == self.active, "targetId": tid if self.report_target_ids else "",
                })
            for tab, url in self.fake.items():
                rows.append({"id": tab, "url": url, "title": "", "active": tab == self.active, "targetId": "FAKE-TARGET" if self.report_target_ids else ""})
            return {"ok": True, "tabs": rows, "active": self.active, "closedTabs": 0}

    def handler(self):
        rig = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/tabs"):
                    self._send(rig.tabs_view())
                else:
                    self._send({"ok": True})

            def do_POST(self):  # noqa: N802
                self._send({"ok": True})

        return Handler
