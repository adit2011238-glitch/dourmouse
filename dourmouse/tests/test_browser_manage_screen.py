"""Phase B3: the BROWSER screen's Extensions panel, Profiles and import panel, and the DRM line in
Site settings. The pure model (ui/.../manage-model.js) is run under node; the DOM code
(manage-ui.js, index.js) is pinned by the rules it must keep. What the DOM does with them was
verified live in the Electron app and is recorded in the B3 finding."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "browser"
_NODE = shutil.which("node")


def node(tmp_path, module, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "m.mjs"
    script.write_text(f"import * as m from {(_DIR / module).as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def source(name):
    return (_DIR / name).read_text(encoding="utf-8")


class TestTheModel:
    def test_extension_rows_keep_only_what_is_safe_to_draw(self, tmp_path):
        out = node(tmp_path, "manage-model.js", """
R.rows = m.extensionRows({ extensions: [
  { id: 'a1', name: 'N'.repeat(200), version: '1', enabled: true, loaded: true, risk: 'high', summary: ['x', 5, 'y'.repeat(900)], path: '/Users/x/ext', tree: 'T', error: '' },
  { id: 'a2', name: '', enabled: 'yes', risk: 'weird', error: 'boom' }, { name: 'no id' }, null, 'x' ] });
R.none = [m.extensionRows(null), m.extensionRows({}), m.extensionRows({ extensions: 'x' })];
R.states = [{ enabled: false }, { enabled: true, error: 'e' }, { enabled: true, loaded: true }, { enabled: true }].map((r) => m.extensionState({ error: '', loaded: false, ...r }));
""")
        a, b = out["rows"]
        assert len(a["name"]) == 80 and a["risk"] == "high" and a["summary"][0] == "x" and len(a["summary"]) == 2 and len(a["summary"][1]) == 300
        assert "path" not in a and "tree" not in a and "/Users" not in json.dumps(out)
        assert b["name"] == "(unnamed)" and b["enabled"] is False and b["risk"] == "normal" and b["error"] == "boom"
        assert len(out["rows"]) == 2 and out["none"] == [[], [], []]
        assert out["states"] == ["Off", "On, but not running", "On", "Loading"]

    def test_profile_list_and_name_checks_agree_with_the_shell(self, tmp_path):
        out = node(tmp_path, "manage-model.js", """
R.list = m.profileList({ active: 'work', max: 8, profiles: [{ name: 'default', active: false }, { name: 'work', active: true }, { name: '../x' }, { name: 5 }, null] });
R.empty = m.profileList(null);
const rows = [{ name: 'default' }, { name: 'work' }];
R.problems = ['', 'default', '../x', 'a b', 'x'.repeat(30), 'Work', 'work', 'client-1'].map((n) => m.profileNameProblem(n, rows));
R.label = [m.profileLabel('default'), m.profileLabel('work'), m.profileLabel('../x'), m.profileLabel(null)];
""")
        assert [r["name"] for r in out["list"]["rows"]] == ["default", "work"] and out["list"]["active"] == "work"
        assert out["empty"] == {"rows": [], "active": "default", "max": 8}
        p = out["problems"]
        assert all(p[i] for i in (0, 1, 2, 3, 4)) and p[5] and p[6] and p[7] == ""  # "Work" lower-cases to the existing "work"
        assert out["label"] == ["", "work", "", ""]

    def test_an_import_summary_is_words_made_from_counts_only(self, tmp_path):
        out = node(tmp_path, "manage-model.js", """
R.chrome = m.importLines({ bookmarks: { found: 5, added: 2, existing: 1, skipped: 2, full: 0 }, history: { found: 5, added: 3, existing: 0, skipped: 2, dropped: 4 }, notes: ['x'.repeat(500)] });
R.pw = m.importLines({ rows: 6, usable: 3, skipped: 3, added: 2, updated: 1, same: 0, refused: 0, never: 0, password: 'LEAK', passwords: ['LEAK'] });
R.junk = [m.importLines(null), m.importLines('x'), m.importLines({})];
""")
        c = out["chrome"]
        assert c[0].startswith("Bookmarks: 2 added of 5 found, 1 was already here, 2 skipped") and "4 older ones cut" in c[1]
        assert len(c[2]) == 300
        assert out["pw"][0].startswith("Passwords: 2 added, 1 updated, 0 already saved, 3 rows skipped")
        assert "Delete the CSV file now" in out["pw"][1] and "did not copy it" in out["pw"][1] and "LEAK" not in json.dumps(out)
        assert out["junk"] == [[], [], []]

    def test_the_drm_line_is_the_shells_own_words_or_an_honest_failure(self, tmp_path):
        out = node(tmp_path, "manage-model.js", """
R.line = m.drmLine({ ok: true, line: 'DRM: not available. This is stock Electron 44.3.0.' });
R.bad = [m.drmLine(null), m.drmLine({ ok: false }), m.drmLine({ ok: true }), m.drmLine('x')];
R.long = m.drmLine({ ok: true, line: 'x'.repeat(2000) }).length;
R.avail = [m.drmAvailable({ ready: true, widevine: 'available' }), m.drmAvailable({ ready: true, widevine: 'not available' }), m.drmAvailable(null)];
""")
        assert out["line"].startswith("DRM: not available") and out["long"] == 600
        assert out["bad"] == ["DRM status could not be read."] * 4
        assert out["avail"] == [True, False, False]

    def test_the_tabs_model_carries_the_profile_name_and_defaults_safely(self, tmp_path):
        out = node(tmp_path, "helpers.js", """
R.p = [m.tabsModel(null).profile, m.tabsModel({ profile: 'work' }).profile, m.tabsModel({ profile: '../x' }).profile, m.tabsModel({ profile: 5 }).profile];
""")
        assert out["p"] == ["default", "work", "default", "default"]


class TestTheScreenKeepsItsPromises:
    def test_manage_ui_talks_only_to_the_shell_and_never_to_a_server_route(self):
        text = source("manage-ui.js")
        code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        for banned in ("ctx.api", "fetch(", "XMLHttpRequest", "/api/", "localStorage", "sessionStorage"):
            assert banned not in code, banned

    def test_no_path_is_typed_or_held_in_the_page(self):
        code = re.sub(r"/\*.*?\*/", "", source("manage-ui.js"), flags=re.S)
        assert "type = 'file'" not in code and 'type="file"' not in code and "webkitdirectory" not in code
        assert "importFrom.chrome({ bookmarks: wantBm.checked, history: wantHi.checked })" in code  # only two flags go over
        assert "importFrom.passwords()" in code and "extensions.add()" in code

    def test_removing_and_switching_ask_here_first_and_adding_enabling_importing_ask_natively(self):
        code = source("manage-ui.js")
        assert code.count("confirmHere(") >= 2
        for spec in ("macOS confirmation", "macOS folder picker", "macOS file picker"):
            assert spec in code

    def test_a_password_is_never_shown_or_asked_for(self):
        code = re.sub(r"/\*.*?\*/", "", source("manage-ui.js"), flags=re.S)
        assert "type = 'password'" not in code and "textContent = res.password" not in code

    def test_the_preload_manage_block_exposes_exactly_these_requests(self):
        pre = (_ROOT / "electron" / "preload.js").read_text(encoding="utf-8")
        block = pre[pre.index("manage: {"):]
        invoked = re.findall(r'ipcRenderer\.invoke\("([^"]+)"', block)
        assert set(invoked) == {"ext:list", "ext:add", "ext:enable", "ext:disable", "ext:remove",
                                "profile:list", "profile:switch", "profile:create", "profile:remove", "import:chrome", "import:passwords"}
        assert "ipcRenderer.send" not in block and "ipcRenderer.on" not in block

    def test_the_preload_never_hands_the_page_a_path_a_handle_or_a_listener_for_these(self):
        pre = (_ROOT / "electron" / "preload.js").read_text(encoding="utf-8")
        block = pre[pre.index("manage: {"):]
        for banned in ("require(", "shell.", "fs.", "webContents", "executeJavaScript"):
            assert banned not in block

    def test_an_older_shell_without_manage_hides_the_two_buttons_instead_of_failing(self):
        idx = source("index.js")
        assert "extBtn.hidden = !hasB3;" in idx and "profBtn.hidden = !hasB3;" in idx
        assert "const hasB3 = hasB2 && Boolean(pane.manage)" in idx

    def test_the_panels_are_wired_and_the_manage_ui_is_released_on_unmount(self):
        idx = source("index.js")
        assert "extBtn.addEventListener('click', () => openPanel('extensions'));" in idx and "profBtn.addEventListener('click', () => openPanel('profiles'));" in idx
        assert "manageUi.panels[panel](panelRoot" in idx and "manageUi = null;" in idx
        assert "profileBefore !== tm.profile) loadBookmarks()" in idx  # another profile has its own bookmarks

    def test_site_settings_shows_the_drm_line_read_from_the_shell(self):
        ui = source("privacy-ui.js")
        assert "privacy.drm()" in ui and "drmLine(r)" in ui
        pre = (_ROOT / "electron" / "preload.js").read_text(encoding="utf-8")
        assert 'drm: () => ipcRenderer.invoke("drm:status")' in pre

    def test_no_em_dash_and_no_decorative_slashes_were_added(self):
        for name in ("manage-ui.js", "manage-model.js", "privacy-ui.js", "privacy-model.js"):
            text = source(name)
            assert "\u2014" not in text, name
            assert "//" not in re.sub(r"https?://", "", re.sub(r"/\*.*?\*/", "", text, flags=re.S)).replace("'//'", ""), name
        assert "\u2014" not in (_DIR / "browser.css").read_text(encoding="utf-8")
