"""Phase F2: the APPS screen's pure helpers, the permission walkthrough, HOME's
one-time offer and the screen's source rules.

The DOM code is verified live in a real browser (EVIDENCE/169_f2_*.png). The
routes are tested in test_app_driver_os_api.py and test_driving_strip_hook.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OS = _ROOT / "ui" / "assets" / "os"
_DIR = _OS / "screens" / "apps"
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not on PATH in this environment")


def node(tmp_path, body):
    script = tmp_path / "h.mjs"
    script.write_text(
        f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\n"
        f"import * as w from {(_DIR / 'walkthrough.js').as_uri()!r};\n"
        f"import * as reg from {(_OS / 'core' / 'registry.js').as_uri()!r};\n"
        f"const R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestHelpers:
    def test_accessibility_is_granted_not_granted_or_honestly_unknown(self, tmp_path):
        out = node(tmp_path, """
R.yes = h.trustView({ trusted: true, backend: 'pyobjc' });
R.no = h.trustView({ trusted: false, trust_help: 'Grant it in System Settings.', backend: 'osascript' });
R.noHelp = h.trustView({ trusted: false, backend_note: 'AX import failed' });
R.unknown = h.trustView({});
R.junk = h.trustView(null);
""")
        assert out["yes"]["word"] == "granted" and out["yes"]["tone"] == "ok" and out["yes"]["backend"] == "pyobjc"
        assert "unless auto approve is on" in out["yes"]["text"]
        assert out["no"]["word"] == "not granted" and out["no"]["tone"] == "warn" and out["no"]["text"] == "Grant it in System Settings."
        assert out["noHelp"]["text"] == "AX import failed"
        assert out["unknown"]["word"] == "unknown" and out["junk"]["word"] == "unknown"

    def test_the_kill_switch_names_the_reason_and_who_stopped_it(self, tmp_path):
        out = node(tmp_path, """
R.off = h.killView({ engaged: false });
R.on = h.killView({ engaged: true, reason: 'Stopped from the driving strip', by: 'owner' });
R.bare = h.killView({ engaged: true });
R.fileErr = h.killView({ engaged: true, file_error: 'OSError: read-only' });
R.junk = h.killView(undefined);
""")
        assert out["off"]["engaged"] is False and out["junk"]["engaged"] is False
        assert out["on"]["engaged"] is True and "Stopped from the driving strip" in out["on"]["line"] and "owner" in out["on"]["line"]
        assert "No reason was recorded" in out["bare"]["line"]
        assert "only this server process is stopped" in out["fileErr"]["line"]

    def test_running_apps_are_allowed_first_then_by_name_and_denied_last(self, tmp_path):
        out = node(tmp_path, """
R.rows = h.runningRows([
  { name: 'Terminal', bundle_id: 'com.apple.Terminal', denied: true, deny_reason: 'Terminal is on the hard deny list', allowed: false },
  { name: 'Safari', bundle_id: 'com.apple.Safari', denied: false, allowed: false },
  { name: 'TextEdit', bundle_id: 'com.apple.TextEdit', denied: false, allowed: true },
  { name: 'Notes', denied: false, allowed: false },
  { name: '', denied: false }, null, { bundle_id: 'x' },
]);
R.junk = h.runningRows('nope');
""")
        names = [r["name"] for r in out["rows"]]
        assert names == ["TextEdit", "Notes", "Safari", "Terminal"]
        assert out["rows"][3]["denied"] is True and "hard deny list" in out["rows"][3]["reason"]
        assert out["junk"] == []

    def test_the_log_is_newest_first_and_reads_as_sentences(self, tmp_path):
        out = node(tmp_path, """
R.rows = h.logRows([
  { at: 1, kind: 'executed', action: 'allow', actor: 'owner', app: 'TextEdit' },
  { at: 2, kind: 'refused', action: 'press_key', actor: 'model', error: 'not allowed' },
  { at: 3, kind: 'executed', action: 'kill' },
]);
R.junk = h.logRows(null);
""")
        assert [r["at"] for r in out["rows"]] == [3, 2, 1]
        assert out["rows"][0]["text"] == "kill"
        assert out["rows"][1]["text"] == "press key by model" and out["rows"][1]["tone"] == "bad" and out["rows"][1]["detail"] == "not allowed"
        assert out["rows"][2]["text"] == "allow in TextEdit by owner" and out["rows"][2]["tone"] == "ok"
        assert out["junk"] == []

    def test_a_403_owner_only_is_told_apart_from_a_deny_list_403(self, tmp_path):
        out = node(tmp_path, """
const ownerErr = Object.assign(new Error('owner only'), { status: 403, body: { error: 'owner only', detail: 'this action can only be taken from the Dourmouse app window; a script cannot take it' } });
const denyErr = Object.assign(new Error('Terminal cannot be allowed: Terminal is on the hard deny list.'), { status: 403, body: { error: 'x' } });
R.owner = [h.isOwnerOnly(ownerErr), h.errorText(ownerErr)];
R.deny = [h.isOwnerOnly(denyErr), h.errorText(denyErr)];
R.plain = h.errorText(new Error('boom'));
R.fiveHundred = h.isOwnerOnly(Object.assign(new Error('owner only'), { status: 500 }));
R.bare = h.errorText(Object.assign(new Error('owner only'), { status: 403 }));
""")
        assert out["owner"][0] is True and out["owner"][1].startswith("Owner only. This action can only be taken from the Dourmouse app window")
        assert out["deny"][0] is False and "hard deny list" in out["deny"][1] and "Owner only" not in out["deny"][1]
        assert out["plain"] == "boom" and out["fiveHundred"] is False
        assert out["bare"].startswith("Owner only.")

    def test_the_allow_card_says_what_allowing_does_and_what_never_changes(self, tmp_path):
        out = node(tmp_path, "R.allow = h.allowPrompt({ name: 'TextEdit' }); R.resume = h.resumePrompt();")
        assert "TextEdit" in out["allow"] and "terminal" in out["allow"] and "password manager" in out["allow"] and "remove TextEdit" in out["allow"]
        assert "kill switch" in out["resume"]

    def test_pane_urls_are_the_security_privacy_scheme(self, tmp_path):
        out = node(tmp_path, "R.url = h.paneUrl('Privacy_Accessibility');")
        assert out["url"] == "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


class TestWalkthrough:
    def test_it_lists_the_five_permissions_and_only_used_ones_get_a_button(self, tmp_path):
        out = node(tmp_path, "R.p = w.PERMISSIONS.map((p) => [p.id, p.used, p.pane, Boolean(p.enables)]);")
        by_id = {p[0]: p for p in out["p"]}
        assert list(by_id) == ["accessibility", "screen", "microphone", "camera", "files"]
        assert by_id["accessibility"][2] == "Privacy_Accessibility" and by_id["microphone"][2] == "Privacy_Microphone"
        assert by_id["camera"][2] == "Privacy_Camera" and by_id["files"][2] == "Privacy_FilesAndFolders"
        # no feature asks for a permission it does not use
        assert by_id["screen"][1] is False
        assert all(by_id[i][1] is True for i in ("accessibility", "microphone", "camera", "files"))
        assert all(p[3] for p in out["p"])

    def test_screen_recording_says_nothing_uses_it(self, tmp_path):
        out = node(tmp_path, "R.screen = w.PERMISSIONS.find((p) => p.id === 'screen');")
        assert "does not record or capture your screen" in out["screen"]["enables"] and out["screen"]["uses"] == "Not used today"

    def test_the_claims_match_the_code(self):
        # Screen Recording is described as unused: no module may capture the screen.
        offenders = []
        for path in (_ROOT / "dourmouse").rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"CGWindowListCreateImage|CGDisplayCreateImage|ScreenCaptureKit|\bscreencapture\b", text):
                offenders.append(str(path.relative_to(_ROOT)))
        assert offenders == [], f"a module now captures the screen; update the walkthrough: {offenders}"
        text = (_DIR / "walkthrough.js").read_text(encoding="utf-8")
        assert "VOICE" in text and "getUserMedia" in (_OS / "screens" / "voice" / "index.js").read_text(encoding="utf-8")

    def test_open_pane_says_where_to_click_when_the_host_cannot_open_it(self, tmp_path):
        out = node(tmp_path, """
const perm = w.PERMISSIONS[0];
const seen = [];
R.no = w.openPane({ host: { openExternal: (u) => { seen.push(u); return false; } } }, perm);
R.yes = w.openPane({ host: { openExternal: (u) => { seen.push(u); return true; } } }, perm);
R.seen = seen;
""")
        assert out["no"]["opened"] is False and "Open System Settings yourself" in out["no"]["text"] and "Accessibility" in out["no"]["text"]
        assert out["yes"]["opened"] is True
        assert out["seen"] == ["x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"] * 2

    def test_seen_is_read_from_the_server_store_and_unknown_when_it_cannot_tell(self, tmp_path):
        out = node(tmp_path, """
const ctxWith = (get) => ({ api: { get, isStale: (d) => Boolean(d && d.__stale) } });
R.seen = await w.readSeen(ctxWith(async () => ({ prefs: { [w.SEEN_KEY]: true } })));
R.unseen = await w.readSeen(ctxWith(async () => ({ prefs: {} })));
R.noPrefs = await w.readSeen(ctxWith(async () => ({})));
R.stale = await w.readSeen(ctxWith(async () => ({ __stale: true, prefs: {} })));
R.err = await w.readSeen(ctxWith(async () => { throw new Error('down'); }));
R.key = w.SEEN_KEY;
""")
        assert out["seen"] is True and out["unseen"] is False and out["noPrefs"] is False
        assert out["stale"] is None and out["err"] is None
        assert out["key"] == "os.permissions_walkthrough_seen"

    def test_marking_it_seen_posts_the_flag_to_the_settings_store_and_reports_a_failure(self, tmp_path):
        out = node(tmp_path, """
const posts = [];
R.ok = await w.markSeen({ api: { post: async (p, b) => { posts.push([p, b]); return {}; } } });
R.fail = await w.markSeen({ api: { post: async () => { throw new Error('HTTP 500'); } } });
R.posts = posts;
""")
        assert out["posts"] == [["/api/state/prefs", {"key": "os.permissions_walkthrough_seen", "value": True}]]
        assert out["ok"] == "" and out["fail"] == "HTTP 500"


class TestRegistry:
    def test_apps_is_registered_in_the_system_group_right_after_settings(self, tmp_path):
        out = node(tmp_path, "R.ids = reg.IDS; R.groups = reg.SIDEBAR_GROUPS; R.apps = reg.byId('APPS'); R.thread = reg.THREAD_SCREENS.includes('APPS');")
        i = out["ids"].index("SETTINGS")
        assert out["ids"][i + 1] == "APPS"
        assert ["System", "SETTINGS"] in out["groups"]
        assert out["apps"]["slug"] == "apps" and out["apps"]["thread"] is False and out["apps"]["sub"] == "app driving"
        assert out["thread"] is False
        assert out["apps"]["icon"].startswith("M")  # a raw path: kit/icons.js has no APPS entry

    def test_the_system_group_is_still_a_contiguous_run_ending_the_list(self, tmp_path):
        out = node(tmp_path, "R.ids = reg.IDS; R.groups = reg.SIDEBAR_GROUPS;")
        starts = [out["ids"].index(g[1]) for g in out["groups"]]
        assert starts == sorted(starts)
        assert out["ids"][starts[-1]:] == ["SETTINGS", "APPS", "OFFICE"]


class TestSourceRules:
    @staticmethod
    def _code(name):
        return re.sub(r"/\*.*?\*/", "", (_DIR / name).read_text(encoding="utf-8"), flags=re.S)

    def test_the_owner_only_actions_go_through_ctx_api_post(self):
        code = self._code("index.js")
        for path in ("/api/os/apps/allow", "/api/os/apps/deny", "/api/os/apps/resume", "/api/os/apps/kill"):
            assert f"ctx.api.post('{path}'" in code, path
        assert "/api/os/apps/act" not in code and "/api/os/apps/snapshot" not in code  # the screen never drives an app

    def test_allow_and_resume_ask_first_remove_and_stop_do_not(self):
        code = self._code("index.js")
        assert re.search(r"confirmHere\(confirmEl, allowPrompt\(row\)", code) and re.search(r"confirmHere\(confirmEl, resumePrompt\(\)", code)
        assert code.count("errorText(err)") >= 4  # every refusal, a 403 owner-only included, is shown through errorText, never swallowed

    def test_the_accessibility_button_and_the_guide_use_the_hosts_open_path(self):
        code = self._code("index.js") + self._code("walkthrough.js")
        assert "ctx.host.openExternal(paneUrl(perm.pane))" in code
        assert "window.open" not in code and "location" not in code
        assert "OPEN ACCESSIBILITY SETTINGS" in code

    def test_the_screen_listens_to_the_shared_stream_and_never_polls(self):
        code = self._code("index.js")
        assert "ctx.events.on('app_driver_indicator'" in code and "ctx.every" not in code

    def test_no_em_dash_anywhere_in_the_new_files(self):
        for path in list(_DIR.rglob("*")) + [_OS / "chrome" / "driving-strip.js"]:
            if path.is_file():
                assert "—" not in path.read_text(encoding="utf-8"), path.name


class TestHomeOffer:
    def _home(self):
        return (_OS / "screens" / "home" / "index.js").read_text(encoding="utf-8")

    def test_home_offers_the_guide_once_through_the_settings_store(self):
        src = self._home()
        assert "from '../apps/walkthrough.js'" in src
        assert "const seen = await readSeen(ctx);" in src and "if (seen !== false" in src
        assert "await markSeen(ctx)" in src
        assert src.count("offerWalkthrough()") == 2  # defined once, called once after the thread is up
        assert "href = '#/apps'" in src

    def test_a_read_that_cannot_tell_offers_nothing(self):
        # readSeen answers null for a failed or stale read; HOME only offers on an explicit false
        src = self._home()
        assert "seen !== false" in src

    def test_the_offer_is_recorded_as_seen_the_moment_it_is_shown(self):
        src = self._home()
        shown = src.index("walkEl.hidden = false;")
        marked = src.index("await markSeen(ctx)")
        assert shown < marked < src.index("home-walk-t") + 2000


def test_system_settings_links_are_allowed_exactly_and_nothing_else():
    """Finding #169: only the macOS Privacy panes join http(s) on the open-external path."""
    import pathlib
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        import pytest

        pytest.skip("node not available")
    root = pathlib.Path(__file__).resolve().parents[2]
    script = (
        "const p=require('./electron/policy.js');"
        "const ok=['x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'];"
        "const bad=['x-apple.systempreferences:com.apple.preference.security?Privacy_Camera&x=1','file:///etc/passwd',"
        "'x-apple.systempreferences:com.apple.preference.network','javascript:alert(1)','smb://host/share'];"
        "console.log(JSON.stringify([ok.map(p.systemSettingsUrlAllowed),bad.map(p.systemSettingsUrlAllowed),p.externalUrlAllowed(ok[0])]))"
    )
    out = subprocess.run([node, "-e", script], cwd=root, capture_output=True, text=True, timeout=30, check=True).stdout
    assert out.strip() == "[[true],[false,false,false,false,false],false]"
    main = (root / "electron" / "main.js").read_text()
    block = main[main.index('ipcMain.handle("bridge:open_external"'):][:1600]
    assert "policy.systemSettingsUrlAllowed(url)" in block
