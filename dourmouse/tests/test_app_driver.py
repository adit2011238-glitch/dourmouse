"""Phase F1 app driving core: policy, kill switch, re-resolve, actions, tools.

Every macOS call goes through a FakeBackend. No real app is read or driven.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

from dourmouse import execution_policy
from dourmouse.app_driver import backend as backend_mod
from dourmouse.app_driver import driver, policy, safety, tools
from dourmouse.app_driver.errors import AppDriverError

TEXTEDIT = {"name": "TextEdit", "bundle_id": "com.apple.TextEdit", "pid": 4242, "frontmost": False}
TERMINAL = {"name": "Terminal", "bundle_id": "com.apple.Terminal", "pid": 5151, "frontmost": False}


def n(role, title="", children=None, **kw):
    return {"role": role, "subrole": kw.pop("subrole", ""), "title": title, "description": kw.pop("description", ""),
            "placeholder": kw.pop("placeholder", ""), "value": kw.pop("value", None), "secure": kw.pop("secure", False),
            "enabled": kw.pop("enabled", True), "focused": False, "x": 10.0, "y": 20.0, "w": 100.0, "h": 30.0,
            "children": children or []}


def textedit_tree():
    return {"windows": [n("AXWindow", "Untitled", [
        n("AXButton", "Save"),
        n("AXScrollArea", "", [n("AXTextArea", "", value="hello")]),
        n("AXButton", "Cancel", enabled=False),
        n("AXTextField", "", subrole="AXSecureTextField", secure=True),
        n("AXTextField", "Password"),
        n("AXStaticText", "", value="key sk-ant-abcdefghijklmnopqrstuvwxyz"),
    ])], "truncated": False}


def with_handles(tree):
    tree = copy.deepcopy(tree)

    def walk(node, path):
        node["handle"] = ("h",) + tuple(path)
        for i, c in enumerate(node["children"]):
            walk(c, path + [i])

    for i, w in enumerate(tree["windows"]):
        walk(w, [i])
    return tree


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.trusted = True
        self.apps = [dict(TEXTEDIT), dict(TERMINAL)]
        self.front = 1
        self.tree = textedit_tree()
        self.calls = []
        self.reads = 0
        self.on_post_text = None
        self.activate_brings_front = True

    def is_trusted(self):
        return self.trusted

    def running_apps(self):
        return [dict(a) for a in self.apps]

    def frontmost_pid(self):
        return self.front

    def activate(self, pid):
        self.calls.append(("activate", pid))
        if self.activate_brings_front:
            self.front = pid
        return True

    def read_tree(self, pid, max_depth, max_nodes):
        self.reads += 1
        self.calls.append(("read_tree", pid))
        tree = with_handles(self.tree)
        tree["read_no"] = self.reads
        return tree

    def press(self, pid, handle):
        self.calls.append(("press", pid, handle))

    def focus(self, pid, handle):
        self.calls.append(("focus", pid, handle))

    def post_text(self, pid, text):
        self.calls.append(("post_text", pid, text))
        if self.on_post_text:
            self.on_post_text(self)

    def post_key(self, pid, keycode, modifiers):
        self.calls.append(("post_key", pid, keycode, tuple(modifiers)))

    def scroll(self, pid, area_handle, orientation, delta):
        self.calls.append(("scroll", pid, area_handle, orientation, delta))
        return 0.5, 0.5 + delta

    def native(self):
        return [c for c in self.calls if c[0] not in ("read_tree",)]


@pytest.fixture
def fake(monkeypatch):
    fb = FakeBackend()
    backend_mod.set_backend(fb)
    driver.clear_cache()
    monkeypatch.setattr(safety, "_killed", False)
    monkeypatch.setattr(safety, "_state", {"active": False, "app": None, "action": None, "since": None, "last_action_at": None})
    monkeypatch.setattr(driver, "_sleep", lambda s: None)
    safety._recent.clear()
    events = []
    execution_policy.set_action_sink(lambda kind, st, sid, actor, payload: events.append((kind, sid, actor, payload)))
    fb.events = events
    yield fb
    execution_policy.set_action_sink(None)
    backend_mod.set_backend(None)
    driver.clear_cache()


@pytest.fixture
def allowed(fake):
    policy.allow("TextEdit", "com.apple.TextEdit", by="test")
    return fake


def snap_id(fb):
    return driver.snapshot("TextEdit")["snapshot_id"]


# Policy.

class TestPolicy:
    def test_nothing_is_allowed_by_default(self, fake):
        assert policy.list_allowed() == []
        with pytest.raises(AppDriverError) as e:
            driver.snapshot("TextEdit")
        assert e.value.code == "not_allowed"
        assert fake.reads == 0

    @pytest.mark.parametrize("name,bundle", [
        ("Terminal", None), ("iTerm2", "com.googlecode.iterm2"), ("System Settings", None),
        ("Keychain Access", None), ("Passwords", None), ("1Password 7", None), ("Bitwarden", None),
        ("Dourmouse", None), (None, "com.dourmouse.app"), ("Script Editor", None), ("Shortcuts", None),
        ("Terminal.app", None), ("Proton Pass", None),
    ])
    def test_deny_list_cannot_be_allowed(self, fake, name, bundle):
        with pytest.raises(AppDriverError) as e:
            policy.allow(name or "", bundle)
        assert e.value.code == "denied"
        assert policy.list_allowed() == []

    def test_own_process_is_denied(self):
        assert policy.deny_reason("Whatever", "x.y", os.getpid())
        assert policy.deny_reason("Whatever", "x.y", os.getppid())
        assert policy.deny_reason("TextEdit", "com.apple.TextEdit", 4242) is None

    def test_hand_edited_allow_file_cannot_unlock_a_denied_app(self, fake):
        path = policy.allow_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "apps": [{"name": "Terminal", "bundle_id": "com.apple.Terminal"}]}))
        with pytest.raises(AppDriverError) as e:
            driver.snapshot("Terminal")
        assert e.value.code == "denied"
        assert fake.reads == 0

    def test_allow_persists_in_the_config_dir(self, fake):
        policy.allow("TextEdit", "com.apple.TextEdit")
        data = json.loads(policy.allow_file().read_text())
        assert data["apps"][0]["bundle_id"] == "com.apple.TextEdit"
        assert policy.is_allowed("textedit", "com.apple.TextEdit")

    def test_a_renamed_impostor_does_not_inherit_the_grant(self, fake):
        policy.allow("TextEdit", "com.apple.TextEdit")
        assert not policy.is_allowed("TextEdit", "com.evil.textedit")

    def test_a_corrupt_allow_file_allows_nothing(self, fake):
        path = policy.allow_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        with pytest.raises(AppDriverError) as e:
            policy.check_drivable("TextEdit", "com.apple.TextEdit", 4242)
        assert e.value.code == "failed"

    def test_disallow_removes(self, fake):
        policy.allow("TextEdit", "com.apple.TextEdit")
        assert policy.disallow("TextEdit") is True
        assert policy.list_allowed() == []
        assert policy.disallow("TextEdit") is False


# Kill switch.

class TestKillSwitch:
    def test_memory_flag_stops_snapshot_and_act(self, allowed):
        sid = snap_id(allowed)
        safety.engage_kill("test", by="test")
        for call in (lambda: driver.snapshot("TextEdit"), lambda: driver.act(sid, "0.0", "click")):
            with pytest.raises(AppDriverError) as e:
                call()
            assert e.value.code == "killed"
        assert allowed.native() == []

    def test_file_flag_alone_stops_driving(self, allowed, monkeypatch):
        sid = snap_id(allowed)
        safety.kill_file().parent.mkdir(parents=True, exist_ok=True)
        safety.kill_file().write_text("{}")
        assert safety._killed is False and safety.is_killed()
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.0", "click")
        assert e.value.code == "killed"

    def test_release_clears_both(self, allowed):
        safety.engage_kill("x")
        assert safety.kill_file().exists()
        safety.release_kill(by="owner")
        assert not safety.is_killed() and not safety.kill_file().exists()
        driver.act(snap_id(allowed), "0.0", "click")

    def test_kill_mid_typing_stops_after_the_first_chunk(self, allowed):
        sid = snap_id(allowed)
        allowed.on_post_text = lambda fb: safety.engage_kill("stop")
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.1.0", "type", text="word " * 20)
        assert e.value.code == "killed" and "after 20 of 100" in str(e.value)
        assert len([c for c in allowed.calls if c[0] == "post_text"]) == 1


# Secret checks.

class TestSecrets:
    @pytest.mark.parametrize("text", ["Hunter2!x", "sk-ant-abcdefghijklmnopqrstuvwx", "Bearer abcdefghijklmnop1234",
                                      "a1b2c3d4e5f6g7h8i9j0k1", "password=supersecret123"])
    def test_secret_shaped_text_is_never_typed(self, allowed, text):
        sid = snap_id(allowed)
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.1.0", "type", text=text)
        assert e.value.code == "secret"
        assert allowed.native() == []

    def test_ordinary_sentences_are_typed(self, allowed):
        sid = snap_id(allowed)
        out = driver.act(sid, "0.1.0", "type", text="Meeting notes for Tuesday, 3 items.")
        assert out["ok"] is True

    def test_secure_and_password_labelled_fields_are_refused(self, allowed):
        sid = snap_id(allowed)
        for eid in ("0.3", "0.4"):
            with pytest.raises(AppDriverError) as e:
                driver.act(sid, eid, "type", text="hello there")
            assert e.value.code == "secret"
        assert allowed.native() == []

    def test_looks_like_password(self):
        assert safety.looks_like_password("Tr0ub4dor&3")
        assert not safety.looks_like_password("hello world")
        assert not safety.looks_like_password("Hello")
        assert not safety.looks_like_password("2026-10-02")


# Snapshot.

class TestSnapshot:
    def test_not_trusted_is_honest_and_reads_nothing(self, allowed):
        allowed.trusted = False
        with pytest.raises(AppDriverError) as e:
            driver.snapshot("TextEdit")
        assert e.value.code == "not_trusted" and "Accessibility" in str(e.value)
        assert allowed.reads == 0

    def test_not_running(self, allowed):
        with pytest.raises(AppDriverError) as e:
            driver.snapshot("Music")
        assert e.value.code == "not_running" and "TextEdit" in str(e.value)

    def test_ids_secure_values_and_dlp(self, allowed):
        snap = driver.snapshot("TextEdit")
        by_id = {e["id"]: e for e in snap["elements"]}
        assert by_id["0"]["role"] == "AXWindow" and by_id["0.1.0"]["value"] == "hello"
        assert by_id["0.3"]["secure"] is True and by_id["0.3"]["value"] is None
        assert "sk-ant" not in by_id["0.5"]["value"] and "REDACTED" in by_id["0.5"]["value"]
        assert all("handle" not in e for e in snap["elements"])
        assert snap["app"] == {"name": "TextEdit", "bundle_id": "com.apple.TextEdit", "pid": 4242}

    def test_bundle_id_query_works(self, allowed):
        assert driver.snapshot("com.apple.TextEdit")["count"] == 8


# Re-resolve and actions.

class TestAct:
    def test_click_uses_the_freshly_resolved_handle(self, allowed):
        sid = snap_id(allowed)
        out = driver.act(sid, "0.0", "click")
        assert out["ok"] and "Save" in out["detail"]
        press = [c for c in allowed.calls if c[0] == "press"]
        assert press == [("press", 4242, ("h", 0, 0))]
        assert allowed.reads == 2  # snapshot + re-read before acting

    def test_sibling_inserted_means_refusal(self, allowed):
        sid = snap_id(allowed)
        allowed.tree["windows"][0]["children"].insert(0, n("AXButton", "Delete All"))
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.0", "click")
        assert e.value.code == "tree_changed"
        assert allowed.native() == []

    def test_target_renamed_means_refusal(self, allowed):
        sid = snap_id(allowed)
        allowed.tree["windows"][0]["children"][0]["title"] = "Delete"
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.0", "click")
        assert e.value.code == "tree_changed"

    def test_a_value_change_is_not_a_structure_change(self, allowed):
        sid = snap_id(allowed)
        allowed.tree["windows"][0]["children"][1]["children"][0]["value"] = "hello, edited"
        assert driver.act(sid, "0.0", "click")["ok"]

    def test_relaunched_app_means_stale(self, allowed):
        sid = snap_id(allowed)
        allowed.apps[0]["pid"] = 9999
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.0", "click")
        assert e.value.code == "stale"

    def test_unknown_and_expired_snapshots(self, allowed, monkeypatch):
        with pytest.raises(AppDriverError) as e:
            driver.act("nope", "0.0", "click")
        assert e.value.code == "stale"
        sid = snap_id(allowed)
        real = driver._monotonic
        monkeypatch.setattr(driver, "_monotonic", lambda: real() + driver.SNAPSHOT_TTL + 1)
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.0", "click")
        assert e.value.code == "stale"

    def test_revoked_between_snapshot_and_act(self, allowed):
        sid = snap_id(allowed)
        policy.disallow("TextEdit")
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.0", "click")
        assert e.value.code == "not_allowed"

    def test_disabled_and_missing_elements(self, allowed):
        sid = snap_id(allowed)
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.2", "click")
        assert e.value.code == "invalid" and "disabled" in str(e.value)
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.99", "click")
        assert e.value.code == "invalid"

    def test_type_focuses_then_posts_chunks_to_the_pid(self, allowed):
        sid = snap_id(allowed)
        driver.act(sid, "0.1.0", "type", text="x" * 45)
        native = allowed.native()
        assert native[0] == ("focus", 4242, ("h", 0, 1, 0))
        assert native[1] == ("activate", 4242)
        posts = [c for c in native if c[0] == "post_text"]
        assert [len(c[2]) for c in posts] == [20, 20, 5] and all(c[1] == 4242 for c in posts)

    def test_owner_switching_apps_stops_typing(self, allowed):
        sid = snap_id(allowed)

        def switch(fb):
            fb.front = 1

        allowed.on_post_text = switch
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.1.0", "type", text="y" * 60)
        assert e.value.code == "not_frontmost" and "after 20 of 60" in str(e.value)

    def test_app_that_never_comes_forward_gets_nothing_typed(self, allowed):
        sid = snap_id(allowed)
        allowed.activate_brings_front = False
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.1.0", "type", text="hello")
        assert e.value.code == "not_frontmost"
        assert not [c for c in allowed.calls if c[0] == "post_text"]

    def test_dry_run_does_nothing_native(self, allowed):
        sid = snap_id(allowed)
        out = driver.act(sid, "0.0", "click", dry_run=True)
        assert out["dry_run"] and "DRY RUN" in out["detail"]
        assert allowed.native() == []

    def test_scroll_finds_the_enclosing_scroll_area(self, allowed):
        sid = snap_id(allowed)
        out = driver.act(sid, "0.1.0", "scroll", direction="up", amount=0.5)
        assert ("scroll", 4242, ("h", 0, 1), "vertical", -0.5) in allowed.calls
        assert "scrolled up" in out["detail"]
        with pytest.raises(AppDriverError) as e:
            driver.act(sid, "0.0", "scroll")
        assert e.value.code == "invalid"

    def test_press_key(self, allowed):
        out = driver.press_key("TextEdit", "return", ["command"])
        assert ("post_key", 4242, 36, ("command",)) in allowed.calls and out["ok"]
        with pytest.raises(AppDriverError) as e:
            driver.press_key("TextEdit", "f13")
        assert e.value.code == "invalid"
        with pytest.raises(AppDriverError) as e:
            driver.press_key("Terminal", "return")
        assert e.value.code == "denied"
        assert not [c for c in allowed.calls if c[0] == "post_key" and c[1] == 5151]


# Indicator and audit.

class TestIndicatorAndAudit:
    def test_indicator_during_and_after(self, allowed):
        seen = []
        safety.add_indicator_listener(seen.append)
        try:
            sid = snap_id(allowed)

            def check(fb):
                state = safety.indicator()
                assert state["active"] and state["driving"] and state["label"] == "Model is driving TextEdit"

            allowed.on_post_text = check
            driver.act(sid, "0.1.0", "type", text="hi there")
        finally:
            safety.remove_indicator_listener(seen.append)
        after = safety.indicator()
        assert after["active"] is False and after["driving"] is True  # lingers briefly
        assert any(s["active"] for s in seen) and seen[-1]["active"] is False
        safety.engage_kill("x")
        assert safety.indicator()["driving"] is False

    def test_every_action_is_audited_without_raw_text(self, allowed):
        sid = snap_id(allowed)
        driver.act(sid, "0.1.0", "type", text="private meeting words")
        with pytest.raises(AppDriverError):
            driver.act(sid, "0.0", "scroll")
        kinds = [(k, s) for k, s, _, _ in allowed.events]
        assert ("action.executed", "app_driver.snapshot") in kinds
        assert ("action.executed", "app_driver.type") in kinds
        assert ("action.denied", "app_driver.scroll") in kinds
        assert "private meeting words" not in json.dumps([e[3] for e in allowed.events])


# Tools.

class TestTools:
    def test_specs_and_permissions(self):
        from dourmouse.dispatch import Permission

        specs = {s.name: s for s in tools.build_app_driver_tools()}
        assert set(specs) == {"app_driver_status", "app_driver_snapshot", "app_driver_stop", "app_driver_click",
                              "app_driver_type", "app_driver_press_key", "app_driver_scroll"}
        for name in tools.CONFIRMATION_TOOL_NAMES:
            assert specs[name].permission is Permission.REQUIRES_CONFIRMATION and specs[name].confirm_prompt
        for name in ("app_driver_status", "app_driver_snapshot", "app_driver_stop"):
            assert specs[name].permission is Permission.REGULAR
        assert not any(w in " ".join(specs) for w in ("allow", "resume", "deny"))

    def test_confirm_prompt_names_the_element(self, allowed):
        sid = snap_id(allowed)
        specs = {s.name: s for s in tools.build_app_driver_tools()}
        prompt = specs["app_driver_click"].confirm_prompt({"snapshot_id": sid, "element_id": "0.0"})
        assert "button 'Save'" in prompt and "TextEdit" in prompt
        prompt = specs["app_driver_type"].confirm_prompt({"snapshot_id": sid, "element_id": "0.1.0", "text": "abc"})
        assert "'abc'" in prompt

    def test_handlers_refuse_honestly_and_snapshot_formats(self, fake):
        specs = {s.name: s for s in tools.build_app_driver_tools()}
        out = specs["app_driver_snapshot"].handler({"app_name": "TextEdit"})
        assert out.startswith("REFUSED:") and "allow list" in out
        policy.allow("TextEdit", "com.apple.TextEdit")
        out = specs["app_driver_snapshot"].handler({"app_name": "TextEdit"})
        assert out.startswith("SNAPSHOT ") and "0.0 AXButton title='Save'" in out and "<secure field, not read>" in out
        fake.trusted = False
        assert specs["app_driver_snapshot"].handler({"app_name": "TextEdit"}).startswith("ERROR:")

    def test_stop_tool_engages_kill_and_status_reports_it(self, fake):
        specs = {s.name: s for s in tools.build_app_driver_tools()}
        assert specs["app_driver_stop"].handler({}).startswith("STOPPED")
        assert json.loads(specs["app_driver_status"].handler({}))["kill_switch_engaged"] is True

    def test_dry_run_setting_is_honoured(self, allowed, monkeypatch):
        monkeypatch.setattr("dourmouse.config.app_control_dry_run_enabled", lambda: True)
        sid = snap_id(allowed)
        specs = {s.name: s for s in tools.build_app_driver_tools()}
        out = specs["app_driver_click"].handler({"snapshot_id": sid, "element_id": "0.0"})
        assert out.startswith("DRY RUN") and allowed.native() == []


# The osascript fallback, with a fake runner (no osascript is executed).

class TestOsaBackend:
    def test_inputs_travel_as_json_and_errors_map(self):
        seen = []

        class Proc:
            def __init__(self, out, rc=0, err=""):
                self.stdout, self.returncode, self.stderr = out, rc, err

        replies = iter([Proc(json.dumps({"ok": True})), Proc(json.dumps({"error": "not_frontmost"})),
                        Proc("", 1, "execution error: osascript is not allowed assistive access. (-1719)")])

        def runner(argv, timeout):
            seen.append(argv)
            return next(replies)

        osa = backend_mod.OsaBackend(runner)
        osa.post_text(4242, 'x"; do shell script "rm -rf ~')
        assert json.loads(seen[0][-1]) == {"op": "text", "pid": 4242, "text": 'x"; do shell script "rm -rf ~'}
        assert seen[0][-2] == backend_mod.JXA_PROGRAM  # the script text never contains the input
        with pytest.raises(AppDriverError) as e:
            osa.post_text(4242, "a")
        assert e.value.code == "not_frontmost"
        assert osa.is_trusted() is False

    @pytest.mark.skipif(not os.path.exists("/usr/bin/osacompile"), reason="osacompile is macOS only")
    def test_jxa_program_compiles_without_running(self, tmp_path):
        import subprocess

        out = tmp_path / "prog.scpt"
        proc = subprocess.run(["/usr/bin/osacompile", "-l", "JavaScript", "-o", str(out), "-e", backend_mod.JXA_PROGRAM],
                              capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, check=False)
        assert proc.returncode == 0, proc.stderr
