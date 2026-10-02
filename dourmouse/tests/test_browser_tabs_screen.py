"""Phase B1: the BROWSER screen's pure helpers for tabs, find, zoom, downloads, history and
bookmarks (node), and the rules the screen's DOM code must keep. What the DOM does with them
is verified live in the Electron app; this checks what can be checked without a browser."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "ui" / "assets" / "os" / "screens" / "browser"
_ELECTRON = Path(__file__).resolve().parents[2] / "electron"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def source(name="index.js"):
    return (_DIR / name).read_text(encoding="utf-8")


class TestTabsModel:
    def test_a_missing_or_odd_state_is_one_empty_model(self, tmp_path):
        out = node(tmp_path, "R.a = h.tabsModel(null); R.b = h.tabsModel('x'); R.c = h.tabsModel({ tabs: 5, zoom: 'big', find: 3 });")
        for key in ("a", "b", "c"):
            assert out[key]["tabs"] == [] and out[key]["zoom"] == 1 and out[key]["find"] is None and out[key]["activeId"] == 0

    def test_tabs_are_cleaned_and_only_image_data_addresses_survive_as_a_favicon(self, tmp_path):
        out = node(tmp_path, """
const png = 'data:image/png;base64,iVBORw0KGgo=';
R.m = h.tabsModel({ activeTab: 2, zoom: 1.5, closedTabs: 2, blockedPopups: 1, find: { text: 'a', active: 1, matches: 4 },
  tabs: [{ id: 1, url: 'https://a.b/', title: 'A', favicon: png, loading: true },
         { id: 2, url: 'https://c.d/', title: 'C', favicon: 'https://evil.example/x.png', active: true, audible: true },
         { id: '3', url: 'x' }, null, { id: 4, url: 5, title: 7, favicon: 'javascript:alert(1)' },
         { id: 5, favicon: 'data:text/html;base64,PHNjcmlwdD4=' }, { id: 6, favicon: 'data:image/svg+xml;base64,PHN2Zz4=' }] });
R.fav = ['', null, 5, png, 'data:image/png;base64,', 'data:image/png;base64,' + 'A'.repeat(40001), 'DATA:image/png;base64,AA'].map(h.safeFavicon);
""")
        tabs = out["m"]["tabs"]
        assert [t["id"] for t in tabs] == [1, 2, 4, 5, 6]  # a non-integer id and null are dropped
        assert tabs[0]["favicon"].startswith("data:image/png") and tabs[0]["loading"] is True
        assert tabs[1]["favicon"] == "" and tabs[1]["active"] and tabs[1]["audible"]
        assert tabs[2]["url"] == "" and tabs[2]["title"] == "" and tabs[2]["favicon"] == ""
        assert tabs[3]["favicon"] == "" and tabs[4]["favicon"] == ""  # html and svg data are not images the strip may draw
        assert out["m"]["activeId"] == 2 and out["m"]["zoom"] == 1.5 and out["m"]["closed"] == 2 and out["m"]["blocked"] == 1
        assert out["m"]["find"] == {"text": "a", "active": 1, "matches": 4}
        assert out["fav"][0] == "" and out["fav"][3].startswith("data:image/png") and out["fav"][4:] == ["", "", ""]

    def test_labels_zoom_find_and_the_redraw_key(self, tmp_path):
        out = node(tmp_path, """
R.label = [h.tabLabel(null), h.tabLabel({ url: '' }), h.tabLabel({ url: 'https://a.b/x', title: 'Hello' }), h.tabLabel({ url: 'https://a.b/x', title: '' }), h.tabLabel({ url: 'https://a.b/x', title: 'https://a.b/x' })];
R.zoom = [h.zoomLabel(1), h.zoomLabel(1.25), h.zoomLabel(0.67), h.zoomLabel(NaN), h.zoomLabel(undefined)];
R.find = [h.findLabel(null, ''), h.findLabel(null, 'a'), h.findLabel({ text: 'a', active: 2, matches: 7 }, 'a'), h.findLabel({ text: 'a', active: 0, matches: 0 }, 'a'), h.findLabel({ text: 'a', active: 2, matches: 7 }, 'ab')];
const m = (loading) => ({ tabs: [{ id: 1, url: 'https://a.b/', title: 'A', favicon: '', loading, active: true, audible: false }] });
R.key = [h.tabsKey(m(false), ''), h.tabsKey(m(false), ''), h.tabsKey(m(true), ''), h.tabsKey(m(false), 'https://a.b/')];
""")
        assert out["label"] == ["New tab", "New tab", "Hello", "a.b", "a.b"]
        assert out["zoom"] == ["100%", "125%", "67%", "100%", "100%"]
        assert out["find"] == ["", "", "2 of 7", "No matches", ""]
        key = out["key"]
        assert key[0] == key[1] and len({key[0], key[2], key[3]}) == 3  # unchanged tabs do not redraw; any change does


class TestDownloadsHistoryBookmarks:
    def test_downloads_are_cleaned_and_described_in_plain_words(self, tmp_path):
        out = node(tmp_path, """
const raw = [{ id: 'a', filename: 'f.zip', state: 'progressing', received: 512, total: 2048, percent: 25 },
             { id: 'b', filename: 'g.pdf', state: 'completed', received: 3, total: 3, openable: true, quarantined: true },
             { id: 'c', filename: 'x.sh', state: 'completed', received: 1500000, total: 1500000, quarantined: false },
             { id: 'd', filename: 'h', state: 'cancelled' }, { id: 'e', filename: 'i', state: 'weird', error: 'disk full' },
             { id: 'f', filename: 'j', state: 'progressing', paused: true, received: 10 }, { filename: 'no id' }, null];
const m = h.downloadsModel(raw);
R.n = m.length; R.states = m.map((d) => d.state); R.openable = m.map((d) => d.openable);
R.lines = m.map(h.downloadLine); R.active = h.activeDownloads(m);
R.size = [0, 1023, 1024, 1536, 1048576, 150 * 1048576, -1, NaN, 'x'].map(h.formatBytes);
R.notList = [h.downloadsModel(null), h.downloadsModel({})];
""")
        assert out["n"] == 6 and out["states"] == ["progressing", "completed", "completed", "cancelled", "interrupted", "progressing"]
        assert out["openable"] == [False, True, False, False, False, False]  # only the shell says what may be opened
        assert out["lines"][0] == "512 B of 2.0 KB, 25%"
        assert out["lines"][1] == "Done, 3 B, flagged for Gatekeeper"
        assert out["lines"][2].startswith("Done, 1.4 MB") and "not flagged" in out["lines"][2]
        assert out["lines"][3] == "Cancelled" and out["lines"][4] == "Did not finish: disk full"
        assert out["lines"][5] == "Paused, 10 B"
        assert out["active"] == 2
        assert out["size"] == ["0 B", "1023 B", "1.0 KB", "1.5 KB", "1.0 MB", "150 MB", "", "", ""]
        assert out["notList"] == [[], []]

    def test_history_groups_by_day_newest_first(self, tmp_path):
        out = node(tmp_path, """
const now = new Date(2026, 9, 2, 15, 0, 0).getTime();
const at = (d, hh) => new Date(2026, 9, d, hh, 0, 0).getTime();
const g = h.groupHistory([{ id: '1', url: 'https://a.b/1', title: 'One', at: at(2, 14) }, { id: '2', url: 'https://a.b/2', title: 'Two', at: at(2, 9) },
  { id: '3', url: 'https://a.b/3', title: '', at: at(1, 20) }, { id: '4', url: 'https://a.b/4', title: 'Old', at: at(2, 10) - 86400000 * 5 },
  { url: 5, at: 1 }, null, { id: '9', url: 'https://x.y/', title: 'No time', at: 'soon' }], now);
R.labels = g.map((x) => x.label); R.counts = g.map((x) => x.items.length); R.first = g[0].items.map((e) => e.id);
R.empty = [h.groupHistory(null), h.groupHistory([])];
""")
        assert out["labels"][:2] == ["Today", "Yesterday"] and out["labels"][2] == "Sun Sep 27 2026"
        assert out["counts"][:2] == [2, 1] and out["first"] == ["1", "2"]
        assert out["empty"] == [[], []]

    def test_bookmarks_keep_only_web_addresses_and_name_themselves(self, tmp_path):
        out = node(tmp_path, """
const m = h.bookmarksModel([{ id: 'a', url: 'https://a.b/x', title: 'A' }, { id: 'b', url: 'https://c.d/', title: '' }, { id: 'c', url: 'javascript:1', title: 'X' },
  { id: 'd', url: 'file:///etc/passwd', title: 'Y' }, { url: 'https://e.f/' }, null, { id: 'g', url: 'https://g.h/', title: 'G'.repeat(400) }]);
R.ids = m.map((b) => b.id); R.titles = m.map((b) => b.title.length); R.c = m[1].title;
R.find = [h.bookmarkFor(m, 'https://a.b/x').id, h.bookmarkFor(m, 'https://nope/')];
R.notList = h.bookmarksModel('x');
""")
        assert out["ids"] == ["a", "b", "g"] and out["c"] == "c.d" and out["titles"][2] == 300
        assert out["find"] == ["a", None] and out["notList"] == []


class TestScreenRules:
    """The generic contract is checked for every screen by test_os_screen_contract.py; these are the
    B1-specific promises."""

    def test_the_screen_reaches_the_shell_only_through_the_pane_bridge_and_the_server_only_through_ctx_api(self):
        src = source()
        assert "ctx.api.get('/api/os/browser/bookmarks')" in src
        assert "ctx.api.post('/api/os/browser/bookmarks/add'" in src and "ctx.api.post('/api/os/browser/history/clear'" in src
        assert not re.search(r"\bfetch\s*\(|XMLHttpRequest|sendBeacon|setInterval", src)
        assert "ipcRenderer" not in src and "require(" not in src

    def test_keys_go_through_ctx_keys_and_are_released_with_the_screen(self):
        src = source()
        assert src.count("ctx.keys.bind(") >= 10
        assert "ctx.keys.pushEsc" in src
        assert not re.search(r"document\.addEventListener|window\.addEventListener", src)
        assert "offFindEsc()" in src and "offPanelEsc()" in src and "offDl()" in src and "offCmd()" in src  # every subscription is undone on unmount

    def test_the_native_view_is_hidden_while_a_panel_covers_it_and_when_the_screen_is_left(self):
        src = source()
        assert "!panel &&" in src  # applyView: no panel open
        assert "A BrowserView left visible after leaving this screen is a defect." in src
        assert "pane.screen(false)" in src

    def test_the_dangerous_download_actions_are_buttons_the_owner_presses_and_open_is_disabled_when_the_shell_says_so(self):
        src = source()
        assert "d.openable ? null : { disabled: true" in src
        assert "dlAction(d.id, act)" in src
        assert "auto" not in "".join(re.findall(r"btn\('OPEN'[^\n]*", src)).lower()

    def test_history_is_cleared_only_after_a_confirmation(self):
        src = source()
        assert "confirmHere(" in src and "/api/os/browser/history/clear" in src
        before_confirm = src.split("confirmHere(", 1)[0]
        assert "history/clear" not in before_confirm.split("function askClearHistory")[-1]

    def test_a_favicon_is_drawn_only_from_the_model_and_everything_else_is_text(self):
        src = source()
        assert "img.src = t.favicon" in src
        assert src.count(".src =") == src.count("frameEl.src =") + 1  # the iframe fallback and the one favicon
        assert not re.search(r"\.innerHTML\s*=", src)


def test_the_preload_exposes_only_named_calls_and_no_raw_electron():
    pre = (_ELECTRON / "preload.js").read_text(encoding="utf-8")
    assert pre.count("contextBridge.exposeInMainWorld") == 2
    for leaked in ("ipcRenderer,", "ipcRenderer:", "require:", "shell:", "fs:"):
        assert leaked not in pre.replace("const { contextBridge, ipcRenderer }", "")
    for name in ("newTab", "closeTab", "selectTab", "reopenTab", "find", "findStop", "zoom", "print", "downloads", "downloadAction", "onDownloads", "onCommand"):
        assert f"{name}:" in pre
    # a subscription hands back its own remover
    assert "removeListener" in pre
