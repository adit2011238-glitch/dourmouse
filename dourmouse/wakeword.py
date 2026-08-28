"""Local always-on wake-word listener (Vision stage 4).

Engine: openWakeWord (https://github.com/dscripka/openWakeWord) — a real,
open-weight, ONNX-backed local wake-word detector. Genuinely installable in
this project's .venv: it and its capture-side companion ``sounddevice``
(PortAudio bindings, real macOS microphone access) were both pip-installed
here and verified (see requirements-voice.txt and
dourmouse/tests/test_wakeword.py) — this is not a hypothetical dependency.

Non-negotiable safety contract (dourmouse/tray.py, Vision stage 3): this
module MUST call ``dourmouse.tray.mic_allowed()`` before it EVER opens a
continuous microphone stream, and MUST keep checking it while listening so
the tray's kill switch can force-stop an in-progress listen session. Wired
FIRST, before any model-loading or audio code — see ``WakeWordListener.start``
(the mic_allowed() check is the very first thing it does after the env
gate) and ``WakeWordListener._watch_kill_switch`` (the re-check loop that
runs for as long as the stream is open).

Honesty (Rule 2.1/2.2) — read this before assuming more than what's here:

- Model inference is GENUINELY VERIFIED headlessly in this sandbox: real
  pretrained openWakeWord models download over the network on first use
  (openwakeword.utils.download_models(), from the project's own GitHub
  releases) and real ONNX inference runs against synthetic 16kHz/int16 PCM
  frames — no live microphone involved for that half.
  dourmouse/tests/test_wakeword.py exercises this for real (skips honestly,
  never fabricates a pass, if the model cache is unavailable and there is
  no network to fetch it).
- The CONTINUOUS MICROPHONE CAPTURE LOOP (``sounddevice.InputStream``
  reading a real mic) could NOT be conclusively verified working end-to-end
  in this sandbox. ``sounddevice`` does enumerate real Core Audio devices
  here (including a real "MacBook Air Microphone"), and opening a stream
  and calling ``sd.rec()`` did not raise — but every sample returned was
  exactly zero for a non-silent duration, which is the signature of a
  process that has not actually been granted macOS microphone (TCC)
  permission, not of genuine live audio. This needs a live desktop session
  — the packaged app actually launched with mic permission granted in
  System Settings -> Privacy & Security -> Microphone — to honestly confirm
  wake-word detection fires on a real spoken wake word.
- What happens AFTER a wake word fires (recording the following utterance,
  running it through voice.speech_to_text, handing it to the dispatch loop)
  is deliberately left as a pluggable ``on_wake`` callback rather than
  hard-wired here. Building a robust "when did the utterance end" segmenter
  is a distinct, substantial feature in its own right; wiring something
  fragile and untested into the live dispatch path would risk exactly the
  kind of unverified capability this project's honesty rules exist to
  prevent. The default ``on_wake`` simply logs the detection.

Env:
- ``DOURMOUSE_WAKEWORD=1``            -> enable the listener (default off,
  opt-in exactly like ``DOURMOUSE_VOICE``).
- ``DOURMOUSE_WAKEWORD_MODEL``        -> openWakeWord pretrained model name
  (default ``hey_jarvis``). openWakeWord ships no "dourmouse"-trained model,
  so this uses one of its real pretrained community models as a documented
  stand-in — never a fabricated custom wake word.
- ``DOURMOUSE_WAKEWORD_THRESHOLD``    -> detection score threshold in [0, 1]
  (default 0.5).

Run it standalone (needs a live desktop session with a real microphone to
do anything beyond printing NOT CONFIGURED):
    .venv/bin/python -m dourmouse.wakeword
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

_WAKEWORD_ENV = "DOURMOUSE_WAKEWORD"
_MODEL_ENV = "DOURMOUSE_WAKEWORD_MODEL"
_THRESHOLD_ENV = "DOURMOUSE_WAKEWORD_THRESHOLD"
_OFF_VALUES = {"", "0", "false", "no", "off"}

_DEFAULT_MODEL = "hey_jarvis"
_DEFAULT_THRESHOLD = 0.5
_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 1280  # 80ms @ 16kHz -- openWakeWord's expected frame size
_MIC_WATCHDOG_INTERVAL = 0.5  # seconds between mic_allowed() re-checks while listening
_COOLDOWN_SECONDS = 2.5  # per-keyword re-fire guard (mirrors ui/index.html's GS.COOLDOWN_MS gesture pattern)


class WakeWordNotConfiguredError(RuntimeError):
    """Raised honestly when the listener cannot run (Rule 2.2)."""


# --------------------------------------------------------------------------- #
# Honest capability probes (cheap — no heavy imports until actually used)
# --------------------------------------------------------------------------- #

def wakeword_enabled(value: str | None = None) -> bool:
    """``DOURMOUSE_WAKEWORD`` gate; default OFF (opt-in, like voice.py)."""
    if value is None:
        value = os.environ.get(_WAKEWORD_ENV, "0")
    return str(value).strip().lower() not in _OFF_VALUES


def wakeword_model() -> str:
    raw = os.environ.get(_MODEL_ENV, _DEFAULT_MODEL)
    return (raw or "").strip() or _DEFAULT_MODEL


def wakeword_threshold() -> float:
    raw = os.environ.get(_THRESHOLD_ENV, "")
    try:
        return float(raw) if raw.strip() else _DEFAULT_THRESHOLD
    except ValueError:
        return _DEFAULT_THRESHOLD


def _inference_available() -> bool:
    try:
        import openwakeword  # noqa: F401
    except ImportError:
        return False
    return True


def _capture_available() -> bool:
    try:
        import sounddevice  # noqa: F401
    except ImportError:
        return False
    return True


def wakeword_status() -> dict[str, Any]:
    """Honest capability report — the same shape as voice.voice_status()."""
    return {
        "enabled": wakeword_enabled(),
        "inference_engine": "openwakeword" if _inference_available() else "not-configured",
        "capture_engine": "sounddevice" if _capture_available() else "not-configured",
        "model": wakeword_model(),
        "threshold": wakeword_threshold(),
    }


# --------------------------------------------------------------------------- #
# Pure inference wrapper — no audio I/O, fully unit-testable with synthetic
# frames (see dourmouse/tests/test_wakeword.py, which genuinely loads the
# real pretrained model and runs real ONNX inference against silence).
# --------------------------------------------------------------------------- #

class WakeWordDetector:
    """Thin wrapper around ``openwakeword.model.Model``. ``model_factory``
    is the test seam (mirrors voice.py's lazy-singleton pattern, but per
    instance rather than module-global so tests never share state)."""

    def __init__(
        self,
        model_name: str | None = None,
        *,
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._model_name = model_name or wakeword_model()
        self._model_factory = model_factory or self._default_factory
        self._model: Any = None

    @staticmethod
    def _default_factory(model_name: str) -> Any:
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise WakeWordNotConfiguredError(
                "openwakeword is not installed — run: pip install -r "
                "requirements-voice.txt"
            ) from exc
        try:
            return Model(wakeword_models=[model_name], inference_framework="onnx")
        except Exception as exc:  # model download failure / corrupt cache / bad name
            raise WakeWordNotConfiguredError(
                f"openwakeword could not load model {model_name!r}: {exc}"
            ) from exc

    def _ensure_model(self) -> Any:
        if self._model is None:
            self._model = self._model_factory(self._model_name)
        return self._model

    def feed(self, chunk: Any) -> dict[str, float]:
        """One int16 PCM chunk (ideally ``_CHUNK_SAMPLES`` samples @ 16kHz,
        mono) in -> ``{model_name: score}`` out. Raises
        ``WakeWordNotConfiguredError`` honestly if the model cannot load."""
        model = self._ensure_model()
        return {k: float(v) for k, v in dict(model.predict(chunk)).items()}

    def reset(self) -> None:
        """Clear the model's internal streaming buffers (best-effort —
        older/newer openwakeword releases don't all expose ``reset``)."""
        model = self._ensure_model()
        if hasattr(model, "reset"):
            try:
                model.reset()
            except Exception:  # noqa: BLE001 -- reset is best-effort
                pass


# --------------------------------------------------------------------------- #
# The continuous mic loop
# --------------------------------------------------------------------------- #

class WakeWordListener:
    """Owns the continuous microphone stream + detector loop.

    ``stream_factory`` and ``mic_allowed`` are test seams (same shape as
    tray.py's ``icon_factory`` seam): production code gets real
    ``sounddevice.InputStream`` + real ``dourmouse.tray.mic_allowed``; tests
    inject fakes so the full control flow — refuse-when-killed,
    force-stop-when-killed-mid-listen, cooldown-gated ``on_wake`` firing —
    is exercised without touching real hardware.
    """

    def __init__(
        self,
        *,
        detector: WakeWordDetector | None = None,
        stream_factory: Callable[[Callable[..., None]], Any] | None = None,
        mic_allowed: Callable[[], bool] | None = None,
        on_wake: Callable[[str, float], None] | None = None,
        threshold: float | None = None,
        watchdog_interval: float = _MIC_WATCHDOG_INTERVAL,
        cooldown_seconds: float = _COOLDOWN_SECONDS,
    ) -> None:
        self._detector = detector or WakeWordDetector()
        self._stream_factory = stream_factory or self._default_stream_factory
        if mic_allowed is None:
            from dourmouse.tray import mic_allowed as _default_mic_allowed

            mic_allowed = _default_mic_allowed
        self._mic_allowed = mic_allowed
        self._on_wake = on_wake or self._default_on_wake
        self._threshold = threshold if threshold is not None else wakeword_threshold()
        self._watchdog_interval = watchdog_interval
        self._cooldown_seconds = cooldown_seconds
        self._stream: Any = None
        self._watchdog: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._last_fire: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._stream is not None

    @staticmethod
    def _default_on_wake(keyword: str, score: float) -> None:
        print(f"[WAKEWORD] detected {keyword!r} (score={score:.3f})")

    @staticmethod
    def _default_stream_factory(callback: Callable[..., None]) -> Any:
        import sounddevice as sd

        return sd.InputStream(
            samplerate=_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=_CHUNK_SAMPLES,
            callback=callback,
        )

    def start(self) -> tuple[bool, str]:
        """Open the mic and start listening. Returns ``(ok, reason)`` —
        NEVER raises. Refuses honestly, WITHOUT ever touching the audio
        backend, when the feature is off, the kill switch is off, or a
        dependency is missing. This ordering (env gate, then mic_allowed(),
        then dependency probes, THEN — and only then — open a stream) is the
        non-negotiable part of the contract."""
        if self.running:
            return True, "already listening"
        if not wakeword_enabled():
            return False, "DOURMOUSE_WAKEWORD is off — set DOURMOUSE_WAKEWORD=1 to enable."
        if not self._mic_allowed():
            return False, "mic is KILLED (tray kill-switch) — refusing to open a stream."
        if not _capture_available():
            return False, "sounddevice is not installed — pip install -r requirements-voice.txt"
        if not _inference_available():
            return False, "openwakeword is not installed — pip install -r requirements-voice.txt"
        self._stop_evt.clear()
        try:
            stream = self._stream_factory(self._on_audio)
            stream.start()
        except Exception as exc:  # noqa: BLE001 -- honest failure, never crash the caller
            return False, f"could not open microphone stream: {exc}"
        self._stream = stream
        self._watchdog = threading.Thread(
            target=self._watch_kill_switch, daemon=True, name="dourmouse-wakeword-watchdog"
        )
        self._watchdog.start()
        return True, "listening"

    def stop(self) -> None:
        self._stop_evt.set()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 -- stopping must never raise
                pass
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=2)

    def _watch_kill_switch(self) -> None:
        """The 'reach' half of the mic contract: re-checks mic_allowed()
        every ``watchdog_interval`` seconds for as long as the stream is
        open, and force-stops the moment it flips False — an in-progress
        listen session does not get to keep running just because it already
        started."""
        while not self._stop_evt.wait(self._watchdog_interval):
            try:
                if not self._mic_allowed():
                    self.stop()
                    return
            except Exception:  # noqa: BLE001 -- the watchdog must never die
                continue

    def _on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:  # noqa: ARG002 -- sounddevice callback signature
        if self._stop_evt.is_set():
            return
        try:
            chunk = indata[:, 0].copy() if hasattr(indata, "shape") and len(indata.shape) > 1 else indata
            scores = self._detector.feed(chunk)
        except Exception:  # noqa: BLE001 -- a bad frame must never kill the stream
            return
        now = time.monotonic()
        for keyword, score in scores.items():
            if score < self._threshold:
                continue
            with self._lock:
                last = self._last_fire.get(keyword, 0.0)
                if now - last < self._cooldown_seconds:
                    continue
                self._last_fire[keyword] = now
            try:
                self._on_wake(keyword, float(score))
            except Exception:  # noqa: BLE001 -- a bad callback must never kill the stream
                pass


def launch() -> int:
    status = wakeword_status()
    if not status["enabled"]:
        print(
            "[WAKEWORD] DOURMOUSE_WAKEWORD is off — NOT CONFIGURED. "
            "Set DOURMOUSE_WAKEWORD=1 to enable it."
        )
        return 1
    listener = WakeWordListener()
    ok, reason = listener.start()
    print(f"[WAKEWORD] {reason}")
    if not ok:
        return 1
    print(
        f"[WAKEWORD] listening for {wakeword_model()!r} "
        f"(threshold={wakeword_threshold()}) — Ctrl+C to stop"
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(launch())
