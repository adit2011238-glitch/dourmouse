"""Phase B2: the rules in electron/passwords.js (the saved-password vault and the address
book), exercised under plain node with a fake cipher. The wiring (safeStorage, the IPC
handlers, the bars) is in test_browser_passwords_shell.py; what the real Keychain does is
checked live and recorded in the B2 finding."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ELECTRON = Path(__file__).resolve().parents[2] / "electron"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = r"""
const p = require(%r);
const R = {};
// A reversible stand-in for safeStorage: not secure, but opaque enough that a plaintext
// leak into the stored file is visible to the test.
let seq = 0;
const makeCipher = (available = true) => ({
  available: () => available,
  encrypt: (t) => 'ENC1:' + Buffer.from(String(t), 'utf8').toString('base64').split('').reverse().join(''),
  decrypt: (b) => {
    if (!String(b).startsWith('ENC1:')) throw new Error('bad blob');
    return Buffer.from(String(b).slice(5).split('').reverse().join(''), 'base64').toString('utf8');
  },
});
const makeStore = (init) => { let data = init; return { get: () => data, set: (v) => { data = v; }, raw: () => JSON.stringify(data) }; };
const newId = () => 'id' + (++seq);
const O = 'https://login.example';
""" % str(ELECTRON / "passwords.js")


def run(tmp_path, body):
    script = tmp_path / "v.js"
    script.write_text(PRELUDE + body + "\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_saved_password_is_ciphertext_at_rest_never_plaintext(tmp_path):
    out = run(tmp_path, """
const store = makeStore({ entries: [], never: [] });
const v = p.createVault({ store, crypto: makeCipher(), newId });
R.saved = v.save({ origin: O, username: 'alice@example.test', password: 'S3cret-pass-Zq7' });
const raw = store.raw();
R.leaks = ['S3cret-pass-Zq7', 'alice@example.test', 'S3cret'].map((t) => raw.includes(t));
R.siteIsVisible = raw.includes('login.example');
R.shape = Object.keys(store.get().entries[0]).sort();
""")
    assert out["saved"]["ok"] is True and out["saved"]["status"] == "added"
    assert out["leaks"] == [False, False, False]  # neither the password nor the username is in the file
    assert out["siteIsVisible"] is True  # the site must be, to be matched
    assert out["shape"] == ["blob", "created", "id", "lastUsed", "origin", "updated"]


def test_with_no_encryption_nothing_is_saved_there_is_no_plaintext_fallback(tmp_path):
    out = run(tmp_path, """
const store = makeStore({ entries: [], never: [] });
const v = p.createVault({ store, crypto: makeCipher(false), newId });
R.verdict = v.classify({ origin: O, username: 'a', password: 'pw' });
R.save = v.save({ origin: O, username: 'a', password: 'pw' });
R.raw = store.raw();
const book = p.createAddressBook({ store: makeStore({ profiles: [] }), crypto: makeCipher(false), newId });
R.address = book.save({ name: 'A Person' });
""")
    assert out["verdict"] == {"status": "unavailable"}
    assert out["save"]["ok"] is False and "encryption" in out["save"]["error"]
    assert json.loads(out["raw"]) == {"entries": [], "never": []}
    assert out["address"]["ok"] is False


def test_the_list_never_contains_a_password_and_only_the_one_call_returns_it(tmp_path):
    out = run(tmp_path, """
const v = p.createVault({ store: makeStore({ entries: [], never: [] }), crypto: makeCipher(), newId });
const a = v.save({ origin: O, username: 'alice', password: 'PW-ALICE-1' });
v.save({ origin: 'https://other.example', username: 'bob', password: 'PW-BOB-2' });
const everythingTheConsoleListsCanSee = JSON.stringify([v.list(), v.entriesFor(O), v.never(), v.counts()]);
R.listLeaks = ['PW-ALICE-1', 'PW-BOB-2'].map((t) => everythingTheConsoleListsCanSee.includes(t));
R.list = v.list().map((e) => [e.origin, e.username, e.readable]);
R.forOrigin = v.entriesFor(O).map((e) => Object.keys(e).sort().join(',') + ':' + e.username);
R.cred = v.credentials(a.id);
R.noCred = v.credentials('missing');
""")
    assert out["listLeaks"] == [False, False]
    assert out["list"] == [["https://login.example", "alice", True], ["https://other.example", "bob", True]]
    assert out["forOrigin"] == ["id,username:alice"]
    assert out["cred"] == {"origin": "https://login.example", "username": "alice", "password": "PW-ALICE-1"}
    assert out["noCred"] is None


def test_a_login_belongs_to_one_exact_origin(tmp_path):
    out = run(tmp_path, """
const v = p.createVault({ store: makeStore({ entries: [], never: [] }), crypto: makeCipher(), newId });
v.save({ origin: O, username: 'alice', password: 'pw1' });
R.match = v.entriesFor(O).length;
R.notOtherScheme = v.entriesFor('http://login.example').length;
R.notSubdomain = v.entriesFor('https://sub.login.example').length;
R.notParent = v.entriesFor('https://example').length;
R.notOtherPort = v.entriesFor('https://login.example:8443').length;
R.notLookalike = v.entriesFor('https://login.example.evil.test').length;
R.notUserinfo = v.entriesFor('https://login.example@evil.test').length;
R.junk = [v.entriesFor(''), v.entriesFor('about:blank'), v.entriesFor(null), v.entriesFor('javascript:1')].map((x) => x.length);
""")
    assert out["match"] == 1
    assert [out[k] for k in ("notOtherScheme", "notSubdomain", "notParent", "notOtherPort", "notLookalike", "notUserinfo")] == [0] * 6
    assert out["junk"] == [0, 0, 0, 0]


def test_only_https_or_this_machine_is_ever_saved_or_filled(tmp_path):
    out = run(tmp_path, """
const v = p.createVault({ store: makeStore({ entries: [], never: [] }), crypto: makeCipher(), newId });
R.can = ['https://a.example', 'https://a.example:8443', 'http://localhost:3000', 'http://127.0.0.1:18851', 'http://[::1]:8080', 'http://app.localhost'].map(p.savableOrigin);
R.cannot = ['http://a.example', 'http://192.168.1.5', 'ftp://a.example', 'file:///x', 'about:blank', '', 'https://a.example/path', 'https://A.example'].map(p.savableOrigin);
R.plainHttp = v.save({ origin: 'http://a.example', username: 'u', password: 'p' });
R.local = v.save({ origin: 'http://127.0.0.1:18851', username: 'u', password: 'p' }).ok;
""")
    assert out["can"] == [True] * 6
    assert out["cannot"] == [False] * 8
    assert out["plainHttp"]["ok"] is False
    assert out["local"] is True


def test_a_changed_password_is_an_update_the_same_one_is_nothing_and_a_new_user_is_new(tmp_path):
    out = run(tmp_path, """
const v = p.createVault({ store: makeStore({ entries: [], never: [] }), crypto: makeCipher(), newId });
const first = v.save({ origin: O, username: 'Alice', password: 'one' });
R.same = v.classify({ origin: O, username: 'alice', password: 'one' }).status;      // user names compare without case
R.update = v.classify({ origin: O, username: 'ALICE', password: 'two' });
R.newUser = v.classify({ origin: O, username: 'bob', password: 'one' }).status;
R.otherSite = v.classify({ origin: 'https://other.example', username: 'Alice', password: 'one' }).status;
const up = v.save({ origin: O, username: 'Alice', password: 'two' });
R.up = [up.status, up.id === first.id, v.list().length, v.credentials(first.id).password];
R.sameAgain = v.save({ origin: O, username: 'Alice', password: 'two' }).status;
R.noUser = v.classify({ origin: O, username: '', password: 'x' }).status;
""")
    assert out["same"] == "same" and out["update"]["status"] == "update" and out["newUser"] == "new" and out["otherSite"] == "new"
    assert out["up"] == ["updated", True, 1, "two"]
    assert out["sameAgain"] == "same" and out["noUser"] == "new"


def test_never_for_this_site_stops_the_offer_and_can_be_undone(tmp_path):
    out = run(tmp_path, """
const v = p.createVault({ store: makeStore({ entries: [], never: [] }), crypto: makeCipher(), newId });
R.before = v.classify({ origin: O, username: 'a', password: 'p' }).status;
R.add = [v.addNever(O), v.addNever(O), v.addNever('http://a.example'), v.addNever('about:blank')];
R.after = v.classify({ origin: O, username: 'a', password: 'p' }).status;
R.save = v.save({ origin: O, username: 'a', password: 'p' }).ok;
R.list = v.never(); R.isNever = [v.isNever(O), v.isNever('https://other.example')];
R.remove = [v.removeNever(O), v.removeNever(O)];
R.again = v.classify({ origin: O, username: 'a', password: 'p' }).status;
R.counts = v.counts();
""")
    assert out["before"] == "new" and out["add"] == [True, True, False, False]
    assert out["after"] == "never" and out["save"] is False
    assert out["list"] == ["https://login.example"] and out["isNever"] == [True, False]
    assert out["remove"] == [True, False] and out["again"] == "new" and out["counts"] == {"passwords": 0, "neverSaved": 0}


def test_empty_oversized_and_odd_passwords_are_refused(tmp_path):
    out = run(tmp_path, """
const v = p.createVault({ store: makeStore({ entries: [], never: [] }), crypto: makeCipher(), newId });
R.refused = ['', null, undefined, 'x'.repeat(p.MAX_PASSWORD + 1)].map((pw) => v.classify({ origin: O, username: 'a', password: pw }).status);
R.longUser = v.list().length;
const ok = v.save({ origin: O, username: 'u'.repeat(1000) + '\\u0000\\n', password: 'p' });
R.storedUserLength = v.list()[0].username.length;
R.max = p.MAX_USERNAME;
""")
    assert out["refused"] == ["refused"] * 4
    assert out["storedUserLength"] == out["max"]


def test_delete_removes_one_entry_and_a_blob_that_cannot_be_read_stays_listed_but_unusable(tmp_path):
    out = run(tmp_path, """
const store = makeStore({ entries: [], never: [] });
const v = p.createVault({ store, crypto: makeCipher(), newId });
const a = v.save({ origin: O, username: 'alice', password: 'pw' });
const b = v.save({ origin: O, username: 'bob', password: 'pw2' });
store.get().entries[1].blob = 'garbage';   // the Keychain key changed, or the file was edited
R.list = v.list().map((e) => [e.username, e.readable]);
R.credBroken = v.credentials(b.id);
R.fillOnlyReadable = v.entriesFor(O).map((e) => e.username);
R.classifyDoesNotCrash = v.classify({ origin: O, username: 'bob', password: 'pw2' }).status;
R.delete = [v.remove(b.id), v.remove(b.id), v.list().length];
""")
    assert out["list"] == [["", False], ["alice", True]]  # sorted by site, then user name
    assert out["credBroken"] is None and out["fillOnlyReadable"] == ["alice"]
    assert out["classifyDoesNotCrash"] == "new"
    assert out["delete"] == [True, False, 1]


def test_touching_a_login_and_the_version_counter(tmp_path):
    out = run(tmp_path, """
let t = 100;
const v = p.createVault({ store: makeStore({ entries: [], never: [] }), crypto: makeCipher(), newId, now: () => t });
const a = v.save({ origin: O, username: 'alice', password: 'pw' });
const v1 = v.version();
t = 500; v.touch(a.id);
R.used = v.list()[0].lastUsed; R.created = v.list()[0].created; R.bumped = v.version() > v1;
""")
    assert out["used"] == 500 and out["created"] == 100 and out["bumped"] is True


def test_the_vault_survives_a_store_with_missing_arrays(tmp_path):
    out = run(tmp_path, """
const v = p.createVault({ store: makeStore({}), crypto: makeCipher(), newId });
R.list = v.list(); R.never = v.never();
R.save = v.save({ origin: O, username: 'a', password: 'p' }).ok;
""")
    assert out["list"] == [] and out["never"] == [] and out["save"] is True


def test_the_address_book_is_encrypted_bounded_and_cleaned(tmp_path):
    out = run(tmp_path, """
const store = makeStore({ profiles: [] });
const book = p.createAddressBook({ store, crypto: makeCipher(), newId });
const r = book.save({ label: 'Home', name: 'Ada Lovelace', phone: '555-0100', line1: '12 Analytical Way', city: 'London', extra: 'ignored' });
const raw = store.raw();
R.leaks = ['Ada', '555-0100', 'Analytical', 'London', 'Home'].map((t) => raw.includes(t));
R.got = book.get(r.id);
R.noExtra = 'extra' in book.get(r.id);
R.empty = book.save({ label: 'Only a label' });
R.update = book.save({ name: 'Ada King' }, r.id).ok;
R.after = book.get(r.id).name;
R.missing = book.save({ name: 'x' }, 'nope').ok;
const ids = [r.id];
for (let i = 0; i < 10; i += 1) { const s = book.save({ name: 'P' + i }); if (s.ok) ids.push(s.id); }
R.count = book.count(); R.max = p.MAX_PROFILES;
R.remove = [book.remove(ids[0]), book.remove(ids[0])];
R.list = book.list().map((x) => x.label);
""")
    assert out["leaks"] == [False] * 5
    assert out["got"]["name"] == "Ada Lovelace" and out["noExtra"] is False
    assert out["empty"]["ok"] is False
    assert out["update"] is True and out["after"] == "Ada King" and out["missing"] is False
    assert out["count"] == out["max"] == 5
    assert out["remove"] == [True, False]
    assert out["list"][0] == "P0"  # an unlabeled profile takes its name as the label


def test_passwords_js_has_no_electron_import_no_logging_and_no_network():
    src = (ELECTRON / "passwords.js").read_text(encoding="utf-8")
    assert "require(" not in src
    for forbidden in ("console.", "log(", "fetch(", "http.request", "XMLHttpRequest", "WebSocket", "process.env", "writeFile", "readFile"):
        assert forbidden not in src, forbidden
    assert "—" not in src
