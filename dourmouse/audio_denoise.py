"""Real-time acoustic noise scrubbing ahead of STT (v13.5).

Vision OS checklist item 5: "Audio input streams through RNNoise, a
lightweight recurrent neural network library designed specifically for
real-time suppression of office background noise, keyboard clatter, and
HVAC hums directly on CPU threads. Cleaned audio is then processed
locally by Faster-Whisper..."

Wraps the REAL RNNoise C library via the ``pyrnnoise`` package's
low-level ``pyrnnoise.rnnoise`` ctypes binding — NOT that package's
higher-level ``RNNoise``/``denoise_chunk`` wrapper, which (installed
version 0.4.3 against audiolab 0.5.2, both current on PyPI at the time
this was written) has a real, live-reproduced bug: it calls its internal
resampling ``Graph(rate=...)`` but the installed ``audiolab.av.graph.Graph``
only accepts ``sample_rate=...`` — a genuine upstream version mismatch,
confirmed live in this exact environment (``TypeError: Graph.__init__()
got an unexpected keyword argument 'rate'``), not a mistake in how this
module calls it. The low-level binding (``pyrnnoise.rnnoise.create/
process_mono_frame/destroy``) sidesteps that broken layer entirely and
was verified live, right here: a real 480-sample int16 frame through it
returned real denoised audio and a real speech-probability float.

RNNoise's native format is fixed and non-negotiable (it is a trained
model, not a configurable DSP filter): 48000 Hz, mono, int16, exactly
480 samples (10ms) per frame. This codebase's real audio pipeline
(dourmouse/wakeword.py's ``_SAMPLE_RATE = 16000`` / ``_CHUNK_SAMPLES =
1280``, reused directly by dourmouse/hands_free.py) captures at 16000 Hz
in 1280-sample (80ms) chunks. 1280 samples at 16kHz resampled to 48kHz is
exactly 3840 samples = exactly 8 whole 480-sample RNNoise frames with ZERO
remainder (1280 * 3 / 480 == 8.0 exactly) — this is not a coincidence
this module relies on blindly; it is the real reason ``RnnoiseDenoiser``
below only needs to support the ONE chunk size (1280 @ 16kHz) this
codebase's real capture loop actually produces, with no partial-frame
buffering across calls.

Resampling uses scipy's real polyphase resampler (``resample_poly``,
exact 3:1 / 1:3 ratios for 16000<->48000 — no approximation error from a
non-integer ratio). scipy is already a real dependency of this project.

Honesty (Rule 2.1/2.2, same as every other voice/audio capability this
session): this is verified real Python + a real RNNoise C library call
against real synthetic and file-based audio (dourmouse/tests/
test_audio_denoise.py) — what is NOT and cannot be verified from this
sandbox is whether it actually improves faster-whisper's real transcription
accuracy against real background noise; that needs a live desktop session
with a real noisy room and a real microphone.
"""

from __future__ import annotations

import os

import numpy as np

_DENOISE_ENV = "DOURMOUSE_DENOISE"
_CAPTURE_RATE = 16000  # dourmouse/wakeword.py's own fixed capture rate
_RNNOISE_RATE = 48000  # RNNoise's fixed native rate — not configurable
_RNNOISE_FRAME = 480  # RNNoise's fixed native frame size (10ms @ 48kHz)
_UP, _DOWN = 3, 1  # 16000 * 3 == 48000 exactly


def denoise_enabled(value: str | None = None) -> bool:
    """DOURMOUSE_DENOISE: run captured mic audio through RNNoise before
    it reaches the wake-word detector / speech-to-text. Default on — set
    to 0/off to disable (e.g. if pyrnnoise isn't installed on a given
    machine; see RnnoiseDenoiser.create_default's own honest fallback).
    """
    raw = value if value is not None else os.environ.get(_DENOISE_ENV, "1")
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


class RnnoiseDenoiser:
    """One real RNNoise state, reused across a whole recording session
    (matching RNNoise's own intended usage — the model carries adaptive
    state across frames, which is HOW it tracks "this is the noise floor"
    vs "this is speech"; a fresh state per chunk would throw that away
    and denoise worse, not better).

    ``process()`` takes a 1D int16 numpy array of EXACTLY 1280 samples at
    16000 Hz (this codebase's one real capture chunk size — see this
    module's own docstring for why that's not a limitation in practice)
    and returns a denoised 1280-sample int16 array at the same rate.
    Never mutates the input array.
    """

    def __init__(self) -> None:
        from pyrnnoise import rnnoise as _rn

        self._rn = _rn
        self._state = _rn.create()
        self._closed = False

    def process(self, chunk: np.ndarray) -> np.ndarray:
        if self._closed:
            raise RuntimeError("RnnoiseDenoiser used after close()")
        if chunk.ndim != 1:
            raise ValueError(f"expected a 1D mono chunk, got shape {chunk.shape}")
        if chunk.shape[0] * _UP % _RNNOISE_FRAME != 0:
            raise ValueError(
                f"chunk length {chunk.shape[0]} does not resample to a whole "
                f"number of {_RNNOISE_FRAME}-sample RNNoise frames at "
                f"{_RNNOISE_RATE}Hz — this codebase's real capture chunk "
                f"(1280 samples @ 16kHz) always does; a different chunk size "
                f"needs its own framing/buffering, not silently zero-padded."
            )
        # Real polyphase upsample 16kHz -> 48kHz (exact 3:1 ratio, no
        # approximation error).
        from scipy.signal import resample_poly

        up_float = resample_poly(chunk.astype(np.float32), up=_UP, down=_DOWN)
        up_int16 = np.clip(np.round(up_float), -32768, 32767).astype(np.int16)

        denoised_frames: list[np.ndarray] = []
        n_frames = up_int16.shape[0] // _RNNOISE_FRAME
        for i in range(n_frames):
            frame = up_int16[i * _RNNOISE_FRAME:(i + 1) * _RNNOISE_FRAME]
            denoised_frame, _speech_prob = self._rn.process_mono_frame(self._state, frame)
            denoised_frames.append(denoised_frame)
        denoised_48k = np.concatenate(denoised_frames)

        # Real polyphase downsample back to 16kHz for the rest of the
        # pipeline (wake-word detection, faster-whisper) — both expect
        # 16kHz per dourmouse/wakeword.py's own _SAMPLE_RATE contract.
        down_float = resample_poly(denoised_48k.astype(np.float32), up=_DOWN, down=_UP)
        return np.clip(np.round(down_float), -32768, 32767).astype(np.int16)

    def close(self) -> None:
        if not self._closed:
            self._rn.destroy(self._state)
            self._closed = True

    def __enter__(self) -> "RnnoiseDenoiser":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def create_default() -> RnnoiseDenoiser | None:
    """Honest factory: returns a real RnnoiseDenoiser, or None if
    pyrnnoise isn't installed on this machine (never raises — a missing
    optional dependency must degrade to "no denoising" for the rest of
    the audio pipeline, not crash hands-free/wake-word startup, same
    fail-open discipline as every other optional feature in this
    codebase — e.g. dourmouse/webui.py's hands_free wiring)."""
    if not denoise_enabled():
        return None
    try:
        return RnnoiseDenoiser()
    except Exception:  # noqa: BLE001 - a missing/broken optional dep must never crash startup
        return None
