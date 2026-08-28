"""Tests for dourmouse/overlay.py (Vision stage 2: always-on-top overlay).

PyWebView opens a real GUI window, which can never run in a headless test
suite — so the window layer is exercised through the ``webview_loader`` test
seam with a fake webview module (same approach as dourmouse/tests/
test_desktop.py), while the real logic — activity-snapshot summarization,
the privacy-state read, and the background poller's HTTP + threading
behaviour — runs for real against actual code paths, no fabricated results.
"""

from __future__ import annotations

import sys
import time

import pytest

from dourmouse import overlay, tray


# --------------------------------------------------------------------------- #
# summarize_activity: pure function over an ActivityTracker snapshot
# --------------------------------------------------------------------------- #

class TestSummarizeActivity:
    def test_none_snapshot_is_idle(self):
        summary = overlay.summarize_activity(None)
        assert summary["busy"] is False
        assert summary["headline"] == "IDLE"

    def test_empty_agents_is_idle(self):
        summary = overlay.summarize_activity({"agents": {}})
        assert summary["busy"] is False
        assert summary["headline"] == "IDLE"

    def test_computing_agent_is_working(self):
        snapshot = {
            "agents": {
                "researcher": {
                    "status": "computing",
                    "last": {"tool": "web_search"},
                }
            }
        }
        summary = overlay.summarize_activity(snapshot)
        assert summary["busy"] is True
        assert summary["headline"] == "WORKING"
        assert "researcher" in summary["detail"]
        assert "web_search" in summary["detail"]

    def test_multiple_computing_agents_shows_count(self):
        snapshot = {
            "agents": {
                "a": {"status": "computing", "last": {"tool": "x"}},
                "b": {"status": "computing", "last": {"tool": "y"}},
            }
        }
        summary = overlay.summarize_activity(snapshot)
        assert "+1 more" in summary["detail"]

    def test_auth_takes_priority_over_computing(self):
        snapshot = {
            "agents": {
                "a": {"status": "computing", "last": {"tool": "x"}},
                "b": {"status": "auth"},
            }
        }
        summary = overlay.summarize_activity(snapshot)
        assert summary["headline"] == "WAITING ON YOU"
        assert "b" in summary["detail"]

    def test_live_only_is_idle_but_notes_monitor_count(self):
        snapshot = {
            "agents": {
                "watcher": {"status": "live"},
                "other": {"status": "idle"},
            }
        }
        summary = overlay.summarize_activity(snapshot)
        assert summary["busy"] is False
        assert summary["headline"] == "IDLE"
        assert "1 live monitor" in summary["detail"]

    def test_missing_last_tool_does_not_crash(self):
        snapshot = {"agents": {"a": {"status": "computing", "last": None}}}
        summary = overlay.summarize_activity(snapshot)
        assert summary["headline"] == "WORKING"


# --------------------------------------------------------------------------- #
# Privacy state: shares tray.py's persisted flag, never invents its own
# --------------------------------------------------------------------------- #

class TestReadPrivacyState:
    def test_reflects_real_tray_state(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_WORKSPACE", raising=False)
        state_path = tmp_path / "privacy_state.json"
        monkeypatch.setenv("DOURMOUSE_PRIVACY_STATE", str(state_path))
        tray.save_state(
            tray.KillSwitchState(mic_enabled=False, camera_enabled=True),
            state_path,
        )
        result = overlay._read_privacy_state()
        assert result == {"mic_enabled": False, "camera_enabled": True}

    def test_defaults_when_tray_state_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "DOURMOUSE_PRIVACY_STATE", str(tmp_path / "never-written.json")
        )
        result = overlay._read_privacy_state()
        assert result == {"mic_enabled": True, "camera_enabled": True}

    def test_defaults_when_tray_load_state_raises(self, monkeypatch):
        def _raiser(path=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(tray, "load_state", _raiser)
        result = overlay._read_privacy_state()
        assert result == {"mic_enabled": True, "camera_enabled": True}


# --------------------------------------------------------------------------- #
# OverlayStatusPoller: poll_once (single call, no thread)
# --------------------------------------------------------------------------- #

class TestPollOnce:
    def test_offline_when_fetch_returns_none(self):
        poller = overlay.OverlayStatusPoller(
            "http://127.0.0.1:1",
            on_update=lambda s: None,
            fetch=lambda url: None,
            privacy_reader=lambda: {"mic_enabled": True, "camera_enabled": True},
        )
        summary = poller.poll_once()
        assert summary["online"] is False
        assert summary["headline"] == "OFFLINE"

    def test_online_merges_activity_and_privacy(self):
        fake_snapshot = {"agents": {"a": {"status": "computing", "last": {"tool": "t"}}}}
        poller = overlay.OverlayStatusPoller(
            "http://127.0.0.1:1",
            on_update=lambda s: None,
            fetch=lambda url: fake_snapshot,
            privacy_reader=lambda: {"mic_enabled": False, "camera_enabled": False},
        )
        summary = poller.poll_once()
        assert summary["online"] is True
        assert summary["headline"] == "WORKING"
        assert summary["mic_enabled"] is False
        assert summary["camera_enabled"] is False

    def test_fetch_receives_activity_endpoint_url(self):
        captured = []

        def _fetch(url):
            captured.append(url)
            return {"agents": {}}

        poller = overlay.OverlayStatusPoller(
            "http://127.0.0.1:8765/",
            on_update=lambda s: None,
            fetch=_fetch,
            privacy_reader=lambda: {"mic_enabled": True, "camera_enabled": True},
        )
        poller.poll_once()
        assert captured == ["http://127.0.0.1:8765/api/activity"]

    def test_default_fetch_against_unreachable_port_returns_none(self):
        """Real network path (no injected fetch): an unreachable loopback
        port must degrade to None, never raise."""
        poller = overlay.OverlayStatusPoller(
            "http://127.0.0.1:1",  # port 1 is never a real dourmouse server
            on_update=lambda s: None,
        )
        result = poller._default_fetch("http://127.0.0.1:1/api/activity")
        assert result is None


# --------------------------------------------------------------------------- #
# OverlayStatusPoller: background thread lifecycle
# --------------------------------------------------------------------------- #

class TestPollerThread:
    def test_start_calls_on_update_and_stop_ends_thread(self):
        updates = []
        poller = overlay.OverlayStatusPoller(
            "http://127.0.0.1:1",
            on_update=updates.append,
            fetch=lambda url: {"agents": {}},
            privacy_reader=lambda: {"mic_enabled": True, "camera_enabled": True},
            interval=0.05,
        )
        poller.start()
        deadline = time.time() + 2
        while not updates and time.time() < deadline:
            time.sleep(0.02)
        poller.stop(join=True)
        assert len(updates) >= 1
        assert updates[0]["headline"] == "IDLE"

    def test_start_is_idempotent(self):
        poller = overlay.OverlayStatusPoller(
            "http://127.0.0.1:1",
            on_update=lambda s: None,
            fetch=lambda url: {"agents": {}},
            interval=0.5,
        )
        poller.start()
        thread1 = poller._thread
        poller.start()
        assert poller._thread is thread1
        poller.stop(join=True)

    def test_broken_on_update_does_not_kill_the_loop(self):
        calls = []

        def _on_update(summary):
            calls.append(summary)
            raise RuntimeError("boom")

        poller = overlay.OverlayStatusPoller(
            "http://127.0.0.1:1",
            on_update=_on_update,
            fetch=lambda url: {"agents": {}},
            interval=0.05,
        )
        poller.start()
        deadline = time.time() + 2
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.02)
        poller.stop(join=True)
        assert len(calls) >= 2  # kept polling despite the raising callback


# --------------------------------------------------------------------------- #
# _push: JS bridge into the live window
# --------------------------------------------------------------------------- #

class TestPush:
    def test_pushes_json_via_evaluate_js(self):
        calls = []

        class _FakeWindow:
            def evaluate_js(self, script):
                calls.append(script)

        overlay._push(_FakeWindow(), {"headline": "WORKING", "busy": True})
        assert len(calls) == 1
        assert "__dourmouseOverlayUpdate" in calls[0]
        assert "WORKING" in calls[0]

    def test_window_without_evaluate_js_is_a_noop(self):
        class _BareWindow:
            pass

        overlay._push(_BareWindow(), {"headline": "IDLE"})  # must not raise

    def test_evaluate_js_exception_is_swallowed(self):
        class _BrokenWindow:
            def evaluate_js(self, script):
                raise RuntimeError("webview gone")

        overlay._push(_BrokenWindow(), {"headline": "IDLE"})  # must not raise


# --------------------------------------------------------------------------- #
# _corner_x: best-effort placement, honest fallback
# --------------------------------------------------------------------------- #

class TestCornerX:
    def test_returns_none_or_a_non_negative_int(self):
        result = overlay._corner_x(260)
        assert result is None or (isinstance(result, int) and result >= 0)

    def test_appkit_failure_falls_back_to_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "AppKit", None)
        assert overlay._corner_x(260) is None


# --------------------------------------------------------------------------- #
# launch(): honest fallback, and real wiring against a fake webview module
# --------------------------------------------------------------------------- #

class _FakeEvents:
    def __init__(self):
        self.closed = _Signal()


class _Signal:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeWindow:
    def __init__(self, title, **kwargs):
        self.title = title
        self.kwargs = kwargs
        self.events = _FakeEvents()
        self.evaluated = []

    def evaluate_js(self, script):
        self.evaluated.append(script)


class _FakeWebview:
    def __init__(self):
        self.windows = []
        self.started = False

    def create_window(self, title, **kwargs):
        win = _FakeWindow(title, **kwargs)
        self.windows.append(win)
        return win

    def start(self):
        self.started = True


class TestLaunch:
    def test_missing_webview_reports_and_exits_without_browser_fallback(
        self, monkeypatch, capsys
    ):
        def _raise_not_configured():
            raise RuntimeError("NOT CONFIGURED: the native window needs pywebview")

        code = overlay.launch(webview_loader=_raise_not_configured)
        assert code == 1
        out = capsys.readouterr().out
        assert "NOT CONFIGURED" in out

    def test_creates_always_on_top_transparent_frameless_window(self, monkeypatch):
        fake = _FakeWebview()
        monkeypatch.setattr(
            overlay,
            "OverlayStatusPoller",
            lambda *a, **k: _StubPoller(),
        )
        code = overlay.launch(
            webview_loader=lambda: fake, base_url="http://127.0.0.1:1"
        )
        assert code == 0
        assert fake.started is True
        assert len(fake.windows) == 1
        win = fake.windows[0]
        assert win.kwargs["on_top"] is True
        assert win.kwargs["frameless"] is True
        assert win.kwargs["transparent"] is True
        assert win.kwargs["focus"] is False


class _StubPoller:
    def start(self):
        pass

    def stop(self, join=False):
        pass
