"""Phase C2: the browser agent's side of the owner/model lock, against the REAL shell code.

The agent's tools (browser_type, browser_fill_form, a click by id, a navigation) run in this
process exactly as in the server. Their claim on the tab goes to electron/main.js's own pane bridge
(``/control*``), which runs under node with the fake ``electron`` of the B1 tests, so the lock that
decides is the real one. A small "puppet" server inside that node process plays the owner: it
raises ``before-input-event`` and ``input-event`` on a tab, or presses the console's Stop and Take
control, at an exact point (for example after the agent's second chunk of text). The page itself is
a stand-in (no Chromium here); real pages are covered by test_browser_editors.py and the live run
recorded in the C2 finding.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from dourmouse import browser_agent as ba
from dourmouse.tests.test_browser_control_lock_shell import COMMON, HOOKED
from dourmouse.tests.test_browser_tabs_shell import ELECTRON, NODE, _free_port

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

SERVE = COMMON + r'''
const PUPPET_PORT = parseInt(process.env.T_PUPPET_PORT, 10);
let quit = null;
const puppetHooks = []; // { tab, after, what }
function tabIdOf(wc) { for (const [id, t] of T.tabs) if (t.view.webContents === wc) return id; return 0; }
main(async () => {
  T.ensurePaneView();
  installInsert();
  const proto = Object.getPrototypeOf(T.view().webContents);
  proto.insertText = async function (t) {
    (this.inserted = this.inserted || []).push(t);
    const tab = tabIdOf(this);
    for (const h of puppetHooks.splice(0)) {
      if (h.tab === tab && h.after === this.inserted.length) {
        if (h.what === "key") ownerKey(tab);
        else if (h.what === "click") input(tab, { type: "mouseDown", x: 3, y: 3, button: "left", clickCount: 1 });
        else await handlers["control:" + h.what](CONSOLE);
      } else puppetHooks.push(h);
    }
  };
  await call("POST", "/show", {});
  await call("POST", "/navigate", { url: "https://a.example/" });
  const t1 = T.active();
  const t2 = (await call("POST", "/tabs/new", { url: "https://b.example/" })).body.id;
  await call("POST", "/tabs/select", { id: t1 });
  const puppet = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      const obj = body ? JSON.parse(body) : {};
      const url = new URL(req.url, "http://x");
      let out = { ok: true };
      if (url.pathname === "/owner") {
        if (obj.kind === "key") ownerKey(obj.tab);
        else input(obj.tab, { type: obj.kind === "wheel" ? "mouseWheel" : "mouseDown", x: obj.x || 1, y: obj.y || 1, button: "left", clickCount: 1 });
      } else if (url.pathname === "/hook") puppetHooks.push(obj);
      else if (url.pathname === "/console") out = await handlers["control:" + obj.what](CONSOLE);
      else if (url.pathname === "/inserted") out = wcOf(Number(url.searchParams.get("tab"))).inserted || [];
      else if (url.pathname === "/view") out = T.controlConsoleView();
      else if (url.pathname === "/reset") {
        for (const t of T.tabs.values()) t.view.webContents.inserted = [];
        puppetHooks.splice(0); T.control.lastOwner.clear(); T.control.actions.clear(); T.control.held = false; T.control.waiting = null; T.control.last = null;
      } else if (url.pathname === "/quit") { res.end("{}"); quit(); return; }
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(out));
    });
  }).listen(PUPPET_PORT, "127.0.0.1");
  console.log("READY:" + JSON.stringify({ t1, t2 }));
  await new Promise((r) => (quit = r));
  puppet.close();
});
'''


class Shell:
    def __init__(self, proc, pane_port, puppet_port, t1, t2):
        self.proc, self.pane_port, self.puppet_port, self.t1, self.t2 = proc, pane_port, puppet_port, t1, t2

    def puppet(self, path, body=None):
        data = json.dumps(body or {}).encode() if body is not None or path != "/inserted" else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.puppet_port}{path}", data=data, method="POST" if data else "GET")
        return json.loads(urllib.request.urlopen(req, timeout=10).read().decode() or "null")

    def inserted(self, tab):
        req = urllib.request.Request(f"http://127.0.0.1:{self.puppet_port}/inserted?tab={tab}")
        return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())

    def view(self):
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.puppet_port}/view", timeout=10).read().decode())

    def bridge(self):
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.pane_port}/control", timeout=10).read().decode())


@pytest.fixture(scope="module")
def shell(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c2serve")
    script = tmp / "serve.js"
    script.write_text(HOOKED + SERVE, encoding="utf-8")
    pane_port, puppet_port = _free_port(), _free_port()
    env = {**os.environ, "T_MAIN": str(ELECTRON / "main.js"), "DOURMOUSE_ELECTRON_PANE_PORT": str(pane_port), "T_PUPPET_PORT": str(puppet_port)}
    proc = subprocess.Popen([NODE, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    line = ""
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.startswith("READY:") or not line:
            break
    if not line.startswith("READY:"):
        proc.kill()
        pytest.fail(f"the shell harness did not start: {line!r} {proc.stderr.read()}")
    ids = json.loads(line[len("READY:") :])
    s = Shell(proc, pane_port, puppet_port, ids["t1"], ids["t2"])
    yield s
    with contextlib.suppress(Exception):  # already gone
        s.puppet("/quit", {})
    proc.wait(timeout=10)


class FakeMouse:
    def __init__(self, page):
        self.page = page
        self.clicks = []

    async def click(self, x, y):
        self.clicks.append((x, y))
        if self.page.on_click:
            await asyncio.to_thread(self.page.on_click, x, y)


class FakeKeyboard:
    def __init__(self):
        self.inserted = []
        self.typed = []

    async def insert_text(self, text):
        self.inserted.append(text)

    async def type(self, text, delay=0):
        self.typed.append(text)

    async def press(self, key, timeout=0):
        self.typed.append(f"<{key}>")


class FakePage:
    url = "https://a.example/"

    def __init__(self):
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard()
        self.on_click = None
        self.goto_hook = None

    def is_closed(self):
        return False

    async def goto(self, url, **kw):
        if self.goto_hook:
            await asyncio.to_thread(self.goto_hook)
        raise RuntimeError("net::ERR_ABORTED")


@pytest.fixture
def pane(shell, monkeypatch):
    """The agent in pane mode on tab 1 of the harness shell, with a stand-in page."""
    ba._ensure_loop()
    shell.puppet("/reset", {})
    monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "1")
    monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(shell.pane_port))
    page = FakePage()
    monkeypatch.setattr(ba, "_PAGE_MODE", "pane")
    monkeypatch.setattr(ba, "_PANE_SEEN", {"active": shell.t1, "ids": frozenset({shell.t1, shell.t2})})

    async def fake_ensure(quiet=False):
        return page

    async def fake_world(pg, op, params, *, fresh_ok, api="__dmAgent"):
        if op in ("focusState",):
            return {"has": True, "tag": "textarea", "multiline": True, "editable": True}
        if op == "focusCheck":
            return {"ok": True}
        raise AssertionError(op)

    async def fake_prepare(pg, num, op, extra=None):
        if op == "click":
            return {"ok": True, "id": num, "name": f"button {num}", "x": 40.0, "y": 20.0}
        return {"ok": True, "id": num, "name": f"field {num}", "tag": "textarea", "focused": True, "multiline": True}

    monkeypatch.setattr(ba, "_ensure_browser", fake_ensure)
    monkeypatch.setattr(ba, "_world_call", fake_world)
    monkeypatch.setattr(ba, "_id_prepare", fake_prepare)
    ba._NOTES.clear()
    yield page
    ba._NOTES.clear()


TEXT = "".join(chr(ord("a") + i % 26) for i in range(100))


class TestTyping:
    def test_text_goes_through_the_shell_in_chunks_into_the_agents_tab(self, shell, pane):
        out = ba.browser_type({"text": TEXT, "delay_ms": 0})
        assert out.startswith("TYPED 100 characters into the focused element")
        chunks = shell.inserted(shell.t1)
        assert "".join(chunks) == TEXT and max(len(c) for c in chunks) == ba._TYPE_CHUNK
        assert shell.inserted(shell.t2) == []
        assert pane.keyboard.inserted == []  # nothing went around the shell
        assert shell.bridge()["state"] == "idle"

    def test_the_owner_starts_typing_mid_way_and_the_agent_stops_and_says_how_far_it_got(self, shell, pane):
        shell.puppet("/hook", {"tab": shell.t1, "after": 2, "what": "key"})
        with pytest.raises(RuntimeError) as err:
            ba.browser_type({"text": TEXT, "delay_ms": 0})
        msg = str(err.value)
        assert msg.startswith("STOPPED: the owner started using this tab (key)")
        assert "Typed 48 of 100 characters" in msg and "NOT typed: the last 52" in msg
        assert "".join(shell.inserted(shell.t1)) == TEXT[:48]
        view = shell.view()
        assert view["last"]["outcome"] == "owner-input" and view["acting"] == []

    def test_a_click_by_the_owner_also_stops_the_typing(self, shell, pane):
        shell.puppet("/hook", {"tab": shell.t1, "after": 1, "what": "click"})
        with pytest.raises(RuntimeError, match=r"\(click\).*Typed 24 of 100"):
            ba.browser_type({"text": TEXT, "delay_ms": 0})

    def test_stop_in_the_console_stops_the_typing(self, shell, pane):
        shell.puppet("/hook", {"tab": shell.t1, "after": 3, "what": "stop"})
        with pytest.raises(RuntimeError) as err:
            ba.browser_type({"text": TEXT, "delay_ms": 0})
        assert str(err.value).startswith("STOPPED BY THE OWNER") and "Typed 72 of 100" in str(err.value)
        assert "Do not retry" in str(err.value)

    def test_take_control_mid_way(self, shell, pane):
        shell.puppet("/hook", {"tab": shell.t1, "after": 1, "what": "take"})
        with pytest.raises(RuntimeError, match="OWNER HAS CONTROL"):
            ba.browser_type({"text": TEXT, "delay_ms": 0})
        # and nothing starts again until release
        with pytest.raises(RuntimeError) as err:
            ba.browser_type({"text": "more", "delay_ms": 0})
        assert "OWNER HAS CONTROL" in str(err.value) and "Nothing was done" in str(err.value)
        assert "".join(shell.inserted(shell.t1)) == TEXT[:24]
        shell.puppet("/console", {"what": "release"})
        assert ba.browser_type({"text": "more", "delay_ms": 0}).startswith("TYPED 4")


class TestWaitingForTheOwner:
    def test_the_owner_is_active_the_agent_waits_a_bounded_time_then_refuses(self, shell, pane, monkeypatch):
        monkeypatch.setattr(ba, "_CONTROL_WAIT_SECONDS", 0.6)
        shell.puppet("/owner", {"tab": shell.t1, "kind": "key"})
        t0 = time.monotonic()
        with pytest.raises(RuntimeError) as err:
            ba.browser_type({"text": "hello", "delay_ms": 0})
        waited = time.monotonic() - t0
        assert 0.5 <= waited < 2.0
        msg = str(err.value)
        assert msg.startswith(f"OWNER IS USING THIS TAB: the owner typed, clicked or scrolled in tab {shell.t1}")
        assert "Nothing was done" in msg and "ask the owner" in msg
        assert shell.inserted(shell.t1) == []

    def test_when_the_owner_pauses_within_the_wait_the_agent_goes_ahead(self, shell, pane):
        shell.puppet("/owner", {"tab": shell.t1, "kind": "click"})
        time.sleep(0.4)
        t0 = time.monotonic()
        out = ba.browser_type({"text": "after you", "delay_ms": 0})
        waited = time.monotonic() - t0
        assert out.startswith("TYPED 9")
        assert 1.5 <= waited <= 3.2  # the hold is 2.5 s from the owner's click

    def test_owner_input_on_another_tab_does_not_block(self, shell, pane):
        shell.puppet("/owner", {"tab": shell.t2, "kind": "key"})
        t0 = time.monotonic()
        assert ba.browser_type({"text": "free", "delay_ms": 0}).startswith("TYPED 4")
        assert time.monotonic() - t0 < 1.0


class TestFormsClicksAndNavigation:
    def test_fill_form_stops_between_fields_and_lists_what_was_and_was_not_filled(self, shell, pane):
        shell.puppet("/hook", {"tab": shell.t1, "after": 1, "what": "key"})
        with pytest.raises(RuntimeError) as err:
            ba.browser_fill_form({"fields": {"e1": "Ada", "e2": "Lovelace", "e3": "London"}})
        msg = str(err.value)
        assert msg.startswith("STOPPED: the owner started using this tab")
        assert "Filled 1 of 3 fields (e1); NOT filled: e2, e3." in msg
        assert shell.inserted(shell.t1) == ["Ada"]

    def test_the_agents_own_click_is_not_taken_for_the_owner(self, shell, pane):
        pane.on_click = lambda x, y: shell.puppet("/owner", {"tab": shell.t1, "kind": "click", "x": x, "y": y})

        async def scenario():
            async with ba._acting(pane, "click") as claim:
                await ba._click_by_id(pane, 5, claim)

        ba._call(scenario)
        assert pane.mouse.clicks == [(40.0, 20.0)]
        assert shell.bridge()["state"] == "idle"  # not owner-active: the click was declared
        assert shell.view()["last"]["outcome"] == "done"

    def test_a_click_sent_without_declaring_it_would_count_as_the_owner(self, shell, pane):
        shell.puppet("/owner", {"tab": shell.t1, "kind": "click", "x": 40, "y": 20})
        assert shell.bridge()["state"] == "owner-active"

    def test_a_navigation_the_owner_stopped_is_reported_as_stopped_not_as_a_page_error(self, shell, pane):
        pane.goto_hook = lambda: shell.puppet("/console", {"what": "stop"})
        with pytest.raises(RuntimeError) as err:
            ba.browser_open({"url": "https://example.com/"})
        assert str(err.value).startswith("STOPPED BY THE OWNER")
        assert shell.view()["last"]["outcome"] == "stopped"


class TestWithoutTheLock:
    def test_a_separate_headless_chrome_has_no_owner_and_no_bridge_calls(self, monkeypatch):
        page = FakePage()
        monkeypatch.setattr(ba, "_PAGE_MODE", "headless")
        claim = asyncio.run(ba._claim(page, "type"))
        assert isinstance(claim, ba._NoClaim) and not claim.pane
        asyncio.run(claim.insert("abc"))
        assert page.keyboard.inserted == ["abc"]

    def test_an_older_shell_without_control_acts_as_before_and_says_so_once(self, monkeypatch):
        class Old(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = b'{"ok": false, "error": "not found"}'
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), Old)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "1")
            monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(srv.server_address[1]))
            monkeypatch.setattr(ba, "_PAGE_MODE", "pane")
            monkeypatch.setattr(ba, "_PANE_SEEN", {"active": 1, "ids": frozenset({1})})
            monkeypatch.setattr(ba, "_CONTROL_NOTED_OLD_SHELL", False)
            ba._NOTES.clear()
            first = asyncio.run(ba._claim(FakePage(), "type"))
            second = asyncio.run(ba._claim(FakePage(), "type"))
            assert not first.pane and not second.pane
            notes = ba._drain_notes()
            assert notes.count("older than the owner/model lock") == 1
        finally:
            srv.shutdown()

    def test_an_unreachable_bridge_fails_closed(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "1")
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(_free_port()))
        monkeypatch.setattr(ba, "_PAGE_MODE", "pane")
        monkeypatch.setattr(ba, "_PANE_SEEN", {"active": 1, "ids": frozenset({1})})
        with pytest.raises(ba._OwnerControl, match="could not check who has the browser tab"):
            asyncio.run(ba._claim(FakePage(), "type"))


class TestTexts:
    def test_every_stop_reason_has_a_plain_sentence_and_no_em_dash(self):
        for reason in ("owner-input", "stopped", "owner-control", "no-tab", "expired"):
            text = str(ba._stop_error(reason, "key"))
            assert text and "—" not in text and "{" not in text
        assert "STOPPED" in str(ba._stop_error("weird"))

    def test_progress_wording(self):
        assert ba._progress(0, 10, "e1", "abcdefghij") == "Nothing was typed into e1."
        assert ba._progress(4, 10, "e1", "abcdefghij") == "Typed 4 of 10 characters into e1; NOT typed: the last 6 ('efghij')."
        assert "already" in ba._progress(10, 10, "e1", "abcdefghij")

    def test_the_module_source_has_no_em_dash_in_the_c2_parts(self):
        src = Path(ba.__file__).read_text(encoding="utf-8")
        start = src.index("# Shared control (phase C2)")
        end = src.index("def _note(text: str)")
        assert "—" not in src[start:end]


class TestFocusMovedByWhom:
    def test_the_owners_click_that_took_the_focus_is_reported_as_the_owner_not_the_page(self, shell, pane, monkeypatch):
        """Seen live: the owner's click lands between two chunks and takes the focus with it, so the
        focus check fires before the next chunk is refused. The claim decides who moved it."""
        shell.puppet("/hook", {"tab": shell.t1, "after": 2, "what": "click"})
        calls = {"n": 0}

        async def world(pg, op, params, *, fresh_ok, api="__dmAgent"):
            if op == "focusCheck":
                calls["n"] += 1
                return {"ok": calls["n"] < 2}  # the focus is gone after the owner's click
            return {"has": True, "tag": "textarea", "multiline": True, "editable": True}

        monkeypatch.setattr(ba, "_world_call", world)
        with pytest.raises(RuntimeError) as err:
            ba.browser_type({"target": "e7", "text": TEXT, "delay_ms": 0})
        assert str(err.value).startswith("STOPPED: the owner started using this tab (click)"), str(err.value)
        assert "Typed 48 of 100" in str(err.value)

    def test_a_focus_the_page_moved_is_still_reported_as_the_page(self, shell, pane, monkeypatch):
        async def world(pg, op, params, *, fresh_ok, api="__dmAgent"):
            if op == "focusCheck":
                return {"ok": False}
            return {"has": True, "tag": "textarea", "multiline": True, "editable": True}

        monkeypatch.setattr(ba, "_world_call", world)
        with pytest.raises(RuntimeError, match="the page moved it"):
            ba.browser_type({"target": "e7", "text": TEXT, "delay_ms": 0})
        assert shell.view()["last"]["outcome"] == "focus-moved"
