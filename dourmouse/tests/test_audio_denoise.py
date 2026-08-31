"""dourmouse/audio_denoise.py — Vision OS checklist item 5, real-time
RNNoise noise scrubbing ahead of STT. See that module's own docstring for
the real, live-reproduced pyrnnoise/audiolab version-mismatch bug this
sidesteps by using the low-level ctypes binding directly.

Runs against the REAL, installed pyrnnoise/RNNoise C library — not a
mock — the same "prefer real execution over a fake" discipline already
established for e.g. test_wakeword.py's ONNX inference and
test_hands_free.py's synthetic-audio segmenter tests. Skips gracefully
(not a failure) on a machine where pyrnnoise isn't installed, same
pattern test_workspace_hand_gestures.py uses for a missing `node`.

Honesty limit (same as every other audio capability this session): this
proves the real library integrates correctly and processes real audio
data end to end — it does NOT and cannot prove denoising improves real
transcription accuracy against real background noise; that needs a live
desktop session with a real noisy room and a real microphone.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

try:
    import pyrnnoise  # noqa: F401

    _HAS_PYRNNOISE = True
except ImportError:
    _HAS_PYRNNOISE = False

from dourmouse import audio_denoise

pytestmark = pytest.mark.skipif(
    not _HAS_PYRNNOISE, reason="pyrnnoise not installed on this machine"
)

_CHUNK_SAMPLES = 1280  # dourmouse/wakeword.py's own real chunk size


def _white_noise_chunk(amplitude=4000, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(_CHUNK_SAMPLES) * amplitude).astype(np.int16)


def _silence_chunk():
    return np.zeros(_CHUNK_SAMPLES, dtype=np.int16)


class TestDenoiseEnabled:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_DENOISE", raising=False)
        assert audio_denoise.denoise_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_off_values(self, value):
        assert audio_denoise.denoise_enabled(value) is False


class TestRnnoiseDenoiserRealLibrary:
    def test_process_returns_the_same_shape_and_dtype(self):
        with audio_denoise.RnnoiseDenoiser() as d:
            out = d.process(_silence_chunk())
        assert out.shape == (_CHUNK_SAMPLES,)
        assert out.dtype == np.int16

    def test_process_actually_changes_noisy_input(self):
        # Not a perceptual-quality claim -- just proves this is genuinely
        # running real RNNoise processing, not a silent passthrough.
        with audio_denoise.RnnoiseDenoiser() as d:
            noisy = _white_noise_chunk()
            out = d.process(noisy)
        assert not np.array_equal(out, noisy)

    def test_state_persists_across_multiple_calls(self):
        # RNNoise adapts to the noise floor across frames -- the SAME
        # instance must accept repeated real calls without error (this is
        # its actual intended usage, one state per recording session).
        with audio_denoise.RnnoiseDenoiser() as d:
            for i in range(5):
                out = d.process(_white_noise_chunk(seed=i))
                assert out.shape == (_CHUNK_SAMPLES,)

    def test_wrong_length_raises_a_clear_error(self):
        with audio_denoise.RnnoiseDenoiser() as d:
            with pytest.raises(ValueError, match="does not resample"):
                d.process(np.zeros(999, dtype=np.int16))

    def test_2d_input_raises_a_clear_error(self):
        with audio_denoise.RnnoiseDenoiser() as d:
            with pytest.raises(ValueError, match="1D mono"):
                d.process(np.zeros((_CHUNK_SAMPLES, 2), dtype=np.int16))

    def test_process_after_close_raises(self):
        d = audio_denoise.RnnoiseDenoiser()
        d.close()
        with pytest.raises(RuntimeError, match="after close"):
            d.process(_silence_chunk())

    def test_double_close_never_raises(self):
        d = audio_denoise.RnnoiseDenoiser()
        d.close()
        d.close()  # must be a genuine no-op, not a double-free crash

    def test_input_array_is_never_mutated(self):
        noisy = _white_noise_chunk()
        original = noisy.copy()
        with audio_denoise.RnnoiseDenoiser() as d:
            d.process(noisy)
        assert np.array_equal(noisy, original)


class TestCreateDefault:
    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_DENOISE", "0")
        assert audio_denoise.create_default() is None

    def test_enabled_returns_a_real_working_instance(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_DENOISE", "1")
        d = audio_denoise.create_default()
        try:
            assert d is not None
            out = d.process(_silence_chunk())
            assert out.shape == (_CHUNK_SAMPLES,)
        finally:
            if d is not None:
                d.close()
