"""Finding #136: the console's security actions carry identity and take the lock."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from dourmouse.security.web_actions import handle_action


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(0.2)
    yield proc
    if proc.poll() is None:
        proc.kill()


class TestKillCarriesIdentity:
    def test_the_identity_lookup_says_who_the_pid_is(self, sleeper):
        out = handle_action({"action": "process_identity", "pid": sleeper.pid})
        assert out["ok"] and out["pid"] == sleeper.pid and out["name"] and out["create_time"]

    def test_a_kill_with_the_looked_up_identity_stops_it(self, sleeper):
        who = handle_action({"action": "process_identity", "pid": sleeper.pid})
        out = handle_action({
            "action": "kill_process", "pid": sleeper.pid, "expect_name": who["name"],
            "expect_create_time": who["create_time"], "expect_exe": who["exe"],
        })
        assert out["ok"] and sleeper.wait(timeout=5) is not None

    def test_a_pid_that_now_belongs_to_another_process_is_refused(self, sleeper):
        who = handle_action({"action": "process_identity", "pid": sleeper.pid})
        out = handle_action({"action": "kill_process", "pid": sleeper.pid, "expect_create_time": who["create_time"] - 500})
        assert out["ok"] is False and sleeper.poll() is None

    def test_a_kill_that_names_nobody_is_refused(self, sleeper):
        out = handle_action({"action": "kill_process", "pid": sleeper.pid})
        assert out["ok"] is False and sleeper.poll() is None

    def test_a_bad_identity_type_is_a_clean_refusal(self, sleeper):
        out = handle_action({"action": "kill_process", "pid": sleeper.pid, "expect_create_time": "yesterday"})
        assert out["ok"] is False and "number" in out["error"]


class TestLockdownEdits:
    def test_add_and_remove_round_trip(self, config_dir):
        added = handle_action({"action": "lockdown_add", "kind": "site", "entry": "example-time-waster.com"})
        assert added["ok"]
        saved = json.loads((config_dir / "lockdown.json").read_text())
        assert [s["domain"] for s in saved["sites"]] == ["example-time-waster.com"]
        removed = handle_action({"action": "lockdown_remove", "entry": "example-time-waster.com"})
        assert removed["ok"]
        assert json.loads((config_dir / "lockdown.json").read_text())["sites"] == []

    def test_an_app_the_mac_depends_on_is_refused_not_saved(self, config_dir):
        out = handle_action({"action": "lockdown_add", "kind": "app", "entry": "Finder"})
        assert out["ok"] is False and "error" in out
        assert not (config_dir / "lockdown.json").exists() or json.loads((config_dir / "lockdown.json").read_text())["apps"] == []

    def test_a_name_macos_needs_is_refused_not_saved(self, config_dir):
        out = handle_action({"action": "lockdown_add", "kind": "site", "entry": "apple.com"})
        assert out["ok"] is False


class TestLockdownUrlKind:
    def test_a_url_is_added_and_removed_by_the_console(self, config_dir):
        added = handle_action({"action": "lockdown_add", "kind": "url", "entry": "https://reddit.com/r/all"})
        assert added["ok"] and added["added"]["url"] == "reddit.com/r/all"
        assert [u["url"] for u in json.loads((config_dir / "lockdown.json").read_text())["urls"]] == ["reddit.com/r/all"]
        assert handle_action({"action": "lockdown_remove", "entry": "reddit.com/r/all"})["ok"]
        assert json.loads((config_dir / "lockdown.json").read_text())["urls"] == []

    def test_a_bad_url_is_a_clean_refusal_and_an_unknown_kind_is_named(self, config_dir):
        out = handle_action({"action": "lockdown_add", "kind": "url", "entry": "reddit.com"})
        assert out["ok"] is False and "no path" in out["error"]
        out = handle_action({"action": "lockdown_add", "kind": "page", "entry": "x.com/a"})
        assert out["ok"] is False and "'url'" in out["error"]
