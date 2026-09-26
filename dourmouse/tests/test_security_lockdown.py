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


class TestHelperRefusesUnsafeRequests:
    """Security review S04, S28: the request file is user-writable, so the
    root helper distrusts it."""

    @pytest.mark.parametrize("name", [
        "ocsp.apple.com", "swscan.apple.com", "apple.com", "mail.icloud.com", "ocsp.digicert.com", "crl.example.com",
        "ocsp2.example.org", "127.0.0.1", "1.2.3.4", "a.com # x", "a.com\n", "host.local", "x.localhost",
    ])
    def test_reserved_and_malformed_names_are_never_written(self, name):
        assert helper.valid_domains(["ok.example", name]) == ["ok.example"]

    def test_too_many_names_is_refused_not_cut_short(self):
        with pytest.raises(ValueError, match="more than"):
            helper.valid_domains([f"h{i}.example" for i in range(helper.MAX_DOMAINS + 1)])

    def test_a_fifo_does_not_hang_the_helper(self, tmp_path):
        fifo = tmp_path / "req.json"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="regular file"):
            helper.read_request(str(fifo))

    def test_a_file_owned_by_someone_else_or_writable_by_others_is_refused(self, tmp_path):
        req = tmp_path / "req.json"
        req.write_text(json.dumps({"domains": ["a.example"]}), encoding="utf-8")
        assert helper.read_request(str(req), os.getuid()) == ["a.example"]
        with pytest.raises(ValueError, match="not owned"):
            helper.read_request(str(req), os.getuid() + 1)
        req.chmod(0o666)
        with pytest.raises(ValueError, match="writable"):
            helper.read_request(str(req), os.getuid())

    @pytest.mark.parametrize("content", ['{"domains": ["a.exa', b"\xff\xfe\x00", "[]", '"text"'])
    def test_a_corrupt_request_clears_the_block_instead_of_crashing(self, tmp_path, monkeypatch, content):
        hosts = tmp_path / "hosts"
        hosts.write_text("127.0.0.1 localhost\n" + helper.block_for(["stale.example"]), encoding="utf-8")
        req = tmp_path / "req.json"
        req.write_bytes(content if isinstance(content, bytes) else content.encode())
        monkeypatch.setattr(helper, "HOSTS", str(hosts))
        monkeypatch.setattr(helper, "write_hosts", lambda text: hosts.write_text(text, encoding="utf-8"))
        monkeypatch.setattr(helper, "flush_dns", lambda: None)
        assert helper.do_apply(str(req)) == 0
        assert hosts.read_text(encoding="utf-8") == "127.0.0.1 localhost\n"

    def test_a_symlink_swapped_in_after_the_check_is_not_followed(self, tmp_path):
        real = tmp_path / "real.json"
        real.write_text(json.dumps({"domains": ["a.example"]}), encoding="utf-8")
        link = tmp_path / "req.json"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="link"):
            helper.read_request(str(link))


class TestSitesAndCopy:
    def test_ip_literals_and_macos_hosts_cannot_be_blocklisted(self):
        for bad in ("127.0.0.1", "https://10.0.0.5/x", "ocsp.apple.com", "icloud.com"):
            with pytest.raises(ValueError):
                ld.normalize_site(bad)

    def test_status_says_exactly_which_names_are_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ld, "site_is_blocked", lambda d: d == "youtube.com")
        bl = ld.Blocklist(active=True)
        bl.add_site("youtube.com")
        st = ld.status(bl)
        assert st["sites"][0]["blocks"] == ["youtube.com", "www.youtube.com"]
        assert st["sites"][0]["blocked_now"] is False  # www still resolves: not "blocked"
        assert "other subdomains" in " ".join(st["limits"])


class TestCorruptState:
    """Security review S06, S25: a damaged state file never wedges lockdown."""

    @pytest.mark.parametrize("content", ['{"active": tr', b"\xff\xfe\x00", "[]"])
    def test_a_corrupt_file_loads_empty_and_inactive_with_a_warning(self, tmp_path, content):
        p = tmp_path / "l.json"
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        bl = ld.Blocklist.load(p)
        assert not bl.active and bl.apps == [] and "unreadable" in bl.load_warning
        assert (tmp_path / "l.json.corrupt").exists() and not p.exists()

    def test_ending_lockdown_works_after_corruption_and_says_why(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ld, "config_path", lambda: tmp_path / "l.json")
        monkeypatch.setattr(ld, "hosts_request_path", lambda: tmp_path / "req.json")
        (tmp_path / "l.json").write_text('{"active": true, "sites": [{"dom', encoding="utf-8")
        (tmp_path / "req.json").write_text(json.dumps({"domains": ["old.example"]}), encoding="utf-8")
        st = ld.stop()
        assert not st["active"] and "unreadable" in st["warnings"][0]
        assert json.loads((tmp_path / "req.json").read_text())["domains"] == []

    def test_the_enforcer_reports_a_damaged_state_and_clears_the_hosts_block(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ld, "config_path", lambda: tmp_path / "l.json")
        monkeypatch.setattr(ld, "hosts_request_path", lambda: tmp_path / "req.json")
        (tmp_path / "l.json").write_text("{", encoding="utf-8")
        told = []
        ld.AppEnforcer(notify=told.append).sweep()
        assert "unreadable" in told[0]
        assert json.loads((tmp_path / "req.json").read_text())["domains"] == []

    def test_a_failed_save_leaves_the_old_file_intact(self, tmp_path, monkeypatch):
        p = tmp_path / "l.json"
        bl = ld.Blocklist()
        bl.add_site("keep.example")
        bl.save(p)
        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        bl.add_site("other.example")
        with pytest.raises(OSError, match="disk full"):
            bl.save(p)
        monkeypatch.undo()
        assert [s["domain"] for s in ld.Blocklist.load(p).sites] == ["keep.example"]
        assert [f.name for f in tmp_path.iterdir()] == ["l.json"]  # no temp file left behind

    def test_concurrent_edits_do_not_lose_each_other(self, tmp_path, monkeypatch):
        import threading

        monkeypatch.setattr(ld, "config_path", lambda: tmp_path / "l.json")

        def add(i):
            with ld.locked_blocklist() as bl:
                bl.add_site(f"site{i}.example")
                bl.save()

        threads = [threading.Thread(target=add, args=(i,)) for i in range(25)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(ld.Blocklist.load().sites) == 25


class _FakeProc:
    def __init__(self, pid, name, exe):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "exe": exe}
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout):
        return 0

    def kill(self):
        self.signals.append("KILL")


class TestEnforcerNeverClosesTheSystem:
    """Security review S03."""

    @pytest.mark.parametrize("entry", ["Finder", "Dock", "Terminal", "iTerm", "Python", "python3.14", "com.apple.finder",
                                       "loginwindow", "Dourmouse"])
    def test_protected_apps_cannot_be_blocklisted(self, entry):
        with pytest.raises(ValueError, match="cannot be blocked"):
            ld.Blocklist().add_app(entry)

    def test_a_protected_entry_already_on_disk_is_never_closed(self, monkeypatch):
        import psutil

        procs = [
            _FakeProc(501, "Finder", "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder"),
            _FakeProc(502, "Terminal", "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
            _FakeProc(503, "Python", sys.executable),
            _FakeProc(504, "python3", "/opt/homebrew/bin/python3"),
            _FakeProc(505, "Dock", "/System/Library/CoreServices/Dock.app/Contents/MacOS/Dock"),
        ]
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
        bl = ld.Blocklist(active=True, apps=[{"name": n, "bundle_id": "", "path": ""}
                                              for n in ("Finder", "Terminal", "Python", "python3", "Dock")])
        told = []
        assert ld.AppEnforcer(blocklist_loader=lambda: bl, notify=told.append).sweep() == []
        assert all(p.signals == [] for p in procs) and told == []

    def test_the_interpreter_running_the_server_is_never_closed(self, monkeypatch):
        import psutil

        me = _FakeProc(os.getpid() + 100000, "Server", sys.executable)
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter([me]))
        bl = ld.Blocklist(active=True, apps=[{"name": "Server", "bundle_id": "", "path": ""}])
        assert ld.AppEnforcer(blocklist_loader=lambda: bl, notify=lambda m: None).sweep() == []
        assert me.signals == []

    def test_a_name_fragment_does_not_match(self):
        app = {"name": "Discord", "bundle_id": "", "path": ""}
        assert not ld.process_matches(app, None, "Discord Helper")
        assert not ld.process_matches(app, None, "notdiscord")
        assert ld.process_matches(app, None, "discord")
        with_id = {"name": "Discord", "bundle_id": "com.hnc.Discord", "path": ""}
        assert not ld.process_matches(with_id, "/Applications/Other.app/Contents/MacOS/Discord", "Discord")

    def test_an_ordinary_app_is_still_closed(self, monkeypatch):
        import psutil

        game = _FakeProc(600, "Game", "/Applications/Game.app/Contents/MacOS/Game")
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter([game]))
        bl = ld.Blocklist(active=True, apps=[{"name": "Game", "bundle_id": "", "path": "/Applications/Game.app"}])
        closed = ld.AppEnforcer(blocklist_loader=lambda: bl, notify=lambda m: None).sweep()
        assert [c["pid"] for c in closed] == [600]


class TestUrlPathEntries:
    def test_a_url_is_normalised_to_host_plus_path_prefix(self):
        row = ld.normalize_url("https://WWW.Reddit.com/r/all?sort=top#x")
        assert (row["host"], row["path"], row["url"]) == ("reddit.com", "/r/all", "reddit.com/r/all")
        assert ld.normalize_url("reddit.com/r/all")["url"] == "reddit.com/r/all"
        assert ld.normalize_url("http://example.com/feed")["url"] == "example.com/feed"

    @pytest.mark.parametrize("bad", [
        "", "reddit.com", "https://reddit.com/", "ftp://example.com/x", "javascript:alert(1)",
        "https://user:pw@example.com/x", "https://user@example.com/x", "https://example.com/a b",
        "https://127.0.0.1/x", "https://[::1]/x", "https://localhost/x", "https://apple.com/x",
        "https://example.com:8080/x", "https://example.com/a*b", "https://example.com/a^b", "https://example.com/a|b",
        "https://example.com/" + "a" * 200, "https://-bad.com/x", "https://no-dot/x",
    ])
    def test_unsafe_or_unsupported_urls_are_refused(self, bad):
        with pytest.raises(ValueError):
            ld.normalize_url(bad)

    def test_a_path_of_exactly_the_limit_is_accepted(self):
        assert ld.normalize_url("example.com/" + "a" * 199)["path"] == "/" + "a" * 199

    def test_the_count_is_capped_and_duplicates_are_not_added_twice(self):
        bl = ld.Blocklist()
        bl.add_url("example.com/a")
        bl.add_url("https://www.example.com/a?x=1")
        assert len(bl.urls) == 1
        for i in range(ld.MAX_URLS - 1):
            bl.add_url(f"example.com/p{i}")
        assert len(bl.urls) == ld.MAX_URLS
        with pytest.raises(ValueError, match="200"):
            bl.add_url("example.com/one-too-many")
        bl.add_url("example.com/a")  # a duplicate of an entry at the cap is still fine

    def test_urls_survive_save_and_load_and_old_files_load_without_them(self, tmp_path):
        p = tmp_path / "l.json"
        bl = ld.Blocklist()
        bl.add_url("reddit.com/r/all")
        bl.save(p)
        assert [u["url"] for u in ld.Blocklist.load(p).urls] == ["reddit.com/r/all"]
        p.write_text(json.dumps({"apps": [], "sites": [{"entry": "a.com", "domain": "a.com"}], "active": True}))
        old = ld.Blocklist.load(p)
        assert old.urls == [] and old.active and [s["domain"] for s in old.sites] == ["a.com"]

    def test_a_hand_edited_file_cannot_smuggle_a_filter_in(self, tmp_path):
        p = tmp_path / "l.json"
        p.write_text(json.dumps({"urls": [
            {"entry": "https://evil.com/a*", "url": "evil.com/a*"}, {"entry": "ok.com/x", "url": "*"}, "junk", 3,
            {"entry": "https://apple.com/x"}, {"url": "no-entry.com/x"},
        ], "active": False}))
        bl = ld.Blocklist.load(p)
        assert [u["url"] for u in bl.urls] == ["ok.com/x"]

    def test_removing_a_url_leaves_the_whole_site_block_alone(self):
        bl = ld.Blocklist()
        bl.add_site("reddit.com")
        bl.add_url("reddit.com/r/all")
        assert bl.remove("https://www.reddit.com/r/all") is True
        assert bl.urls == [] and [s["domain"] for s in bl.sites] == ["reddit.com"]
        bl.add_url("reddit.com/r/all")
        assert bl.remove("reddit.com") is True
        assert [u["url"] for u in bl.urls] == ["reddit.com/r/all"] and bl.sites == []

    def test_a_url_never_reaches_the_hosts_request(self, tmp_path):
        bl = ld.Blocklist(active=True)
        bl.add_url("reddit.com/r/all")
        req = ld.write_hosts_request(bl, tmp_path / "req.json")
        assert json.loads(req.read_text())["domains"] == []

    def test_status_lists_urls_and_says_which_browsers_cover_them(self):
        bl = ld.Blocklist(active=True)
        bl.add_url("reddit.com/r/all")
        st = ld.status(bl, check_sites=False)
        assert st["urls"][0]["url"] == "reddit.com/r/all"
        assert any("extension" in limit for limit in st["limits"])
