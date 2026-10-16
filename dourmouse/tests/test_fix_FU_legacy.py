"""FU: legacy page fixes (console.html, all_hands.html, atlas_lab.html, hud.html), in real headless Chrome.

Findings: U1-16 U1-19 U1-20 U1-21 U1-22 U1-23 U1-24 U1-25 U1-26 U1-27 U1-17 U1-18.
Login/setup, hub and product pages are in test_fix_FU_pages.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dourmouse.tests.test_fix_FU_common import OLD_UI, ROOT, DemoServer, chrome_usable, install_router, json_body

pytestmark = pytest.mark.skipif(not chrome_usable(), reason="headless Chrome is not available here")


@pytest.fixture(scope="module")
def srv():
    s = DemoServer()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    ctx.close()


def open_console(page, srv, mocks=()):
    """The console script is one IIFE with no globals, so every console test drives the real UI."""
    page.add_init_script("localStorage.setItem('dm_theme_v1', 'arc'); sessionStorage.setItem('dm_booted_v1', '1');")
    me = ("/api/auth/me", lambda r, q: r.fulfill(**json_body({"me": {"email": "owner@example.com", "name": "Owner"}})))
    conns = ("/api/connections", lambda r, q: r.fulfill(**json_body({})))  # no sign-in popup over the page
    status = ("/api/auth/status", lambda r, q: r.fulfill(**json_body({"configured": False})))
    install_router(page, srv.base, [*mocks, me, conns, status])
    page.goto(f"{srv.base}/console")
    page.wait_for_selector('#tabs [data-screen="HOME"]', timeout=20000)
    page.wait_for_timeout(800)


def goto_screen(page, name):
    page.evaluate("(n) => document.querySelector('[data-screen=\"' + n + '\"]').click()", name)
    page.wait_for_timeout(300)


def send(page, text):
    page.fill("#ta", text)
    page.click("#go")


def sse(*events) -> str:
    return "".join("data: " + json.dumps(e) + "\n\n" for e in events)


def stream(*events):
    return {"status": 200, "content_type": "text/event-stream", "body": sse(*events)}


# ---------------------------------------------------------------- U1-20 approval focus
def test_console_approval_never_focuses_approve(srv, page):
    open_console(page, srv, [("/api/chat", lambda r, q: r.fulfill(**stream(
        {"type": "confirmation_requested", "id": "fu-1", "prompt": "Delete a file"}, {"type": "done", "final_text": ""})))])
    send(page, "please clean up")
    page.wait_for_selector(".approve button.go", timeout=8000)
    page.wait_for_timeout(400)
    active = page.evaluate("(() => { const a = document.activeElement; return { tag: a.tagName, text: a.textContent.slice(0, 12) }; })()")
    assert not (active["tag"] == "BUTTON" and active["text"].startswith("APPROVE")), active


# ---------------------------------------------------------------- U1-22 copy
def test_console_copy_button_copies_only_the_code(srv, page):
    page.add_init_script("""window.__copied = null;
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (t) => { window.__copied = t; } } });""")
    code = "```sh\necho one\necho two\n```\n"
    open_console(page, srv, [("/api/chat", lambda r, q: r.fulfill(**stream(
        {"type": "assistant_delta", "text": code}, {"type": "done", "final_text": code})))])
    send(page, "show me a snippet")
    page.wait_for_selector("pre .copybtn", timeout=10000)
    page.click("pre .copybtn")
    page.wait_for_timeout(300)
    copied = page.evaluate("window.__copied")
    assert copied.strip() == "echo one\necho two", repr(copied)
    assert "COPY" not in copied


# ---------------------------------------------------------------- U1-21 / U1-24 settings
ORCH = {"current": "m-a", "source": "persisted", "persisted": "other", "backends": [{"name": "cloudx", "configured": True, "model": "m-b", "detail": ""}]}


def test_console_settings_refusal_stays_readable(srv, page):
    def orch(route, request):
        if request.method == "POST":
            route.fulfill(**json_body({"ok": False, "detail": "claude cannot be the orchestrator model"}))
        else:
            route.fulfill(**json_body(ORCH))

    open_console(page, srv, [("/api/settings/orchestrator-model", orch)])
    goto_screen(page, "SETTINGS")
    page.wait_for_selector("#orchModelRow button", timeout=8000)
    page.click("#orchModelRow button")
    page.wait_for_timeout(700)
    assert "Could not save: claude cannot be the orchestrator model" in page.inner_text("#orchModelNote")


def test_console_failed_key_save_keeps_the_typed_key_and_the_error(srv, page):
    def keys(route, request):
        if request.method == "POST":
            route.fulfill(**json_body({"ok": False, "detail": "that key was refused"}))
        else:
            route.fulfill(**json_body({}))

    open_console(page, srv, [("/api/settings/api-keys", keys)])
    goto_screen(page, "SETTINGS")
    page.wait_for_selector(".byokInput", timeout=8000)
    page.fill(".byokInput >> nth=0", "typed-key-value")
    page.click(".byokSave >> nth=0")
    page.wait_for_timeout(700)
    assert page.input_value(".byokInput >> nth=0") == "typed-key-value"
    assert "Could not save: that key was refused" in page.inner_text(".byokStatus >> nth=0")


def test_console_auto_approve_on_asks_first(srv, page):
    posts = []

    def auto(route, request):
        if request.method == "POST":
            posts.append(request.post_data)
            route.fulfill(**json_body({"ok": True}))
        else:
            route.fulfill(**json_body({"enabled": False}))

    open_console(page, srv, [("/api/settings/auto-approve", auto)])
    goto_screen(page, "SETTINGS")
    page.wait_for_selector("#autoApproveRow button", timeout=8000)
    messages = []
    page.once("dialog", lambda d: (messages.append(d.message), d.dismiss()))
    page.locator("#autoApproveRow button", has_text="ON").click()
    page.wait_for_timeout(500)
    assert messages and "confirmations" in messages[0].lower()
    assert posts == [], "declining the question must not change the setting"
    page.once("dialog", lambda d: d.accept())
    page.locator("#autoApproveRow button", has_text="ON").click()
    page.wait_for_timeout(500)
    assert len(posts) == 1


# ---------------------------------------------------------------- U1-23 queued directive
def test_console_queued_directive_runs_in_the_thread_it_was_queued_on(srv, page):
    bodies, held = [], []

    def chat(route, request):
        bodies.append(json.loads(request.post_data))
        if len(bodies) == 1:
            held.append(route)  # the first turn stays open
        else:
            route.fulfill(**stream({"type": "done", "final_text": "ok"}))

    open_console(page, srv, [("/api/chat", chat)])
    goto_screen(page, "RESEARCH")
    send(page, "first question")
    page.wait_for_timeout(500)
    send(page, "queued question")
    page.wait_for_timeout(300)
    goto_screen(page, "NEWS")
    assert len(bodies) == 1 and held
    held[0].fulfill(**stream({"type": "done", "final_text": "ok"}))
    for _ in range(40):
        if len(bodies) >= 2:
            break
        page.wait_for_timeout(150)
    assert len(bodies) == 2, bodies
    assert bodies[1]["prompt"] == "queued question"
    assert bodies[1]["screen"] == "RESEARCH"
    assert page.evaluate("document.querySelector('[data-screen][aria-selected=true]').dataset.screen") == "NEWS"


# ---------------------------------------------------------------- U1-25 feed links
def _extract(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i : src.index(end, i) + len(end)]


def test_console_world_card_links_only_http_and_https(srv, page):
    """cardHTML is inside the console IIFE, so its source (and safeFeedLink, when present) is run on a blank page."""
    src = ((Path(OLD_UI) if OLD_UI else ROOT / "ui") / "console.html").read_text(encoding="utf-8")
    esc_def = _extract(src, "const esc = (s) =>", "[c]));")
    card = _extract(src, "function cardHTML(p){", 'return parts.join("");\n}')
    helper = _extract(src, "const safeFeedLink", ": \"\";") if "const safeFeedLink" in src else ""
    page.goto("about:blank")
    page.evaluate("(src) => { (0, eval)(src); }", "\n".join([esc_def, helper, card]))
    html = page.evaluate("""() => [
      cardHTML({ title: 't', chan: 'c', severity: 's', lat: 1, lon: 2, link: 'javascript:window.__pwn=1' }),
      cardHTML({ title: 't', chan: 'c', severity: 's', lat: 1, lon: 2, link: 'data:text/html,x' }),
      cardHTML({ title: 't', chan: 'c', severity: 's', lat: 1, lon: 2, link: 'https://example.com/a?b=1&c=2' })]""")
    assert "href" not in html[0] and "href" not in html[1]
    assert 'href="https://example.com/a?b=1&amp;c=2"' in html[2]


def test_console_news_row_never_opens_a_script_link(srv, page):
    page.add_init_script("window.__opened = []; window.open = (u) => { window.__opened.push(u); return null; };")
    pushed = [{"type": "news_item", "item": {"channel": "a", "title": "bad", "link": "javascript:window.__pwn=1", "summary": "s1"}},
              {"type": "news_item", "item": {"channel": "b", "title": "good", "link": "https://example.com/x", "summary": "s2"}}]
    open_console(page, srv, [(lambda q: q.url.endswith("/api/events"),
                              lambda r, q: r.fulfill(status=200, content_type="text/event-stream", body=sse(*pushed)))])
    goto_screen(page, "NEWS")
    page.wait_for_selector("#newsFeedList .row", timeout=8000)
    page.locator("#newsFeedList .row", has_text="[A] bad").click()
    page.locator("#newsFeedList .row", has_text="[B] good").click()
    assert page.evaluate("window.__opened") == ["https://example.com/x"]


# ---------------------------------------------------------------- U1-27 boot readout
def test_console_boot_readout_escapes_reported_strings(srv, page):
    page.add_init_script("localStorage.setItem('dm_theme_v1', 'arc');")
    ctx_page = page
    install_router(ctx_page, srv.base, [("/api/server", lambda r, q: r.fulfill(**json_body(
        {"online": True, "node": "<img src=x id=fu-node>", "detail": "<b id=fu-detail>x</b>"})))])
    ctx_page.emulate_media(reduced_motion="reduce")
    ctx_page.goto(f"{srv.base}/console")
    ctx_page.wait_for_selector("#btRead .bt-rise", timeout=20000)
    ctx_page.wait_for_timeout(500)
    assert ctx_page.locator("#btRead #fu-node, #btRead #fu-detail").count() == 0
    assert "<img src=x id=fu-node>" in ctx_page.inner_text("#btRead")


# ---------------------------------------------------------------- U1-26 3D editor
def open_3d(page):
    goto_screen(page, "DESIGN3D")
    page.get_by_text("3D WORKSPACE", exact=True).click()


def test_console_3d_editor_gives_up_with_a_visible_error_when_three_is_missing(srv, page):
    open_console(page, srv, [("/assets/vendor/three/", lambda r, q: r.abort())])
    page.clock.install()
    open_3d(page)
    page.clock.run_for(8000)
    page.wait_for_timeout(300)
    assert "could not start" in page.inner_text("#d3dCanvasWrap").lower()


def test_console_3d_editor_stops_drawing_while_its_pane_is_hidden(srv, page):
    page.add_init_script("""window.__raf = 0; const r = window.requestAnimationFrame.bind(window);
      window.requestAnimationFrame = (f) => { window.__raf += 1; return r(f); };""")
    open_console(page, srv)
    open_3d(page)
    page.wait_for_function("document.querySelector('#d3dCanvasWrap canvas')", timeout=15000)
    page.wait_for_timeout(500)
    running = page.evaluate("(() => { const a = window.__raf; return new Promise(r => setTimeout(() => r(window.__raf - a), 500)); })()")
    assert running > 5, "the editor draws while visible"
    page.evaluate("document.getElementById('d3dCanvasWrap').parentElement.style.display = 'none'")
    page.wait_for_timeout(700)  # let the loop notice
    idle = page.evaluate("(() => { const a = window.__raf; return new Promise(r => setTimeout(() => r(window.__raf - a), 600)); })()")
    assert idle < 5, f"{idle} animation frames requested while hidden"
    page.evaluate("document.getElementById('d3dCanvasWrap').parentElement.style.display = ''")
    page.wait_for_timeout(900)
    again = page.evaluate("(() => { const a = window.__raf; return new Promise(r => setTimeout(() => r(window.__raf - a), 500)); })()")
    assert again > 5, "drawing resumes when the pane is shown again"


# ---------------------------------------------------------------- U1-16 all hands poll
def test_all_hands_polls_a_run_started_in_the_window(srv, page):
    polls = []

    def run(route, request):
        if request.method == "POST":
            route.fulfill(**json_body({"ok": True, "run_id": "rX"}))
            return
        route.fulfill(**json_body({"ok": True, "runs": []}))

    def one(route, request):
        polls.append(1)
        route.fulfill(**json_body({"ok": True, "run": {"id": "rX", "goal": "g", "status": "running", "started": "2026-10-16T10:00:00+00:00", "brains": {}}}))

    page.add_init_script("window.__events = 0;")
    install_router(page, srv.base, [("/api/allhands/rX", one), ("/api/allhands", run)])
    page.goto(f"{srv.base}/all-hands")
    page.fill("#goalinput", "compare")
    page.click("#startbtn")
    page.wait_for_timeout(500)
    first = len(polls)
    page.wait_for_timeout(4600)
    assert len(polls) >= first + 2, f"only {len(polls)} reads of the run"


def test_all_hands_event_stream_is_not_closed_on_error(srv, page):
    hits = []

    def events(route, request):
        hits.append(1)
        route.fulfill(status=200, content_type="text/event-stream", body="retry: 200\n\n")  # ends at once, so onerror fires

    install_router(page, srv.base, [("/api/events", events), ("/api/allhands", lambda r, q: r.fulfill(**json_body({"ok": True, "runs": []})))])
    page.goto(f"{srv.base}/all-hands")
    page.wait_for_timeout(3000)
    assert len(hits) >= 3, f"the stream was opened {len(hits)} time(s); the browser must keep reconnecting"


# ---------------------------------------------------------------- U1-19 atlas lab
PROPOSAL = {"id": "p1", "strategy_name": "s", "source": "chat", "explanation": "e", "code": "print(1)", "status": "pending"}


def test_atlas_lab_poll_keeps_open_code_and_the_chosen_target(srv, page):
    install_router(page, srv.base, [
        ("/api/atlas-lab/proposals", lambda r, q: r.fulfill(**json_body({"ok": True, "proposals": [PROPOSAL] if "status=pending" in q.url else []}))),
        ("/api/atlas-lab/runs", lambda r, q: r.fulfill(**json_body({"ok": True, "runs": []}))),
        ("/api/atlas-lab/leaderboard", lambda r, q: r.fulfill(**json_body({"ok": True, "rows": []}))),
    ])
    page.clock.install()
    page.goto(f"{srv.base}/atlas-lab")
    page.wait_for_timeout(1500)
    # open the PROPOSALS tab
    page.locator("text=PROPOSALS").first.click()
    page.wait_for_selector("#pendingList .prop-card", timeout=8000)
    page.locator("#pendingList summary").click()
    page.select_option("#target-p1", "desktop")
    page.clock.run_for(11000)
    page.wait_for_timeout(400)
    assert page.evaluate("document.querySelector('#pendingList details').open") is True
    assert page.input_value("#target-p1") == "desktop"


# ---------------------------------------------------------------- U1-17 / U1-18 HUD
def test_hud_shows_no_invented_gauge_values(srv, page):
    page.add_init_script("localStorage.setItem('dm_token', 'x');")
    install_router(page, srv.base, [
        ("/api/telemetry", lambda r, q: r.fulfill(**json_body({"mem_pct": 41, "cpu_pct": 12, "mem_used_gb": 6.5, "mem_total_gb": 16, "load": [2.5, 2.0, 1.8]}))),
        ("/api/memory", lambda r, q: r.fulfill(**json_body({"count": 0}))),
    ])
    page.goto(f"{srv.base}/hud")
    page.wait_for_function("document.getElementById('mRam').textContent.includes('GB')", timeout=10000)
    assert page.inner_text("#mArc") == "n/a"
    assert page.inner_text("#mShield") == "n/a"
    assert page.inner_text("#o2Val") == "n/a"
    assert page.inner_text("#bioVal").startswith("2.5")
    assert page.inner_text("#confVal") == "—"


def test_hud_drive_button_targets_a_real_html_page(srv, page):
    install_router(page, srv.base, [])
    page.goto(f"{srv.base}/hud")
    target = page.get_attribute("button.atlas", "data-goto")
    assert target == "/atlas-lab"
    import urllib.request

    with urllib.request.urlopen(srv.base + target, timeout=5) as r:
        assert "text/html" in r.headers.get("Content-Type", "")


def test_atlas_lab_approve_button_works_and_a_hostile_id_runs_no_script(srv, page):
    evil = {**PROPOSAL, "id": "x');window.__pwn=1;//"}
    posts = []

    def approve(route, request):
        posts.append(request.url)
        route.fulfill(**json_body({"ok": True}))

    install_router(page, srv.base, [
        ("/approve", approve),
        ("/api/atlas-lab/proposals", lambda r, q: r.fulfill(**json_body({"ok": True, "proposals": [evil] if "status=pending" in q.url else []}))),
        ("/api/atlas-lab/runs", lambda r, q: r.fulfill(**json_body({"ok": True, "runs": []}))),
        ("/api/atlas-lab/leaderboard", lambda r, q: r.fulfill(**json_body({"ok": True, "rows": []}))),
    ])
    page.goto(f"{srv.base}/atlas-lab")
    page.locator("text=PROPOSALS").first.click()
    page.wait_for_selector("#pendingList .prop-card", timeout=8000)
    page.locator("#pendingList .btn-approve").click()
    page.wait_for_timeout(500)
    assert page.evaluate("window.__pwn") is None
    assert len(posts) == 1 and posts[0].endswith("/approve")
