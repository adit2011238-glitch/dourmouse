"""Phase B3: the rules in electron/importers.js (reading a Chrome profile folder and a password
CSV, read-only and explicit), exercised under plain node against a FAKE Chrome profile built in a
temp folder with Python's sqlite3 module. Nothing here touches the real Chrome profile, its
Keychain item or any real password. The wiring (native dialogs, the stores, the vault) is in
test_browser_import_shell.py."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

ELECTRON = Path(__file__).resolve().parents[2] / "electron"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

WEBKIT_EPOCH_MS = 11644473600000


def chrome_us(ms: int) -> int:
    return (ms + WEBKIT_EPOCH_MS) * 1000


def make_chrome_profile(root: Path, *, history=True, bookmarks=True) -> Path:
    """A fake Chrome profile folder: Bookmarks, History, and files that must never be read."""
    prof = root / "Default"
    prof.mkdir(parents=True)
    if bookmarks:
        (prof / "Bookmarks").write_text(json.dumps({
            "roots": {
                "bookmark_bar": {"type": "folder", "name": "Bar", "children": [
                    {"type": "url", "name": "Example", "url": "https://example.com/a", "date_added": str(chrome_us(1_700_000_000_000))},
                    {"type": "folder", "name": "Sub", "children": [
                        {"type": "url", "name": "Deep", "url": "https://deep.example/x", "date_added": "0"},
                        {"type": "url", "name": "Script", "url": "javascript:alert(1)"},
                    ]},
                    {"type": "url", "name": "File", "url": "file:///etc/passwd"},
                ]},
                "other": {"type": "folder", "name": "Other", "children": [{"type": "url", "name": "Dup", "url": "https://example.com/a"}]},
                "synced": {"type": "folder", "name": "Mobile", "children": []},
            },
            "version": 1,
        }), encoding="utf-8")
    if history:
        con = sqlite3.connect(prof / "History")
        con.execute("create table urls (id integer primary key, url longvarchar, title longvarchar, visit_count integer default 0, typed_count integer default 0, last_visit_time integer not null, hidden integer default 0)")
        rows = [
            ("https://news.example/story", "A story", chrome_us(1_700_000_500_000), 0),
            ("https://example.com/a", "Example", chrome_us(1_700_000_100_000), 0),
            ("https://hidden.example/", "Hidden", chrome_us(1_700_000_900_000), 1),
            ("chrome://settings/", "Settings", chrome_us(1_700_000_800_000), 0),
            ("file:///Users/x/secret.txt", "A local file", chrome_us(1_700_000_700_000), 0),
            ("https://never.example/", "Never visited", 0, 0),
            ("https://old.example/", "Old", chrome_us(1_600_000_000_000), 0),
        ]
        con.executemany("insert into urls (url, title, last_visit_time, hidden) values (?, ?, ?, ?)", rows)
        con.commit()
        con.close()
    # things in a real Chrome profile that this code must never open
    (prof / "Login Data").write_bytes(b"CANARY-LOGIN-DATA " * 8)
    (prof / "Cookies").write_bytes(b"CANARY-COOKIES " * 8)
    (prof / "Web Data").write_bytes(b"CANARY-WEB-DATA " * 8)
    (prof / "Preferences").write_text('{"canary": "CANARY-PREFERENCES"}', encoding="utf-8")
    return prof


def run(tmp_path, body, *, extra=""):
    script = tmp_path / "i.js"
    script.write_text(
        f"const imp = require({str(ELECTRON / 'importers.js')!r});\nconst policy = require({str(ELECTRON / 'policy.js')!r});\n"
        f"const fs = require('fs'); const path = require('path'); const os = require('os');\nconst R = {{}};\n{extra}\n{body}\nconsole.log(JSON.stringify(R));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


READER = """
const { execFileSync } = require('child_process');
const reader = imp.makeSqliteReader({ execFileSync, requireModule: (n) => require(n) });
const cliReader = imp.makeSqliteReader({ execFileSync, requireModule: () => { throw new Error('no node:sqlite'); } });
// every path the importer asks the file system about, so what it opens can be checked
const seen = [];
const rec = (name) => (...a) => { seen.push(String(a[0])); return fs[name](...a); };
const spyFs = { lstatSync: rec('lstatSync'), readFileSync: rec('readFileSync'), copyFileSync: rec('copyFileSync'), mkdtempSync: rec('mkdtempSync'), chmodSync: rec('chmodSync'), rmSync: rec('rmSync'),
  statSync: rec('statSync'), readdirSync: rec('readdirSync'), openSync: rec('openSync'), existsSync: rec('existsSync') };
"""


class TestChromeTimeAndBookmarks:
    def test_chrome_microseconds_since_1601_become_milliseconds_since_1970(self, tmp_path):
        out = run(tmp_path, f"""
R.v = [imp.chromeTimeToMs({chrome_us(1_700_000_000_000)}), imp.chromeTimeToMs('{chrome_us(1_700_000_000_000)}'), imp.chromeTimeToMs(0), imp.chromeTimeToMs(-5), imp.chromeTimeToMs('x'), imp.chromeTimeToMs(null), imp.chromeTimeToMs(1)];
""")
        assert out["v"] == [1_700_000_000_000, 1_700_000_000_000, 0, 0, 0, 0, 0]

    def test_bookmarks_are_flattened_from_every_folder_in_order(self, tmp_path):
        prof = make_chrome_profile(tmp_path / "c")
        out = run(tmp_path, f"R.p = imp.parseChromeBookmarks(fs.readFileSync({str(prof / 'Bookmarks')!r}, 'utf8'));")
        assert out["p"]["ok"] is True
        assert [i["url"] for i in out["p"]["items"]] == ["https://example.com/a", "https://deep.example/x", "javascript:alert(1)", "file:///etc/passwd", "https://example.com/a"]
        assert out["p"]["items"][0]["at"] == 1_700_000_000_000 and out["p"]["items"][1]["at"] == 0

    def test_a_file_that_is_not_a_bookmarks_file_is_refused_with_a_reason(self, tmp_path):
        out = run(tmp_path, "R.v = ['', 'not json', '[]', '{}', '{\"roots\": 5}', 'null'].map((t) => imp.parseChromeBookmarks(t));")
        assert all(v["ok"] is False and v["error"] and v["items"] == [] for v in out["v"])

    def test_merging_adds_web_pages_only_and_counts_the_rest(self, tmp_path):
        out = run(tmp_path, """
const items = [{ url: 'https://a.example/', title: 'A', at: 5 }, { url: 'https://a.example/', title: 'A again', at: 6 }, { url: 'javascript:alert(1)', title: 'x', at: 0 },
  { url: 'file:///etc/passwd', title: 'x', at: 0 }, { url: 'https://b.example/', title: 'B', at: 0 }];
let n = 0;
const existing = [{ id: 'old', url: 'https://b.example/', title: 'B mine', at: 1 }];
const r = imp.mergeBookmarks(existing, items, () => 'id' + (++n), () => 999);
R.counts = r.counts; R.list = r.list.map((b) => [b.url, b.title, b.at]);
R.untouched = existing.length === 1 && existing[0].title === 'B mine';
""")
        assert out["counts"] == {"found": 5, "added": 1, "existing": 2, "skipped": 2, "full": 0}
        assert out["list"] == [["https://b.example/", "B mine", 1], ["https://a.example/", "A", 5]]  # an existing bookmark keeps its own title
        assert out["untouched"] is True

    def test_the_bookmark_list_stops_at_its_cap(self, tmp_path):
        out = run(tmp_path, """
const items = Array.from({ length: 1500 }, (_, i) => ({ url: 'https://s.example/' + i, title: 't', at: i + 1 }));
let n = 0; const r = imp.mergeBookmarks([], items, () => 'id' + (++n));
R.counts = r.counts; R.len = r.list.length;
""")
        assert out["len"] == 1000 and out["counts"]["added"] == 1000 and out["counts"]["full"] == 500


class TestHistory:
    def test_the_real_history_file_is_read_through_a_temp_copy_that_is_deleted(self, tmp_path):
        prof = make_chrome_profile(tmp_path / "c")
        before = (prof / "History").read_bytes()
        out = run(tmp_path, f"""
const before = new Set(fs.readdirSync(os.tmpdir()).filter((f) => f.startsWith('dm-import-')));
const r = imp.readChromeProfile({{ fs: spyFs, path, os, readHistory: reader }}, {str(prof)!r}, {{ bookmarks: false, history: true }});
R.ok = r.ok; R.rows = r.history; R.notes = r.notes;
R.leftBehind = fs.readdirSync(os.tmpdir()).filter((f) => f.startsWith('dm-import-') && !before.has(f)).length;
R.copyTarget = seen.filter((p) => p.includes('dm-import-'));
""", extra=READER)
        assert out["ok"] is True and out["notes"] == []
        # hidden, never-visited rows are left out in SQL; newest first; chrome:// and file:// still come back (the merge, not the reader, refuses them)
        urls = [r["url"] for r in out["rows"]]
        assert urls == ["chrome://settings/", "file:///Users/x/secret.txt", "https://news.example/story", "https://example.com/a", "https://old.example/"]
        assert "https://hidden.example/" not in urls and "https://never.example/" not in urls
        assert out["rows"][0]["at"] == 1_700_000_800_000 and out["rows"][2]["at"] == 1_700_000_500_000  # converted to a unix time with no precision lost
        assert out["leftBehind"] == 0  # the copy was removed
        assert (prof / "History").read_bytes() == before  # Chrome's own file was not touched

    def test_the_command_line_fallback_reads_the_same_rows(self, tmp_path):
        prof = make_chrome_profile(tmp_path / "c")
        out = run(tmp_path, f"""
const a = imp.readChromeProfile({{ fs, path, os, readHistory: reader }}, {str(prof)!r}, {{ history: true }}).history;
const b = imp.readChromeProfile({{ fs, path, os, readHistory: cliReader }}, {str(prof)!r}, {{ history: true }}).history;
R.same = JSON.stringify(a) === JSON.stringify(b); R.n = a.length;
""", extra=READER)
        assert out["same"] is True and out["n"] == 5

    def test_a_merge_keeps_one_entry_per_address_newest_first_and_counts_what_it_skipped(self, tmp_path):
        out = run(tmp_path, """
const existing = [{ id: 'e1', url: 'https://example.com/a', title: 'mine', at: 50 }];
const incoming = [{ url: 'https://news.example/story', title: 'S\\u0000x', at: 500 }, { url: 'chrome://settings/', title: 'x', at: 400 }, { url: 'file:///x', title: 'x', at: 300 },
  { url: 'https://example.com/a', title: 'dup', at: 100 }, { url: 'https://old.example/', title: 'Old', at: 0 }];
let n = 0; const r = imp.mergeHistory(existing, incoming, () => 'n' + (++n), () => 1000);
R.counts = r.counts; R.list = r.list.map((e) => [e.url, e.title, e.at]);
""")
        assert out["counts"] == {"found": 5, "added": 2, "existing": 1, "skipped": 2, "dropped": 0}
        assert out["list"] == [["https://old.example/", "Old", 1000], ["https://news.example/story", "S x", 500], ["https://example.com/a", "mine", 50]]  # an unknown time is "now", control characters are cleaned

    def test_the_history_cap_cuts_the_oldest_and_says_how_many(self, tmp_path):
        out = run(tmp_path, """
const existing = Array.from({ length: 4000 }, (_, i) => ({ id: 'e' + i, url: 'https://e.example/' + i, title: '', at: 100000 - i }));
const incoming = Array.from({ length: 3000 }, (_, i) => ({ url: 'https://i.example/' + i, title: '', at: 200000 - i }));
let n = 0; const r = imp.mergeHistory(existing, incoming, () => 'n' + (++n), () => 1);
R.len = r.list.length; R.counts = r.counts; R.first = r.list[0].url;
""")
        assert out["len"] == 5000 and out["counts"]["added"] == 3000 and out["counts"]["dropped"] == 2000 and out["first"] == "https://i.example/0"


class TestOnlyTwoFilesAreEverOpened:
    def test_bookmarks_and_history_only_and_never_logins_cookies_web_data_or_preferences(self, tmp_path):
        prof = make_chrome_profile(tmp_path / "c")
        out = run(tmp_path, f"""
const r = imp.readChromeProfile({{ fs: spyFs, path, os, readHistory: reader }}, {str(prof)!r});
R.ok = r.ok; R.paths = seen.filter((p) => !p.includes('dm-import-') && p !== os.tmpdir());
""", extra=READER)
        assert out["ok"] is True
        names = {Path(p).name for p in out["paths"]}
        assert names <= {"Default", "Bookmarks", "History"}, names
        for banned in ("Login Data", "Cookies", "Web Data", "Preferences", "Login Data-journal", "History-journal"):
            assert banned not in names

    def test_a_symbolic_link_in_place_of_a_file_is_refused_not_followed(self, tmp_path):
        prof = make_chrome_profile(tmp_path / "c")
        (prof / "Bookmarks").unlink()
        (prof / "Bookmarks").symlink_to(prof / "Login Data")
        (prof / "History").unlink()
        (prof / "History").symlink_to(prof / "Cookies")
        out = run(tmp_path, f"""
const r = imp.readChromeProfile({{ fs: spyFs, path, os, readHistory: reader }}, {str(prof)!r});
R.r = r; R.opened = seen.filter((p) => p.endsWith('Login Data') || p.endsWith('Cookies'));
""", extra=READER)
        assert out["r"]["ok"] is False and out["r"]["bookmarks"] is None and out["r"]["history"] is None
        assert any("not a regular file" in n for n in out["r"]["notes"])
        assert out["opened"] == []

    def test_a_folder_with_neither_file_or_a_plain_file_is_a_clear_error(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        f = tmp_path / "afile"
        f.write_text("x")
        out = run(tmp_path, f"""
R.empty = imp.readChromeProfile({{ fs, path, os, readHistory: reader }}, {str(empty)!r});
R.file = imp.readChromeProfile({{ fs, path, os, readHistory: reader }}, {str(f)!r});
R.missing = imp.readChromeProfile({{ fs, path, os, readHistory: reader }}, {str(tmp_path / 'nope')!r});
""", extra=READER)
        assert out["empty"]["ok"] is False and "No Bookmarks" in out["empty"]["error"]
        assert "not a file" in out["file"]["error"] and "cannot be read" in out["missing"]["error"]

    def test_a_history_that_cannot_be_read_says_to_quit_chrome_and_does_not_lose_the_bookmarks(self, tmp_path):
        prof = make_chrome_profile(tmp_path / "c", history=False)
        (prof / "History").write_bytes(b"this is not a sqlite database at all")
        out = run(tmp_path, f"""
const r = imp.readChromeProfile({{ fs, path, os, readHistory: reader }}, {str(prof)!r});
R.ok = r.ok; R.bm = r.bookmarks && r.bookmarks.length; R.hist = r.history; R.notes = r.notes;
""", extra=READER)
        assert out["ok"] is True and out["bm"] == 5 and out["hist"] is None
        assert "quit it and try again" in out["notes"][0]


class TestThePasswordCsv:
    CSV = (
        "name,url,username,password,note\r\n"
        'shop.example,https://shop.example/login,alice@example.test,"Pw,with""quotes",\r\n'
        'multi,https://multi.example/,bob,"line1\nline2","a note, with comma"\r\n'
        "plain,http://insecure.example/,carol,PlainPw1,\r\n"
        "dev,http://localhost:3000/,dave,DevPw1,\r\n"
        "android,android://abc@com.example.app/,erin,AndroidPw,\r\n"
        "empty,https://empty.example/,frank,,\r\n"
        "nosite,,gina,NoSitePw,\r\n"
    )

    def test_quotes_commas_and_line_breaks_inside_fields_are_kept(self, tmp_path):
        out = run(tmp_path, f"R.p = imp.parseCsv({self.CSV!r});".replace("'", "'") if False else f"R.p = imp.parseCsv({json.dumps(self.CSV)});")
        rows = out["p"]["rows"]
        assert rows[0] == ["name", "url", "username", "password", "note"]
        assert rows[1][3] == 'Pw,with"quotes' and rows[2][3] == "line1\nline2" and rows[2][4] == "a note, with comma"
        assert len(rows) == 8 and all(len(r) == 5 for r in rows)

    def test_only_secure_web_logins_and_this_mac_are_importable_and_the_rest_are_counted(self, tmp_path):
        out = run(tmp_path, f"R.p = imp.passwordRowsFromCsv({json.dumps(self.CSV)});")
        p = out["p"]
        assert p["ok"] is True
        assert [(r["origin"], r["username"]) for r in p["rows"]] == [
            ("https://shop.example", "alice@example.test"), ("https://multi.example", "bob"), ("http://localhost:3000", "dave")]
        assert p["counts"] == {"rows": 7, "usable": 3, "notWeb": 2, "notSecure": 1, "noPassword": 1, "tooLong": 0, "truncated": False}

    def test_a_bom_and_other_browsers_column_order_are_accepted(self, tmp_path):
        csv = "﻿url,username,password\nhttps://a.example/,u,p1\n"
        out = run(tmp_path, f"R.p = imp.passwordRowsFromCsv({json.dumps(csv)});")
        assert out["p"]["rows"] == [{"origin": "https://a.example", "username": "u", "password": "p1"}]

    @pytest.mark.parametrize("text", ["", "name,url\nx,https://a.example/\n", "just one line", "a,b,c\n1,2,3\n"])
    def test_a_file_that_is_not_a_password_export_is_refused_with_a_reason(self, tmp_path, text):
        out = run(tmp_path, f"R.p = imp.passwordRowsFromCsv({json.dumps(text)});")
        assert out["p"]["ok"] is False and out["p"]["error"] and out["p"]["rows"] == []

    def test_a_giant_csv_is_cut_at_its_row_limit(self, tmp_path):
        out = run(tmp_path, """
const lines = ['url,username,password']; for (let i = 0; i < 12000; i += 1) lines.push('https://s' + i + '.example/,u,p' + i);
const r = imp.passwordRowsFromCsv(lines.join('\\n'));
R.n = r.rows.length; R.counts = r.counts;
""")
        assert out["n"] == 10000 and out["counts"]["truncated"] is True

    def test_a_password_that_is_too_long_is_skipped_not_cut(self, tmp_path):
        out = run(tmp_path, """
const r = imp.passwordRowsFromCsv('url,username,password\\nhttps://a.example/,u,' + 'x'.repeat(2000) + '\\n');
R.n = r.rows.length; R.c = r.counts.tooLong;
""")
        assert out["n"] == 0 and out["c"] == 1


class TestImportIntoTheVault:
    VAULT = """
const pw = require(%r);
let seq = 0;
const cipher = (available = true) => ({ available: () => available, encrypt: (t) => 'ENC:' + Buffer.from(String(t)).toString('base64'), decrypt: (b) => Buffer.from(String(b).slice(4), 'base64').toString('utf8') });
let data = { entries: [], never: [] };
const store = { get: () => data, set: (v) => { data = v; } };
""" % str(ELECTRON / "passwords.js")

    def test_counts_added_updated_and_same_and_never_a_value(self, tmp_path):
        out = run(tmp_path, """
const v = pw.createVault({ store, crypto: cipher(), newId: () => 'i' + (++seq) });
v.save({ origin: 'https://a.example', username: 'u', password: 'old' });
v.save({ origin: 'https://same.example', username: 'u', password: 'same' });
v.addNever('https://never.example');
const rows = [
  { origin: 'https://a.example', username: 'u', password: 'NEW-SECRET' },
  { origin: 'https://same.example', username: 'u', password: 'same' },
  { origin: 'https://fresh.example', username: 'u2', password: 'FRESH-SECRET' },
  { origin: 'https://never.example', username: 'u', password: 'x' },
];
R.res = imp.importPasswords(v, rows);
R.raw = JSON.stringify(data);
""", extra=self.VAULT)
        assert out["res"] == {"added": 1, "updated": 1, "same": 1, "refused": 0, "never": 1, "full": 0, "unavailable": False}
        assert "NEW-SECRET" not in out["raw"] and "FRESH-SECRET" not in out["raw"]  # ciphertext on disk, never plaintext
        assert "NEW-SECRET" not in json.dumps(out["res"])

    def test_without_encryption_the_first_refusal_stops_everything_and_nothing_is_stored(self, tmp_path):
        out = run(tmp_path, """
const v = pw.createVault({ store, crypto: cipher(false), newId: () => 'i' + (++seq) });
R.res = imp.importPasswords(v, [{ origin: 'https://a.example', username: 'u', password: 'p' }, { origin: 'https://b.example', username: 'u', password: 'p' }]);
R.raw = JSON.stringify(data);
""", extra=self.VAULT)
        assert out["res"]["unavailable"] is True and out["res"]["added"] == 0
        assert json.loads(out["raw"]) == {"entries": [], "never": []}


def test_importers_js_never_names_the_chrome_secret_stores_or_the_keychain_and_has_no_network():
    text = (ELECTRON / "importers.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    for banned in ("Login Data", "Cookies", "Web Data", "Safe Storage", "security find-generic-password", "keychain", "safeStorage", 'require("electron")', "http.request", "fetch(", "console.log"):
        assert banned not in code, banned
    assert "\u2014" not in text
    assert '"Bookmarks"' in code and '"History"' in code
