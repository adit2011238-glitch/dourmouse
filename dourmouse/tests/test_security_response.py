"""Finding #106 (MS-8): response actions. Each acts for real on a scratch
target, and each refusal is the one that protects the Mac or the owner."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from dourmouse.security import mac_telemetry as mt
from dourmouse.security import response as rs


class TestKill:
    def test_a_real_process_is_stopped(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            time.sleep(0.2)
            import psutil

            name = psutil.Process(proc.pid).name()
            r = rs.kill_process(proc.pid, expect_name=name)
            assert r["ok"] and r["result"].startswith("stopped") and proc.wait(timeout=5) is not None
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_a_process_that_ignores_sigterm_is_force_killed(self):
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout.readline().strip() == "ready"
            r = rs.kill_process(proc.pid, expect_create_time=rs.process_identity(proc.pid)["create_time"], grace=0.5)
            assert r["ok"] and r["result"].startswith("force-killed")
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_a_reused_pid_is_not_killed(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with pytest.raises(rs.ResponseRefused, match="reused"):
                rs.kill_process(proc.pid, expect_name="SomethingElse")
            assert proc.poll() is None
        finally:
            proc.kill()

    @pytest.mark.parametrize("pid", [0, 1, os.getpid()])
    def test_the_system_and_dourmouse_are_refused(self, pid):
        with pytest.raises(rs.ResponseRefused):
            rs.kill_process(pid)

    def test_a_protected_macos_process_is_refused(self, monkeypatch):
        import psutil

        class Fake:
            def __init__(self, pid):
                pass

            def name(self):
                return "WindowServer"

            def exe(self):
                return "/System/x"

            def username(self):
                return "_windowserver"

        monkeypatch.setattr(psutil, "Process", Fake)
        with pytest.raises(rs.ResponseRefused, match="part of macOS"):
            rs.kill_process(4242)


class TestQuarantine:
    def test_quarantine_and_restore_round_trip(self, tmp_path):
        f = tmp_path / "evil.command"
        f.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        f.chmod(0o755)
        q = rs.quarantine_file(f, reason="test")
        assert not f.exists() and q["sha256"] and q["mode"] == 0o755
        moved = rs.quarantine_dir() / q["id"] / "item" / "evil.command"
        assert moved.stat().st_mode & 0o777 == 0o400
        assert [m["id"] for m in rs.list_quarantine()][0] == q["id"]
        r = rs.restore(q["id"])
        assert r["ok"] and f.read_text(encoding="utf-8").endswith("echo hi\n") and f.stat().st_mode & 0o777 == 0o755
        assert all(m["id"] != q["id"] for m in rs.list_quarantine())

    def test_restore_never_overwrites(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"1")
        q = rs.quarantine_file(f)
        f.write_bytes(b"new")
        with pytest.raises(rs.ResponseRefused, match="already exists"):
            rs.restore(q["id"])
        assert f.read_bytes() == b"new"

    @pytest.mark.parametrize("path", ["/System/Library/CoreServices/Finder.app", "/usr/bin/true", "/etc/hosts",
                                      "/bin/ls", "/Applications/Utilities/Terminal.app"])
    def test_macos_itself_is_never_quarantined(self, path):
        with pytest.raises(rs.ResponseRefused):
            rs.quarantine_file(path)

    def test_links_home_and_bad_ids_are_refused(self, tmp_path):
        real = tmp_path / "r"
        real.write_text("x", encoding="utf-8")
        (tmp_path / "l").symlink_to(real)
        with pytest.raises(rs.ResponseRefused, match="link"):
            rs.quarantine_file(tmp_path / "l")
        with pytest.raises(rs.ResponseRefused, match="home folder"):
            rs.quarantine_file(os.path.expanduser("~"))
        for bad in ("", "../x", ".hidden", "nope"):
            with pytest.raises(rs.ResponseRefused):
                rs.restore(bad)


class TestStartupItems:
    def test_a_user_agent_is_unloaded_and_quarantined(self, tmp_path, monkeypatch):
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        plist = agents / "com.evil.agent.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        calls = []
        monkeypatch.setattr(mt, "persistence_dirs", lambda: [agents, tmp_path / "SysDaemons"])
        monkeypatch.setattr(rs, "user_agents_dir", lambda: agents)
        monkeypatch.setattr(rs, "_launchctl", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
        r = rs.disable_startup_item(plist)
        assert r["ok"] and not plist.exists() and calls[0][0] == "bootout"
        rs.restore(r["quarantine_id"])
        assert plist.exists()

    def test_a_system_item_returns_the_commands_and_changes_nothing(self, tmp_path, monkeypatch):
        daemons = tmp_path / "LaunchDaemons"
        daemons.mkdir()
        plist = daemons / "com.x.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(mt, "persistence_dirs", lambda: [tmp_path / "LaunchAgents", daemons])
        monkeypatch.setattr(rs, "_launchctl", lambda *a: pytest.fail("must not run launchctl"))
        r = rs.disable_startup_item(plist)
        assert not r["ok"] and r["needs_root"][0].startswith("sudo launchctl bootout system") and plist.exists()

    def test_anything_outside_a_startup_folder_is_refused(self, tmp_path, monkeypatch):
        f = tmp_path / "x.plist"
        f.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(mt, "persistence_dirs", lambda: [tmp_path / "LaunchAgents"])
        with pytest.raises(rs.ResponseRefused, match="not a launch agent"):
            rs.disable_startup_item(f)


class TestKillIdentity:
    """Security review S12: the pid alone is never enough."""

    def test_no_identity_kills_nothing(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with pytest.raises(rs.ResponseRefused, match="no identity"):
                rs.kill_process(proc.pid)
            assert proc.poll() is None
        finally:
            proc.kill()

    def test_a_different_start_time_or_exe_means_a_reused_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            who = rs.process_identity(proc.pid)
            with pytest.raises(rs.ResponseRefused, match="reused"):
                rs.kill_process(proc.pid, expect_create_time=who["create_time"] - 500)
            with pytest.raises(rs.ResponseRefused, match="reused"):
                rs.kill_process(proc.pid, expect_exe="/tmp/not-this-program")
            assert proc.poll() is None
            r = rs.kill_process(proc.pid, expect_create_time=who["create_time"], expect_exe=who["exe"])
            assert r["ok"] and proc.wait(timeout=5) is not None
        finally:
            if proc.poll() is None:
                proc.kill()


class TestQuarantineLayout:
    """Security review S08, S10."""

    def test_a_file_named_manifest_json_is_quarantined_and_restored(self, tmp_path):
        f = tmp_path / "manifest.json"
        f.write_text('{"name": "my extension", "version": "1"}', encoding="utf-8")
        q = rs.quarantine_file(f, reason="suspicious extension")
        assert not f.exists()
        assert [m["id"] for m in rs.list_quarantine() if m["id"] == q["id"]] == [q["id"]]
        assert rs.restore(q["id"])["ok"]
        assert f.read_text(encoding="utf-8") == '{"name": "my extension", "version": "1"}'

    def test_an_app_bundle_cannot_run_inside_quarantine_and_comes_back_exactly(self, tmp_path):
        app = tmp_path / "Evil.app"
        binary = app / "Contents" / "MacOS" / "Evil"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        binary.chmod(0o755)
        (app / "Contents" / "Info.plist").write_text("<plist/>", encoding="utf-8")
        helper_bin = app / "Contents" / "Resources" / "helper.sh"
        helper_bin.parent.mkdir()
        helper_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        helper_bin.chmod(0o4755)
        q = rs.quarantine_file(app, reason="test")
        stored = rs.quarantine_dir() / q["id"] / "item"
        assert [p.name for p in stored.iterdir()] == ["Evil.app.quarantined"]  # not an app any more
        for f in stored.rglob("*"):
            if f.is_file():
                assert f.stat().st_mode & 0o7111 == 0 and not os.access(f, os.X_OK)
        rs.restore(q["id"])
        assert binary.stat().st_mode & 0o7777 == 0o755
        assert helper_bin.stat().st_mode & 0o7777 == 0o4755
        assert not (tmp_path / "Evil.app.quarantined").exists()

    def test_a_damaged_manifest_is_refused_and_not_listed(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"1")
        q = rs.quarantine_file(f)
        mpath = rs.quarantine_dir() / q["id"] / "manifest.json"
        import json

        m = json.loads(mpath.read_text(encoding="utf-8"))
        m["name"] = "other"  # no longer the last part of original_path
        mpath.write_text(json.dumps(m), encoding="utf-8")
        assert all(x["id"] != q["id"] for x in rs.list_quarantine())
        with pytest.raises(rs.ResponseRefused, match="damaged"):
            rs.restore(q["id"])
        assert not f.exists()  # nothing was moved anywhere else
