"""Fully-local voice round-trip (v4.1, P7) — STT + TTS with zero cloud calls.

Honest degradation is the contract (Rules 2.2 / 2.8): nothing here is ever
faked, and every capability reports exactly why it cannot run.

Engines:
- STT: ``faster-whisper`` (CTranslate2, fully local). Lazy-loaded; the model
  id/dir comes from ``DOURMOUSE_WHISPER_MODEL`` (default ``large-v3-turbo`` —
  the best accuracy-per-watt STT that still runs on CPU/Metal; first use
  downloads it once from HuggingFace, afterwards it is local).
- TTS: ``piper`` (local ONNX) when importable; otherwise the macOS built-in
  ``say`` CLI (zero dependencies, fully local) as a documented fallback;
  otherwise honest NOT CONFIGURED.

Content shaping (v8.18): ``text_to_speech`` strips markdown formatting
(``strip_markdown_for_speech``) before either engine ever sees the text,
so a reply written with headers/bullets/tables is spoken as prose instead
of literal ``#``/``*``/``|`` characters. The companion half of the voice/
text response split — the system-prompt-level instruction that shapes a
*reply* for being spoken in the first place (short, no markdown structure,
plain confirmations) — lives in dispatch.py's ``_VOICE_MARKER``, applied
only to turns the caller marks as arriving on the voice channel.

Env:
- ``DOURMOUSE_VOICE=1``          -> enable the voice endpoints (default off).
- ``DOURMOUSE_WHISPER_MODEL``    -> faster-whisper model id or local dir.
- ``DOURMOUSE_WHISPER_DEVICE``   -> ``auto`` | ``cpu`` | ``cuda`` (default auto).
- ``DOURMOUSE_PIPER_VOICE``      -> piper voice key (e.g. en_US-amy-medium).

All engine imports are lazy (first real call only), so the desktop app's
startup cost stays zero even when the heavy wheels are installed.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave as _wave
from pathlib import Path
from typing import Any

_VOICE_ENV = "DOURMOUSE_VOICE"
_WHISPER_MODEL_ENV = "DOURMOUSE_WHISPER_MODEL"
_WHISPER_DEVICE_ENV = "DOURMOUSE_WHISPER_DEVICE"
_PIPER_VOICE_ENV = "DOURMOUSE_PIPER_VOICE"

_OFF_VALUES = {"", "0", "false", "no", "off"}

_DEFAULT_WHISPER_MODEL = "large-v3-turbo"
_DEFAULT_PIPER_VOICE = "en_US-amy-medium"
_MAX_TTS_CHARS = 500

# Serializes BOTH model loading and transcription: faster-whisper's
# WhisperModel is not safe for concurrent transcribe() on one instance, and
# the ThreadingHTTPServer can serve parallel /api/speech requests. RLock so
# speech_to_text can safely call _load_whisper (which locks too).
_stt_lock = threading.RLock()


class VoiceNotConfiguredError(RuntimeError):
    """Raised honestly when a voice capability cannot run (Rule 2.2)."""


def voice_enabled(value: str | None = None) -> bool:
    """``DOURMOUSE_VOICE`` gate; default OFF (voice is an opt-in extra)."""
    if value is None:
        value = os.environ.get(_VOICE_ENV, "0")
    return str(value).strip().lower() not in _OFF_VALUES


def whisper_model() -> str:
    raw = os.environ.get(_WHISPER_MODEL_ENV, _DEFAULT_WHISPER_MODEL)
    return (raw or "").strip() or _DEFAULT_WHISPER_MODEL


def whisper_device() -> str:
    raw = os.environ.get(_WHISPER_DEVICE_ENV, "auto")
    return (raw or "").strip().lower() or "auto"


def piper_voice() -> str:
    raw = os.environ.get(_PIPER_VOICE_ENV, "")
    return (raw or "").strip() or _DEFAULT_PIPER_VOICE


# --------------------------------------------------------------------------- #
# Honest capability probes (no heavy imports — cheap for the status endpoint)
# --------------------------------------------------------------------------- #

def stt_available() -> bool:
    """True when the gate is on AND faster-whisper is importable."""
    if not voice_enabled():
        return False
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def tts_engine() -> str:
    """``piper`` | ``say`` | ``not-configured`` — which TTS engine can run.

    Resolved deterministically (Rule 2.8): piper when importable, else the
    macOS built-in ``say`` when present, else honest not-configured.
    """
    if not voice_enabled():
        return "not-configured"
    try:
        import piper  # noqa: F401
    except ImportError:
        return "say" if shutil.which("say") else "not-configured"
    return "piper"


def voice_status() -> dict[str, Any]:
    """Honest capability report for the HUD + /api/voice."""
    return {
        "enabled": voice_enabled(),
        "stt": "faster-whisper" if stt_available() else "not-configured",
        "tts": tts_engine(),
        "whisper_model": whisper_model(),
        "whisper_device": whisper_device(),
        "piper_voice": piper_voice(),
    }


# --------------------------------------------------------------------------- #
# STT — faster-whisper (local, lazy)
# --------------------------------------------------------------------------- #

_whisper_singleton: Any | None = None


def _load_whisper() -> Any:
    """Load the faster-whisper model once; raise VoiceNotConfiguredError honestly.

    Double-checked under ``_stt_lock`` so concurrent requests can never
    double-load the model (ThreadingHTTPServer)."""
    global _whisper_singleton
    if _whisper_singleton is not None:
        return _whisper_singleton
    with _stt_lock:
        if _whisper_singleton is not None:
            return _whisper_singleton
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceNotConfiguredError(
                "faster-whisper is not installed — run: pip install -r "
                "requirements-voice.txt"
            ) from exc
        try:
            _whisper_singleton = WhisperModel(
                whisper_model(),
                device=whisper_device(),
                compute_type="int8",
            )
        except Exception as exc:  # model download failure / bad device / corrupt cache
            _whisper_singleton = None
            raise VoiceNotConfiguredError(
                f"faster-whisper could not load model {whisper_model()!r}: {exc}"
            ) from exc
        return _whisper_singleton


def speech_to_text(audio_bytes: bytes, language: str | None = None) -> str:
    """Transcribe local audio bytes (wav/webm/ogg/mp3) fully locally.

    Raises ``VoiceNotConfiguredError`` honestly when the gate is off or the
    engine/model is unavailable; ``ValueError`` on empty input. The web layer
    converts both into clean JSON responses.
    """
    if not voice_enabled():
        raise VoiceNotConfiguredError(
            "DOURMOUSE_VOICE is off — speech-to-text is NOT CONFIGURED. "
            "Set DOURMOUSE_VOICE=1 in .env to enable it."
        )
    if not audio_bytes:
        raise ValueError("no audio data received")
    # Hold the lock across load AND transcribe: WhisperModel is not safe for
    # concurrent inference on one instance.
    with _stt_lock:
        model = _load_whisper()
        try:
            segments, _info = model.transcribe(
                io.BytesIO(audio_bytes),
                language=language or None,
                vad_filter=True,
            )
            return "".join(seg.text for seg in segments).strip()
        except VoiceNotConfiguredError:
            raise
        except Exception as exc:
            raise VoiceNotConfiguredError(f"transcription failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# Markdown stripping — v8.18 (voice/text response split). Text reaching a
# TTS engine must be prose, not markup: nothing in dourmouse/requirements*
# pulls in a markdown-to-text library (checked before writing this), so
# this is a targeted pass over only the constructs that actually show up in
# real replies -- headers, bold/italic, fenced/inline code, bullet and
# numbered lists, table pipes -- not a general CommonMark parser. A parser
# broad enough to handle the whole spec is also broad enough to mangle
# ordinary punctuation (contractions, hyphenated words, code identifiers
# with underscores) that only looks like markdown, which is the actual
# failure mode to avoid here.
# --------------------------------------------------------------------------- #

_MD_FENCE_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# Emphasis markers require a non-word boundary OUTSIDE and non-space content
# immediately INSIDE — this is what keeps "my_var_name" and "2 * 3" intact
# while still catching "**bold**" / "*italic*" / "__bold__" / "_italic_".
_MD_BOLD_STAR_RE = re.compile(r"(?<!\w)\*\*(\S(?:[^*\n]*\S)?)\*\*(?!\w)")
_MD_BOLD_UNDER_RE = re.compile(r"(?<!\w)__(\S(?:[^_\n]*\S)?)__(?!\w)")
_MD_ITALIC_STAR_RE = re.compile(r"(?<!\w)\*(\S(?:[^*\n]*\S)?)\*(?!\w)")
_MD_ITALIC_UNDER_RE = re.compile(r"(?<!\w)_(\S(?:[^_\n]*\S)?)_(?!\w)")
_MD_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_BULLET_RE = re.compile(r"^\s*[*\-+]\s+")
_MD_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+")
# A standalone table-separator row, e.g. "|---|---|" or "| :-- | --: |" —
# requires at least one pipe, so a bare "---" thematic break is left alone
# (out of scope: the task asks for table PIPES, not every markdown rule).
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def strip_markdown_for_speech(text: str) -> str:
    """Plain-prose version of ``text`` for a TTS engine to actually speak.

    Not a general markdown renderer (Rule 2.8 — deterministic, no LLM
    judgement involved): a fixed pass over the specific constructs above,
    ordered so a later pass never re-sees what an earlier one consumed:

    1. fenced code blocks — the ``` fence markers are dropped and the code
       text inside is kept and spoken as plain words, never read aloud as
       literal backtick characters;
    2. inline code spans — backticks dropped, content kept;
    3. per-line prefixes — header hashes, bullet markers, numbered-list
       markers, and standalone table-separator lines;
    4. table pipes remaining in a data row — turned into commas so a row
       reads as a list of values instead of a wall of ``|`` characters;
    5. bold/italic markers — dropped, content kept (double-char markers
       resolved before single-char ones, so "**x**" is fully consumed
       before the italic pass ever sees a lone "*").
    """
    if not text:
        return text

    text = _MD_FENCE_RE.sub(lambda m: m.group(1).strip("\n"), text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)

    out_lines = []
    for line in text.split("\n"):
        if _MD_TABLE_SEP_RE.match(line):
            continue  # a separator row carries no spoken content at all
        line = _MD_HEADER_RE.sub("", line)
        line = _MD_BULLET_RE.sub("", line)
        line = _MD_NUMBERED_RE.sub("", line)
        if line.count("|") >= 2:
            line = ", ".join(p.strip() for p in line.strip().strip("|").split("|"))
        out_lines.append(line)
    text = "\n".join(out_lines)

    text = _MD_BOLD_STAR_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDER_RE.sub(r"\1", text)
    text = _MD_ITALIC_STAR_RE.sub(r"\1", text)
    text = _MD_ITALIC_UNDER_RE.sub(r"\1", text)

    # Collapse the blank lines/extra spacing list and table stripping tends
    # to leave behind, without joining separate sentences onto one line.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# TTS — piper (local ONNX) with a macOS 'say' zero-dep fallback
# --------------------------------------------------------------------------- #

def text_to_speech(text: str) -> bytes:
    """Synthesize WAV audio locally; returns 16-bit PCM WAV bytes.

    Engine resolution: piper when importable, else macOS ``say`` (zero-dep,
    fully local). Raises ``VoiceNotConfiguredError`` honestly when the gate
    is off or no engine can run; ``ValueError`` on empty text.

    v8.18: markdown is stripped BEFORE the length cap and BEFORE either
    engine ever sees the text — a reply with headers/bullets/tables would
    otherwise have Piper or ``say`` read out literal ``#``/``*``/``|``
    characters, and the cap is more accurate measured against what will
    actually be spoken than against the un-stripped source. A stripped
    result that comes back empty (e.g. input was pure markup with no prose)
    falls back to the original text rather than silently failing.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("no text to speak")
    stripped = strip_markdown_for_speech(text)
    text = stripped if stripped.strip() else text
    if len(text) > _MAX_TTS_CHARS:
        raise ValueError(
            f"text too long for TTS ({len(text)} > {_MAX_TTS_CHARS} chars)"
        )
    if not voice_enabled():
        raise VoiceNotConfiguredError(
            "DOURMOUSE_VOICE is off — text-to-speech is NOT CONFIGURED. "
            "Set DOURMOUSE_VOICE=1 in .env to enable it."
        )
    engine = tts_engine()
    if engine == "piper":
        return _piper_speak(text)
    if engine == "say":
        return _say_speak(text)
    raise VoiceNotConfiguredError(
        "no local TTS engine — install piper (pip install -r requirements-voice.txt) "
        "or run on macOS (uses the built-in 'say')."
    )


def _say_speak(text: str) -> bytes:
    """macOS built-in TTS -> 16-bit PCM 22.05kHz mono WAV. Zero dependencies.

    ``say`` interprets ``[[...]]`` control sequences inside the text (rate/
    voice commands); they are neutralized so a crafted ?text= cannot alter
    the spoken output."""
    if not shutil.which("say"):
        raise VoiceNotConfiguredError("macOS 'say' not found — no TTS engine."
        )
    safe = text.replace("[[", "(").replace("]]", ")")
    tmp = Path(tempfile.mkdtemp(prefix="dourmouse_voice_"))
    out = tmp / "speech.wav"
    try:
        try:
            proc = subprocess.run(
                ["say", "-o", str(out), "--data-format=LEI16@22050", safe],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise VoiceNotConfiguredError(f"'say' failed: {exc}") from exc
        if proc.returncode != 0 or not out.is_file():
            detail = (proc.stderr or b"").decode(errors="replace").strip()[:200]
            raise VoiceNotConfiguredError(f"'say' failed: {detail or 'no audio produced'}")
        data = out.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not data:
        raise VoiceNotConfiguredError("'say' produced no audio.")
    return data


def _piper_speak(text: str) -> bytes:
    """piper (local ONNX) TTS -> WAV bytes. Voice fetched once, then local.

    Written against piper-tts >= 1.6 (the API that installs cleanly on
    Python 3.12 / macOS arm64): ``download_voice`` fetches the voice into a
    local dir on first use, ``PiperVoice.load`` reads it, and
    ``synthesize_wav`` writes a stdlib ``wave`` stream.
    """
    try:
        from piper import PiperVoice  # type: ignore[import-not-found]
        from piper.download_voices import (
            download_voice,  # type: ignore[import-not-found]
        )
    except ImportError as exc:
        raise VoiceNotConfiguredError(
            "piper is not installed (piper-tts >= 1.6 required) — run: "
            "pip install -r requirements-voice.txt"
        ) from exc
    voice = piper_voice()
    # download_voice writes FLAT files (<dir>/<voice>.onnx[.json]) and does
    # NOT create the directory itself — so mkdir first.
    data_dir = Path.home() / ".local" / "share" / "piper_models"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        download_voice(voice, data_dir)
        model_path = data_dir / f"{voice}.onnx"
        engine = PiperVoice.load(model_path, download_dir=data_dir)
        buf = io.BytesIO()
        with _wave.open(buf, "wb") as wav_out:
            engine.synthesize_wav(text, wav_out)
        return buf.getvalue()
    except VoiceNotConfiguredError:
        raise
    except Exception as exc:
        raise VoiceNotConfiguredError(f"piper failed: {exc}") from exc
