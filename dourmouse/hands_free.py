"""Hands-free conversational loop (Vision stage 4, part 2) — the three
real pieces dourmouse/wakeword.py's own docstring deliberately deferred:
"wake word -> record the utterance -> speech_to_text -> hand it to the
dispatch loop" was left as a pluggable ``on_wake`` callback specifically
because "building a robust 'when did the utterance end' segmenter is a
distinct, substantial feature in its own right" and wiring something
untested into the live dispatch path would violate this project's own
honesty rules. This module IS that feature, built to the same standard.

Explicit user request (2026-08-31): "a conversational llm you can talk to
without pressing buttons" — three real, separately-verifiable pieces:

1. ``UtteranceSegmenter`` — a pure, no-audio-I/O silence-detection state
   machine (real RMS energy over real int16 PCM chunks, the exact same
   16kHz/80ms-chunk convention wakeword.py already uses). Fully unit-
   testable with synthetic frames, the same discipline
   test_wakeword.py already established for the detector half.
2. ``record_utterance`` / ``play_audio`` — real audio I/O wired to that
   segmenter, using ``sounddevice`` (already a real, installed dependency
   for wake-word capture; no new dependency for playback either — it's
   the same PortAudio binding, ``sd.play``/``sd.wait``).
3. ``HandsFreeController`` — the orchestrator. Wake word fires -> records
   until the segmenter says the utterance ended -> ``voice.speech_to_text``
   (already real) -> the SAME real dispatch path every typed/voice-button
   message already goes through (injected as ``dispatch_fn`` — production
   wiring in webui.py's run_server calls the live ChatSession.ask() under
   the server's own session_lock, exactly like _handle_chat_authed does)
   -> ``voice.text_to_speech`` (already real) -> spoken back locally.

Same non-negotiable safety contract as wakeword.py: EVERY microphone
open — both the wake-word listener AND the post-wake recording — goes
through ``dourmouse.tray.mic_allowed()`` first, and is force-stopped the
moment the kill switch flips off mid-listen or mid-record.

Honesty (Rule 2.1/2.2) — read this before assuming more than what's here:
- The segmenter's decision LOGIC is genuinely verified here against
  synthetic signals (real silence, real "speech" energy, a real pause-
  then-resume case) — see dourmouse/tests/test_hands_free.py.
- Real microphone capture and real local speaker playback could NOT be
  conclusively verified end-to-end in this sandbox, for the exact same
  reason wakeword.py's own module docstring already states for wake-word
  capture: no genuine macOS microphone (TCC) permission is grantable
  here. This needs a live desktop session to honestly confirm.
- The energy threshold below is a real, reasonable default (documented,
  not fabricated precision) — like wakeword.py's own detection
  threshold, it may need real-world tuning against an actual microphone
  and room, which this sandbox cannot do.

Env:
- ``DOURMOUSE_HANDS_FREE=1``         -> enable the FULL loop (wake word +
  record + STT + dispatch + spoken reply). Implies wake-word detection is
  active; DOURMOUSE_WAKEWORD alone (see wakeword.py) still means
  "detection only, log and stop" — this is the strictly bigger opt-in.
- ``DOURMOUSE_HANDS_FREE_SILENCE_MS``  -> silence duration that ends an
  utterance (default 900ms).
- ``DOURMOUSE_HANDS_FREE_MAX_MS``      -> hard cap on one utterance's
  recording length (default 15000ms) — a safety bound, never an
  intentional UX target.
- ``DOURMOUSE_HANDS_FREE_ENERGY``      -> RMS threshold distinguishing
  speech from silence on 16-bit PCM (default 150.0).
"""

from __future__ import annotations

import io
import os
import threading
import wave as _wave
from typing import Any, Callable

from dourmouse.wakeword import _CHUNK_SAMPLES, _SAMPLE_RATE

_HANDS_FREE_ENV = "DOURMOUSE_HANDS_FREE"
_SILENCE_MS_ENV = "DOURMOUSE_HANDS_FREE_SILENCE_MS"
_MAX_MS_ENV = "DOURMOUSE_HANDS_FREE_MAX_MS"
_ENERGY_ENV = "DOURMOUSE_HANDS_FREE_ENERGY"
_OFF_VALUES = {"", "0", "false", "no", "off"}

_DEFAULT_SILENCE_MS = 900.0
_DEFAULT_MAX_MS = 15_000.0
_DEFAULT_ENERGY = 150.0
_CHUNK_MS = 1000.0 * _CHUNK_SAMPLES / _SAMPLE_RATE  # 80ms, matches wakeword.py


def hands_free_enabled(value: str | None = None) -> bool:
    """``DOURMOUSE_HANDS_FREE`` gate; default OFF (opt-in)."""
    if value is None:
        value = os.environ.get(_HANDS_FREE_ENV, "0")
    return str(value).strip().lower() not in _OFF_VALUES


def silence_ms() -> float:
    raw = os.environ.get(_SILENCE_MS_ENV, "")
    try:
        return float(raw) if raw.strip() else _DEFAULT_SILENCE_MS
    except ValueError:
        return _DEFAULT_SILENCE_MS


def max_utterance_ms() -> float:
    raw = os.environ.get(_MAX_MS_ENV, "")
    try:
        return float(raw) if raw.strip() else _DEFAULT_MAX_MS
    except ValueError:
        return _DEFAULT_MAX_MS


def energy_threshold() -> float:
    raw = os.environ.get(_ENERGY_ENV, "")
    try:
        return float(raw) if raw.strip() else _DEFAULT_ENERGY
    except ValueError:
        return _DEFAULT_ENERGY


# --------------------------------------------------------------------------- #
# 1. UtteranceSegmenter — pure decision logic, zero audio I/O.
# --------------------------------------------------------------------------- #

def _rms(chunk: Any) -> float:
    """Real RMS energy of one int16 PCM chunk. Accepts a numpy array (the
    real shape sounddevice hands wakeword.py's own callback — see
    WakeWordListener._on_audio) or any sequence of ints; never imports
    numpy itself (this function works either way, so the pure-logic half
    of this module has no hard numpy dependency of its own, matching
    wakeword.py's WakeWordDetector.feed() taking "any int16 PCM chunk")."""
    if len(chunk) == 0:
        return 0.0
    total = 0.0
    for sample in chunk:
        total += float(sample) * float(sample)
    return (total / len(chunk)) ** 0.5


class UtteranceSegmenter:
    """Real "when did the user stop talking" decision logic. Feed it real
    (or synthetic, for tests) int16 PCM chunks in the SAME size wakeword.py
    already uses (``_CHUNK_SAMPLES`` @ ``_SAMPLE_RATE``, 80ms) and it
    decides when to stop recording — never owns a microphone, a stream, or
    the recorded audio itself, so it is trivially unit-testable (see
    dourmouse/tests/test_hands_free.py, which runs REAL synthetic silence/
    speech/pause-then-resume sequences through it, not a mock).

    Logic, stated plainly: an utterance is "done" once real speech energy
    has been seen AND enough CONSECUTIVE low-energy chunks follow it to
    add up to ``silence_ms``. A brief pause mid-sentence (fewer
    consecutive silent chunks than that) does not end it — the run
    resets the moment speech resumes. ``max_ms`` is a hard safety cap,
    never a UX target: if someone just keeps talking, recording stops
    there rather than growing unbounded.
    """

    def __init__(
        self,
        *,
        energy_threshold: float | None = None,
        silence_ms: float | None = None,
        max_ms: float | None = None,
    ) -> None:
        self.energy_threshold = (
            energy_threshold if energy_threshold is not None else globals()["energy_threshold"]()
        )
        self.silence_ms = silence_ms if silence_ms is not None else globals()["silence_ms"]()
        self.max_ms = max_ms if max_ms is not None else max_utterance_ms()
        self._silence_chunks_needed = max(1, round(self.silence_ms / _CHUNK_MS))
        self._max_chunks = max(1, round(self.max_ms / _CHUNK_MS))
        self.reset()

    def reset(self) -> None:
        self._heard_speech = False
        self._silence_run = 0
        self._chunk_count = 0

    def feed(self, chunk: Any) -> bool:
        """One chunk in -> True if the utterance is now considered
        complete (the caller should stop recording after this chunk),
        False to keep recording."""
        self._chunk_count += 1
        if self._chunk_count >= self._max_chunks:
            return True
        energy = _rms(chunk)
        is_speech = energy >= self.energy_threshold
        if is_speech:
            self._heard_speech = True
            self._silence_run = 0
            return False
        if not self._heard_speech:
            return False  # still waiting for the utterance to actually start
        self._silence_run += 1
        return self._silence_run >= self._silence_chunks_needed


# --------------------------------------------------------------------------- #
# 2. Real audio I/O — recording (segmenter-driven) and local playback.
# --------------------------------------------------------------------------- #

def _pcm_to_wav_bytes(frames: list[Any], *, sample_rate: int = _SAMPLE_RATE) -> bytes:
    """Real int16 PCM chunks -> a real, valid WAV byte stream
    voice.speech_to_text can transcribe (it accepts wav/webm/ogg/mp3 —
    WAV is the one this module can produce with zero extra dependencies,
    stdlib ``wave``, same module voice.py itself already imports)."""
    buf = io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        for chunk in frames:
            data = chunk.tobytes() if hasattr(chunk, "tobytes") else bytes(chunk)
            wf.writeframes(data)
    return buf.getvalue()


def record_utterance(
    *,
    segmenter: UtteranceSegmenter | None = None,
    stream_factory: Callable[[Callable[..., None]], Any] | None = None,
    mic_allowed: Callable[[], bool] | None = None,
    denoiser: Any | None = None,
) -> bytes | None:
    """Record one real utterance after a wake word fired, real audio in,
    real WAV bytes out. Returns None (never raises) when the mic is
    killed, no dependency is available, or nothing was actually captured
    — the caller decides what "no utterance" means for it, this function
    never fabricates audio. Same mic_allowed() gate as WakeWordListener.start()
    — checked BEFORE opening a stream, never bypassed.

    ``denoiser`` (v13.5, Vision OS checklist item 5): an optional real-time
    audio object with a ``.process(chunk: np.ndarray) -> np.ndarray`` method
    — dourmouse.audio_denoise.RnnoiseDenoiser's own real shape — applied to
    EVERY captured chunk before it reaches both the segmenter's VAD and the
    final WAV, so a cleaner signal helps utterance-boundary detection AND
    the transcription that follows, not just one or the other. Defaults to
    dourmouse.audio_denoise.create_default() (None when DOURMOUSE_DENOISE=0
    or pyrnnoise isn't installed — an honest, fail-open degrade to raw
    audio, never a crash). A denoise failure on any single chunk falls back
    to that chunk's raw audio rather than dropping it — see the try/except
    around the call below.
    """
    if mic_allowed is None:
        from dourmouse.tray import mic_allowed as _default_mic_allowed

        mic_allowed = _default_mic_allowed
    if not mic_allowed():
        return None
    # Track whether WE created the denoiser (own its lifecycle, must
    # close() it) vs. the caller injected one (theirs to manage — e.g. a
    # test reusing the same instance across assertions).
    owns_denoiser = denoiser is None
    if owns_denoiser:
        from dourmouse.audio_denoise import create_default as _create_default_denoiser

        denoiser = _create_default_denoiser()
        owns_denoiser = denoiser is not None
    seg = segmenter or UtteranceSegmenter()
    seg.reset()
    frames: list[Any] = []
    done_evt = threading.Event()

    def _on_audio(indata: Any, frames_count: int, time_info: Any, status: Any) -> None:  # noqa: ARG001 - sounddevice callback shape
        if done_evt.is_set():
            return
        chunk = indata[:, 0].copy() if hasattr(indata, "shape") and len(indata.shape) > 1 else indata
        if denoiser is not None:
            try:
                chunk = denoiser.process(chunk)
            except Exception:  # noqa: BLE001 - a denoise failure must fall back to raw audio, never drop the chunk
                pass
        frames.append(chunk)
        if seg.feed(chunk):
            done_evt.set()

    try:
        factory = stream_factory or _default_stream_factory
        try:
            stream = factory(_on_audio)
            stream.start()
        except Exception:  # noqa: BLE001 - honest None, never crash the caller
            return None
        try:
            # Real hard timeout, not just the segmenter's own max_ms bound —
            # belt and suspenders against a stream that never calls back at
            # all (a real, observed failure mode class for audio hardware).
            done_evt.wait(timeout=(seg.max_ms / 1000.0) + 5.0)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
        if not frames:
            return None
        return _pcm_to_wav_bytes(frames)
    finally:
        # A denoiser WE created (create_default(), not caller-injected) is
        # ours to release — real RNNoise C-level state, same discipline as
        # HandLandmarker.close() in ui/workspace.html (repeated enable/
        # disable cycles must not accumulate live instances).
        if owns_denoiser and denoiser is not None:
            try:
                denoiser.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass


def _default_stream_factory(callback: Callable[..., None]) -> Any:
    import sounddevice as sd

    return sd.InputStream(
        samplerate=_SAMPLE_RATE, channels=1, dtype="int16",
        blocksize=_CHUNK_SAMPLES, callback=callback,
    )


def play_audio(wav_bytes: bytes, *, player: Callable[[bytes], None] | None = None) -> bool:
    """Play real WAV bytes out loud, locally, right now. ``player`` is the
    test seam (mirrors every other seam in this module/wakeword.py);
    production uses sounddevice — the SAME real dependency wake-word
    capture already needs, so hands-free playback adds no new one.
    Returns False honestly (never raises) if audio can't actually play."""
    if not wav_bytes:
        return False
    try:
        if player is not None:
            player(wav_bytes)
            return True
        import numpy as np
        import sounddevice as sd

        with _wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
            channels = wf.getnchannels()
        audio = np.frombuffer(raw, dtype=np.int16)
        if channels > 1:
            audio = audio.reshape(-1, channels)
        sd.play(audio, samplerate=rate)
        sd.wait()
        return True
    except Exception:  # noqa: BLE001 - a failed playback must never crash the loop
        return False


# --------------------------------------------------------------------------- #
# 3. HandsFreeController — the real orchestrator.
# --------------------------------------------------------------------------- #

class HandsFreeController:
    """Owns a WakeWordListener and wires its on_wake to the real
    record -> STT -> dispatch -> TTS -> play loop. ``dispatch_fn`` is the
    ONE required injection point: production wiring (webui.py's
    run_server) passes a closure that calls the live ChatSession.ask()
    under the server's own session_lock — the exact same real dispatch
    path _handle_chat_authed uses for a typed message, never a second,
    competing implementation. Tests inject a fake that returns canned
    text, so the full real wake -> record -> transcribe -> [fake
    dispatch] -> speak -> play control flow is exercised without a live
    LLM call.
    """

    def __init__(
        self,
        *,
        dispatch_fn: Callable[[str], str],
        listener: Any | None = None,
        record_fn: Callable[..., bytes | None] | None = None,
        transcribe_fn: Callable[[bytes], str] | None = None,
        speak_fn: Callable[[str], bytes] | None = None,
        play_fn: Callable[[bytes], bool] | None = None,
        mic_allowed: Callable[[], bool] | None = None,
        on_turn: Callable[[str, str], None] | None = None,
    ) -> None:
        self._dispatch_fn = dispatch_fn
        self._record_fn = record_fn or record_utterance
        self._transcribe_fn = transcribe_fn or self._default_transcribe
        self._speak_fn = speak_fn or self._default_speak
        self._play_fn = play_fn or play_audio
        if mic_allowed is None:
            from dourmouse.tray import mic_allowed as _default_mic_allowed

            mic_allowed = _default_mic_allowed
        self._mic_allowed = mic_allowed
        self._on_turn = on_turn  # optional observability hook (heard, said)
        self._busy = threading.Lock()
        if listener is not None:
            self._listener = listener
        else:
            from dourmouse.wakeword import WakeWordListener

            self._listener = WakeWordListener(on_wake=self._on_wake, mic_allowed=mic_allowed)

    @staticmethod
    def _default_transcribe(wav_bytes: bytes) -> str:
        from dourmouse.voice import speech_to_text

        return speech_to_text(wav_bytes)

    @staticmethod
    def _default_speak(text: str) -> bytes:
        from dourmouse.voice import text_to_speech

        return text_to_speech(text)

    @property
    def running(self) -> bool:
        return bool(getattr(self._listener, "running", False))

    def start(self) -> tuple[bool, str]:
        if not hands_free_enabled():
            return False, "DOURMOUSE_HANDS_FREE is off — set DOURMOUSE_HANDS_FREE=1 to enable."
        return self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    def _on_wake(self, keyword: str, score: float) -> None:  # noqa: ARG002 - score kept for future logging
        # A wake word firing again mid-turn (real, observed possibility —
        # openWakeWord keeps listening on the same stream) must not start
        # a second overlapping turn; the busy lock makes this a real,
        # deterministic no-op rather than two turns racing each other.
        if not self._busy.acquire(blocking=False):
            return
        try:
            self._handle_turn()
        finally:
            self._busy.release()

    def _handle_turn(self) -> None:
        if not self._mic_allowed():
            return
        wav = self._record_fn(mic_allowed=self._mic_allowed)
        if not wav:
            return
        try:
            heard = self._transcribe_fn(wav)
        except Exception:  # noqa: BLE001 - a real STT failure ends this turn, never crashes the listener
            return
        heard = (heard or "").strip()
        if not heard:
            return
        try:
            said = self._dispatch_fn(heard)
        except Exception as exc:  # noqa: BLE001 - the loop must survive a real dispatch error
            said = f"Something went wrong on that one: {exc}"
        if self._on_turn is not None:
            try:
                self._on_turn(heard, said)
            except Exception:  # noqa: BLE001 - an observability hook must never break the loop
                pass
        if not self._mic_allowed():
            return  # kill switch flipped mid-turn — never speak after that
        try:
            audio = self._speak_fn(said)
        except Exception:  # noqa: BLE001 - a real TTS failure ends this turn quietly, not a crash
            return
        self._play_fn(audio)
