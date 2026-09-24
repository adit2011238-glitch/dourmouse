"""Finding #112 (MS-12): privacy mode keeps security data on the Mac, and
the self-audit catches the ways Dourmouse itself could be the weak point."""

from __future__ import annotations

import hashlib
import plistlib
import stat

from dourmouse.security import analyst as an
from dourmouse.security import privacy as pv
from dourmouse.security import self_audit as sa


def test_privacy_mode_persists_and_stops_the_cloud_analyst(monkeypatch):
    assert pv.privacy_mode() is False
    pv.set_privacy_mode(True)
    assert pv.privacy_mode() is True and stat.S_IMODE(pv.settings_path().stat().st_mode) == 0o600
    monkeypatch.setattr(an, "default_complete", lambda: (_ for _ in ()).throw(AssertionError("must not call")))
    r = an.analyze([{"kind": "k", "severity": "high", "title": "t", "detail": "d"}])
    assert not r["ok"] and "privacy mode is on" in r["error"]
    pv.set_privacy_mode(False)
    assert pv.privacy_mode() is False


def test_security_folders_are_private(tmp_path):
    from dourmouse.config import workspace_dir

    root = workspace_dir() / "security"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    made = pv.private_dir(root / "reports")
    assert stat.S_IMODE(made.stat().st_mode) == 0o700 and stat.S_IMODE(root.stat().st_mode) == 0o700


def test_bind_and_auto_approve():
    assert sa.check_bind("127.0.0.1", "") == []
    assert sa.check_bind("0.0.0.0", "")[0]["severity"] == "high"
    assert sa.check_bind("0.0.0.0", "tok")[0]["severity"] == "low"
    assert sa.check_auto_approve(False) == [] and sa.check_auto_approve(True)[0]["severity"] == "high"


def test_readable_key_file_and_listable_folder(tmp_path):
    env = tmp_path / ".env"
    env.write_text("K=v\n", encoding="utf-8")
    env.chmod(0o644)
    assert "chmod 600" in sa.check_secret_file(env)[0]["fix"]
    env.chmod(0o600)
    assert sa.check_secret_file(env) == []
    d = tmp_path / "q"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    assert sa.check_private_dir(d, "Q")[0]["severity"] == "low"


def test_a_tampered_or_redirected_helper_is_caught(tmp_path):
    packaged = tmp_path / "packaged.py"
    packaged.write_text("print('helper')\n", encoding="utf-8")
    installed = tmp_path / "installed"
    installed.write_bytes(packaged.read_bytes())
    plist = tmp_path / "helper.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["/usr/bin/python3", str(installed)]}))
    titles = [f["title"] for f in sa.check_helper(installed, plist, packaged)]
    # Same bytes and the right target; only ownership differs (a test cannot be root).
    assert titles == ["The lockdown helper can be modified by a non-root user"]
    installed.write_text("import os; os.system('evil')\n", encoding="utf-8")
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["/usr/bin/python3", "/tmp/other.py"]}))
    titles = [f["title"] for f in sa.check_helper(installed, plist, packaged)]
    assert "The installed lockdown helper differs from Dourmouse's own copy" in titles
    assert "launchd runs something other than the lockdown helper" in titles
    assert hashlib.sha256(packaged.read_bytes()).hexdigest() != hashlib.sha256(installed.read_bytes()).hexdigest()
    assert sa.check_helper(tmp_path / "none", tmp_path / "none.plist", packaged) == []


def test_the_live_audit_runs_and_lists_what_it_could_not_check():
    r = sa.run_self_audit()
    assert r["checked"] and r["not_checked"] and isinstance(r["findings"], list)
