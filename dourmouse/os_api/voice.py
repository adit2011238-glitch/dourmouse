"""Backend for the OS shell's VOICE screen (finding #152).

The command parser (``POST /api/voice/command``) and the capability report
(``GET /api/voice``) already exist and the screen calls them as they are. Two
things had no route:

* ``GET /api/os/voice/info``: one cheap, honest read of what the screen needs
  around them: the wake-word state (the environment gate and whether the two audio packages are installed, checked
  without importing them, because importing ``sounddevice`` can block on the
  microphone prompt), whether the hands-free loop is actually running (what the server
  started at boot, as ``/api/hands_free/status`` reports it), the parser's own command list (``available_commands``, so the help
  text can never drift from the grammar) and the panel ids the parser accepts.
* ``POST /api/os/voice/transcribe {audio_b64}``: the same local speech to text
  as ``POST /api/speech``, but the audio arrives as base64 inside JSON because
  the shell's one API client sends JSON only. It is capped at 8 MB of audio,
  refuses honestly when ``DOURMOUSE_VOICE`` is off, and performs no action: it
  returns text and the screen decides what to do with it.
"""

from __future__ import annotations

import base64
import binascii
import importlib.util
from typing import Any

from . import ApiError, Request, route

MAX_AUDIO_BYTES = 8 * 1024 * 1024


def _installed(module: str) -> str:
    """Whether a package is installed, decided WITHOUT importing it: importing
    ``sounddevice`` initialises PortAudio, which can block on (or trigger) the
    microphone permission prompt for the whole server."""
    try:
        return "installed" if importlib.util.find_spec(module) is not None else "not-configured"
    except (ImportError, ValueError):
        return "not-configured"


def wakeword_report() -> dict[str, Any]:
    """The same fields as ``wakeword.wakeword_status`` but reading only the
    environment and the package index, never importing the audio libraries."""
    from dourmouse.wakeword import wakeword_enabled, wakeword_model, wakeword_threshold

    return {"enabled": wakeword_enabled(), "inference_engine": _installed("openwakeword"),
            "capture_engine": _installed("sounddevice"), "model": wakeword_model(),
            "threshold": wakeword_threshold()}


@route("GET", "/api/os/voice/info")
def info(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse.voice_commands import _PANEL_ALIASES, available_commands

    hands = dict(getattr(req.server, "hands_free_status", None) or {"enabled": False, "reason": "not started"})
    controller = getattr(req.server, "hands_free", None)
    hands["running"] = bool(controller and controller.running)
    return 200, {"ok": True, "wakeword": wakeword_report(), "hands_free": hands, "commands": available_commands(),
                 "panels": sorted(set(_PANEL_ALIASES.values()))}


@route("POST", "/api/os/voice/transcribe")
def transcribe(req: Request) -> tuple[int, dict[str, Any]]:
    body = req.body if isinstance(req.body, dict) else {}
    raw = body.get("audio_b64")
    if not isinstance(raw, str) or not raw:
        raise ApiError(400, "audio_b64 is required")
    if len(raw) > (MAX_AUDIO_BYTES * 4) // 3 + 8:
        raise ApiError(413, f"the recording is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
    try:
        audio = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "audio_b64 is not valid base64") from None
    if not audio:
        raise ApiError(400, "the recording is empty")
    from dourmouse.voice import VoiceNotConfiguredError, speech_to_text

    try:
        text = speech_to_text(audio)
    except VoiceNotConfiguredError as exc:
        raise ApiError(503, f"NOT CONFIGURED: {exc}") from None
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return 200, {"ok": True, "text": text, "heard_speech": bool(text)}
