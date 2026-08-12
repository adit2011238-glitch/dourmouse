"""Hermetic tests for the v5.19 secure self-update feed.

The feed is a ``latest.json`` served over a local HTTP server (the artifact
URL inside the feed is validated https-only, so tests never touch the real
network). Artifact staging runs against a local server + tmp workspace.
"""

import hashlib
import http.server
import json
import threading

import pytest

from dourmouse import updates


class _FeedHandler(http.server.BaseHTTPRequestHandler):
    """Serves the configured payload / artifact bytes (per-test via class
    attributes so each test can point the feed where it wants)."""

    payload = b"{}"
    artifact = b""

    def log_message(self, *args):  # noqa: D102
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/artifact"):
            body = self.__class__.artifact
        else:
            body = self.__class__.payload
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def feed_server():
    _FeedHandler.payload = b"{}"
    _FeedHandler.artifact = b""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _good_feed(version: str = "6.0.0") -> dict:
    return {
        "channel": "stable",
        "version": version,
        "url": "https://example.com/dourmouse-6.0.0.zip",  # https-only by rule
        "sha256": "a" * 64,
        "notes": "the next release",
    }


# -- the /api/version surface (check_for_updates) ------------------------ #


def test_not_configured_when_no_feed(monkeypatch):
    monkeypatch.delenv("DOURMOUSE_UPDATE_FEED", raising=False)
    info = updates.check_for_updates()
    assert info.configured is False
    assert info.latest is None
    assert info.current  # the real app version, never None
    assert info.error is None


def test_happy_path(monkeypatch, feed_server):
    _FeedHandler.payload = json.dumps(_good_feed()).encode()
    monkeypatch.setenv("DOURMOUSE_UPDATE_FEED", feed_server)
    monkeypatch.setenv("DOURMOUSE_UPDATE_CHANNEL", "stable")
    info = updates.check_for_updates()
    assert info.configured is True
    assert info.latest == "6.0.0"
    assert info.url == "https://example.com/dourmouse-6.0.0.zip"
    assert info.sha256 == "a" * 64
    assert info.notes == "the next release"
    assert info.error is None


def test_unreachable_feed_is_honest_error(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_UPDATE_FEED", "http://127.0.0.1:1/none.json")
    info = updates.check_for_updates()
    assert info.configured is True
    assert info.latest is None
    assert info.error  # real reason, never a guess


def test_malformed_json_is_honest_error(monkeypatch, feed_server):
    _FeedHandler.payload = b"{not json"
    monkeypatch.setenv("DOURMOUSE_UPDATE_FEED", feed_server)
    info = updates.check_for_updates()
    assert info.configured is True
    assert "JSONDecodeError" in info.error


def test_non_object_json_feed_is_honest_error(monkeypatch, feed_server):
    # valid JSON that is NOT a dict must be an honest error, never a 500
    _FeedHandler.payload = b"[1, 2, 3]"
    monkeypatch.setenv("DOURMOUSE_UPDATE_FEED", feed_server)
    info = updates.check_for_updates()
    assert info.configured is True
    assert "JSON object" in info.error
    assert info.latest is None


def test_failed_feed_is_negative_cached(monkeypatch, feed_server):
    _FeedHandler.payload = b"{not json"
    monkeypatch.setenv("DOURMOUSE_UPDATE_FEED", feed_server)
    first = updates.check_for_updates()
    assert first.error
    # poison the server so a RE-fetch would succeed differently — the cache
    # must serve the error without a second network attempt
    _FeedHandler.payload = json.dumps(_good_feed()).encode()
    second = updates.check_for_updates()
    assert second.error == first.error
    assert second.latest is None


def test_feed_requires_https_artifact(monkeypatch, feed_server):
    bad = _good_feed()
    bad["url"] = "http://insecure.example.com/x.zip"
    _FeedHandler.payload = json.dumps(bad).encode()
    monkeypatch.setenv("DOURMOUSE_UPDATE_FEED", feed_server)
    info = updates.check_for_updates()
    assert "https" in info.error


def test_feed_requires_64_hex_sha(monkeypatch, feed_server):
    bad = _good_feed()
    bad["sha256"] = "short"
    _FeedHandler.payload = json.dumps(bad).encode()
    monkeypatch.setenv("DOURMOUSE_UPDATE_FEED", feed_server)
    info = updates.check_for_updates()
    assert "sha256" in info.error


def test_feed_rejects_unknown_channel(monkeypatch, feed_server):
    bad = _good_feed()
    bad["channel"] = "nightly"
    _FeedHandler.payload = json.dumps(bad).encode()
    monkeypatch.setenv("DOURMOUSE_UPDATE_FEED", feed_server)
    info = updates.check_for_updates()
    assert "channel" in info.error


def test_feed_missing_version_rejected(monkeypatch, feed_server):
    bad = _good_feed()
    del bad["version"]
    _FeedHandler.payload = json.dumps(bad).encode()
    monkeypatch.setenv("DOURMOUSE_UPDATE_FEED", feed_server)
    info = updates.check_for_updates()
    assert "version" in info.error


# -- the artifact gate ---------------------------------------------------- #


def test_verify_sha256():
    data = b"hello"
    good = hashlib.sha256(data).hexdigest()
    assert updates.verify_sha256(data, good) is True
    assert updates.verify_sha256(data, good.upper()) is True  # case-insensitive
    assert updates.verify_sha256(data, "0" * 64) is False
    assert updates.verify_sha256(data, "") is False


def test_stage_artifact_verifies_then_stages(monkeypatch, feed_server, tmp_path):
    payload = b"release bytes"
    _FeedHandler.artifact = payload
    feed = {
        "version": "9.9.9",
        "url": f"{feed_server}/artifact",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    staged = updates.stage_artifact(feed)
    assert staged.name == "dourmouse-9.9.9"
    assert staged.read_bytes() == payload


def test_stage_artifact_refuses_hash_mismatch(monkeypatch, feed_server, tmp_path):
    _FeedHandler.artifact = b"corrupted bytes"
    feed = {
        "version": "9.9.9",
        "url": f"{feed_server}/artifact",
        "sha256": "0" * 64,
    }
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    with pytest.raises(ValueError) as exc:
        updates.stage_artifact(feed)
    assert "sha256 mismatch" in str(exc.value)
    # the corrupt partial is never left staged
    root = tmp_path / "updates"
    assert not list(root.glob("dourmouse-*"))


def test_stage_keeps_previous_for_rollback(monkeypatch, feed_server, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    _FeedHandler.artifact = b"v1"
    updates.stage_artifact({
        "version": "1.0.0",
        "url": f"{feed_server}/artifact",
        "sha256": hashlib.sha256(b"v1").hexdigest(),
    })
    _FeedHandler.artifact = b"v2"
    updates.stage_artifact({
        "version": "2.0.0",
        "url": f"{feed_server}/artifact",
        "sha256": hashlib.sha256(b"v2").hexdigest(),
    })
    root = tmp_path / "updates"
    assert (root / "dourmouse-2.0.0").read_bytes() == b"v2"
    assert (root / "previous" / "dourmouse-1.0.0").read_bytes() == b"v1"
