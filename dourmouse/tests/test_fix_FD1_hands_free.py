"""Fix agent FD1: P5-23 (mic kill switch ignored while recording) and P5-24 (the whole turn ran inside the
wake-word audio callback)."""

from __future__ import annotations

import threading
import time

from dourmouse import hands_free


class _Stream:
    def __init__(self, callback):
        self.cb = callback
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        pass


class _NeverDone:
    """A segmenter that never says the utterance is over."""
    max_ms = 3000.0

    def reset(self):
        pass

    def feed(self, chunk):
        return False


def test_kill_switch_flipping_mid_record_stops_it_and_discards_the_audio():
    allowed = {"v": True}
    streams = []

    def factory(cb):
        s = _Stream(cb)
        streams.append(s)
        return s

    def flip():
        time.sleep(0.25)
        allowed["v"] = False

    threading.Thread(target=flip, daemon=True).start()

    def feeder():
        # keep delivering audio like a live stream would
        while not streams:
            time.sleep(0.01)
        s = streams[0]
        end = time.time() + 2.0
        while time.time() < end and not s.stopped:
            s.cb([100] * 1280, 1280, None, None)
            time.sleep(0.05)

    threading.Thread(target=feeder, daemon=True).start()
    t0 = time.time()
    out = hands_free.record_utterance(
        segmenter=_NeverDone(), stream_factory=factory, mic_allowed=lambda: allowed["v"], denoiser=object(),
    )
    elapsed = time.time() - t0
    assert out is None, "audio captured before the kill switch flipped must be discarded"
    assert elapsed < 1.5, f"recording ran {elapsed:.1f}s after the kill switch flipped"
    assert streams[0].stopped


def test_on_wake_returns_immediately_and_runs_the_turn_on_a_worker_thread():
    entered = threading.Event()
    release = threading.Event()
    turn_thread = {}

    def slow_dispatch(text):
        turn_thread["t"] = threading.current_thread()
        entered.set()
        release.wait(timeout=3)
        return "done"

    class L:
        running = False

        def start(self):
            return True, ""

        def stop(self):
            pass

    ctrl = hands_free.HandsFreeController(
        dispatch_fn=slow_dispatch, listener=L(), record_fn=lambda **_: b"wav",
        transcribe_fn=lambda w: "hello", speak_fn=lambda t: b"a", play_fn=lambda a: True,
        mic_allowed=lambda: True,
    )
    t0 = time.time()
    ctrl._on_wake("hey", 0.9)
    assert time.time() - t0 < 0.5, "_on_wake must not wait for the turn (it runs inside the audio callback)"
    assert entered.wait(timeout=2)
    assert turn_thread["t"] is not threading.current_thread()
    assert turn_thread["t"].daemon
    # a second wake while the turn is in flight is still a no-op
    ctrl._on_wake("hey", 0.9)
    release.set()
    ctrl._turn_thread.join(timeout=3)
    assert not ctrl._busy.locked()
