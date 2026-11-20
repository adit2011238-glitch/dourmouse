"""T5 step 1 (finding 214): the Dourmouse Chrome engine. Pure tests for preferences, the port file and the status
wording; live tests start a real headless Chrome on a temporary profile (skipped when Chrome is not installed)."""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from dourmouse import chrome_profile as cp

needs_chrome = pytest.mark.skipif(cp.chrome_binary() is None, reason="Google Chrome is not installed")


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    folder = tmp_path / "Chrome Profile"
    monkeypatch.setenv(cp.PROFILE_ENV, str(folder))
    yield folder
    if cp.lock_pid(folder) is not None:
        cp.quit_chrome()


def test_profile_folder_is_created_private_and_tightened(profile):
    profile.mkdir(mode=0o755)
    os.chmod(profile, 0o755)
    folder = cp.ensure_profile_dir()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_preferences_are_merged_and_other_settings_kept(profile):
    path = cp.preferences_path(cp.ensure_profile_dir())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"homepage": "https://example.org", "profile": {"name": "Dourmouse", "password_manager_enabled": True},
                                "credentials_enable_service": True}))
    cp.write_preferences()
    data = json.loads(path.read_text())
    assert data["homepage"] == "https://example.org" and data["profile"]["name"] == "Dourmouse"
    assert data["credentials_enable_service"] is False and data["profile"]["password_manager_enabled"] is False
    assert data["credentials_enable_autosignin"] is False
    assert data["autofill"] == {"profile_enabled": False, "credit_card_enabled": False}
    assert data["session"]["restore_on_startup"] == 1
    assert cp.password_saving() == "off"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_broken_preferences_file_is_replaced_not_crashed_on(profile):
    path = cp.preferences_path(cp.ensure_profile_dir())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    cp.write_preferences()
    assert json.loads(path.read_text())["credentials_enable_service"] is False


@pytest.mark.parametrize("text,expected", [
    ("9222\n/devtools/browser/abc-def\n", (9222, "/devtools/browser/abc-def")),
    ("9222", (9222, "")),
    ("", None), ("abc\n/x", None), ("0\n/x", None), ("70000\n/x", None),
])
def test_port_file_parsing(text, expected):
    assert cp.parse_port_file(text) == expected


def test_status_wording_with_nothing_running_and_nothing_signed_in(profile):
    st = cp.status()
    assert st["running"] is False and st["control_port"] == 0 and st["attached"] is False
    assert st["google_account"] == "" and st["password_saving"] == "unknown"
    assert st["password_sync"] == "not signed in" and st["connected_processes"] == []
    assert st["profile_dir"] == str(profile)


def test_account_and_sync_are_read_from_preferences_and_say_unknown_when_absent(profile):
    path = cp.preferences_path(cp.ensure_profile_dir())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"account_info": [{"email": "me@example.org"}]}))
    assert cp.google_account() == "me@example.org" and cp.password_sync() == "unknown"
    path.write_text(json.dumps({"account_info": [{"email": "me@example.org"}], "sync": {"keep_everything_synced": False, "passwords": False}}))
    assert cp.password_sync() == "off"
    path.write_text(json.dumps({"account_info": [{"email": "me@example.org"}], "sync": {"keep_everything_synced": True}}))
    assert cp.password_sync() == "on"


def test_a_stale_port_file_is_never_trusted(profile):
    folder = cp.ensure_profile_dir()
    (folder / "DevToolsActivePort").write_text("45999\n/devtools/browser/x\n")
    assert cp.read_port_file() == (45999, "/devtools/browser/x")
    assert cp.live_port() is None  # no lock, no Chrome: the file alone proves nothing
    os.symlink(f"host-{os.getpid()}", folder / "SingletonLock")  # a live pid that is not listening on that port
    assert cp.lock_pid() == os.getpid() and cp.live_port() is None
    with pytest.raises(cp.ChromeProfileError, match="Dourmouse itself"):
        cp.quit_chrome()
    (folder / "SingletonLock").unlink()


def test_launch_without_chrome_says_so(profile, monkeypatch):
    monkeypatch.setenv(cp.BINARY_ENV, "/nonexistent/chrome")
    with pytest.raises(cp.ChromeProfileError, match="not installed"):
        cp.launch()


@needs_chrome
def test_live_launch_attach_drop_reattach_status_quit(profile):
    from playwright.async_api import async_playwright

    info = cp.launch(headless=True)
    assert info["port"] > 0 and cp.lock_pid() == info["pid"]
    assert cp.live_port() == (info["port"], info["pid"])
    assert cp.launch(headless=True)["reused"] is True  # a second launch reuses it
    prefs = json.loads(cp.preferences_path().read_text())
    assert prefs["credentials_enable_service"] is False

    async def _round():
        async with async_playwright() as pw:
            browser = await cp.attach(pw)
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("data:text/html,<title>first</title><p>hello</p>")
            seen = cp.status(attached=True)
            assert seen["attached"] is True and seen["running"] is True and seen["control_port"] == info["port"]
            await browser.close()  # drops the connection, Chrome stays
        assert cp.lock_pid() == info["pid"]
        async with async_playwright() as pw:  # reattach, as after a Dourmouse restart
            browser = await cp.attach(pw)
            titles = [await p.title() for ctx in browser.contexts for p in ctx.pages]
            await browser.close()
            return titles

    titles = asyncio.run(_round())
    assert "first" in titles
    assert cp.status()["version"]
    assert cp.quit_chrome() is True
    assert cp.lock_pid() is None
