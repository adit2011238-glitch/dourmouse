"""Phase B3: the B2 review leftovers that live in B3's files.

* the camera, microphone and audio-input entitlements for the packaged app,
* a click-ambush delay before the permission and Save-password bar buttons work,
* a cap on waiting permission requests per prompt, and a remembered Dismiss,
* the browser folder at 0700 and permissions.json at 0600 after every write (the write itself is
  exercised in test_browser_profiles_shell.py, which starts from a loose 0755 folder and 0644 file).
"""

from __future__ import annotations

import json
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ELECTRON = ROOT / "electron"
UI = ROOT / "ui" / "assets" / "os" / "screens" / "browser"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


class TestEntitlements:
    PLIST = ELECTRON / "resources" / "entitlements.mac.plist"

    def test_camera_and_audio_input_were_added(self):
        data = plistlib.loads(self.PLIST.read_bytes())
        assert data["com.apple.security.device.camera"] is True
        assert data["com.apple.security.device.audio-input"] is True

    def test_nothing_that_was_there_was_removed(self):
        data = plistlib.loads(self.PLIST.read_bytes())
        for key in ("com.apple.security.cs.allow-unsigned-executable-memory", "com.apple.security.cs.disable-library-validation",
                    "com.apple.security.cs.allow-jit", "com.apple.security.cs.allow-dyld-environment-variables",
                    "com.apple.security.automation.apple-events"):
            assert data[key] is True, key
        assert set(data) == {"com.apple.security.cs.allow-unsigned-executable-memory", "com.apple.security.cs.disable-library-validation",
                             "com.apple.security.cs.allow-jit", "com.apple.security.cs.allow-dyld-environment-variables",
                             "com.apple.security.automation.apple-events", "com.apple.security.device.camera", "com.apple.security.device.audio-input"}

    def test_the_usage_descriptions_the_entitlements_pair_with_are_already_in_the_package_config(self):
        pkg = json.loads((ELECTRON / "package.json").read_text(encoding="utf-8"))
        extend = pkg["build"]["mac"]["extendInfo"]
        assert "NSMicrophoneUsageDescription" in extend and "NSCameraUsageDescription" in extend

    def test_the_comment_in_the_plist_has_no_double_dash_and_no_em_dash(self):
        text = self.PLIST.read_text(encoding="utf-8")
        new = text.split("Finding B3")[1].split("-->")[0]
        assert "--" not in new and "\u2014" not in new


def node_run(tmp_path, module, body):
    script = tmp_path / "t.mjs"
    script.write_text(f"import * as m from {(UI / module).as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestClickAmbushGuard:
    @needs_node
    def test_the_delay_is_about_600_ms_counted_from_when_the_prompt_first_appeared(self, tmp_path):
        out = node_run(tmp_path, "privacy-model.js", """
R.delay = m.CLICK_DELAY_MS;
R.left = [m.clickDelayLeft(1000, 1000), m.clickDelayLeft(1000, 1300), m.clickDelayLeft(1000, 1600), m.clickDelayLeft(1000, 5000)];
R.backwards = m.clickDelayLeft(2000, 1000);
R.junk = [m.clickDelayLeft(undefined, 5), m.clickDelayLeft(5, NaN), m.clickDelayLeft('x', 'y')];
""")
        assert out["delay"] == 600
        assert out["left"] == [600, 300, 0, 0]
        assert out["backwards"] == 600  # a clock that went backwards counts as "just appeared"
        assert out["junk"] == [600, 600, 600]  # unknown means wait the full time, never click at once

    def test_the_permission_and_save_bars_are_guarded_and_the_fill_bars_are_not(self):
        src = (UI / "privacy-ui.js").read_text(encoding="utf-8")
        paint = src.split("function paintBars()")[1].split("function update(raw)")[0]
        assert "guardBar(permBar(model)" in paint and "guardBar(saveBar(model)" in paint
        assert "guardBar(fillBar" not in paint and "guardBar(addressBar" not in paint and "guardBar(noticeBar" not in paint
        guard = src.split("function guardBar(")[1].split("\n  }\n")[0]
        assert "x.disabled = true" in guard and "x.disabled = false" in guard and "clickDelayLeft(first, Date.now())" in guard
        assert "firstSeen.has(id)" in guard  # a rebuild of the same prompt does not restart the wait

    def test_the_timers_are_cleared_when_the_bars_are_rebuilt_and_when_the_screen_goes(self):
        src = (UI / "privacy-ui.js").read_text(encoding="utf-8")
        assert src.count("guardTimers.forEach((t) => clearTimeout(t))") >= 2
        assert "firstSeen.delete(id)" in src


class TestPromptQueueLimits:
    QUEUE = f"const q = require({str(ELECTRON / 'permissions.js')!r});\nconst R = {{}};\nlet t = 1000; const now = () => t;\n"

    def run(self, tmp_path, body):
        script = tmp_path / "q.js"
        script.write_text(self.QUEUE + body + "\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
        proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])

    @needs_node
    def test_a_prompt_holds_at_most_fifty_waiting_requests(self, tmp_path):
        out = self.run(tmp_path, """
const queue = q.createPromptQueue({ now });
const got = [];
const results = [];
for (let i = 0; i < 60; i += 1) results.push(queue.request(1, 'https://a.example', ['camera'], (d) => got.push(i + ':' + d)));
R.accepted = results.filter((r) => r.ok).length; R.refused = results.filter((r) => !r.ok).map((r) => r.reason);
R.size = queue.size();
queue.answer(results[0].id, 'allow');
R.answered = got.length; R.firstAndLast = [got[0], got[got.length - 1]];
// after it is answered the same site may ask again
R.again = queue.request(1, 'https://a.example', ['camera'], () => {}).ok;
""")
        assert out["accepted"] == 50 and set(out["refused"]) == {"too many requests for this prompt"} and len(out["refused"]) == 10
        assert out["size"] == 1 and out["answered"] == 50 and out["firstAndLast"] == ["0:allow", "49:allow"]
        assert out["again"] is True

    @needs_node
    def test_the_owners_dismiss_is_remembered_for_sixty_seconds_for_that_site_and_those_permissions(self, tmp_path):
        out = self.run(tmp_path, """
const queue = q.createPromptQueue({ now });
const a = queue.request(1, 'https://a.example', ['camera', 'microphone'], () => {});
queue.answer(a.id, 'dismiss');
R.sameRightAway = queue.request(1, 'https://a.example', ['microphone', 'camera'], () => {});   // order of keys does not matter
R.otherTab = queue.request(2, 'https://a.example', ['camera', 'microphone'], () => {}).ok;
R.otherSite = queue.request(1, 'https://b.example', ['camera', 'microphone'], () => {}).ok;
R.otherKeys = queue.request(1, 'https://a.example', ['camera'], () => {}).ok;
t += 59999; R.justBefore = queue.request(1, 'https://a.example', ['camera', 'microphone'], () => {}).ok;
t += 2; const again = queue.request(1, 'https://a.example', ['camera', 'microphone'], () => {}); R.after = again.ok;
""")
        assert out["sameRightAway"] == {"ok": False, "reason": "dismissed a moment ago"}
        assert out["otherTab"] is False  # the owner said no to this site for these permissions: another tab does not raise it again
        assert out["otherSite"] is True and out["otherKeys"] is True
        assert out["justBefore"] is False and out["after"] is True

    @needs_node
    def test_only_the_owners_dismiss_is_remembered_not_an_expiry_a_closed_tab_or_a_move(self, tmp_path):
        out = self.run(tmp_path, """
const queue = q.createPromptQueue({ now, ttlMs: 5000 });
const ask = () => queue.request(1, 'https://a.example', ['fullscreen'], () => {}).ok;
ask(); t += 6000; R.expired = queue.expire(); R.afterExpiry = ask();
queue.dropTab(1); R.afterDropTab = ask();
queue.dropForeign(1, 'https://other.example'); R.afterMove = ask();
queue.dropAll(); R.afterDropAll = ask();
const b = queue.request(1, 'https://c.example', ['fullscreen'], () => {});
queue.answer(b.id, 'block'); R.afterBlock = queue.request(1, 'https://c.example', ['fullscreen'], () => {}).ok;
const c = queue.request(1, 'https://d.example', ['fullscreen'], () => {});
queue.answer(c.id, 'once'); R.afterOnce = queue.request(1, 'https://d.example', ['fullscreen'], () => {}).ok;
""")
        assert out["expired"] == 1 and out["afterExpiry"] is True and out["afterDropTab"] is True and out["afterMove"] is True and out["afterDropAll"] is True
        assert out["afterBlock"] is True and out["afterOnce"] is True  # only a Dismiss is remembered

    @needs_node
    def test_the_dismissed_list_cannot_grow_without_bound(self, tmp_path):
        out = self.run(tmp_path, """
const queue = q.createPromptQueue({ now, max: 1000, maxPerTab: 1000 });
for (let i = 0; i < 500; i += 1) { const r = queue.request(1, 'https://s' + i + '.example', ['fullscreen'], () => {}); queue.answer(r.id, 'dismiss'); }
// the oldest were forgotten to make room, the newest are still remembered
R.oldest = queue.request(1, 'https://s0.example', ['fullscreen'], () => {}).ok;
R.newest = queue.request(1, 'https://s499.example', ['fullscreen'], () => {}).ok;
""")
        assert out["oldest"] is True and out["newest"] is False


SHELL = r'''
main(async () => {
  T.ensurePaneView();
  T.showPane();
  await handlers["pane:screen"](consoleEvt(), true);
  const wc = T.tabs.get(T.active()).view.webContents;
  wc.loadURL("https://meet.example/room");
  const callback = (perm) => new Promise((resolve) => PANE_SESSION.req(wc, perm, resolve, { requestingUrl: "https://meet.example/room", isMainFrame: true }));
  const states = [];
  for (let i = 0; i < 60; i += 1) { const s = watch(callback("geolocation")); states.push(s); }
  await new Promise((r) => setTimeout(r, 20));
  R.fiftyWaiting = states.filter((s) => !s.done).length;
  R.refusedAtOnce = states.filter((s) => s.done && s.value === false).length;
  R.queued = T.promptQueue.size();
  const prompt = T.privacyState().perm;
  await handlers["pane:perm-answer"](consoleEvt(), prompt.id, "dismiss");
  await new Promise((r) => setTimeout(r, 20));
  R.allAnsweredNo = states.every((s) => s.done && s.value === false);
  // asking again straight away raises no bar and is refused quietly
  const again = watch(callback("geolocation"));
  await new Promise((r) => setTimeout(r, 20));
  R.again = { done: again.done, value: again.value, queued: T.promptQueue.size(), bar: T.privacyState().perm };
  // another permission on the same site is not affected
  const other = watch(callback("notifications"));
  await new Promise((r) => setTimeout(r, 20));
  R.other = { done: other.done, bar: Boolean(T.privacyState().perm) };
  R.stored = Object.keys(T.siteTable()).length;  // a Dismiss stores nothing
});
'''


@needs_node
def test_in_the_shell_fifty_wait_the_rest_are_refused_and_a_dismiss_is_remembered(tmp_path):
    from dourmouse.tests.b3_harness import run_scenario

    out = run_scenario(tmp_path, SHELL)
    assert out["fiftyWaiting"] == 50 and out["refusedAtOnce"] == 10 and out["queued"] == 1
    assert out["allAnsweredNo"] is True
    assert out["again"] == {"done": True, "value": False, "queued": 0, "bar": None}
    assert out["other"] == {"done": False, "bar": True}
    assert out["stored"] == 0


@pytest.mark.xfail(strict=False, reason="B3 added four modules to electron/ and electron/package.json (outside B3's files) still has to list them, or a packaged build cannot start")
def test_the_package_file_list_names_the_four_new_b3_modules():
    pkg = json.loads((ELECTRON / "package.json").read_text(encoding="utf-8"))
    files = pkg["build"]["files"]
    for name in ("profiles.js", "extensions.js", "importers.js", "drm.js"):
        assert name in files, name


def test_main_js_requires_exactly_the_modules_this_phase_added():
    main = (ELECTRON / "main.js").read_text(encoding="utf-8")
    assert re.findall(r'require\("\./(\w+)"\)', main) == ["policy", "permissions", "passwords", "profiles", "extensions", "importers", "drm"]
