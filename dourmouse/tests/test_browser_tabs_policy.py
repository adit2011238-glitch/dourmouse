"""Phase B1: the pure decisions in electron/policy.js behind tabs, downloads, zoom,
history and bookmarks, exercised under plain node (the same pattern as
test_electron_hardening.py). The wiring that uses them is in test_browser_tabs_shell.py."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ELECTRON = Path(__file__).resolve().parents[2] / "electron"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(tmp_path, body):
    script = tmp_path / "p.js"
    script.write_text(f"const p = require({str(ELECTRON / 'policy.js')!r});\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_pane_only_loads_real_web_pages(tmp_path):
    out = run(tmp_path, """
R.good = ['http://a.b/', 'https://a.b/x?y=1#z', 'HTTPS://A.B'].map(p.paneUrlAllowed);
R.bad = ['file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,x', 'about:blank', 'chrome://gpu', 'ftp://a/b', 'blob:https://a/b',
         'view-source:https://a.b', 'https://', 'https:///x', 'http:// a.b', '', null, undefined, 42, 'https://a.b/' + 'x'.repeat(9000)].map(p.paneUrlAllowed);
R.blank = [p.isBlankUrl(''), p.isBlankUrl('about:blank'), p.isBlankUrl('data:text/html;charset=utf-8,x#dm-newtab'), p.isBlankUrl('https://a.b/'), p.isBlankUrl('data:text/html,x')];
""")
    assert out["good"] == [True, True, True]
    assert out["bad"] == [False] * 16
    assert out["blank"] == [True, True, True, False, False]


def test_navigation_rules_for_the_console_windows_are_unchanged(tmp_path):
    out = run(tmp_path, """
R.nav = [p.navigationAllowed('http://127.0.0.1:8765/x', 8765), p.navigationAllowed('https://example.com/', 8765), p.navigationAllowed('file:///etc/passwd', 8765)];
R.perm = [p.permissionAllowed('media', 'https://example.com', 8765), p.permissionAllowed('media', 'http://127.0.0.1:8765', 8765)];
""")
    assert out["nav"] == [True, False, False]
    assert out["perm"] == [False, True]


def test_the_rate_limiter_allows_a_burst_then_waits_for_the_window(tmp_path):
    out = run(tmp_path, """
let t = 1000;
const l = p.createLimiter(3, 10000, () => t);
R.burst = [l.allow(), l.allow(), l.allow(), l.allow()];
t += 9999; R.stillFull = l.allow();
t += 2; R.freed = l.allow();
""")
    assert out["burst"] == [True, True, True, False]
    assert out["stillFull"] is False and out["freed"] is True


def test_zoom_follows_chromes_ladder_and_is_bounded(tmp_path):
    out = run(tmp_path, """
R.up = [1, 1.1, 1.25].map((z) => p.nextZoom(z, 'in'));
R.down = [1, 0.9, 0.8].map((z) => p.nextZoom(z, 'out'));
R.top = p.nextZoom(5, 'in'); R.bottom = p.nextZoom(0.25, 'out');
R.reset = p.nextZoom(2.5, 'reset');
R.junk = [p.nextZoom(NaN, 'in'), p.nextZoom(-3, 'out'), p.nextZoom(1, 'sideways')];
R.host = [p.zoomHost('https://Docs.Example.com/a'), p.zoomHost('http://127.0.0.1:8080/'), p.zoomHost('about:blank'), p.zoomHost('data:text/html,x'), p.zoomHost('not a url')];
""")
    assert out["up"] == [1.1, 1.25, 1.5]
    assert out["down"] == [0.9, 0.8, 0.75]
    assert out["top"] == 5 and out["bottom"] == 0.25 and out["reset"] == 1
    assert out["junk"] == [1.1, 0.9, 1]
    assert out["host"] == ["docs.example.com", "127.0.0.1", "", "", ""]


def test_a_download_name_is_reduced_to_a_safe_file_name(tmp_path):
    out = run(tmp_path, """
R.names = ['../../etc/passwd', '..\\\\..\\\\win.ini', '/abs/path/x.txt', '.hidden', '...', 'a\\u0000b\\nc.txt', '  spaced.pdf  ', '', null, undefined, 'dir/'].map(p.safeFileName);
const long = 'x'.repeat(300) + '.tar.gz';
R.long = p.safeFileName(long);
R.taken = [p.uniqueFileName('a.txt', () => false), p.uniqueFileName('a.txt', (n) => n === 'a.txt'),
           p.uniqueFileName('a.txt', (n) => ['a.txt', 'a (1).txt'].includes(n)), p.uniqueFileName('noext', (n) => n === 'noext')];
""")
    assert out["names"] == ["passwd", "win.ini", "x.txt", "hidden", "download", "abc.txt", "spaced.pdf", "download", "download", "download", "download"]
    assert len(out["long"]) <= 120 and out["long"].endswith(".tar.gz") is False and out["long"].endswith(".gz")
    assert out["taken"] == ["a.txt", "a (1).txt", "a (2).txt", "noext (1)"]


def test_files_that_run_code_are_never_opened_from_the_shelf(tmp_path):
    out = run(tmp_path, """
const runs = ['x.app', 'x.command', 'x.pkg', 'x.dmg', 'x.sh', 'X.SH', 'x.py', 'x.js', 'x.jar', 'x.scpt', 'x.workflow', 'x.terminal', 'x.webloc', 'x.url',
              'x.exe', 'x.bat', 'x.ps1', 'x.mobileconfig', 'x.dylib', 'x.plugin', 'noextension', '.dotfile', ''];
R.runs = runs.map(p.isOpenableDownload);
R.safe = ['x.pdf', 'x.png', 'x.txt', 'x.zip', 'x.docx', 'archive.tar.gz', 'X.PDF'].map(p.isOpenableDownload);
""")
    assert out["runs"] == [False] * 23
    assert out["safe"] == [True] * 7


def test_the_quarantine_flag_and_origin_are_well_formed_and_cannot_be_injected(tmp_path):
    out = run(tmp_path, """
R.q = p.quarantineValue(1700000000000, 'Dour mouse;x', 'ABC-123;rm -rf');
R.plist = p.whereFromPlist(['https://a.b/?q=<x>&y="z"', '', null, 'https://ref.example/']);
R.plistEmpty = p.whereFromPlist([]);
R.pct = [p.percent(50, 100), p.percent(5, 0), p.percent(500, 100), p.percent(-1, 100), p.percent(1, undefined)];
""")
    flags, seconds, agent, uuid = out["q"].split(";")
    assert (flags, seconds, agent) == ("0081", "6553f100", "Dourmousex")  # a ; or space in the agent name cannot add a field
    assert all(c in "0123456789ABCDEFabcdef-" for c in uuid) and "rm" not in uuid
    assert "&lt;x&gt;" in out["plist"] and "&amp;y=&quot;z&quot;" in out["plist"] and "<array>" in out["plist"]
    assert out["plist"].count("<string>") == 2
    assert "<array></array>" in out["plistEmpty"]
    assert out["pct"] == [50, None, 100, 0, None]


def test_history_keeps_newest_first_dedupes_a_reload_and_refuses_non_web_pages(tmp_path):
    out = run(tmp_path, """
let h = [];
h = p.addVisit(h, { id: 'a', url: 'https://a.b/1', title: 'One', at: 1000 });
h = p.addVisit(h, { id: 'b', url: 'https://a.b/1', title: 'One v2', at: 5000 });      // reload inside 30 s: one visit
h = p.addVisit(h, { id: 'c', url: 'https://a.b/2', title: 'Two', at: 6000 });
h = p.addVisit(h, { id: 'd', url: 'https://a.b/1', title: 'One again', at: 100000 });  // later: a new visit
R.urls = h.map((e) => [e.id, e.url, e.title]);
R.refused = ['file:///x', 'javascript:1', 'about:blank', 'data:text/html,x', 'https://a.b/' + 'x'.repeat(3000)].map((u) => p.addVisit([], { id: 'z', url: u, title: 't', at: 1 }).length);
R.cap = p.addVisit([{ id: '1', url: 'https://a.b/1', title: '', at: 1 }, { id: '2', url: 'https://a.b/2', title: '', at: 2 }], { id: '3', url: 'https://a.b/3', title: '', at: 99999999 }, { cap: 2 }).map((e) => e.id);
R.title = p.updateVisitTitle([{ id: '1', url: 'https://a.b/1', title: '', at: 1 }], 'https://a.b/1', 'New\\ntitle')[0].title;
R.titleOld = p.updateVisitTitle(Array.from({ length: 6 }, (_, i) => ({ id: String(i), url: 'https://a.b/' + i, title: '', at: i })), 'https://a.b/5', 'Late').map((e) => e.title).join('|');
R.clean = p.cleanTitle('  a\\u0000b\\tc  ' + 'x'.repeat(400)).length;
""")
    assert out["urls"] == [["d", "https://a.b/1", "One again"], ["c", "https://a.b/2", "Two"], ["a", "https://a.b/1", "One v2"]]
    assert out["refused"] == [0, 0, 0, 0, 0]
    assert out["cap"] == ["3", "1"] or out["cap"] == ["3", "2"]
    assert out["title"] == "New title"
    assert out["titleOld"] == "|||||"  # a title only belongs to the newest few visits
    assert out["clean"] == 300


def test_history_search_remove_and_limits(tmp_path):
    out = run(tmp_path, """
const h = [{ id: '1', url: 'https://a.b/Alpha', title: 'First', at: 3 }, { id: '2', url: 'https://c.d/', title: 'Second ALPHA', at: 2 }, { id: '3', url: 'https://e.f/', title: 'Third', at: 1 }];
R.q = p.searchHistory(h, 'alpha').map((e) => e.id);
R.none = p.searchHistory(h, 'zzz').length;
R.limit = p.searchHistory(h, '', 2).map((e) => e.id);
R.limits = [p.clampLimit('5'), p.clampLimit('abc'), p.clampLimit('-4'), p.clampLimit('999999'), p.clampLimit(undefined)];
R.byId = p.removeFrom(h, { id: '2' }).list.map((e) => e.id);
R.byUrl = p.removeFrom(h, { url: 'https://e.f/' }).removed;
R.neither = p.removeFrom(h, {}).removed;
""")
    assert out["q"] == ["1", "2"] and out["none"] == 0 and out["limit"] == ["1", "2"]
    assert out["limits"] == [5, 200, 200, 1000, 200]
    assert out["byId"] == ["1", "3"] and out["byUrl"] == 1 and out["neither"] == 0


def test_bookmarks_dedupe_rename_and_are_capped(tmp_path):
    out = run(tmp_path, """
let r = p.addBookmark([], { id: 'a', url: 'https://a.b/', title: '  Site ', at: 1 });
R.first = [r.added, r.entry.title];
const again = p.addBookmark(r.list, { id: 'b', url: 'https://a.b/', title: 'Renamed', at: 2 });
R.again = [again.added, again.list.length, again.entry.id, again.entry.title];
const untitled = p.addBookmark([], { id: 'c', url: 'https://x.y/path', title: '', at: 1 });
R.untitled = untitled.entry.title;
R.bad = ['file:///x', 'javascript:1', 'about:blank'].map((u) => p.addBookmark([], { id: 'z', url: u, title: 't' }).entry);
const full = Array.from({ length: p.BOOKMARK_CAP }, (_, i) => ({ id: 'i' + i, url: 'https://a.b/' + i, title: 't', at: i }));
R.full = p.addBookmark(full, { id: 'n', url: 'https://new.example/', title: 't' }).entry;
""")
    assert out["first"] == [True, "Site"]
    assert out["again"] == [False, 1, "a", "Renamed"]
    assert out["untitled"] == "https://x.y/path"
    assert out["bad"] == [None, None, None] and out["full"] is None


def test_only_a_process_that_died_is_worth_restarting(tmp_path):
    out = run(tmp_path, "R.reasons = ['crashed', 'abnormal-exit', 'launch-failed', 'oom', 'integrity-failure', 'killed', 'clean-exit', '', undefined, null, 'CRASHED'].map(p.isCrashReason);")
    assert out["reasons"] == [True] * 5 + [False] * 6
