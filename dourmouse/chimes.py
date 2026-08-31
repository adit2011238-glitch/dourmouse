"""Proactive audio chimes for finished/failed background work (v13.5).

Vision OS checklist item 6: "when background automation pipelines (such
as a Docker build or regression test suite) finish or fail, the system
avoids jarring text pop-ups. Instead, it triggers Piper... a calm,
concise vocal status update."

In THIS codebase, the real analog of "a background automation pipeline"
is a JobTracker-tracked delegated run (dourmouse/dispatch.py's
JobTracker — every ``delegate_task``/``delegate_parallel`` call spawns
one; this is how a coding agent running tests, a research agent doing a
multi-step search, etc. actually run as background work relative to the
main conversation). Rather than inventing a second, parallel "background
job" concept, this hooks the SAME real JobTracker.finish()/refuse()
calls webui.py's DELEGATED TASKS panel already reads.

Reuses the SAME real TTS/playback primitives already built and live-
verified this session for the hands-free voice loop
(dourmouse/voice.py's text_to_speech, dourmouse/hands_free.py's
play_audio) — no second implementation. Fail-safe by design: an
announce() call NEVER raises and never blocks the caller for long
(spoken in a background thread) — a chime is a pure side effect, and
same house rule as _emit_event/experience_sink elsewhere: an observer
must never break the real work it's observing.

Honesty (Rule 2.1/2.2): like every other voice/audio capability in this
codebase, the actual sound coming out of real speakers cannot be
confirmed from this sandbox — what IS verified here (see
dourmouse/tests/test_chimes.py) is that the right text gets built for a
given job outcome, that play_audio/text_to_speech are called with that
text and nothing else, and that a raising speak/play function can never
propagate out of announce().
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

_CHIMES_ENV = "DOURMOUSE_CHIMES"


def chimes_enabled(value: str | None = None) -> bool:
    """DOURMOUSE_CHIMES: speak a short status chime when a delegated
    (background) job finishes or fails. Default on — set to 0/off to
    disable, same convention as DOURMOUSE_FAST_LANE/DOURMOUSE_HANDS_FREE.
    """
    raw = value if value is not None else os.environ.get(_CHIMES_ENV, "1")
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def job_finished_message(job: dict[str, Any]) -> str:
    """Build a short, calm spoken sentence from a JobTracker job snapshot
    dict (the same shape JobTracker.snapshot() returns). Pure function —
    no I/O, directly testable with fabricated job dicts.

    Deliberately short (Rule: a chime is a status ping, not a report) —
    the full result/error text is still in the DELEGATED TASKS panel for
    anyone who wants the detail; this is just "it's done, here's the
    headline."
    """
    subagent = job.get("subagent") or "a background task"
    status = job.get("status")
    if status == "done":
        return f"{subagent} finished."
    if status == "refused":
        reason = (job.get("error") or "").strip()
        short = (reason[:80] + "…") if len(reason) > 80 else reason
        return f"{subagent} was refused" + (f": {short}" if short else ".")
    if status == "error":
        err = (job.get("error") or "").strip()
        short = (err[:80] + "…") if len(err) > 80 else err
        return f"{subagent} failed" + (f": {short}" if short else ".")
    # Honest fallback for a status this function doesn't recognize —
    # never silently says nothing about a real state change.
    return f"{subagent} status: {status or 'unknown'}."


def announce(
    text: str,
    *,
    speak_fn: Callable[[str], bytes] | None = None,
    play_fn: Callable[[bytes], bool] | None = None,
) -> None:
    """Speak ``text`` as a chime, in a background thread so the caller
    (JobTracker.finish/refuse, called from inside a live dispatch turn)
    is never blocked waiting on TTS synthesis + playback.

    Never raises: any failure in synthesis or playback is swallowed here
    (Rule: an observer must never break the real work it's observing —
    same convention as dispatch._emit_event). ``speak_fn``/``play_fn``
    are injectable for hermetic testing (default to the real
    dourmouse.voice.text_to_speech / dourmouse.hands_free.play_audio).
    """
    if not text.strip():
        return

    def _run() -> None:
        try:
            speak = speak_fn
            play = play_fn
            if speak is None:
                from dourmouse.voice import text_to_speech

                speak = text_to_speech
            if play is None:
                from dourmouse.hands_free import play_audio

                play = play_audio
            wav_bytes = speak(text)
            if wav_bytes:
                play(wav_bytes)
        except Exception:  # noqa: BLE001 - a chime must never break real work
            pass

    threading.Thread(target=_run, daemon=True).start()


def announce_job_result(
    job: dict[str, Any],
    *,
    speak_fn: Callable[[str], bytes] | None = None,
    play_fn: Callable[[bytes], bool] | None = None,
) -> None:
    """The real JobTracker chime_fn hook — builds the message and
    announces it, gated by chimes_enabled(). One call, one job, one
    chime: JobTracker already tracks nested/nested runs by depth, and
    this deliberately does NOT chime for every single one (a deep
    delegate_parallel fan-out would turn into a chime storm) — see
    JobTracker's own chime_fn docstring for the depth==0-only gate that
    keeps this to "one background pipeline, one status ping."
    """
    if not chimes_enabled():
        return
    announce(job_finished_message(job), speak_fn=speak_fn, play_fn=play_fn)
