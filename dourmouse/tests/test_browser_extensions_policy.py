"""Phase B3: the rules in electron/extensions.js (what a manifest says in words an owner can
judge, which folders may be copied, how the approved copy is fingerprinted, what the stored list
looks like after each change), exercised under plain node. The wiring (the native dialogs, the
session, the loading) is in test_browser_extensions_shell.py."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ELECTRON = Path(__file__).resolve().parents[2] / "electron"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(tmp_path, body):
    script = tmp_path / "e.js"
    script.write_text(
        f"const e = require({str(ELECTRON / 'extensions.js')!r});\nconst fs = require('fs');\nconst path = require('path');\nconst os = require('os');\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestWhatAManifestSaysInWords:
    def test_a_small_extension_is_normal_and_names_what_it_reaches(self, tmp_path):
        out = run(tmp_path, """
R.i = e.inspectManifest({ manifest_version: 3, name: 'Page Tagger', version: '1.2', permissions: ['storage', 'activeTab'],
  content_scripts: [{ matches: ['https://example.com/*', 'http://127.0.0.1/*'], js: ['a.js'] }] });
""")
        i = out["i"]
        assert i["ok"] is True and i["name"] == "Page Tagger" and i["version"] == "1.2" and i["risk"] == "normal"
        assert i["allSites"] is False and i["manifestVersion"] == 3
        assert [p["key"] for p in i["permissions"]] == ["storage", "activeTab"]
        assert any("example.com" in line and "127.0.0.1" in line for line in i["lines"])

    def test_access_to_every_site_or_a_powerful_permission_is_high_and_says_so(self, tmp_path):
        out = run(tmp_path, """
R.all = e.inspectManifest({ manifest_version: 3, name: 'A', version: '1', host_permissions: ['<all_urls>'] });
R.star = e.inspectManifest({ manifest_version: 3, name: 'A', version: '1', content_scripts: [{ matches: ['*://*/*'] }] });
R.https = e.inspectManifest({ manifest_version: 2, name: 'A', version: '1', permissions: ['https://*/*', 'cookies'] });
R.dbg = e.inspectManifest({ manifest_version: 3, name: 'A', version: '1', permissions: ['debugger'] });
R.native = e.inspectManifest({ manifest_version: 3, name: 'A', version: '1', permissions: ['nativeMessaging', 'tabs'] });
R.one = e.inspectManifest({ manifest_version: 3, name: 'A', version: '1', host_permissions: ['https://*.example.com/*'] });
""")
        for k in ("all", "star", "https"):
            assert out[k]["allSites"] is True and out[k]["risk"] == "high", k
            assert "every website" in out[k]["lines"][0]
        assert out["dbg"]["risk"] == "high" and out["native"]["risk"] == "high"
        assert out["one"]["allSites"] is False and out["one"]["risk"] == "normal" and "example.com" in out["one"]["lines"][0]

    def test_a_permission_with_no_plain_words_is_shown_by_its_own_name_not_hidden(self, tmp_path):
        out = run(tmp_path, "R.i = e.inspectManifest({ manifest_version: 3, name: 'A', version: '1', permissions: ['someFuturePermission'] });")
        assert out["i"]["permissions"] == [{"key": "someFuturePermission", "words": 'use the "someFuturePermission" permission'}]

    def test_things_that_are_not_an_extension_are_refused_with_a_reason(self, tmp_path):
        out = run(tmp_path, """
R.cases = [null, 'x', [], {}, { manifest_version: 1, name: 'A', version: '1' }, { manifest_version: 3, version: '1' },
  { manifest_version: 3, name: '   ', version: '1' }, { manifest_version: 3, name: 'A' }].map((m) => e.inspectManifest(m));
""")
        assert all(c["ok"] is False and c["error"] for c in out["cases"])

    def test_a_name_cannot_carry_control_or_direction_override_characters(self, tmp_path):
        out = run(tmp_path, "R.i = e.inspectManifest({ manifest_version: 3, name: 'Safe\\u202eexe.txt\\u0000', version: '1\\n2' });")
        assert out["i"]["name"].count("‮") == 0 and "\u0000" not in out["i"]["name"] and "\n" not in out["i"]["version"]

    def test_a_localised_name_is_looked_up_in_its_default_locale(self, tmp_path):
        out = run(tmp_path, """
const read = (loc) => (loc === 'en' ? JSON.stringify({ appName: { message: 'Real Name' } }) : null);
R.ok = e.inspectManifest({ manifest_version: 3, name: '__MSG_appName__', default_locale: 'en', version: '1' }, read).name;
R.noLocale = e.inspectManifest({ manifest_version: 3, name: '__MSG_appName__', version: '1' }, read).name;
R.traversal = e.inspectManifest({ manifest_version: 3, name: '__MSG_appName__', default_locale: '../../x', version: '1' }, (l) => { R.asked = l; return null; }).name;
""")
        assert out["ok"] == "Real Name" and out["noLocale"] == "__MSG_appName__" and out["traversal"] == "__MSG_appName__"
        assert "asked" not in out  # a locale with a path in it is never even looked up

    def test_the_confirmation_names_the_extension_and_every_permission(self, tmp_path):
        out = run(tmp_path, """
const i = e.inspectManifest({ manifest_version: 3, name: 'Page Tagger', version: '1.2', permissions: ['storage', 'cookies'], host_permissions: ['<all_urls>'] });
R.t = e.confirmationText(i, 'my-extension');
""")
        t = out["t"]
        assert 'Add "Page Tagger" (version 1.2)' in t["message"]
        for needle in ("every website", "cookies", "my-extension", "only that copy runs"):
            assert needle in t["detail"]


class TestTheApprovedCopy:
    def test_a_folder_is_copied_and_the_fingerprint_changes_when_any_file_changes(self, tmp_path):
        out = run(tmp_path, f"""
const src = path.join({str(tmp_path)!r}, 'src'); fs.mkdirSync(path.join(src, 'sub'), {{ recursive: true }});
fs.writeFileSync(path.join(src, 'manifest.json'), '{{}}'); fs.writeFileSync(path.join(src, 'sub', 'a.js'), 'one');
fs.mkdirSync(path.join(src, '.git')); fs.writeFileSync(path.join(src, '.git', 'config'), 'x'); fs.writeFileSync(path.join(src, '.DS_Store'), 'x');
const dest = path.join({str(tmp_path)!r}, 'dest');
R.copy = e.copyTree({{ fs, path }}, src, dest);
R.files = fs.readdirSync(dest).sort().concat(fs.readdirSync(path.join(dest, 'sub')));
R.modes = [fs.statSync(path.join(dest, 'manifest.json')).mode & 0o777, fs.statSync(dest).mode & 0o777];
const h1 = e.hashTree({{ fs, path }}, dest);
R.same = h1 === e.hashTree({{ fs, path }}, dest);
fs.writeFileSync(path.join(dest, 'sub', 'a.js'), 'two');
R.edited = h1 !== e.hashTree({{ fs, path }}, dest);
fs.writeFileSync(path.join(dest, 'sub', 'a.js'), 'one'); fs.writeFileSync(path.join(dest, 'extra.js'), 'x');
R.added = h1 !== e.hashTree({{ fs, path }}, dest);
""")
        assert out["copy"]["ok"] is True and out["copy"]["files"] == 2
        assert out["files"] == ["manifest.json", "sub", "a.js"]  # no .git, no .DS_Store
        assert out["modes"] == [0o600, 0o700]
        assert out["same"] is True and out["edited"] is True and out["added"] is True

    def test_a_symbolic_link_stops_the_copy_so_nothing_outside_the_folder_is_reached(self, tmp_path):
        secret = tmp_path / "outside.txt"
        secret.write_text("TOP-SECRET")
        out = run(tmp_path, f"""
const src = path.join({str(tmp_path)!r}, 'src'); fs.mkdirSync(src);
fs.writeFileSync(path.join(src, 'manifest.json'), '{{}}');
fs.symlinkSync({str(secret)!r}, path.join(src, 'link.js'));
const dest = path.join({str(tmp_path)!r}, 'dest');
R.copy = e.copyTree({{ fs, path }}, src, dest);
R.leaked = fs.existsSync(path.join(dest, 'link.js'));
""")
        assert out["copy"]["ok"] is False and "symbolic link" in out["copy"]["error"] and out["leaked"] is False

    def test_too_many_files_or_too_much_depth_stops_the_copy(self, tmp_path):
        out = run(tmp_path, f"""
const root = {str(tmp_path)!r};
const many = path.join(root, 'many'); fs.mkdirSync(many);
for (let i = 0; i < 3005; i += 1) fs.writeFileSync(path.join(many, 'f' + i), '');
R.many = e.copyTree({{ fs, path }}, many, path.join(root, 'd1'));
let deep = path.join(root, 'deep'); fs.mkdirSync(deep);
let cur = deep; for (let i = 0; i < 14; i += 1) {{ cur = path.join(cur, 'd'); fs.mkdirSync(cur); }}
R.deep = e.copyTree({{ fs, path }}, deep, path.join(root, 'd2'));
""")
        assert out["many"]["ok"] is False and "3000" in out["many"]["error"]
        assert out["deep"]["ok"] is False and "deeply" in out["deep"]["error"]

    def test_the_hash_refuses_a_link_that_appeared_in_the_copy(self, tmp_path):
        out = run(tmp_path, f"""
const d = path.join({str(tmp_path)!r}, 'd'); fs.mkdirSync(d); fs.writeFileSync(path.join(d, 'manifest.json'), '{{}}');
fs.symlinkSync('/etc/hosts', path.join(d, 'x'));
try {{ e.hashTree({{ fs, path }}, d); R.threw = false; }} catch (err) {{ R.threw = String(err.message); }}
""")
        assert "symbolic link" in out["threw"]


class TestTheStoredList:
    def test_a_registry_from_an_edited_file_keeps_only_what_this_code_could_have_written(self, tmp_path):
        out = run(tmp_path, """
const good = { id: '0123456789abcdef', name: 'Ok', version: '1', enabled: true, addedAt: 5, tree: 'a'.repeat(64), risk: 'high', summary: ['x', 5, 'y'] };
R.r = e.sanitizeRegistry({ entries: [good, { ...good }, { ...good, id: 'short' }, { ...good, id: 'ffffffffffffffff', tree: 'zz' }, null, 'x',
  { ...good, id: 'aaaaaaaaaaaaaaaa', path: '/etc/passwd', enabled: 'yes' }] });
R.junk = [e.sanitizeRegistry(null), e.sanitizeRegistry('x'), e.sanitizeRegistry({ entries: 'x' })];
R.cap = e.sanitizeRegistry({ entries: Array.from({ length: 40 }, (_, i) => ({ ...good, id: i.toString(16).padStart(16, '0') })) }).entries.length;
""")
        ids = [x["id"] for x in out["r"]["entries"]]
        assert ids == ["0123456789abcdef", "aaaaaaaaaaaaaaaa"]  # the duplicate, the short id, the bad hash and the junk are gone
        second = out["r"]["entries"][1]
        assert second["enabled"] is False and "path" not in second  # only known fields survive; "yes" is not true
        assert out["r"]["entries"][0]["summary"] == ["x", "y"]
        assert all(j == {"entries": []} for j in out["junk"]) and out["cap"] == 20

    def test_add_enable_disable_and_remove(self, tmp_path):
        out = run(tmp_path, """
let reg = { entries: [] };
const entry = (id) => ({ id, name: 'N', version: '1', enabled: true, addedAt: 1, tree: 'a'.repeat(64), risk: 'normal', summary: [] });
reg = e.addEntry(reg, entry('0000000000000001')).registry;
R.off = e.setEnabled(reg, '0000000000000001', false).registry.entries[0].enabled;
R.unknown = [e.setEnabled(reg, 'nope', true).ok, e.removeEntry(reg, 'nope').ok];
R.removed = e.removeEntry(reg, '0000000000000001').registry.entries.length;
for (let i = 2; i < 30; i += 1) { const r = e.addEntry(reg, entry(String(i).padStart(16, '0'))); reg = r.registry; R.full = r.ok ? R.full : r.error; }
R.count = reg.entries.length; R.counts = e.countEntries(reg);
""")
        assert out["off"] is False and out["unknown"] == [False, False] and out["removed"] == 0
        assert out["count"] == 20 and "At most 20" in out["full"]
        assert out["counts"] == {"total": 20, "enabled": 20}

    def test_the_public_view_never_carries_a_path_or_the_fingerprint(self, tmp_path):
        out = run(tmp_path, """
const entry = { id: '0123456789abcdef', name: 'N', version: '1', enabled: true, addedAt: 1, tree: 'c'.repeat(64), risk: 'normal', summary: ['s'], path: '/Users/x/ext', dir: '/x' };
R.v = e.publicView(entry, { loaded: true, error: '', electronId: 'ELECTRON-ID-XYZ' });
R.e = e.publicView(entry, { error: 'boom\\u0000'.repeat(100) });
""")
        assert set(out["v"]) == {"id", "name", "version", "enabled", "addedAt", "risk", "summary", "loaded", "error"}
        assert "/Users" not in json.dumps(out) and "c" * 64 not in json.dumps(out) and "ELECTRON-ID-XYZ" not in json.dumps(out)
        assert len(out["e"]["error"]) <= 300


def test_the_honest_support_note_does_not_claim_the_web_store():
    text = (ELECTRON / "extensions.js").read_text(encoding="utf-8")
    note = re.search(r"const EXTENSION_SUPPORT_NOTE =(.*?)\";\n", text, re.S).group(1)
    assert "only part of Chrome" in note and "Chrome Web Store is not available" in note and ".crx" in note
    assert "full" not in note.lower().replace("full control", "")
    assert "\u2014" not in text
    for banned in ('require("electron")', "fetch(", "http.request", "child_process"):
        assert banned not in text
