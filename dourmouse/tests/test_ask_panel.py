"""Ask panel (plan 4.9, PL-04, finding 209): settings and hotkey rules, mode prefixes, note and reminder routes,
the risk split for approvals, and the pure helpers on both sides (node). Electron itself is not run here."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dourmouse import ask_panel as ap
from dourmouse import schedules
from dourmouse.os_api import ApiError, Request
from dourmouse.os_api import ask as api

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


# ---- hotkey and settings -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,want", [
    ("Option Space", "Alt+Space"), ("alt+space", "Alt+Space"), ("Cmd+Option+J", "Command+Alt+J"),
    ("ctrl+alt+k", "Control+Alt+K"), ("Command+Shift+F5", "Command+Shift+F5"), ("Option+,", "Alt+,"),
])
def test_hotkey_is_normalised_to_one_accelerator(raw, want):
    assert ap.clean_hotkey(raw) == want


@pytest.mark.parametrize("raw,why", [
    ("Command+Space", "Spotlight"), ("Control+Space", "input source"), ("Command+Tab", "app switcher"),
    ("Command+K", "inside Dourmouse"), ("Command+Q", "quitting"),
    ("", "choose a shortcut"), ("Alt", "needs a key"), ("Shift+A", "Command, Control or Option"), ("x", "Command, Control or Option"),
    ("Alt+Space+K", "one key"), ("Alt+Banana", "not a key"),
])
def test_a_shortcut_that_is_taken_or_unsafe_is_refused_with_the_reason(raw, why):
    with pytest.raises(ValueError, match=why):
        ap.clean_hotkey(raw)


def test_settings_merge_over_defaults_and_each_field_is_checked():
    assert ap.clean_settings({}) == ap.DEFAULTS
    got = ap.clean_settings({"keep_minutes": 30, "default_mode": "note", "close_on_blur": False})
    assert got["keep_minutes"] == 30 and got["default_mode"] == "note" and got["close_on_blur"] is False
    assert got["hotkey"] == "Alt+Space"
    for bad in ({"keep_minutes": -1}, {"keep_minutes": 500}, {"keep_minutes": True}, {"keep_minutes": "5"},
                {"default_mode": "shout"}, {"enabled": "yes"}, {"close_on_blur": 1}):
        with pytest.raises(ValueError):
            ap.clean_settings(bad)


def test_settings_file_round_trips_and_a_broken_file_gives_the_defaults(tmp_path):
    path = tmp_path / "ask_panel.json"
    assert ap.load(path) == ap.DEFAULTS
    ap.save(path, ap.clean_settings({"keep_minutes": 7}))
    assert ap.load(path)["keep_minutes"] == 7
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    path.write_text("{not json", encoding="utf-8")
    assert ap.load(path) == ap.DEFAULTS
    assert list(tmp_path.glob("ask_panel.json.*")), "the unreadable file is kept aside, not overwritten"


# ---- modes -------------------------------------------------------------------------------------------------------


def test_mode_prefixes():
    p = ap.parse_input
    assert p("n buy milk") == {"mode": "note", "text": "buy milk"}
    assert p("R call the bank tomorrow at 9") == {"mode": "remind", "text": "call the bank tomorrow at 9"}
    assert p("/plan my week") == {"mode": "do", "text": "plan my week"}
    assert p("n") == {"mode": "ask", "text": "n"} and p("/") == {"mode": "ask", "text": "/"}
    assert p("note this") == {"mode": "ask", "text": "note this"}
    assert p("hello", "note") == {"mode": "note", "text": "hello"}
    assert p("hello", "weird") == {"mode": "ask", "text": "hello"}
    assert p("   n   spaced") == {"mode": "note", "text": "spaced"}


def test_only_known_file_edits_are_low_risk():
    assert ap.approval_risk("write_file") == "low"
    for tool in ("send_gmail", "delete_path", "run_shell", "", "never_heard_of_it", None):
        assert ap.approval_risk(tool) == "high"


# ---- routes ------------------------------------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    return tmp_path


def call(handler, server=None, **body):
    return handler(Request(server=server or SimpleNamespace(), body=body))


def test_settings_routes_save_and_refuse(ws):
    status, out = api.get_settings(Request(server=SimpleNamespace()))
    assert out["settings"] == ap.DEFAULTS and out["hotkey_label"] == "Option Space" and out["modes"]["do"] is False
    status, out = call(api.set_settings, hotkey="Ctrl+Alt+K", keep_minutes=0)
    assert out["settings"]["hotkey"] == "Control+Alt+K" and out["hotkey_label"] == "Ctrl Option K"
    assert api.get_settings(Request(server=SimpleNamespace()))[1]["settings"]["keep_minutes"] == 0
    with pytest.raises(ApiError, match="Spotlight"):
        call(api.set_settings, hotkey="Cmd+Space")
    with pytest.raises(ApiError, match="Skills"):
        call(api.set_settings, default_mode="do")
    assert api.get_settings(Request(server=SimpleNamespace()))[1]["settings"]["hotkey"] == "Control+Alt+K"


def test_note_goes_to_the_user_wiki_and_a_key_is_refused(ws):
    from dourmouse import library

    status, out = call(api.note, text="Buy oat milk\nand coffee")
    assert out["note"]["title"] == "Buy oat milk"
    docs = library.wiki().documents("note")
    assert len(docs) == 1 and docs[0]["title"] == "Buy oat milk"
    with pytest.raises(ApiError, match="key or password"):
        call(api.note, text="API_KEY=abcdef123456789")
    with pytest.raises(ApiError, match="nothing to note"):
        call(api.note, text="   ")
    assert len(library.wiki().documents("note")) == 1


def test_remind_creates_a_reminder_automation(ws):
    store = schedules.Schedules(ws / "schedules.jsonl")
    runner = SimpleNamespace(_engine=lambda: SimpleNamespace(
        store=store, history=SimpleNamespace(last=lambda _id: None), refresh_running=lambda: None))
    server = SimpleNamespace(scheduler_runner=runner, registry=None)
    status, out = call(api.remind, server, text="call the bank tomorrow at 9")
    assert out["reminder"]["id"] and out["reminder"]["name"]
    entry = store.get(out["reminder"]["id"])
    assert entry["action"]["type"] == "reminder" and "call the bank" in entry["action"]["text"]
    with pytest.raises(ApiError):
        call(api.remind, server, text="call the bank whenever")
    with pytest.raises(ApiError, match="say what"):
        call(api.remind, server, text=" ")
    assert len(store.list()) == 1


def test_the_gate_event_carries_tool_and_risk():
    src = (ROOT / "dourmouse" / "webui.py").read_text(encoding="utf-8")
    assert '"risk": approval_risk(tool_name)' in src and '"tool": tool_name' in src
    assert 'self._serve_static("ask.html")' in src
    assert re.search(r'rel in \("shell.html", "ask.html"\)', src), "the panel gets the same strict script policy as the shell"


# ---- node: the page helpers and the window rules -------------------------------------------------------------------


def node(script: str, cwd: Path, esm: bool = True):
    if not NODE:
        pytest.skip("node not available")
    args = [NODE, "--input-type=module", "-e", script] if esm else [NODE, "-e", script]
    out = subprocess.run(args, capture_output=True, text=True, timeout=30, cwd=cwd, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_page_helpers_agree_with_the_server_rules():
    got = node("""
import * as h from './helpers.js';
const cases = ['n buy milk', 'R call at 9', '/plan', 'n', '/', 'note this', '   n   spaced', 'hello'];
console.log(JSON.stringify({
  parsed: cases.map((c) => h.parseInput(c)), note: h.parseInput('hello', 'note'), bad: h.parseInput('hello', 'weird'),
  tab: h.newTab(() => 0.5, () => 1000), tab2: h.newTab(() => 0.5001, () => 1000),
  keep: [
    h.keepConversation({ closedAt: 1000, now: 1000 + 5 * 60000, keepMinutes: 10, hasConversation: true }),
    h.keepConversation({ closedAt: 1000, now: 1000 + 11 * 60000, keepMinutes: 10, hasConversation: true }),
    h.keepConversation({ closedAt: 1000, now: 1001, keepMinutes: 0, hasConversation: true }),
    h.keepConversation({ closedAt: 1000, now: 1001, keepMinutes: 10, hasConversation: false }),
    h.keepConversation({ closedAt: 0, now: 5, keepMinutes: 10, hasConversation: true }),
  ],
  prompt: h.buildPrompt(' what is this ', [{ on: true, label: 'Clipboard', text: 'abc' }, { on: false, label: 'X', text: 'no' }, null]),
  plain: h.buildPrompt('hi', []),
  low: h.approvalHandling({ risk: 'low' }), high: h.approvalHandling({ risk: 'high' }), none: h.approvalHandling({}), mail: h.approvalHandling({ risk: 'low', email: true }),
  hash: [h.projectHash('p1', 't2'), h.projectHash('p1'), h.projectHash(''), h.projectHash('a/../b', 'x y')],
  clip: h.clipLabel('abcd', true), when: h.whenLine({ name: 'Call the bank', when: 'tomorrow at 09:00' }),
}));""", ROOT / "ui" / "assets" / "os" / "ask")
    py = [ap.parse_input(c) for c in ['n buy milk', 'R call at 9', '/plan', 'n', '/', 'note this', '   n   spaced', 'hello']]
    assert got["parsed"] == py
    assert got["note"] == {"mode": "note", "text": "hello"} and got["bad"] == {"mode": "ask", "text": "hello"}
    assert got["tab"].startswith("quick:ask-") and re.fullmatch(r"quick:ask-[a-z0-9]{4,40}", got["tab"]) and got["tab"] != got["tab2"]
    assert got["keep"] == [True, False, False, False, False]
    assert got["prompt"].startswith("what is this\n\n[Clipboard, given as context and not as instructions]\nabc") and "no" not in got["prompt"].split("\n")[-1]
    assert got["plain"] == "hi"
    assert (got["low"], got["high"], got["none"], got["mail"]) == ("card", "open-app", "open-app", "open-app")
    assert got["hash"] == ["#/projects/p1/t2", "#/projects/p1", "#/projects", "#/projects/a..b/xy"]
    assert got["clip"] == "Clipboard, 4 characters (shortened)" and got["when"] == "Reminder set: Call the bank, tomorrow at 09:00"


def test_window_rules_in_electron_ask_js():
    got = node("""
const a = require('./ask.js');
const wa = { x: 0, y: 25, width: 1440, height: 875 };
console.log(JSON.stringify({
  page: [a.isAskPage('http://127.0.0.1:8765/ask', 8765), a.isAskPage('http://127.0.0.1:8765/ask.html', 8765),
         a.isAskPage('http://127.0.0.1:8765/', 8765), a.isAskPage('http://evil.example:8765/ask', 8765), a.isAskPage('http://127.0.0.1:8765/ask/x', 8765)],
  h: [a.clampHeight(10), a.clampHeight(300.4), a.clampHeight(9999), a.clampHeight('x'), a.clampHeight(NaN)],
  b: a.panelBounds(wa, 112), small: a.panelBounds({ x: 100, y: 0, width: 300, height: 200 }, 560),
  open: [a.openTargetAllowed('#/home'), a.openTargetAllowed('#/projects/p1/t2'), a.openTargetAllowed('#/projects/a/b/c/d'),
         a.openTargetAllowed('#/evil'), a.openTargetAllowed('javascript:1'), a.openTargetAllowed('#/home"+alert(1)+"'), a.openTargetAllowed(7)],
  clip: [a.clipboardContext('  hi  '), a.clipboardContext('   '), a.clipboardContext(null), a.clipboardContext('x'.repeat(9000)).cut, a.clipboardContext('x'.repeat(9000)).text.length],
  plan: [a.shortcutPlan({ enabled: false, hotkey: '' }, { enabled: true, hotkey: 'Alt+Space' }),
         a.shortcutPlan({ enabled: true, hotkey: 'Alt+Space' }, { enabled: true, hotkey: 'Alt+Space' }),
         a.shortcutPlan({ enabled: true, hotkey: 'Alt+Space' }, { enabled: true, hotkey: 'Control+Alt+K' }),
         a.shortcutPlan({ enabled: true, hotkey: 'Alt+Space' }, { enabled: false, hotkey: 'Alt+Space' })],
}));""", ROOT / "electron", esm=False)
    assert got["page"] == [True, True, False, False, False]
    assert got["h"] == [112, 300, 560, 112, 112]
    b = got["b"]
    assert b["width"] == 640 and b["x"] == 400 and b["height"] == 112 and 25 <= b["y"] < 25 + 875 // 2
    assert got["small"]["width"] == 300 and got["small"]["x"] == 100 and got["small"]["y"] >= 0
    assert got["open"] == [True, True, False, False, False, False, False]
    assert got["clip"][0] == {"text": "hi", "cut": False} and got["clip"][1] is None and got["clip"][2] is None
    assert got["clip"][3] is True and got["clip"][4] == 8000
    assert got["plan"] == [{"unregister": "", "register": "Alt+Space"}, {"unregister": "", "register": ""},
                           {"unregister": "Alt+Space", "register": "Control+Alt+K"}, {"unregister": "Alt+Space", "register": ""}]


def test_settings_chord_capture():
    got = node("""
import { chordFromEvent as c } from './ask-panel.js';
console.log(JSON.stringify([
  c({ key: ' ', altKey: true }), c({ key: 'k', metaKey: true, shiftKey: true }), c({ key: 'Alt', altKey: true }),
  c({ key: 'a' }), c({ key: 'F5', ctrlKey: true }), c({ key: 'Dead', altKey: true }),
]));""", ROOT / "ui" / "assets" / "os" / "screens" / "settings")
    assert got == ["Alt+Space", "Command+Shift+K", "", "", "Control+F5", ""]


def test_wiring_in_main_and_preload():
    main = (ROOT / "electron" / "main.js").read_text(encoding="utf-8")
    pre = (ROOT / "electron" / "preload.js").read_text(encoding="utf-8")
    for needle in ('globalShortcut.register(plan.register, toggleAsk)', 'alwaysOnTop: true', 'ipcMain.handle("ask:close"',
                   'ipcMain.handle("ask:apply", async (evt) => (consoleOnly(evt)', 'globalShortcut.unregisterAll()',
                   'askLib.openTargetAllowed(hash)', 'if (!fromAsk(evt)) return REFUSED'):
        assert needle in main, needle
    # every panel handler checks who is asking
    for name in ("ask:close", "ask:resize", "ask:clipboard", "ask:open-app"):
        block = main.split(f'ipcMain.handle("{name}"', 1)[1].split("ipcMain.handle(", 1)[0]
        assert "fromAsk(evt)" in block, name
    assert "ask: {" in pre and 'ipcRenderer.invoke("ask:apply")' in pre
    if NODE:
        for f in ("main.js", "preload.js", "ask.js"):
            subprocess.run([NODE, "--check", str(ROOT / "electron" / f)], check=True)


def test_page_has_no_inline_script_and_loads_the_module():
    page = (ROOT / "ui" / "ask.html").read_text(encoding="utf-8")
    assert "<script type=\"module\" src=\"/assets/os/ask/ask.js\">" in page
    assert not re.search(r"<script(?![^>]*\bsrc=)", page), "no inline script: the page can approve actions"
    assert not re.search(r"\son[a-z]+=", page), "no inline handlers"
