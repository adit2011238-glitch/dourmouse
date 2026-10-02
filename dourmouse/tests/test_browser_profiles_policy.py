"""Phase B3: the rules in electron/profiles.js (what a profile may be called, where its files and
its cookie jar live, what the list looks like after an add, a remove or a switch), exercised
under plain node. The wiring (the stores, the sessions, the switch) is in
test_browser_profiles_shell.py."""

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
    script = tmp_path / "p.js"
    script.write_text(
        f"const p = require({str(ELECTRON / 'profiles.js')!r});\nconst path = require('path');\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_name_is_a_safe_folder_and_partition_name_or_it_is_refused(tmp_path):
    out = run(tmp_path, """
const bad = ['', ' ', '../x', 'a/b', 'a\\\\b', 'a b', 'A.b', '.hidden', '-x', '_x', 'x'.repeat(25), 'é', 'a\\u0000b', 'a:b', '~', 'x%2e'];
R.bad = bad.map((n) => p.isValidName(n));
R.good = ['work', 'a', '0', 'client-1', 'my_profile', 'x'.repeat(24)].map((n) => p.isValidName(n));
R.clean = p.cleanName('  Work ');
R.cleanNull = p.cleanName(null);
""")
    assert not any(out["bad"]), out["bad"]
    assert all(out["good"])
    assert out["clean"] == "work" and out["cleanNull"] == ""


def test_the_default_profile_keeps_todays_paths_and_partition(tmp_path):
    out = run(tmp_path, """
R.part = [p.partitionFor('default'), p.partitionFor('work')];
R.dir = [p.dirFor(path, '/ud/browser', 'default'), p.dirFor(path, '/ud/browser', 'work')];
R.throwsPart = (() => { try { p.partitionFor('../x'); return false; } catch { return true; } })();
R.throwsDir = (() => { try { p.dirFor(path, '/ud/browser', '../../etc'); return false; } catch { return true; } })();
""")
    assert out["part"] == ["persist:dourmouse-browser", "persist:dourmouse-browser-work"]
    assert out["dir"] == ["/ud/browser", "/ud/browser/profiles/work"]
    assert out["throwsPart"] is True and out["throwsDir"] is True


def test_the_default_partition_string_is_the_same_one_main_js_uses_today():
    main = (ELECTRON / "main.js").read_text(encoding="utf-8")
    text = (ELECTRON / "profiles.js").read_text(encoding="utf-8")
    assert re.search(r'const PANE_PARTITION = "(persist:dourmouse-browser)"', main).group(1) == re.search(r'DEFAULT_PARTITION = "([^"]+)"', text).group(1)


def test_a_registry_from_an_edited_file_keeps_only_what_this_code_could_have_written(tmp_path):
    out = run(tmp_path, """
R.a = p.sanitizeRegistry(null);
R.b = p.sanitizeRegistry('x');
R.c = p.sanitizeRegistry({ active: 'ghost', names: ['work', 'work', '../x', 'default', 5, 'ok_1', 'A'] });
R.d = p.sanitizeRegistry({ active: 'work', names: ['work'] });
R.cap = p.sanitizeRegistry({ names: Array.from({ length: 30 }, (_, i) => 'p' + i) }).names.length;
""")
    assert out["a"] == {"active": "default", "names": []} and out["b"] == {"active": "default", "names": []}
    assert out["c"] == {"active": "default", "names": ["work", "ok_1"]}  # an unknown active falls back to default
    assert out["d"] == {"active": "work", "names": ["work"]}
    assert out["cap"] == 8


def test_add_remove_and_switch_follow_the_rules(tmp_path):
    out = run(tmp_path, """
let reg = p.sanitizeRegistry({});
const add = (n) => { const r = p.addProfile(reg, n); reg = r.registry; return r; };
R.a1 = add('Work'); R.a2 = add('work'); R.a3 = add('default'); R.a4 = add('../x'); R.a5 = add('');
for (let i = 0; i < 12; i += 1) add('p' + i);
R.count = reg.names.length;
R.full = add('overflow').error;
R.removeDefault = p.removeProfile(reg, 'default').error;
R.removeUnknown = p.removeProfile(reg, 'nope').error;
reg = p.setActive(reg, 'work').registry;
R.removeActive = p.removeProfile(reg, 'work').error;
R.switchUnknown = p.setActive(reg, 'ghost').error;
R.switchDefault = p.setActive(reg, 'default').registry.active;
R.removeOther = p.removeProfile(reg, 'p0').ok;
R.list = p.listProfiles({ active: 'work', names: ['work', 'b'] });
""")
    assert out["a1"]["ok"] is True and out["a1"]["name"] == "work"  # lower-cased
    assert out["a2"]["ok"] is False and "exists" in out["a2"]["error"]
    assert out["a3"]["ok"] is False and out["a4"]["ok"] is False and out["a5"]["ok"] is False
    assert out["count"] == 8 and "At most 8" in out["full"]
    assert "default" in out["removeDefault"] and "No such" in out["removeUnknown"]
    assert "Switch to another" in out["removeActive"] and "No such" in out["switchUnknown"]
    assert out["switchDefault"] == "default" and out["removeOther"] is True
    assert out["list"] == [
        {"name": "default", "active": False, "isDefault": True},
        {"name": "work", "active": True, "isDefault": False},
        {"name": "b", "active": False, "isDefault": False},
    ]


def test_profiles_js_has_no_electron_import_no_network_and_no_em_dash():
    text = (ELECTRON / "profiles.js").read_text(encoding="utf-8")
    assert 'require("electron")' not in text and "require('electron')" not in text
    for banned in ("fetch(", "http.request", "child_process", "console.log"):
        assert banned not in text
    assert "\u2014" not in text
