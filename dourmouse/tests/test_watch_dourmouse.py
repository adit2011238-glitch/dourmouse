"""v8.2 upstream-push watcher tests (tools/watch_dourmouse.py + webui hook).

Hermetic (Rule 2.1): git and HTTP are monkeypatched — no network. Verifies:

- upstream_head() parses real ``git ls-remote`` output; None on failure
- the watcher loop: first run records the head WITHOUT syncing; a changed
  head triggers sync + webui notify exactly once; an unchanged head does
  nothing; the state file persists the last-known head
- the /api/push-notify webui handler posts a real bus message
- transient poll failures back off without crashing
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from tools import watch_dourmouse as w


class _FakeWebuiServer:
    """Minimal stand-in for the webui server object (bus + registry)."""

    def __init__(self, bus) -> None:
        self.bus = bus
        self.session_lock = threading.Lock()


def _fake_completed(returncode: int, stdout: str, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["git", "ls-remote"], returncode, stdout=stdout, stderr=stderr
    )


def _patch_env(tmp_path, monkeypatch, head: str):
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    if head:
        (tmp_path / "workspace" / "upstream_head.txt").write_text(head + "\n", encoding="utf-8")
    monkeypatch.setattr(w, "ROOT", tmp_path)
    monkeypatch.setattr(w, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(w, "STATE_FILE", tmp_path / "workspace" / "upstream_head.txt")
    monkeypatch.setattr(w, "EVENTS_LOG", tmp_path / "workspace" / "push_events.log")
    monkeypatch.setattr(
        "sys.argv", ["watch_dourmouse.py", "--interval", "2", "--webui-port", "8765"]
    )


class TestUpstreamHead:
    def test_parses_real_output(self, monkeypatch):
        monkeypatch.setattr(
            w.subprocess, "run",
            lambda *a, **k: _fake_completed(
                0, "c6d3051b0bc11b9a34f78a912bcf235afd96f829\tHEAD\n"
            ),
        )
        assert w.upstream_head() == "c6d3051b0bc11b9a34f78a912bcf235afd96f829"

    def test_nonzero_exit_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            w.subprocess, "run",
            lambda *a, **k: _fake_completed(128, "", "fatal: could not read"),
        )
        assert w.upstream_head() is None

    def test_timeout_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("git", 20)

        monkeypatch.setattr(w.subprocess, "run", boom)
        assert w.upstream_head() is None


class TestLoop:
    def test_first_run_records_without_sync(self, tmp_path, monkeypatch):
        _patch_env(tmp_path, monkeypatch, "")
        monkeypatch.setattr(
            w.subprocess, "run",
            lambda *a, **k: _fake_completed(
                0, "abc123abc123abc123abc123abc123abc123abc1\tHEAD\n"
            ),
        )
        sync_called: list = []
        monkeypatch.setattr(w, "run_sync", lambda: sync_called.append(1) or "synced")
        monkeypatch.setattr(w, "notify_webui", lambda *a, **k: "notified")
        monkeypatch.setattr(w.time, "sleep",
                            lambda *a: (_ for _ in ()).throw(SystemExit(0)))
        with pytest.raises(SystemExit):
            w.main()
        assert sync_called == []  # first run: record only, no sync
        assert (tmp_path / "workspace" / "upstream_head.txt").read_text().strip() \
            == "abc123abc123abc123abc123abc123abc123abc1"

    def test_changed_head_triggers_sync_and_notify_once(self, tmp_path, monkeypatch):
        _patch_env(tmp_path, monkeypatch, "oldheadoldheadoldheadoldheadoldheadoldheadoldhead")
        monkeypatch.setattr(
            w.subprocess, "run",
            lambda *a, **k: _fake_completed(
                0, "newheadnewheadnewheadnewheadnewheadnewheadnewhead\tHEAD\n"
            ),
        )
        calls: list = []
        monkeypatch.setattr(w, "run_sync", lambda: calls.append("sync") or "synced")
        monkeypatch.setattr(w, "notify_webui",
                            lambda *a, **k: calls.append("notify") or "notified")
        monkeypatch.setattr(w.time, "sleep",
                            lambda *a: (_ for _ in ()).throw(SystemExit(0)))
        with pytest.raises(SystemExit):
            w.main()
        assert calls == ["sync", "notify"]  # exactly once each
        assert (tmp_path / "workspace" / "upstream_head.txt").read_text().strip() \
            == "newheadnewheadnewheadnewheadnewheadnewheadnewhead"

    def test_unchanged_head_does_nothing(self, tmp_path, monkeypatch):
        same = "sameheadsameheadsameheadsameheadsameheadsameheadsamehead"
        _patch_env(tmp_path, monkeypatch, same)
        monkeypatch.setattr(
            w.subprocess, "run",
            lambda *a, **k: _fake_completed(0, same + "\tHEAD\n"),
        )
        calls: list = []
        monkeypatch.setattr(w, "run_sync", lambda: calls.append("sync") or "synced")
        monkeypatch.setattr(w, "notify_webui", lambda *a, **k: "notified")
        monkeypatch.setattr(w.time, "sleep",
                            lambda *a: (_ for _ in ()).throw(SystemExit(0)))
        with pytest.raises(SystemExit):
            w.main()
        assert calls == []

    def test_poll_failure_backs_off_without_crash(self, tmp_path, monkeypatch):
        _patch_env(tmp_path, monkeypatch, "")
        seq = iter([
            _fake_completed(128, "", "boom"),
            _fake_completed(0, "goodheadgoodheadgoodheadgoodheadgoodheadgoodheadgoodhead\tHEAD\n"),
        ])

        def flaky(*a, **k):
            return next(seq)

        monkeypatch.setattr(w.subprocess, "run", flaky)
        sleeps: list = []
        monkeypatch.setattr(w.time, "sleep", lambda s: sleeps.append(s) or
                            (_ for _ in ()).throw(SystemExit(0)))
        with pytest.raises(SystemExit):
            w.main()
        assert sleeps[0] == 30  # backoff on failure


class TestWebuiPushNotify:
    def test_handler_posts_real_bus_message(self):
        from dourmouse.message_bus import MessageBus
        from dourmouse.webui import _Handler

        bus = MessageBus()
        server = _FakeWebuiServer(bus)
        handler = _Handler.__new__(_Handler)
        handler.server = server
        handler.path = "/api/push-notify"
        handler._read_json_body = lambda: {
            "from": "watchdog",
            "subject": "UPSTREAM PUSH DETECTED",
            "body": "old -> new pushed",
        }
        sent: dict = {}
        handler._send_json = lambda payload, status=200: sent.update(payload)

        handler._handle_push_notify()
        assert sent.get("ok") is True
        msgs = bus.snapshot(10)
        assert len(msgs) == 1
        assert msgs[0]["from"] == "watchdog"
        assert msgs[0]["subject"] == "UPSTREAM PUSH DETECTED"
        assert msgs[0]["body"] == "old -> new pushed"
