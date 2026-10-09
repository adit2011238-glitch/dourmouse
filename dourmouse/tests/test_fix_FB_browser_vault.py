"""Fix agent FB: P4-10 (the credential vault: a corrupt file must never be wiped, writes are atomic
and never world-readable, concurrent stores do not lose entries)."""

from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from dourmouse import browser_agent as ba


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ba, "_VAULT_PATH", tmp_path / "browser_creds.json")
    return tmp_path / "browser_creds.json"


def _store(site: str, user: str = "u", password: str = "pw") -> str:
    return ba.browser_creds_store({"site": site, "username": user, "password": password})


def test_a_corrupt_vault_is_set_aside_not_wiped(vault):
    vault.write_text('{"keep.example.com": {"username": "a", "password": "SECRET1"}, "trunc')
    original = vault.read_bytes()
    out = _store("new.example.com")
    assert "STORED" in out and "corrupt" in out.lower()
    aside = [p for p in vault.parent.iterdir() if ".corrupt" in p.name]
    assert len(aside) == 1 and aside[0].read_bytes() == original
    assert stat.S_IMODE(aside[0].stat().st_mode) == 0o600
    assert list(json.loads(vault.read_text())) == ["new.example.com"]


def test_a_vault_that_is_not_an_object_is_also_set_aside(vault):
    vault.write_text("[1, 2, 3]")
    assert "STORED" in _store("new.example.com")
    assert [p for p in vault.parent.iterdir() if ".corrupt" in p.name]


def test_the_vault_is_never_readable_by_others_even_for_a_moment(vault, monkeypatch):
    old = os.umask(0o022)  # the common default: a plain write_text would give 0644
    seen: list[int] = []
    real_replace = os.replace

    def spy(src, dst, *a, **k):
        seen.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", spy)
    try:
        _store("a.example.com", password="topsecret")
    finally:
        os.umask(old)
    assert seen == [0o600], seen  # the file holding the password had 0600 before it got its final name
    assert stat.S_IMODE(vault.stat().st_mode) == 0o600
    assert [p.name for p in vault.parent.iterdir()] == ["browser_creds.json"], "no temp file is left behind"


def test_the_first_write_of_a_new_data_dir_is_private_too(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(ba, "_DATA_DIR", data)
    monkeypatch.setattr(ba, "_VAULT_PATH", data / "browser_creds.json")
    old = os.umask(0o022)
    try:
        _store("a.example.com")
    finally:
        os.umask(old)
    assert stat.S_IMODE((data / "browser_creds.json").stat().st_mode) == 0o600


def test_concurrent_stores_lose_nothing(vault):
    sites = [f"site{i}.example.com" for i in range(40)]
    barrier = threading.Barrier(len(sites))
    errors: list[BaseException] = []

    def go(site: str) -> None:
        try:
            barrier.wait()
            assert "STORED" in _store(site, user=f"user-{site}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(s,)) for s in sites]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert sorted(json.loads(vault.read_text())) == sorted(sites)


def test_a_failed_write_keeps_the_old_vault_and_leaves_no_temp_file(vault, monkeypatch):
    _store("keep.example.com", password="p1")
    before = vault.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    out = _store("other.example.com")
    monkeypatch.undo()
    assert out.startswith("ERROR") and "Nothing was stored" in out
    assert vault.read_bytes() == before
    assert [p.name for p in vault.parent.iterdir()] == ["browser_creds.json"]


def test_forget_is_atomic_and_keeps_other_sites(vault):
    _store("a.example.com")
    _store("b.example.com")
    assert "REMOVED" in ba.browser_creds_forget({"site": "a.example.com"})
    assert list(json.loads(vault.read_text())) == ["b.example.com"]
    assert stat.S_IMODE(vault.stat().st_mode) == 0o600


def test_forget_does_not_touch_a_corrupt_vault(vault):
    vault.write_text("{broken")
    assert "unreadable" in ba.browser_creds_forget({"site": "a.example.com"}).lower()
    assert vault.read_text() == "{broken"
