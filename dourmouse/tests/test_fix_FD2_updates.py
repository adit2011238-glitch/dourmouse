"""FD2 fix P4-64: update feed channel filter, version validation, and a .part name that keeps the full version."""

from __future__ import annotations

import hashlib

import pytest

from dourmouse import updates


def _feed(**kw):
    base = {"channel": "stable", "version": "6.0.0", "url": "https://example.com/a.zip", "sha256": "a" * 64}
    base.update(kw)
    return base


def test_a_beta_feed_entry_is_rejected_for_a_stable_install(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_UPDATE_CHANNEL", "stable")
    with pytest.raises(ValueError, match="channel"):
        updates._validate_feed(_feed(channel="beta"))


def test_a_beta_install_accepts_a_beta_entry_and_rejects_stable(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_UPDATE_CHANNEL", "beta")
    assert updates._validate_feed(_feed(channel="beta"))["channel"] == "beta"
    with pytest.raises(ValueError, match="channel"):
        updates._validate_feed(_feed(channel="stable"))


def test_a_feed_without_a_channel_uses_the_configured_one(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_UPDATE_CHANNEL", "beta")
    data = _feed()
    del data["channel"]
    assert updates._validate_feed(data)["channel"] == "beta"


@pytest.mark.parametrize("bad", ["../../etc/x", "1.0/2", "a b", "..", "v" * 100, "1;rm"])
def test_unsafe_versions_are_rejected(monkeypatch, bad):
    monkeypatch.setenv("DOURMOUSE_UPDATE_CHANNEL", "stable")
    with pytest.raises(ValueError, match="version"):
        updates._validate_feed(_feed(version=bad))


def test_stage_artifact_rejects_an_unsafe_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    with pytest.raises(ValueError):
        updates.stage_artifact(_feed(version="../x"))


def test_part_file_keeps_the_full_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    data = b"payload-bytes"
    seen = []

    class _Resp:
        def __init__(self):
            self._buf = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            seen.extend(p.name for p in (tmp_path / "updates").glob("*.part"))
            out, self._buf = self._buf, b""
            return out

    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda url, timeout=None: _Resp())
    feed = _feed(version="5.20.1", sha256=hashlib.sha256(data).hexdigest())
    dest = updates.stage_artifact(feed)
    assert dest.name == "dourmouse-5.20.1"
    assert dest.read_bytes() == data
    assert "dourmouse-5.20.1.part" in seen  # not dourmouse-5.20.part, which 5.20.2 would share
    assert not list((tmp_path / "updates").glob("*.part"))
