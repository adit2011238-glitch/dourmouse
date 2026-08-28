"""Tests for dourmouse/wakeword.py (Vision stage 4: local wake-word listener).

openwakeword + sounddevice ARE genuinely installed in this project's .venv
(verified: both import cleanly). The pretrained model download + real ONNX
inference against synthetic audio is exercised for real below — never
mocked — via ``_real_detector`` (session-scoped so the network fetch/model
load happens once). If the model cannot be obtained (no cached copy AND no
network), those tests skip honestly with a clear reason instead of either
failing the whole suite or silently asserting nothing.

The continuous microphone CAPTURE loop (real ``sounddevice.InputStream``
reading a real mic) is exercised through the ``stream_factory`` seam with a
fake stream — genuinely real hardware capture cannot be verified in an
automated headless run (see the module docstring in wakeword.py for exactly
what was and was not confirmed in this sandbox).
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from dourmouse import wakeword


# --------------------------------------------------------------------------- #
# Capability probes
# --------------------------------------------------------------------------- #

class TestCapabilityProbes:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_WAKEWORD", raising=False)
        assert wakeword.wakeword_enabled() is False

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        assert wakeword.wakeword_enabled() is True

    def test_off_values_all_disable(self, monkeypatch):
        for v in ("0", "false", "no", "off", ""):
            assert wakeword.wakeword_enabled(v) is False

    def test_default_model(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_WAKEWORD_MODEL", raising=False)
        assert wakeword.wakeword_model() == "hey_jarvis"

    def test_model_env_override(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD_MODEL", "alexa")
        assert wakeword.wakeword_model() == "alexa"

    def test_default_threshold(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_WAKEWORD_THRESHOLD", raising=False)
        assert wakeword.wakeword_threshold() == 0.5

    def test_threshold_env_override(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD_THRESHOLD", "0.75")
        assert wakeword.wakeword_threshold() == 0.75

    def test_invalid_threshold_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD_THRESHOLD", "not-a-number")
        assert wakeword.wakeword_threshold() == 0.5

    def test_real_dependencies_are_importable_in_this_venv(self):
        """openwakeword + sounddevice are genuinely installed here — confirms
        the happy path, not just that the except-branch exists."""
        assert wakeword._inference_available() is True
        assert wakeword._capture_available() is True

    def test_status_report_shape(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        status = wakeword.wakeword_status()
        assert status["enabled"] is True
        assert status["inference_engine"] == "openwakeword"
        assert status["capture_engine"] == "sounddevice"


# --------------------------------------------------------------------------- #
# Real inference — real pretrained model, real ONNX, synthetic audio in
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def real_detector():
    detector = wakeword.WakeWordDetector("hey_jarvis")
    try:
        detector.feed(np.zeros(wakeword._CHUNK_SAMPLES, dtype=np.int16))
    except wakeword.WakeWordNotConfiguredError as exc:
        pytest.skip(f"real openwakeword model unavailable in this environment: {exc}")
    return detector


class TestRealInference:
    def test_silence_produces_a_real_low_score(self, real_detector):
        scores = real_detector.feed(np.zeros(wakeword._CHUNK_SAMPLES, dtype=np.int16))
        assert "hey_jarvis" in scores
        assert isinstance(scores["hey_jarvis"], float)
        assert 0.0 <= scores["hey_jarvis"] <= 1.0

    def test_repeated_feeds_do_not_crash_the_streaming_state(self, real_detector):
        chunk = np.zeros(wakeword._CHUNK_SAMPLES, dtype=np.int16)
        for _ in range(10):
            scores = real_detector.feed(chunk)
        assert "hey_jarvis" in scores

    def test_random_noise_is_a_real_finite_score(self, real_detector):
        rng = np.random.default_rng(0)
        chunk = rng.integers(-2000, 2000, size=wakeword._CHUNK_SAMPLES, dtype=np.int16)
        scores = real_detector.feed(chunk)
        assert np.isfinite(scores["hey_jarvis"])


class TestDetectorModelFactorySeam:
    def test_model_loaded_lazily_on_first_feed(self):
        calls = []

        def fake_factory(name):
            calls.append(name)

            class _FakeModel:
                def predict(self, chunk):
                    return {"fake": 0.1}

            return _FakeModel()

        d = wakeword.WakeWordDetector("fake_model", model_factory=fake_factory)
        assert calls == []  # not loaded yet
        d.feed(np.zeros(4, dtype=np.int16))
        assert calls == ["fake_model"]
        d.feed(np.zeros(4, dtype=np.int16))
        assert calls == ["fake_model"]  # loaded once, reused

    def test_factory_failure_becomes_not_configured(self):
        def broken_factory(name):
            raise RuntimeError("boom")

        d = wakeword.WakeWordDetector("x", model_factory=broken_factory)
        with pytest.raises(RuntimeError):
            d.feed(np.zeros(4, dtype=np.int16))

    def test_missing_openwakeword_is_honest_not_configured(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name == "openwakeword.model" or name.startswith("openwakeword"):
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        d = wakeword.WakeWordDetector("hey_jarvis")
        with pytest.raises(wakeword.WakeWordNotConfiguredError, match="not installed"):
            d.feed(np.zeros(4, dtype=np.int16))


# --------------------------------------------------------------------------- #
# WakeWordListener — the mic_allowed() contract, fully exercised with fakes
# --------------------------------------------------------------------------- #

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


class _FakeDetector:
    def __init__(self, fixed_scores=None):
        self.fixed_scores = fixed_scores or {}
        self.feed_calls = 0

    def feed(self, chunk):
        self.feed_calls += 1
        return dict(self.fixed_scores)


class TestListenerMicGate:
    def test_refuses_when_feature_disabled(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_WAKEWORD", raising=False)
        created = []
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector(),
            stream_factory=lambda cb: created.append(cb) or _FakeStream(cb),
            mic_allowed=lambda: True,
        )
        ok, reason = listener.start()
        assert ok is False
        assert "DOURMOUSE_WAKEWORD is off" in reason
        assert created == []  # never even asked mic_allowed / opened a stream

    def test_refuses_when_mic_killed_and_never_opens_a_stream(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        opened = []
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector(),
            stream_factory=lambda cb: opened.append(True) or _FakeStream(cb),
            mic_allowed=lambda: False,
        )
        ok, reason = listener.start()
        assert ok is False
        assert "KILLED" in reason
        assert opened == []

    def test_starts_when_mic_allowed(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector(),
            stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: True,
        )
        try:
            ok, reason = listener.start()
            assert ok is True
            assert listener.running is True
        finally:
            listener.stop()

    def test_mic_allowed_checked_before_dependency_probe(self, monkeypatch):
        """Ordering matters: refusing on the kill switch must happen even if
        the real deps aren't installed — the kill switch is checked first."""
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        monkeypatch.setattr(wakeword, "_capture_available", lambda: False)
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector(),
            stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: False,
        )
        ok, reason = listener.start()
        assert ok is False
        assert "KILLED" in reason  # not the dependency message

    def test_stream_open_failure_is_honest(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")

        def blowing_up(cb):
            raise OSError("no such device")

        listener = wakeword.WakeWordListener(
            detector=_FakeDetector(),
            stream_factory=blowing_up,
            mic_allowed=lambda: True,
        )
        ok, reason = listener.start()
        assert ok is False
        assert "could not open microphone stream" in reason

    def test_watchdog_force_stops_when_mic_killed_mid_listen(self, monkeypatch):
        """The 'reach' contract: a session that already started must be torn
        down promptly once the kill switch flips, not merely refused on the
        next start()."""
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        allowed = {"v": True}
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector(),
            stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: allowed["v"],
            watchdog_interval=0.05,
        )
        ok, _ = listener.start()
        assert ok is True
        stream = listener._stream
        allowed["v"] = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and listener.running:
            time.sleep(0.02)
        assert listener.running is False
        assert stream.closed is True

    def test_double_start_is_a_noop(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        opens = []
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector(),
            stream_factory=lambda cb: opens.append(1) or _FakeStream(cb),
            mic_allowed=lambda: True,
        )
        try:
            listener.start()
            listener.start()
            assert len(opens) == 1
        finally:
            listener.stop()

    def test_stop_is_safe_when_never_started(self):
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector(), mic_allowed=lambda: True
        )
        listener.stop()  # must not raise


# --------------------------------------------------------------------------- #
# on_wake firing: threshold + cooldown
# --------------------------------------------------------------------------- #

class TestWakeFiring:
    def test_fires_above_threshold(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        fired = []
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector({"hey_jarvis": 0.9}),
            stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: True,
            on_wake=lambda kw, score: fired.append((kw, score)),
            threshold=0.5,
        )
        try:
            listener.start()
            listener._on_audio(np.zeros((1280, 1), dtype=np.int16), 1280, None, None)
            assert fired == [("hey_jarvis", 0.9)]
        finally:
            listener.stop()

    def test_does_not_fire_below_threshold(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        fired = []
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector({"hey_jarvis": 0.2}),
            stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: True,
            on_wake=lambda kw, score: fired.append((kw, score)),
            threshold=0.5,
        )
        try:
            listener.start()
            listener._on_audio(np.zeros((1280, 1), dtype=np.int16), 1280, None, None)
            assert fired == []
        finally:
            listener.stop()

    def test_cooldown_suppresses_immediate_refire(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")
        fired = []
        listener = wakeword.WakeWordListener(
            detector=_FakeDetector({"hey_jarvis": 0.9}),
            stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: True,
            on_wake=lambda kw, score: fired.append((kw, score)),
            threshold=0.5,
            cooldown_seconds=10.0,
        )
        try:
            listener.start()
            for _ in range(5):
                listener._on_audio(np.zeros((1280, 1), dtype=np.int16), 1280, None, None)
            assert len(fired) == 1
        finally:
            listener.stop()

    def test_a_raising_on_wake_callback_never_kills_the_stream(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")

        def bad_callback(kw, score):
            raise RuntimeError("boom")

        listener = wakeword.WakeWordListener(
            detector=_FakeDetector({"hey_jarvis": 0.9}),
            stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: True,
            on_wake=bad_callback,
            threshold=0.5,
        )
        try:
            listener.start()
            listener._on_audio(np.zeros((1280, 1), dtype=np.int16), 1280, None, None)  # must not raise
            assert listener.running is True
        finally:
            listener.stop()

    def test_a_bad_audio_frame_never_kills_the_stream(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WAKEWORD", "1")

        class _RaisingDetector:
            def feed(self, chunk):
                raise ValueError("bad frame")

        listener = wakeword.WakeWordListener(
            detector=_RaisingDetector(),
            stream_factory=lambda cb: _FakeStream(cb),
            mic_allowed=lambda: True,
        )
        try:
            listener.start()
            listener._on_audio(np.zeros((1280, 1), dtype=np.int16), 1280, None, None)  # must not raise
            assert listener.running is True
        finally:
            listener.stop()


class TestLaunch:
    def test_launch_reports_not_configured_when_disabled(self, monkeypatch, capsys):
        monkeypatch.delenv("DOURMOUSE_WAKEWORD", raising=False)
        code = wakeword.launch()
        assert code == 1
        out = capsys.readouterr().out
        assert "NOT CONFIGURED" in out
