"""Fix agent FD1: P5-13 (walker follows symlinks, indexes secrets), P4-22 (unbounded read), P5-14 (negative cap)."""

from __future__ import annotations

import os

import pytest

import dourmouse.chat as chat_module
import dourmouse.device_wiki_tools as dwt
from dourmouse.device_wiki import stages
from dourmouse.device_wiki.walker import ROOTS_ENV, walk_roots

# P5-13 ---------------------------------------------------------------------- #

def test_symlink_to_a_file_outside_the_root_is_not_indexed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "credentials_real.txt"
    secret.write_text("aws_secret_access_key=abc")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("fine")
    os.symlink(secret, root / "notes.txt")
    found = walk_roots([root])
    assert list(found) == [str(root / "ok.txt")]


def test_symlink_to_a_file_inside_the_root_is_also_skipped(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("fine")
    os.symlink(root / "a.txt", root / "alias.txt")
    assert list(walk_roots([root])) == [str(root / "a.txt")]


@pytest.mark.parametrize("name", [
    "id_rsa", "id_ed25519", "server.pem", "tls.key", "store.kdbx", "creds.p12", ".env", "prod.env",
    ".env.local", "credentials.json", "credentials", ".netrc", ".npmrc", "client_secret_123.json",
    "service-account-prod.json", "secrets.yml", "passwords.txt",
])
def test_secret_named_files_inside_a_root_are_not_indexed(tmp_path, name):
    root = tmp_path / "root"
    root.mkdir()
    (root / name).write_text("secret material")
    (root / "readme.txt").write_text("fine")
    assert list(walk_roots([root])) == [str(root / "readme.txt")]


def test_ordinary_names_with_secret_like_substrings_survive(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for n in ("keynote.txt", "monkey.md", "environment.md", "idea.txt", "secretary.md"):
        (root / n).write_text("fine")
    assert len(walk_roots([root])) == 5


def test_reader_also_refuses_a_symlink_and_a_secret_name(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("some text")
    link = tmp_path / "link.txt"
    os.symlink(real, link)
    assert stages.read_file_for_summary(str(link)) is None
    key = tmp_path / "id_rsa"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert stages.read_file_for_summary(str(key)) is None
    assert stages.read_file_for_summary(str(real)) == "some text"


# P4-22 ---------------------------------------------------------------------- #

def test_huge_text_file_is_read_as_a_bounded_prefix(tmp_path, monkeypatch):
    p = tmp_path / "big.log"
    p.write_text("line of log\n" * 200_000)  # about 2.4 MB, no NUL
    sizes = []
    real_open = type(p).open

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *e):
            self.fh.close()

        def read(self, n=-1):
            sizes.append(n)
            return self.fh.read(n)

    monkeypatch.setattr(type(p), "open", lambda self, *a, **k: Spy(real_open(self, *a, **k)))
    monkeypatch.setattr(type(p), "read_text", lambda *a, **k: pytest.fail("read_text loads the whole file"))
    out = stages.read_file_for_summary(str(p))
    assert out is not None and len(out) == stages._MAX_CONTENT_CHARS
    assert all(n > 0 for n in sizes)


# P5-14 ---------------------------------------------------------------------- #

class _FakeSession:
    calls: list[str] = []

    def __init__(self, registry, session_file=None):
        pass

    def ask(self, prompt, force_plain_dispatch=False):
        type(self).calls.append(prompt)
        return {"final_text": "a summary"}


def _tool(name):
    for t in dwt.build_device_wiki_subagent().tools:
        if t.name == name:
            return t
    raise AssertionError(name)


def test_negative_max_files_summarizes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(dwt, "default_db", lambda: tmp_path / "wiki.db")
    _FakeSession.calls = []
    monkeypatch.setattr(chat_module, "ChatSession", _FakeSession)
    root = tmp_path / "root"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"content {i}")
    monkeypatch.setenv(ROOTS_ENV, str(root))
    out = _tool("device_wiki_scan").handler({"max_files_to_summarize": -1})
    assert "0 newly summarized" in out
    assert len(_FakeSession.calls) == 0


def test_huge_max_files_is_clamped():
    assert dwt._clamp_max_files(10**9) == dwt._MAX_FILES_HARD_CAP
    assert dwt._clamp_max_files(-5) == 0
    assert dwt._clamp_max_files(7) == 7
