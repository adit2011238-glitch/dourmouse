"""dourmouse/hands_free.py — the real "wake word -> record -> transcribe
-> dispatch -> speak" loop wakeword.py's own docstring deliberately left
unbuilt. Same testing discipline as test_wakeword.py: real logic run
against synthetic data (never mocked at the decision-logic level), fake
seams only at the real hardware/model boundary (mic stream, STT, TTS,
dispatch).
"""

from __future__ import annotations

import wave as _wave
from io import BytesIO

import numpy as np
import pytest

from dourmouse import hands_free


def _silence_chunk(n=160):
    return np.zeros(n, dtype=np.int16)


def _speech_chunk(n=160, amplitude=2000):
    # Deterministic "loud" synthetic signal -- not real speech, but real
    # energy well above the default 150.0 RMS threshold, which is all
    # the segmenter's decision logic actually looks at.
    return np.full(n, amplitude, dtype=np.int16)


class TestRms:
    def test_silence_is_near_zero(self):
        assert hands_free._rms(_silence_chunk()) == 0.0

    def test_loud_signal_has_real_high_rms(self):
        assert hands_free._rms(_speech_chunk(amplitude=2000)) == pytest.approx(2000.0, rel=0.01)

    def test_empty_chunk_is_zero_not_a_crash(self):
        assert hands_free._rms(np.array([], dtype=np.int16)) == 0.0


class TestUtteranceSegmenter:
    """Real synthetic sequences through the real decision logic -- not a
    mock of "is this speech", the actual RMS-threshold state machine."""

    def _seg(self, **kw):
        kw.setdefault("energy_threshold", 150.0)
        kw.setdefault("silence_ms", 240.0)  # 3 chunks @ 80ms
        kw.setdefault("max_ms", 4000.0)
        return hands_free.UtteranceSegmenter(**kw)

    def test_pure_silence_never_ends_the_utterance_before_max(self):
        seg = self._seg(max_ms=400.0)  # 5 chunks
        results = [seg.feed(_silence_chunk()) for _ in range(4)]
        assert results[:4] == [False, False, False, False]
        assert seg.feed(_silence_chunk()) is True  # hit the real max cap

    def test_speech_then_enough_silence_ends_it(self):
        seg = self._seg()
        assert seg.feed(_speech_chunk()) is False
        assert seg.feed(_silence_chunk()) is False  # 1 silent chunk
        assert seg.feed(_silence_chunk()) is False  # 2 silent chunks
        assert seg.feed(_silence_chunk()) is True   # 3rd -> silence_ms reached

    def test_brief_pause_mid_sentence_does_not_end_the_utterance(self):
        """The real case this whole module exists to handle correctly:
        a person pausing briefly mid-sentence must not get cut off."""
        seg = self._seg()
        assert seg.feed(_speech_chunk()) is False
        assert seg.feed(_silence_chunk()) is False  # 1 silent chunk -- a breath
        assert seg.feed(_speech_chunk()) is False   # speech resumes -- silence run resets
        assert seg.feed(_silence_chunk()) is False  # 1
        assert seg.feed(_silence_chunk()) is False  # 2
        assert seg.feed(_silence_chunk()) is True   # 3 -- NOW it ends

    def test_never_ends_before_any_real_speech_is_heard(self):
        seg = self._seg(max_ms=10_000.0)
        for _ in range(20):
            assert seg.feed(_silence_chunk()) is False  # just waiting, not silence-after-speech

    def test_max_ms_is_a_real_hard_cap_even_mid_speech(self):
        seg = self._seg(max_ms=240.0)  # 3 chunks
        assert seg.feed(_speech_chunk()) is False
        assert seg.feed(_speech_chunk()) is False
        assert seg.feed(_speech_chunk()) is True  # capped, even though still "speaking"

    def test_reset_clears_all_state(self):
        seg = self._seg()
        seg.feed(_speech_chunk())
        seg.feed(_silence_chunk())
        seg.reset()
        assert seg._heard_speech is False
        assert seg._silence_run == 0
        assert seg._chunk_count == 0


class TestPcmToWav:
    def test_produces_a_real_valid_wav_file(self):
        frames = [_speech_chunk(160) for _ in range(3)]
        wav_bytes = hands_free._pcm_to_wav_bytes(frames, sample_rate=16000)
        with _wave.open(BytesIO(wav_bytes), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000
            assert wf.getnframes() == 160 * 3


class _FakeStream:
    def __init__(self, callback):
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class TestRecordUtterance:
    def test_refuses_when_mic_killed_and_never_opens_a_stream(self):
        opened = []
        result = hands_free.record_utterance(
            stream_factory=lambda cb: opened.append(True) or _FakeStream(cb),
            mic_allowed=lambda: False,
        )
        assert result is None
        assert opened == []

    def test_records_until_the_segmenter_says_done(self):
        stream_holder = {}

        def factory(cb):
            s = _FakeStream(cb)
            stream_holder["stream"] = s
            return s

        seg = hands_free.UtteranceSegmenter(energy_threshold=150.0, silence_ms=160.0, max_ms=10_000.0)
        result_box = {}

        def run():
            result_box["wav"] = hands_free.record_utterance(
                segmenter=seg, stream_factory=factory, mic_allowed=lambda: True,
            )

        import threading
        t = threading.Thread(target=run)
        t.start()
        import time
        time.sleep(0.05)  # let the stream open and the callback get wired
        stream = stream_holder["stream"]
        assert stream.started is True
        # speech, then enough silence to end it (silence_ms=160 -> 2 chunks)
        stream.callback(_speech_chunk(), 160, None, None)
        stream.callback(_silence_chunk(), 160, None, None)
        stream.callback(_silence_chunk(), 160, None, None)
        t.join(timeout=5)
        assert stream.closed is True
        wav = result_box["wav"]
        assert wav is not None
        with _wave.open(BytesIO(wav), "rb") as wf:
            assert wf.getnframes() == 160 * 3  # exactly the 3 chunks fed, nothing fabricated

    def test_returns_none_when_stream_open_fails(self):
        def bad_factory(cb):
            raise RuntimeError("no audio device")

        assert hands_free.record_utterance(stream_factory=bad_factory, mic_allowed=lambda: True) is None

    def test_returns_none_when_nothing_was_ever_captured(self):
        # A stream that opens fine but the callback never fires (a real
        # observed hardware failure mode) must time out honestly, not hang.
        seg = hands_free.UtteranceSegmenter(max_ms=50.0)  # tiny cap for a fast test
        result = hands_free.record_utterance(
            segmenter=seg,
            stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: True,
        )
        assert result is None

    def test_injected_denoiser_processes_every_chunk(self):
        # v13.5, Vision OS checklist item 5: a real chunk-processing seam,
        # same discipline as every other test seam in this file -- proves
        # record_utterance() actually calls .process() per chunk and uses
        # ITS return value (not the raw chunk) in the final WAV.
        #
        # Real design constraint this test respects: the DENOISED chunk is
        # what reaches the segmenter's own VAD (correct production
        # behavior -- see record_utterance's own docstring), so a fake
        # denoiser that returns a CONSTANT value regardless of input would
        # make every chunk look like loud speech to the segmenter and the
        # recording would never end. Scaling by 2x instead keeps silence
        # near-zero (still reads as silence) and speech loud (still reads
        # as speech), while still being a real, verifiably DIFFERENT value
        # from the raw input.
        seen = []

        class _FakeDenoiser:
            def process(self, chunk):
                seen.append(chunk.copy())
                return (chunk.astype(np.int32) * 2).astype(np.int16)

        stream_holder = {}

        def factory(cb):
            s = _FakeStream(cb)
            stream_holder["stream"] = s
            return s

        seg = hands_free.UtteranceSegmenter(energy_threshold=150.0, silence_ms=160.0, max_ms=10_000.0)
        denoiser = _FakeDenoiser()
        result_box = {}

        def run():
            result_box["wav"] = hands_free.record_utterance(
                segmenter=seg, stream_factory=factory, mic_allowed=lambda: True, denoiser=denoiser,
            )

        import threading
        t = threading.Thread(target=run)
        t.start()
        import time
        time.sleep(0.05)
        stream = stream_holder["stream"]
        stream.callback(_speech_chunk(), 160, None, None)
        stream.callback(_silence_chunk(), 160, None, None)
        stream.callback(_silence_chunk(), 160, None, None)
        t.join(timeout=5)
        assert len(seen) == 3  # every real callback went through .process()
        wav = result_box["wav"]
        assert wav is not None
        with _wave.open(BytesIO(wav), "rb") as wf:
            import struct

            raw = wf.readframes(wf.getnframes())
            samples = struct.unpack(f"<{len(raw)//2}h", raw)
        # The WAV must contain the DENOISED (2x) values, not the raw
        # speech/silence amplitudes -- proves the return value is used.
        # speech chunk (2000) -> 4000, silence chunks (0) -> 0.
        assert samples[:160] == (4000,) * 160
        assert samples[160:] == (0,) * (len(samples) - 160)

    def test_denoiser_failure_falls_back_to_the_raw_chunk(self):
        # A broken/raising denoiser must never drop real captured audio.
        class _BoomDenoiser:
            def process(self, chunk):
                raise RuntimeError("rnnoise state corrupted")

        seg = hands_free.UtteranceSegmenter(energy_threshold=150.0, silence_ms=160.0, max_ms=10_000.0)
        stream_holder = {}

        def factory(cb):
            s = _FakeStream(cb)
            stream_holder["stream"] = s
            return s

        result_box = {}

        def run():
            result_box["wav"] = hands_free.record_utterance(
                segmenter=seg, stream_factory=factory, mic_allowed=lambda: True,
                denoiser=_BoomDenoiser(),
            )

        import threading
        t = threading.Thread(target=run)
        t.start()
        import time
        time.sleep(0.05)
        stream = stream_holder["stream"]
        stream.callback(_speech_chunk(), 160, None, None)
        stream.callback(_silence_chunk(), 160, None, None)
        stream.callback(_silence_chunk(), 160, None, None)
        t.join(timeout=5)
        wav = result_box["wav"]
        assert wav is not None
        with _wave.open(BytesIO(wav), "rb") as wf:
            assert wf.getnframes() == 160 * 3  # nothing dropped despite the raising denoiser

    def test_injected_denoiser_is_never_closed_by_record_utterance(self):
        # The caller owns an INJECTED denoiser's lifecycle -- only one
        # record_utterance() creates itself (via create_default()) gets
        # closed here.
        class _FakeDenoiser:
            def __init__(self):
                self.closed = False

            def process(self, chunk):
                return chunk

            def close(self):
                self.closed = True

        seg = hands_free.UtteranceSegmenter(max_ms=50.0)
        d = _FakeDenoiser()
        hands_free.record_utterance(
            segmenter=seg, stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: True, denoiser=d,
        )
        assert d.closed is False

    def test_owned_denoiser_is_closed_after_recording(self, monkeypatch):
        # DOURMOUSE_DENOISE is off by default in this whole test suite
        # (see conftest.py's _denoise_off) -- explicitly re-enabled here,
        # with create_default() itself monkeypatched to a fake so this
        # stays hermetic (no real ctypes library load in this test).
        monkeypatch.setenv("DOURMOUSE_DENOISE", "1")

        class _FakeDenoiser:
            def __init__(self):
                self.closed = False

            def process(self, chunk):
                return chunk

            def close(self):
                self.closed = True

        created = _FakeDenoiser()
        monkeypatch.setattr("dourmouse.audio_denoise.create_default", lambda: created)
        seg = hands_free.UtteranceSegmenter(max_ms=50.0)
        hands_free.record_utterance(
            segmenter=seg, stream_factory=lambda cb: _FakeStream(cb), mic_allowed=lambda: True,
        )
        assert created.closed is True


class TestPlayAudio:
    def test_empty_bytes_refused_honestly(self):
        assert hands_free.play_audio(b"") is False

    def test_real_bytes_reach_the_injected_player(self):
        seen = {}
        ok = hands_free.play_audio(b"fake-wav-bytes", player=lambda b: seen.setdefault("bytes", b))
        assert ok is True
        assert seen["bytes"] == b"fake-wav-bytes"

    def test_player_exception_is_honest_false_not_a_crash(self):
        def boom(b):
            raise RuntimeError("no speaker")

        assert hands_free.play_audio(b"x", player=boom) is False


class _FakeListener:
    """Mirrors WakeWordListener's real public surface (start/stop/running)
    closely enough for HandsFreeController's own tests -- captures on_wake
    so a test can fire it directly, the same way _FakeDetector lets
    test_wakeword.py drive WakeWordListener without real hardware."""

    def __init__(self, on_wake=None, mic_allowed=None):
        self.on_wake = on_wake
        self._running = False
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def running(self):
        return self._running

    def start(self):
        self.start_calls += 1
        self._running = True
        return True, "listening"

    def stop(self):
        self.stop_calls += 1
        self._running = False


class TestHandsFreeController:
    def _controller(self, **kw):
        listener = _FakeListener()
        dispatch_calls = []

        def dispatch_fn(text):
            dispatch_calls.append(text)
            return kw.pop("dispatch_reply", "here is the real reply")

        ctrl = hands_free.HandsFreeController(
            dispatch_fn=kw.pop("dispatch_fn", dispatch_fn),
            listener=listener,
            record_fn=kw.pop("record_fn", lambda **_: b"fake-wav"),
            transcribe_fn=kw.pop("transcribe_fn", lambda wav: "what is the weather"),
            speak_fn=kw.pop("speak_fn", lambda text: b"fake-tts-audio"),
            play_fn=kw.pop("play_fn", lambda audio: True),
            mic_allowed=kw.pop("mic_allowed", lambda: True),
            **kw,
        )
        return ctrl, listener, dispatch_calls

    def test_full_happy_path_calls_every_real_stage_in_order(self):
        calls = []
        ctrl, listener, dispatch_calls = self._controller(
            record_fn=lambda **_: (calls.append("record") or b"wav-bytes"),
            transcribe_fn=lambda wav: (calls.append(("transcribe", wav)) or "hello there"),
            dispatch_fn=lambda text: (calls.append(("dispatch", text)) or "hi yourself"),
            speak_fn=lambda text: (calls.append(("speak", text)) or b"audio-bytes"),
            play_fn=lambda audio: (calls.append(("play", audio)) or True),
        )
        ctrl._on_wake("hey_jarvis", 0.9)
        assert calls == [
            "record",
            ("transcribe", b"wav-bytes"),
            ("dispatch", "hello there"),
            ("speak", "hi yourself"),
            ("play", b"audio-bytes"),
        ]

    def test_on_turn_hook_receives_real_heard_and_said_text(self):
        seen = {}
        ctrl, _, _ = self._controller(
            transcribe_fn=lambda wav: "turn the lights on",
            dispatch_fn=lambda text: "done, lights on",
            on_turn=lambda heard, said: seen.update(heard=heard, said=said),
        )
        ctrl._on_wake("hey_jarvis", 0.9)
        assert seen == {"heard": "turn the lights on", "said": "done, lights on"}

    def test_mic_killed_before_recording_skips_the_whole_turn(self):
        ctrl, _, dispatch_calls = self._controller(mic_allowed=lambda: False)
        ctrl._on_wake("hey_jarvis", 0.9)
        assert dispatch_calls == []

    def test_no_utterance_recorded_skips_dispatch(self):
        ctrl, _, dispatch_calls = self._controller(record_fn=lambda **_: None)
        ctrl._on_wake("hey_jarvis", 0.9)
        assert dispatch_calls == []

    def test_empty_transcription_skips_dispatch(self):
        ctrl, _, dispatch_calls = self._controller(transcribe_fn=lambda wav: "   ")
        ctrl._on_wake("hey_jarvis", 0.9)
        assert dispatch_calls == []

    def test_transcription_failure_ends_the_turn_quietly(self):
        def boom(wav):
            raise RuntimeError("whisper crashed")

        ctrl, _, dispatch_calls = self._controller(transcribe_fn=boom)
        ctrl._on_wake("hey_jarvis", 0.9)  # must not raise
        assert dispatch_calls == []

    def test_dispatch_failure_still_speaks_an_honest_error_not_a_crash(self):
        played = []
        spoken = []

        def boom_dispatch(text):
            raise RuntimeError("dispatch exploded")

        ctrl, _, _ = self._controller(
            dispatch_fn=boom_dispatch,
            speak_fn=lambda text: (spoken.append(text) or b"audio"),
            play_fn=lambda audio: (played.append(audio) or True),
        )
        ctrl._on_wake("hey_jarvis", 0.9)  # must not raise
        assert len(spoken) == 1
        assert "dispatch exploded" in spoken[0]
        assert played == [b"audio"]

    def test_mic_killed_after_dispatch_never_speaks(self):
        """A real safety property: the kill switch flipping off MID-TURN
        (after the reply was already generated) must still stop the loop
        from speaking it out loud."""
        mic_state = {"allowed": True}
        played = []

        def dispatch_fn(text):
            mic_state["allowed"] = False  # kill switch flips during dispatch
            return "this must never be spoken"

        ctrl, _, _ = self._controller(
            dispatch_fn=dispatch_fn,
            mic_allowed=lambda: mic_state["allowed"],
            play_fn=lambda audio: (played.append(audio) or True),
        )
        ctrl._on_wake("hey_jarvis", 0.9)
        assert played == []

    def test_tts_failure_does_not_crash_the_loop(self):
        def boom_speak(text):
            raise RuntimeError("piper crashed")

        ctrl, _, dispatch_calls = self._controller(speak_fn=boom_speak)
        ctrl._on_wake("hey_jarvis", 0.9)  # must not raise
        assert dispatch_calls == ["what is the weather"]  # dispatch DID run before the TTS failure

    def test_overlapping_wake_fires_do_not_start_a_second_turn(self):
        """The busy lock's real job: openWakeWord can fire on_wake again
        while a turn is still in flight (it keeps listening on the same
        stream) -- a second concurrent turn must be a real no-op, not two
        turns racing each other."""
        import threading

        entered = threading.Event()
        release = threading.Event()
        dispatch_count = {"n": 0}

        def slow_dispatch(text):
            dispatch_count["n"] += 1
            entered.set()
            release.wait(timeout=2)
            return "done"

        ctrl, _, _ = self._controller(dispatch_fn=slow_dispatch)
        t = threading.Thread(target=ctrl._on_wake, args=("hey_jarvis", 0.9))
        t.start()
        assert entered.wait(timeout=2)
        # A second wake fire while the first turn is still mid-dispatch:
        ctrl._on_wake("hey_jarvis", 0.9)
        release.set()
        t.join(timeout=2)
        assert dispatch_count["n"] == 1  # never two overlapping turns

    def test_start_refuses_honestly_when_hands_free_disabled(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_HANDS_FREE", raising=False)
        ctrl, listener, _ = self._controller()
        ok, reason = ctrl.start()
        assert ok is False
        assert "DOURMOUSE_HANDS_FREE is off" in reason
        assert listener.start_calls == 0  # never even asked the listener to start

    def test_start_delegates_to_the_real_listener_when_enabled(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_HANDS_FREE", "1")
        ctrl, listener, _ = self._controller()
        ok, reason = ctrl.start()
        assert ok is True
        assert listener.start_calls == 1

    def test_stop_delegates_to_the_real_listener(self):
        ctrl, listener, _ = self._controller()
        ctrl.stop()
        assert listener.stop_calls == 1

    def test_running_reflects_the_real_listener_state(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_HANDS_FREE", "1")
        ctrl, listener, _ = self._controller()
        assert ctrl.running is False
        ctrl.start()
        assert ctrl.running is True
        ctrl.stop()
        assert ctrl.running is False


class TestConfigDefaults:
    def test_hands_free_enabled_default_off(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_HANDS_FREE", raising=False)
        assert hands_free.hands_free_enabled() is False

    def test_hands_free_enabled_explicit_on(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_HANDS_FREE", "1")
        assert hands_free.hands_free_enabled() is True

    def test_silence_ms_default_and_override(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_HANDS_FREE_SILENCE_MS", raising=False)
        assert hands_free.silence_ms() == hands_free._DEFAULT_SILENCE_MS
        monkeypatch.setenv("DOURMOUSE_HANDS_FREE_SILENCE_MS", "500")
        assert hands_free.silence_ms() == 500.0

    def test_bad_env_values_fall_back_to_the_real_default_not_a_crash(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_HANDS_FREE_SILENCE_MS", "not-a-number")
        assert hands_free.silence_ms() == hands_free._DEFAULT_SILENCE_MS
