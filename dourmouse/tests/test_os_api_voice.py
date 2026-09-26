"""Finding #152: the VOICE screen's routes (os_api/voice.py) on a port-0 server."""

from __future__ import annotations

import base64
import http.client
import json
import threading

import pytest

from dourmouse.general_roster import build_general_registry


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("DOURMOUSE_VOICE", raising=False)
    monkeypatch.delenv("DOURMOUSE_WAKEWORD", raising=False)
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_info_reports_wakeword_hands_free_and_the_parsers_own_commands(server):
    status, d = call(server, "GET", "/api/os/voice/info")
    assert status == 200 and d["wakeword"]["enabled"] is False
    assert d["hands_free"]["running"] is False
    assert [c["pattern"] for c in d["commands"]][:1] == ["email <person> saying <message>"]
    assert {"mail", "chat", "research", "map", "globe"} <= set(d["panels"])


def test_info_follows_the_environment(server, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
    assert call(server, "GET", "/api/os/voice/info")[1]["wakeword"]["enabled"] is True


def test_the_existing_parser_route_answers_what_the_screen_relies_on(server):
    ok = call(server, "POST", "/api/voice/command", {"text": "open mail"})[1]
    assert ok["recognized"] and ok["command"]["action"] == "open_panel" and ok["command"]["args"]["panel"] == "mail"
    assert call(server, "POST", "/api/voice/command", {"text": "what time is it"})[1] == {"ok": True, "recognized": False}


def test_transcribe_refuses_honestly_when_voice_is_off(server):
    status, d = call(server, "POST", "/api/os/voice/transcribe", {"audio_b64": base64.b64encode(b"abc").decode()})
    assert status == 503 and "NOT CONFIGURED" in d["error"] and "DOURMOUSE_VOICE" in d["error"]


def test_transcribe_validates_its_input(server):
    assert call(server, "POST", "/api/os/voice/transcribe", {})[0] == 400
    assert call(server, "POST", "/api/os/voice/transcribe", {"audio_b64": "***not base64***"})[0] == 400
    too_big = "A" * (8 * 1024 * 1024 * 4 // 3 + 100)
    status, d = call(server, "POST", "/api/os/voice/transcribe", {"audio_b64": too_big})
    assert status == 413 and "larger than 8 MB" in d["error"]


def test_transcribe_returns_text_from_the_speech_engine(server, monkeypatch):
    import dourmouse.voice as voice

    monkeypatch.setattr(voice, "speech_to_text", lambda audio: "open mail" if audio == b"pcm" else "")
    d = call(server, "POST", "/api/os/voice/transcribe", {"audio_b64": base64.b64encode(b"pcm").decode()})[1]
    assert d == {"ok": True, "text": "open mail", "heard_speech": True}
    d = call(server, "POST", "/api/os/voice/transcribe", {"audio_b64": base64.b64encode(b"other").decode()})[1]
    assert d["heard_speech"] is False
