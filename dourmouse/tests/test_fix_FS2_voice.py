"""FS2 P3-67: text must never reach `say` as an argument it can parse as an option."""

from __future__ import annotations

import shutil
import types
from pathlib import Path

import pytest

from dourmouse import voice as voice_module


def test_say_gets_text_on_stdin_not_in_argv(monkeypatch):
    monkeypatch.setattr(voice_module.shutil, "which", lambda _c: "/usr/bin/say")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input")
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"RIFF")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(voice_module.subprocess, "run", fake_run)
    payload = "-f/Users/x/.ssh/id_rsa"
    assert voice_module._say_speak(payload) == b"RIFF"
    assert payload not in seen["cmd"]
    assert all(not a.startswith("-f/") and a != payload for a in seen["cmd"])
    assert seen["cmd"][-2:] == ["-f", "-"]
    assert seen["input"] == payload.encode()


@pytest.mark.skipif(not shutil.which("say"), reason="macOS say not installed")
def test_real_say_does_not_read_a_file_named_by_the_text(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("classified words about nothing at all " * 60)
    hostile = voice_module._say_speak(f"-f{secret}")
    # if the file had been read the audio would be several minutes long (over 8 MB); the literal
    # path is under half a minute of speech (about 650 KB)
    assert len(hostile) < 2_000_000
