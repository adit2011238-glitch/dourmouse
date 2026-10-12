"""FIX-R3 H-FIXW2-4: the Spotify token file holds a refresh token (a credential). It is written 0600
from the first byte, whatever the umask, and through a temp file plus os.replace, so a crash mid-write
leaves the old file or the new one, never a truncated one. No Spotify endpoint is contacted."""
from __future__ import annotations

import json
import os
import stat

import pytest

from dourmouse import spotify_services as sp


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_tokens_are_written_private(loose_umask):
    sp._save_tokens({"refresh_token": "r1", "access_token": "a1"})
    path = sp._tokens_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["refresh_token"] == "r1"


def test_tokens_replace_the_file_instead_of_truncating_it(loose_umask):
    sp._save_tokens({"refresh_token": "r1"})
    path = sp._tokens_path()
    before = path.stat().st_ino
    sp._save_tokens({"refresh_token": "r2"})
    assert path.stat().st_ino != before  # a new file moved into place, not the old one rewritten
    assert sp._load_tokens() == {"refresh_token": "r2"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_old_tokens(monkeypatch, loose_umask):
    sp._save_tokens({"refresh_token": "keep"})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        sp._save_tokens({"refresh_token": "lost"})
    assert sp._load_tokens() == {"refresh_token": "keep"}
