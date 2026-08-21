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
    strip_markdown_for_speech,
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
# Markdown stripping (v8.18 voice/text response split)
# --------------------------------------------------------------------------- #

class TestMarkdownStripping:
    def test_empty_text_passes_through(self):
        assert strip_markdown_for_speech("") == ""

    def test_plain_prose_is_untouched(self):
        text = "The quick brown fox jumps over the lazy dog."
        assert strip_markdown_for_speech(text) == text

    def test_headers_stripped(self):
        out = strip_markdown_for_speech("# Title\n\nSome body text.")
        assert "#" not in out
        assert "Title" in out
        assert "Some body text." in out

    def test_multilevel_headers_stripped(self):
        out = strip_markdown_for_speech("## Section\n### Subsection")
        assert "#" not in out
        assert "Section" in out and "Subsection" in out

    def test_bold_star_stripped(self):
        assert strip_markdown_for_speech("This is **bold** text.") == "This is bold text."

    def test_bold_underscore_stripped(self):
        assert strip_markdown_for_speech("This is __bold__ text.") == "This is bold text."

    def test_italic_star_stripped(self):
        assert strip_markdown_for_speech("This is *italic* text.") == "This is italic text."

    def test_italic_underscore_stripped(self):
        assert strip_markdown_for_speech("This is _italic_ text.") == "This is italic text."

    def test_bold_and_italic_together(self):
        out = strip_markdown_for_speech("**bold** and *italic* and __also bold__")
        assert "*" not in out and "_" not in out
        assert "bold" in out and "italic" in out and "also bold" in out

    def test_fenced_code_block_backticks_removed_content_kept(self):
        out = strip_markdown_for_speech("Run this:\n```python\nx = 1\nprint(x)\n```")
        assert "`" not in out
        assert "x = 1" in out
        assert "print(x)" in out

    def test_fenced_code_block_language_tag_not_spoken(self):
        out = strip_markdown_for_speech("```python\nx = 1\n```")
        # the fence's language annotation is boundary syntax, not code —
        # only the code body ("x = 1") should survive.
        assert out == "x = 1"

    def test_inline_code_backticks_removed_content_kept(self):
        assert strip_markdown_for_speech("Run `ls -la` now") == "Run ls -la now"

    def test_bullet_list_markers_removed(self):
        out = strip_markdown_for_speech("- one\n- two\n- three")
        assert "- " not in out
        for word in ("one", "two", "three"):
            assert word in out

    def test_bullet_list_with_asterisk_and_plus_markers(self):
        out = strip_markdown_for_speech("* alpha\n+ beta")
        assert not out.lstrip().startswith("*")
        assert not out.lstrip().startswith("+")
        assert "alpha" in out and "beta" in out

    def test_numbered_list_markers_removed(self):
        out = strip_markdown_for_speech("1. first\n2. second\n3. third")
        assert "1." not in out and "2." not in out and "3." not in out
        for word in ("first", "second", "third"):
            assert word in out

    def test_table_pipes_and_separator_removed(self):
        table = "| Name | Age |\n|------|-----|\n| Alice | 30 |"
        out = strip_markdown_for_speech(table)
        assert "|" not in out
        assert "---" not in out
        for word in ("Name", "Age", "Alice", "30"):
            assert word in out

    def test_table_data_row_reads_as_a_list(self):
        out = strip_markdown_for_speech("| Alice | 30 | Engineer |")
        assert out == "Alice, 30, Engineer"

    def test_horizontal_rule_alone_is_left_alone(self):
        # Out of scope by design: the task asks for table PIPES, not every
        # markdown rule, so a bare "---" thematic break (no pipes) is not
        # treated as a table separator.
        out = strip_markdown_for_speech("above\n---\nbelow")
        assert "above" in out and "below" in out

    # ---- readability: ordinary punctuation must survive untouched ---- #

    def test_contractions_survive(self):
        text = "I don't think it's ready yet."
        assert strip_markdown_for_speech(text) == text

    def test_hyphenated_compounds_survive(self):
        text = "This is a state-of-the-art, well-tested system."
        assert strip_markdown_for_speech(text) == text

    def test_code_identifiers_with_underscores_survive(self):
        text = "Call get_user_data with my_var_name as the argument."
        assert strip_markdown_for_speech(text) == text

    def test_multiplication_asterisk_survives(self):
        text = "The area is 2 * 3 = 6 square feet."
        assert strip_markdown_for_speech(text) == text

    def test_exponent_double_star_survives(self):
        text = "In Python, 2**3 equals 8."
        assert strip_markdown_for_speech(text) == text

    def test_combined_reply_has_no_stray_markdown_characters(self):
        """A realistic mixed reply loses every markdown character while
        keeping the words readable — not mangled into a run-on blob."""
        reply = (
            "# Summary\n\n"
            "Here is what I found, **in short**:\n\n"
            "- The `config.py` file sets *defaults*\n"
            "- It doesn't override __anything__ else\n\n"
            "| File | Lines |\n|------|-------|\n| config.py | 42 |\n\n"
            "```python\ndef load():\n    return True\n```"
        )
        out = strip_markdown_for_speech(reply)
        for ch in ("#", "*", "`", "|"):
            assert ch not in out, f"stray {ch!r} left in: {out!r}"
        for word in ("Summary", "short", "config.py", "defaults", "doesn't", "anything", "42", "def load", "return True"):
            assert word in out, f"expected {word!r} in: {out!r}"


# --------------------------------------------------------------------------- #
# TTS engines receive stripped text (v8.18)
# --------------------------------------------------------------------------- #

class TestTtsStripsMarkdown:
    def test_say_engine_receives_stripped_text(self, monkeypatch, tmp_path):
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
        text_to_speech("# Heading\n\nThis is **bold** and `code`.")
        assert seen
        spoken = seen[0]
        assert "#" not in spoken and "*" not in spoken and "`" not in spoken
        assert "Heading" in spoken and "bold" in spoken and "code" in spoken

    def test_plain_text_still_reaches_say_unchanged(self, monkeypatch):
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
        text_to_speech("hello there, how are you?")
        assert seen[0] == "hello there, how are you?"


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

    @staticmethod
    def _read_index() -> str:
        # HTML is UTF-8 (declared in <meta charset>); the locale default
        # (cp1252 on Windows) chokes on non-ASCII — read explicitly.
        return (TestHudWiring.UI_DIR / "index.html").read_text(encoding="utf-8")

    def test_mic_and_speaker_buttons_present(self):
        html = self._read_index()
        assert 'id="micBtn"' in html
        assert 'id="spkBtn"' in html

    def test_hud_calls_the_voice_endpoints(self):
        html = self._read_index()
        assert "fetch('/api/voice')" in html
        assert "fetch('/api/speech'" in html
        assert "fetch('/api/speech?text=' + encodeURIComponent" in html

    def test_no_external_urls_in_voice_js(self):
        html = self._read_index()
        script = html.split("<script>")[-1].split("</script>")[0]
        # Loopback URLs are local (the ATLAS Terminal button opens
        # http://127.0.0.1:<port>); the contract is NO EXTERNAL network.
        import re

        external = re.findall(r"https?://[^\s\"'`)]+", script)
        external = [u for u in external if "127.0.0.1" not in u and "localhost" not in u]
        assert not external, f"external URLs in voice js: {external}"
