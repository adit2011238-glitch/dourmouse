"""dourmouse/chimes.py — Vision OS checklist item 6, "proactive audio
interruption & contextual chimes." See that module's own docstring for
why a JobTracker-tracked delegated run is the real analog of "a
background automation pipeline finishing," and for the honesty limits on
what's verified here vs. what needs a real desktop session (same
discipline as dourmouse/hands_free.py's own test file).
"""

from __future__ import annotations

import threading
import time

import pytest

from dourmouse import chimes


class TestChimesEnabled:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_CHIMES", raising=False)
        assert chimes.chimes_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
    def test_off_values(self, value):
        assert chimes.chimes_enabled(value) is False

    def test_reads_the_real_env_var(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_CHIMES", "0")
        assert chimes.chimes_enabled() is False


class TestJobFinishedMessage:
    def test_done_message(self):
        job = {"subagent": "dev_coding", "status": "done", "result": "all green"}
        msg = chimes.job_finished_message(job)
        assert msg == "dev_coding finished."

    def test_error_message_includes_a_short_reason(self):
        job = {"subagent": "dev_coding", "status": "error", "error": "pytest exit 1"}
        msg = chimes.job_finished_message(job)
        assert msg == "dev_coding failed: pytest exit 1"

    def test_error_message_with_no_reason_still_says_failed(self):
        job = {"subagent": "dev_coding", "status": "error", "error": ""}
        assert chimes.job_finished_message(job) == "dev_coding failed."

    def test_refused_message(self):
        job = {"subagent": "gmail_agent", "status": "refused", "error": "policy"}
        assert chimes.job_finished_message(job) == "gmail_agent was refused: policy"

    def test_long_error_is_truncated_for_a_short_spoken_chime(self):
        job = {"subagent": "x", "status": "error", "error": "e" * 200}
        msg = chimes.job_finished_message(job)
        assert msg.startswith("x failed: " + "e" * 80 + "…")
        assert len(msg) < 200  # genuinely shortened, not just cosmetically

    def test_missing_subagent_still_produces_an_honest_sentence(self):
        job = {"subagent": None, "status": "done"}
        assert chimes.job_finished_message(job) == "a background task finished."

    def test_unknown_status_is_not_silently_dropped(self):
        job = {"subagent": "x", "status": "running"}
        assert "running" in chimes.job_finished_message(job)


class TestAnnounce:
    def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_calls_speak_then_play_with_the_real_text(self):
        calls = []

        def fake_speak(text):
            calls.append(("speak", text))
            return b"FAKE_WAV_BYTES"

        def fake_play(wav_bytes):
            calls.append(("play", wav_bytes))
            return True

        chimes.announce("dev_coding finished.", speak_fn=fake_speak, play_fn=fake_play)
        assert self._wait_for(lambda: len(calls) == 2)
        assert calls[0] == ("speak", "dev_coding finished.")
        assert calls[1] == ("play", b"FAKE_WAV_BYTES")

    def test_empty_text_never_calls_speak(self):
        calls = []
        chimes.announce("   ", speak_fn=lambda t: calls.append(t))
        time.sleep(0.05)
        assert calls == []

    def test_empty_wav_bytes_never_calls_play(self):
        played = []
        chimes.announce(
            "hi",
            speak_fn=lambda t: b"",
            play_fn=lambda b: played.append(b),
        )
        time.sleep(0.05)
        assert played == []

    def test_a_raising_speak_fn_never_propagates(self):
        def boom(_text):
            raise RuntimeError("piper crashed")

        # Runs on a background thread -- must not raise here, and must not
        # crash the thread in a way pytest can observe as a test failure.
        chimes.announce("hi", speak_fn=boom)
        time.sleep(0.05)  # let the background thread actually run

    def test_a_raising_play_fn_never_propagates(self):
        def boom(_wav):
            raise RuntimeError("speaker device gone")

        chimes.announce("hi", speak_fn=lambda t: b"WAV", play_fn=boom)
        time.sleep(0.05)

    def test_runs_off_the_calling_thread(self):
        # A real requirement, not incidental: JobTracker.finish() calls
        # this from inside a live dispatch turn -- announce() must not
        # block that turn waiting on real TTS synthesis + playback.
        calling_thread = threading.current_thread()
        seen_thread = {}

        def fake_speak(_text):
            seen_thread["name"] = threading.current_thread()
            return b"WAV"

        chimes.announce("hi", speak_fn=fake_speak, play_fn=lambda b: True)
        assert self._wait_for(lambda: "name" in seen_thread)
        assert seen_thread["name"] is not calling_thread


class TestAnnounceJobResult:
    def test_disabled_never_calls_speak(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_CHIMES", "0")
        calls = []
        chimes.announce_job_result(
            {"subagent": "x", "status": "done"}, speak_fn=lambda t: calls.append(t)
        )
        time.sleep(0.05)
        assert calls == []

    def test_enabled_speaks_the_real_built_message(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_CHIMES", "1")
        calls = []
        job = {"subagent": "dev_coding", "status": "done"}
        chimes.announce_job_result(job, speak_fn=lambda t: calls.append(t) or b"WAV")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not calls:
            time.sleep(0.01)
        assert calls == ["dev_coding finished."]
