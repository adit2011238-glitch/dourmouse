"""SPLIT: the BROWSER split view in a real browser (headless Google Chrome, skipped when it is not installed).

The real screen module (ui/assets/os/screens/browser/index.js) is mounted in a copy of the shell's markup, with the
shell's real chat, approvals, keymap and screen-context code, and a fake Electron pane that records every
bounds/show/hide call. What is checked is the thing that cannot be faked in node: the rectangles. The native view
is drawn above the page's DOM, so the bounds the screen sends must be the 80 percent region and never the column.

Nothing here touches port 8765, the owner's profile or a model: the chat stream is a stub that the test drives.
Also covers two findings that moved from FU to this agent: U1-1 (reload bypassed the proxy) and U1-2 (a pane
request was consumed after the screen was left).
"""

from __future__ import annotations

import http.server
import os
import socketserver
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_UI = Path(os.environ["SPLIT_UI_ROOT"]) if os.environ.get("SPLIT_UI_ROOT") else _ROOT / "ui"  # a scratch copy lets a test be run against an older file
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
pytestmark = pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is not installed")

HARNESS_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>split harness</title>
<link rel="stylesheet" href="/assets/dourmouse-ui.css">
<link rel="stylesheet" href="/assets/dourmouse-os.css">
<link rel="stylesheet" href="/assets/os/shell.css">
<link rel="stylesheet" href="/assets/os/kit/kit.css">
<link rel="stylesheet" href="/assets/os/screens/browser/browser.css" data-screen-css="BROWSER">
</head><body>
<div id="shell" data-sidebar="shown">
  <nav id="sidebar" aria-label="Screens"></nav>
  <main id="stage"><div id="stagebar"><h1 id="stagetitle">BROWSER</h1><span id="stagesub"></span><span class="grow"></span><div id="stageactions"></div></div>
  <div id="body"></div><div class="composer" id="composer" hidden></div></main>
</div>
<script type="module" src="/__harness.js"></script></body></html>"""

HARNESS_JS = r"""
import screen from '/assets/os/screens/browser/index.js';
import { putPaneRequest, takePaneRequest, hasPaneRequest } from '/assets/os/core/pane-inbox.js';
import { createScreenCtx, createTimers } from '/assets/os/core/ctx.js';
import { createChat } from '/assets/os/core/chat.js';
import { createApprovals } from '/assets/os/core/approvals.js';
import { createKeymap } from '/assets/os/core/keymap.js';
import { createScope } from '/assets/os/core/scope.js';
import { html, esc, raw } from '/assets/os/kit/html.js';
import { states } from '/assets/os/kit/states.js';
import { approvalCard } from '/assets/os/kit/approval-card.js';
import { confirmHere } from '/assets/os/kit/confirm-card.js';

const q = new URLSearchParams(location.search);
const H = (window.H = {
  bounds: [], shows: 0, hides: 0, navigates: [], navs: [], api: [], notes: [], sub: '', actions: [],
  shellComposer: [], prefs: new Map(), listeners: new Set(), controlListeners: new Set(),
  stream: { bodies: [], runs: [] }, state: null, controlState: null, controlCalls: [], stateGate: null,
});
const nopane = q.get('nopane') === '1';
H.state = { open: false, url: 'http://127.0.0.1/shop/cart', title: 'Your cart', loading: false, canGoBack: false, canGoForward: false,
  tabs: [{ id: 1, url: 'http://127.0.0.1/shop/cart', title: 'Your cart', active: true, loading: false }], activeTab: 1, zoom: 1, closedTabs: 0, blockedPopups: 0 };
if (q.get('empty') === '1') { H.state.url = ''; H.state.title = ''; H.state.open = false; H.state.tabs = []; }
H.controlState = { state: 'idle', acting: [], activeTab: 1 };

const copy = (o) => JSON.parse(JSON.stringify(o));
const ok = () => Promise.resolve({ ok: true });
const pane = nopane ? null : {
  async state() { if (H.stateGate) await H.stateGate; return copy(H.state); },
  bounds(b) { H.bounds.push(b); return ok(); },
  show() { H.shows += 1; H.state.open = true; return ok(); },
  hide() { H.hides += 1; H.state.open = false; return ok(); },
  async navigate(u) { H.navigates.push(u); H.state.url = u; H.state.open = true; return { ok: true }; },
  nav(a) { H.navs.push(a); return ok(); },
  newTab: ok, selectTab: ok, closeTab: ok, reopenTab: ok, find: ok, findStop: ok, focusPage: ok, zoom: ok, print: ok,
  clearDownloads: ok, downloadAction: ok, screen: ok,
  async downloads() { return []; },
  onDownloads() { return () => {}; },
  onCommand() { return () => {}; },
  control: {
    async state() { return copy(H.controlState); },
    onUpdate(fn) { H.controlListeners.add(fn); return () => H.controlListeners.delete(fn); },
    stop() { H.controlCalls.push('stop'); return ok(); },
    take() { H.controlCalls.push('take'); return ok(); },
    release() { H.controlCalls.push('release'); return ok(); },
  },
};
const host = {
  kind: nopane ? 'browser' : 'electron', pane,
  onPaneState(fn) { H.listeners.add(fn); return () => H.listeners.delete(fn); },
  openExternal: () => false, paneListenerCount: () => H.listeners.size,
};
H.setPane = (patch) => { Object.assign(H.state, patch); H.listeners.forEach((fn) => fn(copy(H.state))); };
H.setControl = (s) => { H.controlState = s; H.controlListeners.forEach((fn) => fn(copy(s))); };

function makeApi(signal) {
  const api = {
    withSignal: (sig) => makeApi(sig),
    async get(path) {
      H.api.push(['GET', path]);
      if (path.startsWith('/api/os/browser/attached')) return { attached: false };
      if (path.startsWith('/api/os/browser/bookmarks')) return { bookmarks: [] };
      if (path.startsWith('/api/os/session')) return { turns: [] };
      if (path.includes('settings/privacy')) return { chat: { private: false }, private_mode: false };
      return {};
    },
    async post(path, body) { H.api.push(['POST', path, body]); return { ok: true }; },
    stream(path, body, onEvent, opts) {
      H.stream.bodies.push(body);
      return new Promise((resolve, reject) => {
        const run = { onEvent, resolve, reject, aborted: false };
        H.stream.runs.push(run);
        const sig = opts && opts.signal;
        if (sig) sig.addEventListener('abort', () => { run.aborted = true; const e = new Error('aborted'); e.name = 'AbortError'; reject(e); });
      });
    },
  };
  return api;
}
H.emit = (evt) => H.stream.runs[H.stream.runs.length - 1].onEvent(evt);
H.finish = () => H.stream.runs[H.stream.runs.length - 1].resolve();

const baseApi = makeApi();
const scope = createScope({ storage: null });
const approvals = createApprovals({ api: baseApi, scope });
const chat = createChat({ api: baseApi, scope, approvals, threadScreens: ['HOME'], storage: null });
const keymap = createKeymap();
keymap.attach(document);
const timers = createTimers();
const prefs = { read: (k) => (H.prefs.has(k) ? H.prefs.get(k) : null), write: (k, v) => { H.prefs.set(k, v); } };
const chrome = {
  setActions: (a) => { H.actions = a; }, setSub: (t) => { H.sub = t; }, setLive: () => {},
  setComposer: (o) => { H.shellComposer.push(o ? 'options' : null); }, focusComposer: () => {},
};
const events = { scope: () => ({ on: () => () => {}, onResync: () => () => {}, onStatus: () => () => {}, dispose() {} }) };
function renderApproval(container, evt, screenId) {
  const key = chat.keyFor(screenId);
  const entry = approvals.add(key, evt) || approvals.get(evt.id);
  if (!entry) return null;
  const card = approvalCard(entry, (okk) => approvals.decide(entry.id, okk));
  const off = approvals.onChange((e) => { if (e.id === entry.id) card.paint(); });
  container.append(card.el);
  return { ...card, entry, off };
}
const kit = { html, esc, raw, states, approvalCard, confirmHere };
const overlays = { open: () => Boolean(H.overlay), onChange: () => () => {} };
const deps = { api: baseApi, events, chat, approvals, scope, host, prefs, keymap, timers, toasts: { show: (n) => H.notes.push(n) }, chrome, overlays, kit, renderApproval };

const body = document.getElementById('body');
H.mount = async () => {
  const root = document.createElement('div');
  root.dataset.screen = 'BROWSER';
  root.dataset.region = '';
  body.replaceChildren(root);
  const sc = createScreenCtx({ id: 'BROWSER', root, deps });
  H.sc = sc; H.root = root; H.ctx = sc.ctx;
  await screen.mount(root, sc.ctx);
  return true;
};
H.unmount = () => { screen.unmount(H.ctx); H.sc.dispose(); body.replaceChildren(); };
H.sidebar = (hidden) => { document.getElementById('shell').dataset.sidebar = hidden ? 'hidden' : 'shown'; };
H.takePaneRequest = takePaneRequest;
H.putPaneRequest = putPaneRequest;
H.hasPaneRequest = hasPaneRequest;
H.pendingApproval = () => approvals.pending('HOME');
H.ready = true;
"""

RECTS_JS = """() => {
  const r = (sel) => { const n = document.querySelector(sel); if (!n) return null; const b = n.getBoundingClientRect(); return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width, height: b.height, hidden: getComputedStyle(n).display === 'none' || b.width === 0 }; };
  const last = window.H.bounds.length ? window.H.bounds[window.H.bounds.length - 1] : null;
  const s = document.querySelector('.bw-split');
  return { split: r('.bw-split'), left: r('.bw-left'), div: r('.bw-sdiv'), chat: r('.bw-chat'), body: r('.bw-chat-body'), rail: r('.bw-rail'),
    page: r('.cb-page'), bounds: last, shows: window.H.shows, hides: window.H.hides,
    mode: s ? s.dataset.mode : null, side: s ? s.dataset.side : null };
}"""


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401
        pass

    def translate_path(self, path):
        path = path.split("?")[0].split("#")[0]
        return str(_UI / path.lstrip("/"))

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path in ("/__harness.html", "/__harness.js", "/api/browser-pane/proxy"):
            body, ctype = {
                "/__harness.html": (HARNESS_HTML, "text/html; charset=utf-8"),
                "/__harness.js": (HARNESS_JS, "text/javascript; charset=utf-8"),
                "/api/browser-pane/proxy": ("<!doctype html><title>proxied</title>proxied page", "text/html; charset=utf-8"),
            }[path]
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()


@pytest.fixture(scope="module")
def server():
    class Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = Srv(("127.0.0.1", 0), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=str(CHROME), headless=True)
        yield b
        b.close()


def open_harness(browser, server, query="", width=1600, height=900):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
    page.goto(f"{server}/__harness.html{query}")
    page.wait_for_function("window.H && window.H.ready")
    page.errors = errors  # type: ignore[attr-defined]
    return page


def mount(page):
    page.evaluate("window.H.mount()")
    settle(page)


def settle(page, ms=250):
    page.wait_for_timeout(ms)
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")


def rects(page):
    return page.evaluate(RECTS_JS)


def drag_to(page, x):
    box = page.locator(".bw-sdiv").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move((cx + x) / 2, cy, steps=4)
    page.mouse.move(x, cy, steps=4)
    return page.mouse


def share(r):
    return r["chat"]["width"] / r["split"]["width"]


# --------------------------------------------------------------------------- #
# layout and the native view's bounds
# --------------------------------------------------------------------------- #


class TestTheDefaultSplit:
    def test_the_page_takes_about_80_percent_on_the_left_and_the_column_the_rest_on_the_right(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        r = rects(page)
        assert r["mode"] == "split" and r["side"] == "right"
        assert 0.19 <= share(r) <= 0.23, share(r)
        assert 0.76 <= r["left"]["width"] / r["split"]["width"] <= 0.82
        assert r["chat"]["left"] >= r["left"]["right"] - 1, "the column sits to the right of the page region"
        assert r["chat"]["width"] >= 280
        page.close()

    def test_the_native_view_is_placed_inside_the_left_region_and_never_reaches_the_column(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        r = rects(page)
        b = r["bounds"]
        assert b is not None and r["shows"] >= 1, "the page is shown with real bounds"
        assert b["x"] + b["width"] <= r["chat"]["left"], (b, r["chat"])
        assert b["x"] + b["width"] <= r["left"]["right"]
        assert b["x"] + b["width"] >= r["left"]["right"] - 4, "and it fills the left region"
        assert abs(b["x"] - r["page"]["left"]) <= 1 and abs(b["y"] - r["page"]["top"]) <= 1
        assert b["width"] / r["split"]["width"] >= 0.74
        assert not page.errors, page.errors
        page.close()

    def test_the_shells_own_bottom_composer_is_never_used_on_this_screen(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.locator(".bw-chat-input").fill("hello")
        calls = page.evaluate("window.H.shellComposer")
        assert calls and all(c is None for c in calls), calls
        assert page.evaluate("document.getElementById('composer').hidden") is True
        page.close()


class TestTheDivider:
    def test_dragging_to_about_30_percent_moves_the_view_with_it_and_hides_it_while_the_pointer_is_down(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        r0 = rects(page)
        target = r0["split"]["right"] - 0.30 * r0["split"]["width"]
        mouse = drag_to(page, target)
        mid = rects(page)
        assert mid["hides"] > r0["hides"], "the native view steps aside while dragging (it would swallow the pointer)"
        mouse.up()
        settle(page)
        r = rects(page)
        assert 0.28 <= share(r) <= 0.32, share(r)
        b = r["bounds"]
        assert b["x"] + b["width"] <= r["chat"]["left"]
        assert b["x"] + b["width"] >= r["left"]["right"] - 4
        assert r["shows"] > mid["shows"] - 1 and r["shows"] >= 2, "and comes back after the drop"
        assert page.evaluate("window.H.prefs.get('dm.os.browser.splitRatio')") is not None
        page.close()

    def test_the_column_never_gets_narrower_than_280_pixels_or_wider_than_45_percent(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        r0 = rects(page)
        drag_to(page, r0["split"]["left"] + 20).up()
        settle(page)
        wide = rects(page)
        assert abs(share(wide) - 0.45) <= 0.01, share(wide)
        drag_to(page, wide["split"]["right"] - 2).up()
        settle(page)
        narrow = rects(page)
        assert abs(narrow["chat"]["width"] - 280) <= 1, narrow["chat"]
        page.close()

    def test_a_double_click_on_the_divider_goes_back_to_20_percent(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        r0 = rects(page)
        drag_to(page, r0["split"]["right"] - 0.40 * r0["split"]["width"]).up()
        settle(page)
        assert share(rects(page)) > 0.35
        page.locator(".bw-sdiv").dblclick()
        settle(page)
        r = rects(page)
        assert 0.19 <= share(r) <= 0.23, share(r)
        assert page.evaluate("window.H.prefs.get('dm.os.browser.splitRatio')") == "0.2"
        b = r["bounds"]
        assert b["x"] + b["width"] <= r["chat"]["left"]
        page.close()

    def test_arrow_keys_on_the_divider_resize_it_and_the_divider_says_its_value(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        before = rects(page)["chat"]["width"]
        page.locator(".bw-sdiv").focus()
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowLeft")
        settle(page)
        after = rects(page)["chat"]["width"]
        assert after == before + 40
        div = page.locator(".bw-sdiv")
        assert div.get_attribute("role") == "separator" and div.get_attribute("aria-orientation") == "vertical"
        assert int(div.get_attribute("aria-valuenow")) == round(after)
        page.close()


class TestCollapse:
    def test_collapsing_leaves_a_slim_rail_gives_the_page_the_width_and_is_remembered(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        before = rects(page)
        page.locator(".bw-chat-hide").click()
        settle(page)
        r = rects(page)
        assert r["mode"] == "rail"
        assert r["chat"]["width"] <= 40 and r["left"]["width"] > before["left"]["width"] + 200
        assert r["bounds"]["x"] + r["bounds"]["width"] <= r["rail"]["left"]
        assert r["bounds"]["x"] + r["bounds"]["width"] >= r["left"]["right"] - 4
        assert page.evaluate("window.H.prefs.get('dm.os.browser.splitCollapsed')") == "1"
        page.locator(".bw-rail").click()
        settle(page)
        back = rects(page)
        assert back["mode"] == "split" and abs(back["chat"]["width"] - before["chat"]["width"]) <= 1
        assert back["bounds"]["x"] + back["bounds"]["width"] <= back["chat"]["left"]
        assert page.evaluate("window.H.prefs.get('dm.os.browser.splitCollapsed')") == "0"
        page.close()

    def test_a_remount_remembers_the_ratio_the_side_and_the_collapsed_state(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        r0 = rects(page)
        drag_to(page, r0["split"]["right"] - 0.33 * r0["split"]["width"]).up()
        settle(page)
        page.locator(".bw-chat-side").click()
        settle(page)
        r1 = rects(page)
        assert r1["side"] == "left" and r1["chat"]["right"] <= r1["left"]["left"] + 1
        page.evaluate("window.H.unmount()")
        mount(page)
        r2 = rects(page)
        assert r2["side"] == "left" and abs(r2["chat"]["width"] - r1["chat"]["width"]) <= 1
        b = r2["bounds"]
        assert b["x"] >= r2["chat"]["right"], "the view starts to the right of the column"
        assert b["x"] + b["width"] <= r2["left"]["right"] + 1
        page.locator(".bw-chat-hide").click()
        settle(page)
        page.evaluate("window.H.unmount()")
        mount(page)
        assert rects(page)["mode"] == "rail"
        page.close()


class TestSmallScreens:
    def test_under_900_pixels_the_column_is_an_overlay_button_and_the_page_is_not_squeezed(self, browser, server):
        page = open_harness(browser, server, width=1000)
        mount(page)
        r = rects(page)
        assert r["split"]["width"] < 900
        assert r["mode"] == "overlay-closed"
        assert r["chat"]["width"] <= 40 and r["left"]["width"] >= r["split"]["width"] - 40
        closed_left = r["left"]["width"]
        page.locator(".bw-rail").click()
        settle(page)
        o = rects(page)
        assert o["mode"] == "overlay-open"
        assert abs(o["left"]["width"] - closed_left) <= 1, "opening the overlay does not resize the page region"
        assert o["body"]["width"] >= 240 and not o["body"]["hidden"]
        assert o["bounds"]["x"] + o["bounds"]["width"] <= o["body"]["left"], "the native view stops where the overlay starts"
        page.locator(".bw-rail").click()
        settle(page)
        c = rects(page)
        assert c["mode"] == "overlay-closed" and c["bounds"]["x"] + c["bounds"]["width"] >= c["left"]["right"] - 4
        page.close()

    def test_hiding_the_sidebar_switches_a_narrow_screen_to_the_side_by_side_layout_and_the_bounds_follow(self, browser, server):
        page = open_harness(browser, server, width=1100)
        mount(page)
        narrow = rects(page)
        assert narrow["mode"] == "overlay-closed"
        page.evaluate("window.H.sidebar(true)")
        settle(page)
        wide = rects(page)
        assert wide["split"]["width"] >= 900 and wide["mode"] == "split"
        assert wide["bounds"]["x"] + wide["bounds"]["width"] <= wide["chat"]["left"]
        assert wide["bounds"]["x"] + wide["bounds"]["width"] >= wide["left"]["right"] - 4
        page.evaluate("window.H.sidebar(false)")
        settle(page)
        back = rects(page)
        assert back["mode"] == "overlay-closed"
        assert back["bounds"]["x"] + back["bounds"]["width"] >= back["left"]["right"] - 4
        page.close()

    def test_resizing_the_window_re_places_the_view(self, browser, server):
        page = open_harness(browser, server, width=1600)
        mount(page)
        page.set_viewport_size({"width": 1300, "height": 900})
        settle(page)
        r = rects(page)
        b = r["bounds"]
        assert b["x"] + b["width"] <= r["chat"]["left"] and b["x"] + b["width"] >= r["left"]["right"] - 4
        page.close()


# --------------------------------------------------------------------------- #
# the screen's other parts keep working inside the left region
# --------------------------------------------------------------------------- #


class TestTheBrowserKeepsWorkingInTheLeftRegion:
    def test_the_tab_strip_address_bar_and_find_bar_live_in_the_left_region(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        inside = page.evaluate("""() => { const l = document.querySelector('.bw-left'); return ['#bwTabs', '#bwAddr', '#bwFind', '#bwBm', '#bwControl', '#bwWatch', '#bwFoot', '#bwPage'].map((s) => l.contains(document.querySelector(s))); }""")
        assert all(inside), inside
        page.close()

    def test_the_history_panel_opens_in_the_left_region_hides_the_native_view_and_leaves_the_column_alone(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        hides = rects(page)["hides"]
        page.locator("#bwHist").click()
        settle(page)
        assert page.evaluate("document.querySelector('.bw-left').contains(document.querySelector('.bw-panel'))")
        assert rects(page)["hides"] > hides, "the native view is hidden while a panel is drawn in the page area"
        assert not rects(page)["body"]["hidden"], "the chat column stays usable"
        page.locator("#bwHist").click()
        settle(page)
        r = rects(page)
        assert r["bounds"]["x"] + r["bounds"]["width"] <= r["chat"]["left"]
        page.close()

    def test_the_page_width_presets_still_centre_the_page_inside_the_left_region(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.locator('[data-preset="phone"]').click()
        settle(page)
        r = rects(page)
        b = r["bounds"]
        assert 360 <= b["width"] <= 382, b
        assert b["x"] >= r["left"]["left"] and b["x"] + b["width"] <= r["left"]["right"]
        page.close()

    def test_the_old_standalone_control_bar_is_hidden_while_the_column_shows_it(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.evaluate("window.H.setControl({ state: 'model-acting', acting: [{ tool: 'click', tabId: 1 }], activeTab: 1 })")
        settle(page)
        assert page.locator(".bw-chat .cb-control").text_content().startswith("Model is acting")
        assert page.evaluate("getComputedStyle(document.querySelector('.cb-control-slot')).display") == "none"
        page.locator(".bw-chat .cb-control [data-control='stop']").click()
        page.locator(".bw-chat .cb-control [data-control='take']").click()
        assert page.evaluate("window.H.controlCalls") == ["stop", "take"]
        page.locator(".bw-chat-hide").click()
        settle(page)
        assert page.evaluate("getComputedStyle(document.querySelector('.cb-control-slot')).display") != "none"
        page.close()


# --------------------------------------------------------------------------- #
# the chat column
# --------------------------------------------------------------------------- #


class TestTheChatColumn:
    def test_the_column_shows_the_current_tab_and_follows_it(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        ctx = page.locator(".bw-chat-ctx")
        assert "Your cart" in ctx.inner_text() and "127.0.0.1" in ctx.inner_text()
        page.evaluate("window.H.setPane({ url: 'http://127.0.0.1/news', title: 'Today news', tabs: [{ id: 1, url: 'http://127.0.0.1/news', title: 'Today news', active: true }] })")
        settle(page)
        assert "Today news" in ctx.inner_text()
        page.close()

    def test_the_empty_state_offers_three_starters_and_a_starter_goes_to_the_browser_agent(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        chips = page.locator(".bw-chat .st-sugg")
        assert [chips.nth(i).text_content() for i in range(chips.count())] == ["Summarise this page", "Find the price", "Fill this form (I'll confirm)"]
        chips.nth(0).click()
        settle(page)
        body = page.evaluate("window.H.stream.bodies[0]")
        assert body["focus_agent"] == "browser" and "Summarise" in body["prompt"]
        assert body["screen"] == "HOME"
        assert "Do not click" in body["prompt"]
        page.close()

    def test_the_fill_starter_tells_the_model_to_ask_before_anything_is_sent(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.locator(".bw-chat .st-sugg").nth(2).click()
        settle(page)
        body = page.evaluate("window.H.stream.bodies[0]")
        assert body["focus_agent"] == "browser" and "Ask me before" in body["prompt"]
        page.close()

    def test_typing_in_the_column_sends_to_the_browser_agent_with_enter_and_shift_enter_makes_a_new_line(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        box = page.locator(".bw-chat-input")
        box.click()
        page.keyboard.type("line one")
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("line two")
        assert box.input_value() == "line one\nline two"
        page.keyboard.press("Enter")
        settle(page)
        body = page.evaluate("window.H.stream.bodies[0]")
        assert body["prompt"] == "line one\nline two" and body["focus_agent"] == "browser"
        assert box.input_value() == ""
        assert "line one" in page.locator(".bw-chat .turn-you").first.inner_text()
        page.close()

    def test_while_the_model_works_the_column_shows_stop_and_stop_ends_the_run(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        stop = page.locator(".bw-chat-stop")
        assert stop.is_hidden()
        page.locator(".bw-chat-input").fill("go to the cart")
        page.keyboard.press("Enter")
        settle(page)
        assert stop.is_visible() and page.locator(".bw-chat-send").text_content() == "QUEUE"
        stop.click()
        settle(page)
        assert page.evaluate("window.H.stream.runs[0].aborted") is True
        assert stop.is_hidden() and page.locator(".bw-chat-send").text_content() == "SEND"
        page.close()

    def test_a_message_typed_while_the_model_works_waits_and_still_goes_to_the_browser_agent(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        box = page.locator(".bw-chat-input")
        box.fill("open the cart")
        page.keyboard.press("Enter")
        settle(page)
        box.fill("and then scroll down")
        page.keyboard.press("Enter")
        settle(page)
        assert page.evaluate("window.H.stream.bodies.length") == 1, "the second line waits for the first run"
        assert "1 waiting" in page.locator(".bw-chat-q").text_content()
        page.evaluate("window.H.finish()")
        settle(page, 500)
        bodies = page.evaluate("window.H.stream.bodies")
        assert len(bodies) == 2 and bodies[1]["prompt"] == "and then scroll down"
        assert bodies[1]["focus_agent"] == "browser", "a queued line must not fall back to the central planner"
        assert page.locator(".bw-chat-q").is_hidden()
        page.close()

    def test_stop_also_drops_what_was_waiting(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        box = page.locator(".bw-chat-input")
        box.fill("open the cart")
        page.keyboard.press("Enter")
        settle(page)
        box.fill("and then scroll down")
        page.keyboard.press("Enter")
        settle(page)
        page.locator(".bw-chat-stop").click()
        settle(page, 500)
        assert page.evaluate("window.H.stream.bodies.length") == 1
        assert page.locator(".bw-chat-q").is_hidden()
        page.close()

    def test_typing_yes_while_an_approval_is_open_answers_it_instead_of_waiting(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.locator(".bw-chat-input").fill("buy it")
        page.keyboard.press("Enter")
        settle(page)
        page.evaluate("window.H.emit({ type: 'confirmation_requested', id: 'c9', tool: 'browser_click', prompt: 'Click Pay now?' })")
        settle(page)
        page.locator(".bw-chat-input").fill("yes")
        page.keyboard.press("Enter")
        settle(page)
        posts = page.evaluate("window.H.api.filter((c) => c[0] === 'POST' && c[1] === '/api/confirm')")
        assert posts and posts[0][2]["id"] == "c9" and posts[0][2]["approved"] is True
        assert page.evaluate("window.H.stream.bodies.length") == 1
        page.close()

    def test_a_reply_is_drawn_in_the_column(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.locator(".bw-chat-input").fill("what is on this page")
        page.keyboard.press("Enter")
        settle(page)
        page.evaluate("window.H.emit({ type: 'assistant_text', text: 'It is a shopping cart.' })")
        page.evaluate("window.H.finish()")
        settle(page)
        assert "shopping cart" in page.locator(".bw-chat .reply").first.inner_text()
        page.close()

    def test_an_approval_card_appears_in_the_column_and_can_be_answered_there(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.locator(".bw-chat-input").fill("buy it")
        page.keyboard.press("Enter")
        settle(page)
        page.evaluate("window.H.emit({ type: 'confirmation_requested', id: 'c1', tool: 'browser_click', prompt: 'Click Pay now on this page?' })")
        settle(page)
        card = page.locator(".bw-chat .approve")
        assert card.count() == 1 and "Pay now" in card.inner_text()
        card.get_by_role("button", name="APPROVE").click()
        settle(page)
        posts = page.evaluate("window.H.api.filter((c) => c[0] === 'POST' && c[1] === '/api/confirm')")
        assert posts and posts[0][2]["id"] == "c1" and posts[0][2]["approved"] is True
        page.close()

    def test_an_approval_opens_a_collapsed_column_without_forgetting_that_it_was_collapsed(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.locator(".bw-chat-input").fill("buy it")
        page.keyboard.press("Enter")
        settle(page)
        page.locator(".bw-chat-hide").click()
        settle(page)
        assert rects(page)["mode"] == "rail"
        page.evaluate("window.H.emit({ type: 'confirmation_requested', id: 'c2', tool: 'browser_click', prompt: 'Submit this form?' })")
        settle(page)
        r = rects(page)
        assert r["mode"] == "split", "a question for the owner must be visible"
        assert r["bounds"]["x"] + r["bounds"]["width"] <= r["chat"]["left"]
        assert page.evaluate("window.H.prefs.get('dm.os.browser.splitCollapsed')") == "1", "the saved choice is untouched"
        page.close()

    def test_an_approval_opens_the_overlay_on_a_small_screen(self, browser, server):
        page = open_harness(browser, server, width=1000)
        mount(page)
        page.locator(".bw-rail").click()
        settle(page)
        page.locator(".bw-chat-input").fill("buy it")
        page.keyboard.press("Enter")
        settle(page)
        page.locator(".bw-rail").click()
        settle(page)
        assert rects(page)["mode"] == "overlay-closed"
        page.evaluate("window.H.emit({ type: 'confirmation_requested', id: 'c3', tool: 'browser_click', prompt: 'Pay?' })")
        settle(page)
        assert rects(page)["mode"] == "overlay-open"
        page.close()

    def test_the_shortcut_focuses_the_chat_box_and_opens_the_column_if_it_was_hidden(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.keyboard.press("Alt+KeyJ")
        settle(page)
        assert page.evaluate("document.activeElement.classList.contains('bw-chat-input')")
        page.locator(".bw-chat-hide").click()
        settle(page)
        page.locator("#bwAddr").focus()
        page.keyboard.press("Alt+KeyJ")
        settle(page)
        assert rects(page)["mode"] == "split"
        assert page.evaluate("document.activeElement.classList.contains('bw-chat-input')")
        page.close()

    def test_without_the_electron_pane_the_column_says_the_ai_cannot_see_this_view(self, browser, server):
        page = open_harness(browser, server, query="?nopane=1")
        mount(page)
        assert "not shared with the AI" in page.locator(".bw-chat-ctx").inner_text()
        page.close()


class TestLeavingTheScreen:
    def test_unmounting_hides_the_view_removes_the_stylesheet_and_leaves_no_listener_behind(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        hides = rects(page)["hides"]
        assert page.evaluate("document.querySelectorAll('link[data-split-css]').length") == 1
        page.evaluate("window.H.unmount()")
        settle(page)
        assert page.evaluate("window.H.hides") > hides
        assert page.evaluate("document.querySelectorAll('link[data-split-css]').length") == 0
        assert page.evaluate("window.H.listeners.size") == 0 and page.evaluate("window.H.controlListeners.size") == 0
        assert page.evaluate("window.H.sc.counts().timers") == 0
        page.keyboard.press("Alt+KeyJ")  # no binding is left to throw
        assert not page.errors, page.errors
        page.close()

    def test_mounting_twice_does_not_stack_stylesheets_or_listeners(self, browser, server):
        page = open_harness(browser, server)
        mount(page)
        page.evaluate("window.H.unmount()")
        mount(page)
        assert page.evaluate("document.querySelectorAll('link[data-split-css]').length") == 1
        assert page.evaluate("window.H.controlListeners.size") == 2, "the bar in the page chrome and the one in the column, once each"
        page.close()


# --------------------------------------------------------------------------- #
# FU findings moved to this agent
# --------------------------------------------------------------------------- #


class TestFUFindingsInTheBrowserScreen:
    def test_U1_1_reload_in_the_fallback_view_reloads_through_the_proxy_not_the_real_site(self, browser, server):
        page = open_harness(browser, server, query="?nopane=1")
        mount(page)
        page.locator("#bwAddr").fill("example.com/page")
        page.keyboard.press("Enter")
        settle(page)
        proxy = "/api/browser-pane/proxy?url=" + "https%3A%2F%2Fexample.com%2Fpage"
        assert page.evaluate("document.querySelector('.cb-page iframe').getAttribute('src')") == proxy
        page.evaluate("document.querySelector('.cb-page iframe').setAttribute('src', 'about:blank')")
        page.locator("#bwReload").click()
        settle(page)
        src = page.evaluate("document.querySelector('.cb-page iframe').getAttribute('src')")
        assert src == proxy, f"reload must not hand the real address to the frame: {src}"
        assert page.locator(".bw-proxy").is_visible()
        page.close()

    def test_U1_2_leaving_BROWSER_during_the_pane_state_round_trip_keeps_the_waiting_request_queued(self, browser, server):
        page = open_harness(browser, server)
        page.evaluate("window.H.putPaneRequest('https://news.example.com/story')")
        page.evaluate("(() => { window.H.stateGate = new Promise((r) => { window.H.openGate = r; }); })()")
        page.evaluate("(() => { window.H.mountPromise = window.H.mount(); return 1; })()")
        page.wait_for_timeout(200)
        page.evaluate("window.H.unmount()")
        page.evaluate("(() => { window.H.openGate(); })()")
        page.evaluate("window.H.mountPromise")
        settle(page)
        assert page.evaluate("window.H.hasPaneRequest()") is True, "the request must still be waiting for the next visit"
        assert page.evaluate("window.H.navigates") == [], "the native view must not navigate over the screen now showing"
        page.close()

    def test_U1_2_a_request_waiting_when_BROWSER_is_open_is_still_opened(self, browser, server):
        page = open_harness(browser, server)
        page.evaluate("window.H.putPaneRequest('https://news.example.com/story')")
        mount(page)
        assert page.evaluate("window.H.navigates") == ["https://news.example.com/story"]
        assert page.evaluate("window.H.hasPaneRequest()") is False
        page.close()
