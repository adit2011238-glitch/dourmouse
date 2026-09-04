"""Lookup-shaped turns must answer short ("stop the essays").

Measured on the live desktop against nvidia/nemotron-3-super-120b-a12b
before this change: tool lookups already answered in 16-18 words, but
question-shaped turns padded a correct one-line answer into an article --
"how do I list files in a folder on windows" came back as 187 words across
four headed sections, "explain what a virtual environment is" as 113.

The classifier is deterministic keywords (Rule 2.8), so the same prompt is
classified the same way twice. Its bias is deliberate: missing one essay is
cheap, squeezing a request that genuinely wanted length is not.
"""

from __future__ import annotations

import pytest

from dourmouse.config import brief_mode_enabled
from dourmouse.dispatch import _BRIEF_MARKER, _is_brief_intent

# The prompts that actually produced essays on the live box, plus the
# lookup shapes around them.
BRIEF = [
    "how do I list files in a folder on windows",
    "explain what a virtual environment is",  # the 113-word one
    "what is a virtual environment",
    "what's the capital of France",
    "how much free disk space do I have",
    "how many bytes in a gigabyte",
    "who is the CEO of NVIDIA",
    "define idempotent",
    "can I run this without admin rights",
    "which port does the web UI use",
]

# Requests that ASK for length. Squeezing these is the regression to fear.
VERBOSE = [
    "write me an essay about virtual environments",
    "explain in detail how the dispatch loop works",
    "compare Ollama and NVIDIA NIM for this workload",
    "give me a step by step guide to setting up the server",
    "draft an email to the team about the release",
    "research the best approach for hand tracking",
    "list all the agents and what each one does",
    "what is the difference between these two, in detail",
    "brainstorm ideas for the world monitor",
    "analyse why the launch is slow",
]


@pytest.mark.parametrize("prompt", BRIEF)
def test_lookup_shapes_are_brief(prompt):
    assert _is_brief_intent(prompt) is True


@pytest.mark.parametrize("prompt", VERBOSE)
def test_requests_that_want_room_keep_it(prompt):
    assert _is_brief_intent(prompt) is False


def test_a_long_prompt_is_never_squeezed():
    """A prompt carrying its own detail wants a real answer, even though it
    opens with a lookup cue."""
    prompt = "what is the cleanest way to " + " ".join(["restructure"] * 30)
    assert _is_brief_intent(prompt) is False


def test_empty_prompt_is_not_brief():
    assert _is_brief_intent("") is False
    assert _is_brief_intent("   ") is False


def test_classifier_is_deterministic():
    for prompt in BRIEF + VERBOSE:
        assert _is_brief_intent(prompt) is _is_brief_intent(prompt)


def test_marker_carries_no_numeric_budget():
    """A word/sentence count reads as a puzzle to a reasoning-tuned model:
    given "under 60 words" this brain wrote the answer and then counted it
    out loud -- "Word count: Let's count. A(1) REST2 API3 ..." -- 202 words
    of visible arithmetic. Nothing in the marker may be countable."""
    assert not any(ch.isdigit() for ch in _BRIEF_MARKER)
    for banned in ("word", "sentences", "count"):
        assert banned not in _BRIEF_MARKER.lower()


def test_marker_is_one_short_clause():
    """A long multi-clause rule made the model treat it as a task and
    deliberate about it in the reply. One parenthetical has nothing worth
    planning about."""
    assert len(_BRIEF_MARKER) < 120
    assert _BRIEF_MARKER.count(".") <= 1
    assert _BRIEF_MARKER.startswith("(") and _BRIEF_MARKER.endswith(")")


def test_marker_still_asks_for_brevity_and_bans_structure():
    text = _BRIEF_MARKER.lower()
    assert "brief" in text
    assert "headings" in text and "lists" in text


def test_brief_mode_defaults_on_and_is_switchable():
    assert brief_mode_enabled() is True
    assert brief_mode_enabled("0") is False
    assert brief_mode_enabled("off") is False


class TestBoundaryPlacement:
    """WHERE the rule goes is the thing that made it work, so it is pinned.

    Appended to the system prompt, the 120B obeyed it only sometimes (two
    identical runs of six lookups: medians 32 and 94 words). As its own
    trailing system message it was treated as a task and deliberated about
    in the answer. On the last user turn -- the same placement _NO_THINK_TOKEN
    already uses, for the same reason -- it holds. These tests fail if
    someone moves it back.
    """

    def _sent(self, prompt):
        from dourmouse.dispatch import run_dispatch
        from dourmouse.general_roster import build_general_registry
        from dourmouse.tests.test_dispatch import (
            FakeClient,
            _FakeMessage,
            _FakeResponse,
        )

        client = FakeClient([_FakeResponse(_FakeMessage(content="Paris"))])
        run_dispatch(prompt, build_general_registry(), client=client)
        return client.chat.completions.calls[0]["messages"]

    def test_marker_rides_the_last_user_turn(self):
        sent = self._sent("what is the capital of France")
        assert sent[-1]["role"] == "user"
        assert sent[-1]["content"].endswith(_BRIEF_MARKER)

    def test_marker_is_not_its_own_message(self):
        """A standalone message is what the model deliberated about."""
        sent = self._sent("what is the capital of France")
        assert all(m.get("content") != _BRIEF_MARKER for m in sent)

    def test_the_system_prompt_is_left_alone(self):
        sent = self._sent("what is the capital of France")
        assert _BRIEF_MARKER not in sent[0]["content"]

    def test_the_users_own_words_are_preserved(self):
        sent = self._sent("what is the capital of France")
        assert "what is the capital of France" in sent[-1]["content"]

    def test_no_marker_when_the_prompt_wants_room(self):
        sent = self._sent("write a detailed essay about France")
        assert all(_BRIEF_MARKER not in (m.get("content") or "") for m in sent)

    def test_marker_is_not_persisted_to_history(self):
        """The boundary copy carries it; the authoritative history must not,
        or every later turn re-reads a stale instruction."""
        from dourmouse.dispatch import run_dispatch
        from dourmouse.general_roster import build_general_registry
        from dourmouse.tests.test_dispatch import (
            FakeClient,
            _FakeMessage,
            _FakeResponse,
        )

        client = FakeClient([_FakeResponse(_FakeMessage(content="Paris"))])
        report = run_dispatch(
            "what is the capital of France",
            build_general_registry(),
            client=client,
        )
        for m in report.get("messages", []):
            assert _BRIEF_MARKER not in (m.get("content") or "")


def test_brevity_never_shrinks_the_generation_cap():
    """Prompt only, no token cap. This brain spends tokens on reasoning
    before it emits content, so a tight max_tokens does not shorten an
    answer -- it truncates one, measured as a reply cut mid-clause at
    "using standard HTTP verbs (GET,". Every call must keep the standard
    cap.

    v13.7: the call sites now go through ``_default_max_tokens()`` rather
    than reading ``_DEFAULT_MAX_TOKENS`` directly, so that
    DOURMOUSE_MAX_RESPONSE_TOKENS is actually honoured (a module constant
    read at the call site is fine, but the accessor is what makes the
    override real). The guard is unchanged in intent: ONE shared cap, no
    brief-specific knob anywhere.
    """
    import inspect

    from dourmouse import dispatch as d

    src = inspect.getsource(d)
    assert "brief_cap" not in src
    # Every generation call must use the ONE shared cap accessor...
    assert "max_tokens=_default_max_tokens()" in src
    # ...and no call site may pass a smaller literal instead. Forwarding
    # the caller's own ``max_tokens`` parameter through an adapter layer
    # (_ClaudeCliCompletions.create -> _create) is not a cap, so it is
    # allowed; a bare number here would be exactly the recurring bug.
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped.startswith("max_tokens="):
            continue
        value = stripped[len("max_tokens="):].split(",")[0].strip()
        assert value in ("_default_max_tokens()", "max_tokens"), (
            f"a generation call bypasses the shared cap: {stripped!r}"
        )
    # no brief-specific cap knob survives
    assert "brief_max_tokens" not in src
