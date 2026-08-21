"""v8.18 voice/text response split — dispatch-level channel gating.

Mirrors ``TestBoundaryPlacement`` in test_brief_intent.py: the voice-reply
rule is appended to the last user turn at the API boundary (never the
system prompt, and never persisted to history) for the exact reason
documented on ``_VOICE_MARKER`` in dispatch.py — placed in the system
prompt instead, this backend has been observed to follow a style note
only inconsistently; on the last user turn (the same spot the brevity
marker and _NO_THINK_TOKEN already use) it holds turn over turn.

These tests are hermetic: FakeClient never leaves the process, and the
prompt used deliberately avoids the brief-intent lookup cues (see
test_brief_intent.py) so a stray ``_BRIEF_MARKER`` never confuses a
``voice``-only assertion.
"""

from __future__ import annotations

from dourmouse.dispatch import _VOICE_MARKER, run_dispatch, run_dispatch_messages
from dourmouse.general_roster import build_general_registry
from dourmouse.tests.test_dispatch import FakeClient, _FakeMessage, _FakeResponse

# Not brief-shaped (no _BRIEF_CUES word in it) so the brief marker never
# rides along and muddies a voice-only assertion.
_NEUTRAL_PROMPT = "tell my sister I said hello"


def _sent(prompt: str, *, voice: bool) -> list[dict]:
    client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
    run_dispatch(prompt, build_general_registry(), client=client, voice=voice)
    return client.chat.completions.calls[0]["messages"]


class TestVoiceChannelGating:
    """The addendum is applied ONLY when the voice channel flag is set."""

    def test_voice_marker_present_when_voice_true(self):
        sent = _sent(_NEUTRAL_PROMPT, voice=True)
        assert sent[-1]["role"] == "user"
        assert _VOICE_MARKER in sent[-1]["content"]

    def test_voice_marker_absent_when_voice_false(self):
        sent = _sent(_NEUTRAL_PROMPT, voice=False)
        assert all(_VOICE_MARKER not in (m.get("content") or "") for m in sent)

    def test_voice_marker_absent_by_default(self):
        """Text-channel behavior is unchanged: a caller that never passes
        ``voice`` at all (every existing call site before this change) must
        never see the spoken-reply rule applied."""
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        run_dispatch(_NEUTRAL_PROMPT, build_general_registry(), client=client)
        sent = client.chat.completions.calls[0]["messages"]
        assert all(_VOICE_MARKER not in (m.get("content") or "") for m in sent)


class TestVoiceBoundaryPlacement:
    def test_marker_rides_the_last_user_turn(self):
        sent = _sent(_NEUTRAL_PROMPT, voice=True)
        assert sent[-1]["role"] == "user"
        assert sent[-1]["content"].endswith(_VOICE_MARKER)

    def test_marker_is_not_its_own_message(self):
        sent = _sent(_NEUTRAL_PROMPT, voice=True)
        assert all(m.get("content") != _VOICE_MARKER for m in sent)

    def test_the_system_prompt_is_left_alone(self):
        sent = _sent(_NEUTRAL_PROMPT, voice=True)
        assert sent[0]["role"] == "system"
        assert _VOICE_MARKER not in sent[0]["content"]

    def test_the_users_own_words_are_preserved(self):
        sent = _sent(_NEUTRAL_PROMPT, voice=True)
        assert _NEUTRAL_PROMPT in sent[-1]["content"]

    def test_marker_is_not_persisted_to_the_authoritative_message_list(self):
        """The boundary copy carries it; the caller-owned list passed into
        run_dispatch_messages must not, or a resumed session re-reads a
        stale voice instruction on a later, typed turn."""
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        messages = [
            {"role": "system", "content": "you are a test system prompt"},
            {"role": "user", "content": _NEUTRAL_PROMPT},
        ]
        run_dispatch_messages(
            messages, build_general_registry(), client=client, voice=True
        )
        for m in messages:
            assert _VOICE_MARKER not in (m.get("content") or "")


class TestVoiceMarkerContent:
    """Pins the wording lessons already learned from _BRIEF_MARKER (see its
    docstring in dispatch.py): no digits (a reasoning-tuned model has been
    observed counting a numeric constraint out loud instead of just
    honoring it), wrapped in parentheses like the other boundary markers."""

    def test_no_numbers_in_marker(self):
        assert not any(ch.isdigit() for ch in _VOICE_MARKER)

    def test_marker_is_parenthetical(self):
        assert _VOICE_MARKER.startswith("(") and _VOICE_MARKER.endswith(")")

    def test_marker_mentions_the_key_constraints(self):
        text = _VOICE_MARKER.lower()
        for cue in ("markdown", "spoken", "question"):
            assert cue in text


class TestVoiceAndBriefStack:
    """A spoken lookup is both brief AND voice — both markers apply, and
    neither replaces the other."""

    def test_both_markers_present_on_a_spoken_lookup(self):
        from dourmouse.dispatch import _BRIEF_MARKER

        sent = _sent("what is the capital of France", voice=True)
        content = sent[-1]["content"]
        assert _BRIEF_MARKER in content
        assert _VOICE_MARKER in content
