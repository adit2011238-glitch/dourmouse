"""Phase B2: the BROWSER screen's permission, Save password and Fill bars and its Site settings
and Passwords panels. The pure model (ui/.../privacy-model.js) is run under node; the DOM code
(privacy-ui.js, index.js) is pinned by the rules it must keep. What the DOM does with them is
verified live in the Electron app and recorded in the B2 finding."""

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


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "m.mjs"
    script.write_text(f"import * as m from {(_DIR / 'privacy-model.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def source(name):
    return (_DIR / name).read_text(encoding="utf-8")


class TestPrivacyModel:
    def test_nothing_or_junk_is_an_empty_model_and_nothing_is_invented(self, tmp_path):
        out = node(tmp_path, "R.a = m.privacyModel(null); R.b = m.privacyModel('x'); R.c = m.privacyModel({ perm: 5, save: [], fill: 'y', vault: 3, sites: null, pendingPerms: -2 });")
        for key in ("a", "b", "c"):
            r = out[key]
            assert r["perm"] is None and r["save"] is None and r["fill"] is None and r["fillAddress"] is None
            assert r["pendingPerms"] == 0 and r["notice"] == "" and r["addresses"] == 0
            assert r["vault"] == {"available": None, "passwords": 0, "neverSaved": 0}  # unknown, not "no"

    def test_the_prompt_keeps_only_known_permissions_and_cuts_long_text(self, tmp_path):
        out = node(tmp_path, """
R.m = m.privacyModel({ perm: { id: 'p1', origin: 'https://a.example', host: 'a.example', keys: ['camera', 'display-capture', 'midi', 'microphone', 5], text: 'x'.repeat(900) } });
R.noId = m.privacyModel({ perm: { origin: 'https://a.example' } }).perm;
""")
        assert out["m"]["perm"]["keys"] == ["camera", "microphone"]
        assert len(out["m"]["perm"]["text"]) == 300
        assert out["noId"] is None

    def test_a_save_prompt_never_carries_a_password_even_if_one_is_sent(self, tmp_path):
        out = node(tmp_path, "R.m = m.privacyModel({ save: { id: 's1', origin: 'https://a.example', host: 'a.example', username: 'alice', update: true, password: 'LEAK', secret: 'LEAK' } });")
        assert out["m"]["save"] == {"id": "s1", "origin": "https://a.example", "host": "a.example", "username": "alice", "update": True}
        assert "LEAK" not in json.dumps(out)

    def test_a_fill_offer_holds_ids_and_usernames_only(self, tmp_path):
        out = node(tmp_path, """
R.m = m.privacyModel({ fill: { origin: 'https://a.example', host: 'a.example', entries: [{ id: 'e1', username: 'alice', password: 'LEAK' }, { username: 'no id' }, null] },
                       fillAddress: { profiles: [{ id: 'a1', label: '' }, { label: 'no id' }] } });
R.empty = m.privacyModel({ fill: { entries: [] }, fillAddress: { profiles: [] } });
""")
        assert out["m"]["fill"]["entries"] == [{"id": "e1", "username": "alice"}]
        assert out["m"]["fillAddress"]["profiles"] == [{"id": "a1", "label": "Address"}]
        assert "LEAK" not in json.dumps(out)
        assert out["empty"]["fill"] is None and out["empty"]["fillAddress"] is None

    def test_the_bars_are_rebuilt_only_when_something_they_draw_changed(self, tmp_path):
        out = node(tmp_path, """
const base = { perm: { id: 'p1', origin: 'https://a.example', host: 'a.example', keys: ['camera'], text: 'a.example wants to use your camera' }, pendingPerms: 1 };
const a = m.barsKey(m.privacyModel(base), '');
R.same = a === m.barsKey(m.privacyModel({ ...base, vault: { available: true, passwords: 9 } }), '');
R.differs = [
  a !== m.barsKey(m.privacyModel({ ...base, perm: { ...base.perm, id: 'p2' } }), ''),
  a !== m.barsKey(m.privacyModel({ ...base, pendingPerms: 3 }), ''),
  a !== m.barsKey(m.privacyModel({ ...base, notice: 'macOS has not allowed the camera' }), ''),
];
R.noticeHidden = m.barsKey(m.privacyModel({ notice: 'n' }), 'n') === m.barsKey(m.privacyModel({}), '');
""")
        assert out["same"] is True and out["differs"] == [True, True, True] and out["noticeHidden"] is True

    def test_the_waiting_line_counts_the_prompts_of_other_tabs(self, tmp_path):
        out = node(tmp_path, """
const p = { id: 'p1', origin: 'https://a.example', host: 'a.example', keys: ['camera'], text: 't' };
R.none = m.waitingLine(m.privacyModel({ perm: p, pendingPerms: 1 }));
R.one = m.waitingLine(m.privacyModel({ perm: p, pendingPerms: 2 }));
R.many = m.waitingLine(m.privacyModel({ perm: p, pendingPerms: 4 }));
R.onlyOthers = m.waitingLine(m.privacyModel({ pendingPerms: 2 }));
R.save = [m.saveLine({ host: 'a.example', username: 'alice', update: false }), m.saveLine({ host: 'a.example', username: '', update: true })];
""")
        assert out["none"] == "" and "1 more request" in out["one"] and out["many"].startswith("3 more")
        assert out["onlyOthers"].startswith("2 more")
        assert out["save"] == ["Save the password for alice on a.example?", "Update the password on a.example?"]

    def test_site_settings_group_by_site_and_drop_anything_that_is_not_a_known_decision(self, tmp_path):
        out = node(tmp_path, """
R.g = m.groupSites([
  { origin: 'https://a.example', permission: 'camera', label: 'Camera', decision: 'allow', at: 5 },
  { origin: 'https://a.example', permission: 'geolocation', label: 'Location', decision: 'block', at: 6 },
  { origin: 'https://b.example:8443', permission: 'fullscreen', label: 'Full screen', decision: 'allow' },
  { origin: 'https://a.example', permission: 'midi', decision: 'allow' }, { origin: 'https://a.example', permission: 'camera', decision: 'maybe' }, null, { permission: 'camera' }]);
R.junk = [m.groupSites(null), m.groupSites('x')];
""")
        assert [(g["host"], [i["permission"] for i in g["items"]]) for g in out["g"]] == [("a.example", ["camera", "geolocation"]), ("b.example:8443", ["fullscreen"])]
        assert out["junk"] == [[], []]

    def test_password_rows_show_sites_and_usernames_and_never_a_password_field(self, tmp_path):
        out = node(tmp_path, """
R.rows = m.passwordRows([{ id: 'e1', origin: 'https://a.example', username: 'alice', readable: true, password: 'LEAK', blob: 'LEAK', updated: 5, lastUsed: 0 },
  { id: 'e2', origin: 'https://b.example', username: '', readable: false }, { origin: 'x' }, null]);
R.never = m.neverRows(['https://a.example', 5, null]);
R.profiles = m.profileRows([{ id: 'a1', label: 'Home', name: 'Ada', line1: '1 Road', secret: 'LEAK' }, { name: 'no id' }]);
""")
        assert [(r["host"], r["username"], r["readable"]) for r in out["rows"]] == [("a.example", "alice", True), ("b.example", "", False)]
        assert "LEAK" not in json.dumps(out)
        assert out["never"] == ["https://a.example"]
        assert out["profiles"][0]["name"] == "Ada" and "secret" not in out["profiles"][0] and len(out["profiles"]) == 1

    def test_an_address_needs_one_real_detail_and_every_field_is_bounded(self, tmp_path):
        out = node(tmp_path, """
R.empty = m.addressFromForm({ label: 'Only a label' });
R.ok = m.addressFromForm({ label: 'Home', name: ' Ada ', line1: 'x'.repeat(500) });
R.junk = m.addressFromForm(null);
R.fields = m.ADDRESS_FIELDS.map((f) => f.key);
""")
        assert out["empty"]["ok"] is False and out["junk"]["ok"] is False
        assert out["ok"]["profile"]["name"] == "Ada" and len(out["ok"]["profile"]["line1"]) == 200
        assert out["fields"] == ["label", "name", "email", "phone", "line1", "line2", "city", "state", "zip", "country"]

    def test_the_vault_line_is_honest_when_encryption_is_missing(self, tmp_path):
        out = node(tmp_path, "R.off = m.vaultLine({ available: false }); R.on = m.vaultLine({ available: true });")
        assert "not available" in out["off"] and "Nothing is ever stored without encryption" in out["off"]
        assert "Keychain" in out["on"] and "no tool of the AI can list or open them" in out["on"]
        assert "browser agent can read field values" in out["on"]  # said plainly: a filled password is in the page's field


class TestTheScreenKeepsItsPromises:
    def test_the_privacy_ui_talks_only_to_the_shell_never_to_a_server_route(self):
        ui = source("privacy-ui.js")
        for forbidden in ("ctx.api", "fetch(", "XMLHttpRequest", "/api/", "localStorage", "sessionStorage", "ctx.prefs"):
            assert forbidden not in ui, forbidden
        assert "privacy.pwReveal" in ui

    def test_the_one_call_that_returns_a_password_sits_behind_a_confirmation_card_and_a_timeout(self):
        ui = source("privacy-ui.js")
        assert ui.count("pwReveal(") == 1
        reveal = ui[ui.index("show.addEventListener"):ui.index("const del = el('button', 'os-btn', 'DELETE');")]
        assert "confirmHere(" in reveal and "privacy.pwReveal(r.id)" in reveal and "REVEAL_MS" in reveal
        assert "setTimeout" in reveal and "textContent = ''" in reveal
        assert "export const REVEAL_MS = 10000;" in ui

    def test_a_password_that_was_shown_is_wiped_when_the_panel_closes(self):
        ui = source("privacy-ui.js")
        assert ".bw-pwshown" in ui and "clearTimeout(t)" in ui

    def test_a_prompt_is_answered_by_buttons_that_call_the_shell_and_nothing_decides_in_the_page(self):
        ui = source("privacy-ui.js")
        for call in ("privacy.permAnswer(id, decision)", "privacy.pwSaveAnswer(id, answer)", "privacy.pwFill(", "privacy.addrFill(", "privacy.siteSet(", "privacy.siteForget(", "privacy.sitesClear()", "privacy.pwDelete(", "privacy.pwList()"):
            assert call in ui, call
        for decision in ("'allow'", "'once'", "'block'"):
            assert decision in ui

    def test_the_three_choices_in_the_bar_are_the_ones_chrome_offers(self):
        ui = source("privacy-ui.js")
        assert "btn('Allow'" in ui and "btn('Allow this time'" in ui and "btn('Block'" in ui
        assert "Never for this site" in ui and "Not now" in ui

    def test_destructive_changes_ask_first(self):
        ui = source("privacy-ui.js")
        assert ui.count("confirmHere(") == 4  # reset all sites, show a password, delete a password, delete an address

    def test_the_screen_wires_the_new_panels_and_removes_what_it_added_when_it_leaves(self):
        idx = source("index.js")
        assert "import { createPrivacy } from './privacy-ui.js';" in idx
        assert "const hasB2 = hasB1 && Boolean(pane.privacy)" in idx
        for needle in ("id=\"bwSites\"", "id=\"bwPass\"", "openPanel('sites')", "openPanel('passwords')", "privacyUi.panels[panel]", "if (typeof offPrivacy === 'function') offPrivacy();", "if (panelHandle) panelHandle.dispose();"):
            assert needle in idx, needle

    def test_an_older_shell_without_privacy_hides_the_buttons_instead_of_failing(self):
        idx = source("index.js")
        assert "sitesBtn.hidden = !hasB2;" in idx and "passBtn.hidden = !hasB2;" in idx

    def test_the_preload_exposes_only_requests_and_the_reveal_is_the_only_password_returning_one(self):
        pre = (_ROOT / "electron" / "preload.js").read_text(encoding="utf-8")
        block = pre[pre.index("privacy: {"):pre.index("manage: {")]  # B3's own block follows and is pinned in test_browser_manage_screen.py
        invoked = re.findall(r'ipcRenderer\.invoke\("([^"]+)"', block)
        assert set(invoked) == {"pane:privacy", "pane:perm-answer", "site:perms", "site:perm-set", "site:perm-forget", "site:perms-clear",
                                "pw:list", "pw:delete", "pw:never-remove", "pw:save-answer", "pw:fill", "pw:reveal",
                                "addr:list", "addr:save", "addr:delete", "addr:fill", "drm:status"}
        assert "ipcRenderer.send" not in block and "contextBridge" not in block.split("privacy: {")[1]

    def test_no_em_dash_and_no_decorative_slashes_were_added(self):
        for name in ("privacy-ui.js", "privacy-model.js"):
            text = source(name)
            assert "—" not in text, name
            assert "//" not in re.sub(r"https?://", "", re.sub(r"/\*.*?\*/", "", text, flags=re.S)).replace("'//'", ""), name
