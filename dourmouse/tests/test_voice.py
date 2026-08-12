"""P7 voice tests — gate, honest probes, engine paths, webui routes.

All hermetic: no microphone, no model download, no network. The heavier
engines are stubbed at the import boundary or replaced with fakes; the HTTP
tests run against a real local server (Rules 2.1 / 2.8).
"""

from __future__ import annotations

import http.client
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from dourmouse import voice as voice_module
from dourmouse.general_roster import build_general_registry
from dourmouse.voice import (
    VoiceNotConfiguredError,
    speech_to_text,
    stt_available,
    text_to_speech,
    tts_engine,
    voice_enabled,
    voice_status,
)

# --------------------------------------------------------------------------- #
# Gate parsing
# --------------------------------------------------------------------------- #

class TestGate:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_VOICE", raising=False)
        assert voice_enabled() is False

    def test_off_values(self):
        for v in ("0", "false", "no", "off", ""):
            assert voice_enabled(v) is False, v

    def test_on_values(self):
        for v in ("1", "true", "yes", "on", "ON"):
            assert voice_enabled(v) is True, v


# --------------------------------------------------------------------------- #
# Honest capability probes
# --------------------------------------------------------------------------- #

class TestProbes:
    def test_stt_off_when_gate_off(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_VOICE", raising=False)
        assert stt_available() is False

    def test_stt_true_when_whisper_importable(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setitem(
            sys.modules, "faster_whisper", types.ModuleType("faster_whisper")
        )
        assert stt_available() is True

    def test_stt_false_when_whisper_missing(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        assert stt_available() is False

    def test_tts_engine_resolution(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        # piper importable -> piper wins
        monkeypatch.setitem(sys.modules, "piper", types.ModuleType("piper"))
        assert tts_engine() == "piper"
        # no piper, macOS say present -> say
        monkeypatch.setitem(sys.modules, "piper", None)
        monkeypatch.setattr(voice_module.shutil, "which", lambda _c: "/usr/bin/say")
        assert tts_engine() == "say"
        # neither -> honest not-configured
        monkeypatch.setattr(voice_module.shutil, "which", lambda _c: None)
        assert tts_engine() == "not-configured"

    def test_tts_engine_off_when_gate_off(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_VOICE", raising=False)
        assert tts_engine() == "not-configured"

    def test_voice_status_shape(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
        monkeypatch.setitem(sys.modules, "piper", None)  # explicit: test the say fallback
        monkeypatch.setattr(voice_module.shutil, "which", lambda _c: "/usr/bin/say")
        status = voice_status()
        assert status["enabled"] is True
        assert status["stt"] == "faster-whisper"
        assert status["tts"] == "say"


# --------------------------------------------------------------------------- #
# STT — gate + fake engine
# --------------------------------------------------------------------------- #

class _FakeSeg:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisper:
    def __init__(self, text: str) -> None:
        self._text = text

    def transcribe(self, audio, language=None, vad_filter=False):
        return ([_FakeSeg(self._text)], None)


class TestStt:
    def test_gate_off_raises_honestly(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_VOICE", raising=False)
        with pytest.raises(VoiceNotConfiguredError):
            speech_to_text(b"\x00fakeaudio")

    def test_empty_audio_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        with pytest.raises(ValueError):
            speech_to_text(b"")

    def test_returns_joined_transcription(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setattr(
            voice_module, "_load_whisper", lambda: _FakeWhisper("hello dourmouse")
        )
        assert speech_to_text(b"audio") == "hello dourmouse"

    def test_engine_failure_is_honest(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setattr(
            voice_module, "_load_whisper", lambda: _FakeWhisper("x")
        )
        monkeypatch.setattr(
            _FakeWhisper, "transcribe",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(VoiceNotConfiguredError):
            speech_to_text(b"audio")


# --------------------------------------------------------------------------- #
# TTS — engine resolution + macOS say path
# --------------------------------------------------------------------------- #

class TestTts:
    def test_gate_off_raises_honestly(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_VOICE", raising=False)
        with pytest.raises(VoiceNotConfiguredError):
            text_to_speech("hello")

    def test_empty_text_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        with pytest.raises(ValueError):
            text_to_speech("   ")

    def test_say_path_returns_wav(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setitem(sys.modules, "piper", None)
        monkeypatch.setattr(voice_module.shutil, "which", lambda _c: "/usr/bin/say")
        wav = (tmp_path / "s.wav")
        wav.write_bytes(b"RIFF-fake-wav")

        def _fake_run(cmd, capture_output=False, timeout=0, check=False):
            # write the wav where the last arg points
            out_path = Path(cmd[2])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"RIFF-fake-wav")
            return types.SimpleNamespace(returncode=0, stderr=b"")

        monkeypatch.setattr(voice_module.subprocess, "run", _fake_run)
        assert text_to_speech("hello") == b"RIFF-fake-wav"

    def test_say_failure_is_honest(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setitem(sys.modules, "piper", None)
        monkeypatch.setattr(voice_module.shutil, "which", lambda _c: "/usr/bin/say")
        monkeypatch.setattr(
            voice_module.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1, stderr=b"bad input"),
        )
        with pytest.raises(VoiceNotConfiguredError):
            text_to_speech("hello")

    def test_say_timeout_is_honest(self, monkeypatch):
        """A hung 'say' must degrade to NOT CONFIGURED, never a raw 500."""
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setitem(sys.modules, "piper", None)
        monkeypatch.setattr(voice_module.shutil, "which", lambda _c: "/usr/bin/say")

        def _hung(*a, **k):
            raise subprocess.TimeoutExpired(cmd=["say"], timeout=120)

        monkeypatch.setattr(voice_module.subprocess, "run", _hung)
        with pytest.raises(VoiceNotConfiguredError):
            text_to_speech("hello")

    def test_say_strips_bracket_commands(self, monkeypatch, tmp_path):
        """[[...]] say control sequences must not reach the engine."""

        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setitem(sys.modules, "piper", None)
        monkeypatch.setattr(voice_module.shutil, "which", lambda _c: "/usr/bin/say")
        seen: list[str] = []

        def _fake_run(cmd, capture_output=False, timeout=0, check=False):
            seen.append(cmd[-1])
            out_path = Path(cmd[2])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"RIFF-x")
            return types.SimpleNamespace(returncode=0, stderr=b"")

        monkeypatch.setattr(voice_module.subprocess, "run", _fake_run)
        text_to_speech("hello [[rate 500]] world")
        assert seen and "[[" not in seen[0] and "]]" not in seen[0]

    def test_tts_rejects_overlong_text(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        with pytest.raises(ValueError):
            text_to_speech("x" * 501)

    def test_no_engine_raises_honestly(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setitem(sys.modules, "piper", None)
        monkeypatch.setattr(voice_module.shutil, "which", lambda _c: None)
        with pytest.raises(VoiceNotConfiguredError):
            text_to_speech("hello")

    def test_piper_flat_path_and_mkdir(self, monkeypatch, tmp_path):
        """Pins the piper-tts >= 1.6 API (live-caught): download_voice writes
        FLAT files and does NOT mkdir; load takes the flat .onnx path."""
        monkeypatch.setenv("DOURMOUSE_VOICE", "1")
        monkeypatch.setenv("DOURMOUSE_PIPER_VOICE", "en_US-lessac-medium")
        monkeypatch.setattr(voice_module.Path, "home", lambda: tmp_path)

        calls: dict[str, str] = {}
        fake_piper = types.ModuleType("piper")
        fake_dl = types.ModuleType("piper.download_voices")

        def _fake_download_voice(voice: str, download_dir) -> None:
            calls["voice"] = voice
            calls["dir"] = str(download_dir)
            (download_dir / f"{voice}.onnx").write_bytes(b"ONNX")
            (download_dir / f"{voice}.onnx.json").write_text("{}")

        fake_dl.download_voice = _fake_download_voice

        class _FakeVoice:
            def __init__(self, model_path, download_dir=None, config_path=None):
                calls["model"] = str(model_path)

            def synthesize_wav(self, text, wav_file):
                wav_file.setframerate(22050)
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.writeframes(b"\x00\x00" * 100)

        fake_piper.PiperVoice = type(
            "PV", (),
            {"load": staticmethod(lambda model_path, download_dir=None, config_path=None: _FakeVoice(model_path))},
        )
        monkeypatch.setitem(sys.modules, "piper", fake_piper)
        monkeypatch.setitem(sys.modules, "piper.download_voices", fake_dl)

        wav = text_to_speech("hello")
        assert wav
        assert calls["voice"] == "en_US-lessac-medium"
        assert calls["model"].endswith("en_US-lessac-medium.onnx"), "flat path, no subdir"
        assert calls["dir"].endswith("piper_models")
        assert (tmp_path / ".local" / "share" / "piper_models").is_dir(), "mkdir happened"


# --------------------------------------------------------------------------- #
# Webui routes over real HTTP
# --------------------------------------------------------------------------- #

@pytest.fixture
def server(monkeypatch):
    from dourmouse.webui import run_server

    registry = build_general_registry()
    srv = run_server(registry, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _host, port = srv.server_address[:2]
    yield int(port)
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=3)


class TestWebRoutes:
    def test_api_voice_status(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", "/api/voice")
        resp = conn.getresponse()
        assert resp.status == 200
        import json

        body = json.loads(resp.read().decode())
        assert set(body) >= {"enabled", "stt", "tts"}
        conn.close()

    def test_post_speech_transcribes(self, server, monkeypatch):
        monkeypatch.setattr(
            voice_module, "speech_to_text", lambda audio, language=None: "mic hello"
        )
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request(
            "POST", "/api/speech",
            body=b"RIFF-audio-bytes",
            headers={"Content-Type": "audio/webm"},
        )
        resp = conn.getresponse()
        assert resp.status == 200
        import json

        body = json.loads(resp.read().decode())
        assert body == {"configured": True, "text": "mic hello"}
        conn.close()

    def test_post_speech_not_configured_is_honest(self, server, monkeypatch):
        monkeypatch.setattr(
            voice_module, "speech_to_text",
            lambda audio, language=None: (_ for _ in ()).throw(
                VoiceNotConfiguredError("DOURMOUSE_VOICE is off")
            ),
        )
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("POST", "/api/speech", body=b"audio", headers={"Content-Type": "audio/webm"})
        resp = conn.getresponse()
        import json

        body = json.loads(resp.read().decode())
        assert resp.status == 200
        assert body["configured"] is False
        assert "NOT CONFIGURED" in body["error"]
        conn.close()

    def test_post_speech_empty_body_400(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("POST", "/api/speech", body=b"", headers={"Content-Type": "audio/webm"})
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_post_speech_invalid_content_length_400(self, server):
        """A malformed Content-Length must not crash the handler (P7 review)."""
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
        conn.putrequest("POST", "/api/speech")
        conn.putheader("Content-Length", "abc")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_post_speech_oversized_body_400(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
        conn.putrequest("POST", "/api/speech")
        conn.putheader("Content-Length", "60000000")  # > 50 MB cap
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_post_speech_no_speech_is_honest(self, server, monkeypatch):
        """Whisper heard nothing -> explicit error, not a silent empty text."""
        import json as _json

        monkeypatch.setattr(voice_module, "speech_to_text", lambda audio, language=None: "")
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("POST", "/api/speech", body=b"audio", headers={"Content-Type": "audio/webm"})
        resp = conn.getresponse()
        body = _json.loads(resp.read().decode())
        assert resp.status == 200
        assert body["configured"] is True
        assert body["text"] == ""
        assert "no speech detected" in body["error"]
        conn.close()

    def test_get_speech_tts_returns_wav(self, server, monkeypatch):
        monkeypatch.setattr(voice_module, "text_to_speech", lambda text: b"RIFF-wav-bytes")
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", "/api/speech?text=" + "hello")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "audio/wav"
        assert resp.read() == b"RIFF-wav-bytes"
        conn.close()

    def test_get_speech_missing_text_400(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", "/api/speech")
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()


# --------------------------------------------------------------------------- #
# HUD wiring
# --------------------------------------------------------------------------- #

class TestHudWiring:
    UI_DIR = Path(__file__).resolve().parents[2] / "ui"

    def test_mic_and_speaker_buttons_present(self):
        html = (self.UI_DIR / "index.html").read_text()
        assert 'id="micBtn"' in html
        assert 'id="spkBtn"' in html

    def test_hud_calls_the_voice_endpoints(self):
        html = (self.UI_DIR / "index.html").read_text()
        assert "fetch('/api/voice')" in html
        assert "fetch('/api/speech'" in html
        assert "fetch('/api/speech?text=' + encodeURIComponent" in html

    def test_no_external_urls_in_voice_js(self):
        html = (self.UI_DIR / "index.html").read_text()
        script = html.split("<script>")[-1].split("</script>")[0]
        assert "https://" not in script and "http://" not in script
