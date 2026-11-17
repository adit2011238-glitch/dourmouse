"""FU: OS shell screen fixes, driven in real headless Chrome against an isolated demo server.

Each test mocks only the API calls it needs; the rest reaches the real demo server.
Findings: U1-6 U1-4 U1-5 U1-7 U1-15 U1-3 (and U1-8 as a source check). U1-1 and U1-2 (BROWSER) moved to the browser split-screen agent.
"""

from __future__ import annotations

import re

import pytest

from dourmouse.tests.test_fix_FU_common import ROOT, DemoServer, chrome_usable, install_router, json_body

pytestmark = pytest.mark.skipif(not chrome_usable(), reason="headless Chrome is not available here")

SCREENS = ROOT / "ui" / "assets" / "os" / "screens"


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


def open_screen(page, srv, slug, mocks, clock=False):
    install_router(page, srv.base, mocks)
    if clock:
        page.clock.install()
    page.goto(f"{srv.base}/shell#/{slug}")
    page.wait_for_selector(f'#body [data-screen="{slug.upper()}"]', timeout=20000)
    page.wait_for_timeout(1500)  # real time: the mount's own requests and module loads need it
    if clock:
        page.clock.run_for(500)


def counted(payload_fn):
    """A mock handler that counts calls and answers payload_fn(n) (n counts from 1)."""
    box = {"n": 0}

    def handler(route, request):
        box["n"] += 1
        out = payload_fn(box["n"])
        route.fulfill(**out)

    return box, handler


# ---------------------------------------------------------------- U1-6 TIMETABLE (Automations, plan 2.2)
AUTO = {"ok": True, "automations": [{
    "id": "sched-001", "name": "Morning brief", "version": 2, "trigger": "Every weekday at 08:00", "action": "Daily digest",
    "action_type": "tool", "next_run": "2026-10-17 08:00", "enabled": True, "auto_disabled": False, "result": "", "result_status": "",
    "last_at": "", "runs_today": 0, "caps": {"max_runs_per_day": 6, "max_steps": 8}, "fail_count": 0, "watch_error": "", "template": ""}],
    "templates": [], "mail_trigger": False, "defaults": {"max_runs_per_day": 6, "max_steps": 8}}


def test_timetable_poll_keeps_what_the_owner_is_typing(srv, page):
    box, h = counted(lambda n: json_body(AUTO))
    open_screen(page, srv, "timetable", [("/api/os/automations", h)], clock=True)
    page.click("text=New automation")
    page.fill("[data-sentence]", "every weekday at 9:15 check my mail")
    before = box["n"]
    page.clock.run_for(10500)
    assert box["n"] > before, "the poll must really have run"
    assert page.input_value("[data-sentence]") == "every weekday at 9:15 check my mail"


def test_timetable_keeps_the_draft_when_the_list_really_changes(srv, page):
    changed = {**AUTO, "automations": [{**AUTO["automations"][0], "result": "Done", "result_status": "ok", "last_at": "2026-10-16T08:00:00"}]}
    box, h = counted(lambda n: json_body(AUTO if n < 3 else changed))
    open_screen(page, srv, "timetable", [("/api/os/automations", h)], clock=True)
    page.click("text=New automation")
    page.fill("[data-sentence]", "every weekday at 9:15 check my mail")
    page.clock.run_for(21000)
    assert box["n"] >= 3
    assert page.input_value("[data-sentence]") == "every weekday at 9:15 check my mail"


# ---------------------------------------------------------------- U1-4 GOALS
BOARD = {"goals": [], "total_goals": 0, "capped": False, "runtime": {"running": True}}


def test_goals_stale_banner_clears_when_the_next_poll_succeeds(srv, page):
    def answer(n):
        return {"status": 500, "content_type": "application/json", "body": '{"error":"down"}'} if n == 2 else json_body(BOARD)

    box, h = counted(answer)
    open_screen(page, srv, "goals", [("/api/os/goals/board", h)], clock=True)
    page.clock.run_for(5200)
    page.wait_for_timeout(300)
    assert page.locator("#body .st-stale").count() == 1, "the failed poll shows its banner"
    page.clock.run_for(5200)
    page.wait_for_timeout(300)
    assert box["n"] >= 3
    assert page.locator("#body .st-stale").count() == 0
    assert page.evaluate("document.querySelector('#body [data-screen=GOALS]').dataset.state") != "stale"


# ---------------------------------------------------------------- U1-5 RESEARCH
def test_research_live_updates_banner_clears_after_a_good_read(srv, page):
    def answer(n):
        if n == 1:
            return json_body({"events": [], "next": 0})  # the mount read
        if n == 2:
            return {"status": 500, "content_type": "application/json", "body": '{"error":"down"}'}
        return json_body({"events": [], "next": 0})  # recovered, nothing new

    box, h = counted(answer)
    open_screen(page, srv, "research", [("/api/events/log", h)], clock=True)
    page.clock.run_for(10500)
    assert "Live updates paused" in page.inner_text("#rsQs, #body")
    page.clock.run_for(10500)
    assert box["n"] >= 3
    assert "Live updates paused" not in page.inner_text("#body")


# ---------------------------------------------------------------- U1-15 HOME, U1-3 ORCHESTRATION
def _iso_ago(seconds):
    import datetime as dt

    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


def finished_run(run_id="run1", ago=60):
    return {"id": run_id, "goal": "compare two plans", "status": "done", "started": _iso_ago(ago + 30), "finished": _iso_ago(ago),
            "owner": "", "synthesis": "merged answer", "error": None,
            "brains": {"claude": {"label": "CLAUDE", "status": "done", "result": "a", "error": None, "elapsed": 3.0}}}


def test_orchestration_open_page_hands_an_absolute_address_to_the_host(srv, page):
    page.add_init_script("window.__opened = []; window.open = (u) => { window.__opened.push(u); return {}; };")
    open_screen(page, srv, "orchestration", [("/api/allhands", lambda r, q: r.fulfill(**json_body({"ok": True, "runs": [finished_run("r7")]})))])
    page.click("[data-ah-window]")
    opened = page.evaluate("window.__opened")
    assert opened == [f"{srv.base}/all-hands?run=r7"]
    assert not page.locator("text=could not open a separate page").count()


# ---------------------------------------------------------------- U1-8 VOICE comment
def test_voice_header_comment_describes_the_opt_in():
    src = (SCREENS / "voice" / "index.js").read_text(encoding="utf-8")
    head = src.split("*/", 1)[0]
    assert "sent to the companion as an ordinary message" not in head
    assert "SEND TO HOME" in head
    assert not re.search(r"—", head)


# ---------------------------------------------------------------- U1-7 VOICE
MIC_STUB = """
window.__gumCalls = 0; window.__resolvers = []; window.__streams = [];
Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true, value: () => new Promise((res) => {
  window.__gumCalls += 1;
  window.__resolvers.push(() => { const s = { stopped: 0, getTracks() { return [{ stop() { s.stopped += 1; } }]; } }; window.__streams.push(s); res(s); });
}) });
window.MediaRecorder = class { constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm'; }
  start() { this.state = 'recording'; } stop() { this.state = 'inactive'; if (this.onstop) this.onstop(); } };
"""


def test_voice_second_listen_press_does_not_open_a_second_microphone(srv, page):
    page.add_init_script(MIC_STUB)
    open_screen(page, srv, "voice", [("/api/voice", lambda r, q: r.fulfill(**json_body({"stt": "whisper", "tts": "none", "whisper_model": "tiny"})))])
    listen = page.get_by_role("button", name="LISTEN")
    listen.click()
    listen.click()  # the permission prompt is still open
    assert page.evaluate("window.__gumCalls") == 1
    page.evaluate("window.__resolvers.forEach((f) => f())")
    page.wait_for_timeout(300)
    page.get_by_role("button", name=re.compile("STOP")).click()
    page.wait_for_timeout(300)
    assert page.evaluate("window.__streams.length") == 1
    assert page.evaluate("window.__streams.every((s) => s.stopped >= 1)")
