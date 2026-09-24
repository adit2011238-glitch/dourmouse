"""Finding #103 (MS-13): lockdown. The root helper is tested adversarially
(it is the only root code in Dourmouse); the app enforcer closes a real
process."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from dourmouse.security import lockdown as ld
from dourmouse.security import lockdown_helper as helper


class TestHelperOnlyWritesBlocks:
    def test_injection_attempts_are_dropped(self):
        evil = ["good.com", "evil.com\n0.0.0.0 bank.com", "127.0.0.1 apple.com", "*.wild.com", "a b.com",
                "-bad.com", "x" * 300 + ".com", "UPPER.COM", 42, None, "localhost", "..", "ok-site.co.uk"]
        assert helper.valid_domains(evil) == ["good.com", "ok-site.co.uk", "upper.com"]
        assert helper.valid_domains("not a list") == []

    def test_block_format(self):
        block = helper.block_for(["example.com"])
        assert block.splitlines() == [helper.BEGIN, "0.0.0.0 example.com", ":: example.com",
                                      "0.0.0.0 www.example.com", ":: www.example.com", helper.END]

    def test_apply_replaces_only_its_own_block_and_is_idempotent(self):
        original = "##\n127.0.0.1\tlocalhost\n255.255.255.255\tbroadcasthost\n::1             localhost\n"
        once = helper.apply_block(original, ["a.com"])
        assert once.startswith(original) and "0.0.0.0 a.com" in once
        assert helper.apply_block(once, ["a.com"]) == once
        twice = helper.apply_block(once, ["b.com"])
        assert "a.com" not in twice and "0.0.0.0 b.com" in twice and twice.count(helper.BEGIN) == 1
        assert helper.apply_block(twice, []) == original

    def test_request_file_must_be_a_real_small_file(self, tmp_path):
        real = tmp_path / "req.json"
        real.write_text(json.dumps({"domains": ["a.com", "bad domain"]}), encoding="utf-8")
        assert helper.read_request(str(real)) == ["a.com"]
        link = tmp_path / "link.json"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="regular file"):
            helper.read_request(str(link))
        big = tmp_path / "big.json"
        big.write_bytes(b" " * (helper.MAX_REQUEST_BYTES + 1))
        with pytest.raises(ValueError, match="too large"):
            helper.read_request(str(big))

    def test_apply_end_to_end_on_a_scratch_hosts_file(self, tmp_path, monkeypatch):
        hosts = tmp_path / "hosts"
        hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
        req = tmp_path / "req.json"
        req.write_text(json.dumps({"domains": ["distracting.example"]}), encoding="utf-8")
        flushed = []
        monkeypatch.setattr(helper, "HOSTS", str(hosts))
        monkeypatch.setattr(helper, "write_hosts", lambda text: hosts.write_text(text, encoding="utf-8"))
        monkeypatch.setattr(helper, "flush_dns", lambda: flushed.append(1))
        assert helper.do_apply(str(req)) == 1
        assert "0.0.0.0 distracting.example" in hosts.read_text(encoding="utf-8") and flushed == [1]
        req.unlink()  # no request file: the block is removed
        helper.do_apply(str(req))
        assert hosts.read_text(encoding="utf-8") == "127.0.0.1 localhost\n"

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS paths")
    def test_install_location_is_root_owned_all_the_way_up(self, tmp_path):
        assert helper.only_root_can_write(os.path.dirname(helper.INSTALLED))
        assert not helper.only_root_can_write(str(tmp_path))

    def test_the_helper_runs_on_python_3_9(self):
        import ast
        from pathlib import Path

        src = Path(helper.__file__).read_text(encoding="utf-8")
        ast.parse(src, feature_version=(3, 9))
        assert "from __future__" not in src and "match " not in src


class TestBlocklist:
    def test_urls_become_blockable_domains_and_paths_are_named_as_ignored(self):
        assert ld.normalize_site("https://www.YouTube.com/shorts/abc") == {
            "entry": "https://www.YouTube.com/shorts/abc", "domain": "youtube.com", "path_ignored": "/shorts/abc"}
        assert ld.normalize_site("reddit.com")["domain"] == "reddit.com"
        with pytest.raises(ValueError):
            ld.normalize_site("not a site")

    def test_add_remove_save_load(self, tmp_path):
        bl = ld.Blocklist()
        bl.add_site("youtube.com")
        bl.add_site("https://www.youtube.com/watch")  # same domain, not duplicated
        bl.add_app("com.example.Game")
        p = tmp_path / "l.json"
        bl.save(p)
        loaded = ld.Blocklist.load(p)
        assert [s["domain"] for s in loaded.sites] == ["youtube.com"]
        assert loaded.apps[0]["bundle_id"] == "com.example.Game"
        assert loaded.remove("youtube.com") and loaded.sites == []

    @pytest.mark.skipif(not os.path.exists("/Applications/Safari.app"), reason="needs Safari")
    def test_an_installed_app_resolves_to_its_bundle(self):
        app = ld.resolve_app("Safari")
        assert app["bundle_id"] == "com.apple.Safari" and app["path"] == "/Applications/Safari.app"

    def test_start_and_stop_write_the_helpers_request(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ld, "config_path", lambda: tmp_path / "l.json")
        monkeypatch.setattr(ld, "hosts_request_path", lambda: tmp_path / "req.json")
        monkeypatch.setattr(ld, "site_is_blocked", lambda d: False)
        bl = ld.Blocklist()
        bl.add_site("example.org")
        st = ld.start(bl)
        assert json.loads((tmp_path / "req.json").read_text())["domains"] == ["example.org"]
        assert st["active"] and st["sites"][0]["blocked_now"] is False
        if not st["helper_installed"]:
            assert "NOT blocked yet" in st["limits"][0] and "sudo /usr/bin/python3" in st["limits"][0]
        ld.stop(bl)
        assert json.loads((tmp_path / "req.json").read_text())["domains"] == []


class TestAppEnforcer:
    def test_a_blocked_app_is_closed_and_the_user_told(self, tmp_path):
        # A real standalone program under the probe's own name: a copy of the
        # actual interpreter binary (the venv's python is a stub that re-runs
        # the framework Python, and a copied /bin/sleep is killed by macOS on
        # launch because system binaries only run from where they belong).
        import psutil

        real_python = psutil.Process().exe()
        probe = tmp_path / "DourmouseBlockProbe"
        shutil.copy(real_python, probe)
        proc = subprocess.Popen([str(probe), "-c", "import time; time.sleep(60)"])
        try:
            time.sleep(0.3)
            bl = ld.Blocklist(apps=[{"name": "DourmouseBlockProbe", "bundle_id": "", "path": ""}], active=True)
            told = []
            enforcer = ld.AppEnforcer(blocklist_loader=lambda: bl, notify=told.append)
            closed = enforcer.sweep()
            assert [c["app"] for c in closed] == ["DourmouseBlockProbe"]
            assert proc.wait(timeout=5) is not None
            assert "blocked while lockdown is on" in told[0]
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_nothing_is_closed_when_lockdown_is_off(self):
        bl = ld.Blocklist(apps=[{"name": "Finder", "bundle_id": "", "path": ""}], active=False)
        assert ld.AppEnforcer(blocklist_loader=lambda: bl, notify=lambda m: None).sweep() == []

    def test_process_matching(self):
        app = {"name": "Discord", "bundle_id": "", "path": "/Applications/Discord.app"}
        assert ld.process_matches(app, "/Applications/Discord.app/Contents/MacOS/Discord", "Discord")
        assert ld.process_matches(app, "/Applications/Discord.app/Contents/Frameworks/Helper.app/x", "Helper")
        assert not ld.process_matches(app, "/Applications/Slack.app/Contents/MacOS/Slack", "Slack")


def test_a_security_block_stays_on_when_lockdown_is_off(tmp_path, monkeypatch):
    """Finding #106: block_domain_always is independent of lockdown."""
    monkeypatch.setattr(ld, "config_path", lambda: tmp_path / "l.json")
    monkeypatch.setattr(ld, "hosts_request_path", lambda: tmp_path / "req.json")
    bl = ld.Blocklist()
    bl.add_site("youtube.com")
    ld.block_domain_always("https://evil-phish.example/login", reason="phishing", bl=bl)
    req = lambda: json.loads((tmp_path / "req.json").read_text())["domains"]  # noqa: E731
    assert req() == ["evil-phish.example"]  # lockdown off: only the security block
    ld.start(bl)
    assert req() == ["evil-phish.example", "youtube.com"]
    ld.stop(bl)
    assert req() == ["evil-phish.example"]
    assert ld.Blocklist.load(tmp_path / "l.json").always[0]["reason"] == "phishing"
    ld.unblock_domain_always("evil-phish.example", bl=bl)
    assert req() == []
