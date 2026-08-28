"""Tests for dourmouse/vision_bridge.py (Vision stage 5, backend half).

Unlike pywebview or pystray, this is plain stdlib ``http.server`` — no
display, no native backend, nothing that needs a live desktop. Every test
below starts a REAL server on an ephemeral port (``port=0``) and makes REAL
HTTP requests against it; nothing here is mocked at the socket layer.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from dourmouse import tray, vision_bridge


def _get(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 -- loopback only
        return resp.status, json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# Port resolution
# --------------------------------------------------------------------------- #

class TestBridgePort:
    def test_default_port(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_VISION_BRIDGE_PORT", raising=False)
        assert vision_bridge.bridge_port() == 8766

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VISION_BRIDGE_PORT", "9999")
        assert vision_bridge.bridge_port() == 9999

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VISION_BRIDGE_PORT", "not-a-number")
        assert vision_bridge.bridge_port() == 8766


# --------------------------------------------------------------------------- #
# Wire format: pure function, no socket
# --------------------------------------------------------------------------- #

class TestStatePayload:
    def test_both_enabled(self):
        payload = vision_bridge._state_payload(lambda: tray.KillSwitchState())
        assert payload == {
            "mic_enabled": True,
            "camera_enabled": True,
            "updated_at": "",
            "online": True,
        }

    def test_both_killed(self):
        state = tray.KillSwitchState(mic_enabled=False, camera_enabled=False, updated_at="t")
        payload = vision_bridge._state_payload(lambda: state)
        assert payload["mic_enabled"] is False
        assert payload["camera_enabled"] is False
        assert payload["updated_at"] == "t"


# --------------------------------------------------------------------------- #
# Real server, real HTTP round-trip
# --------------------------------------------------------------------------- #

@pytest.fixture
def live_server():
    state = tray.KillSwitchState(mic_enabled=True, camera_enabled=True)
    srv = vision_bridge.VisionBridgeServer(state_reader=lambda: state, port=0)
    ok, detail = srv.start()
    assert ok, detail
    yield srv, state
    srv.stop()


class TestLiveServer:
    def test_get_vision_state_reflects_current_state(self, live_server):
        srv, state = live_server
        status, body = _get(f"http://127.0.0.1:{srv.port}/api/vision-state")
        assert status == 200
        assert body["mic_enabled"] is True
        assert body["camera_enabled"] is True
        assert body["online"] is True

    def test_get_reflects_a_kill_immediately(self, live_server):
        srv, state = live_server
        state.mic_enabled = False
        state.camera_enabled = False
        _, body = _get(f"http://127.0.0.1:{srv.port}/api/vision-state")
        assert body["mic_enabled"] is False
        assert body["camera_enabled"] is False

    def test_unknown_path_is_404(self, live_server):
        srv, _ = live_server
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"http://127.0.0.1:{srv.port}/nope")
        assert exc_info.value.code == 404

    def test_cors_header_present(self, live_server):
        srv, _ = live_server
        with urllib.request.urlopen(  # noqa: S310 -- loopback only
            f"http://127.0.0.1:{srv.port}/api/vision-state", timeout=3
        ) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    def test_sse_stream_delivers_current_state(self, live_server):
        srv, state = live_server
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/vision-stream")
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 -- loopback only
            assert resp.headers.get("Content-Type") == "text/event-stream"
            chunk = resp.readline().decode("utf-8")
            assert chunk.startswith("data: ")
            payload = json.loads(chunk[len("data: "):].strip())
            assert payload["mic_enabled"] is True

    def test_double_start_is_idempotent(self, live_server):
        srv, _ = live_server
        ok, detail = srv.start()
        assert ok is True
        assert "already running" in detail

    def test_port_in_use_is_honest_failure(self):
        state = tray.KillSwitchState()
        first = vision_bridge.VisionBridgeServer(state_reader=lambda: state, port=0)
        assert first.start()[0] is True
        try:
            second = vision_bridge.VisionBridgeServer(
                state_reader=lambda: state, port=first.port
            )
            ok, detail = second.start()
            assert ok is False
            assert "could not bind" in detail
        finally:
            first.stop()

    def test_stop_then_start_again_reopens_the_port(self, live_server):
        srv, _ = live_server
        srv.stop()
        ok, detail = srv.start()
        assert ok, detail
        status, _ = _get(f"http://127.0.0.1:{srv.port}/api/vision-state")
        assert status == 200
        srv.stop()
