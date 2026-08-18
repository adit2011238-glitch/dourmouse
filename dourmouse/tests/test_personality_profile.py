"""Tests for dourmouse/personality_profile.py.

Explicit user spec, tested directly: run ONCE, ever, never on a
schedule -- a second call without ``force=True`` must refuse, even if
the store is otherwise untouched. The LLM client is faked throughout
(no test may make a real API call); the one thing worth checking without
a mock is that _gather_source_material actually reads what
history_import.py wrote, since a schema drift between the two modules
would silently starve the profile of source material.
"""

from __future__ import annotations

import json

import pytest

from dourmouse import personality_profile as pp
from dourmouse.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem" / "test.db")
    yield s
    s.close()


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(_FakeMessage(content))]


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeClient:
    def __init__(self, content=None, exc=None):
        response = exc if exc is not None else _FakeResponse(content)
        self.chat = _FakeChat(response)


_GOOD_JSON = json.dumps({
    "working_style": "Iterates quickly, prefers hands-on debugging over planning docs.",
    "preferences": "Terse, direct answers; dislikes padding.",
    "communication_preferences": "Short status updates, technical detail on request.",
    "general_interests": "Systems programming, trading infrastructure.",
})


def _seed_history(store, n=3):
    for i in range(n):
        store.remember(
            "claude_history",
            f"Session {i} [abcd1234]",
            f"Project: some-project\nAsked: how do I fix bug {i}\nLast reply: patched it.",
        )


class TestHasProfile:
    def test_false_before_generation(self, store):
        assert pp.has_profile(store) is False

    def test_true_after_generation(self, store):
        store.remember(pp.PROFILE_SOURCE, pp.PROFILE_TITLE, "some profile text")
        assert pp.has_profile(store) is True


class TestGatherSourceMaterial:
    def test_reads_claude_and_codex_history_sources(self, store):
        store.remember("claude_history", "A [11111111]", "claude body")
        store.remember("codex_history", "B [22222222]", "codex body")
        store.remember("agent", "unrelated", "must not appear")
        material = pp._gather_source_material(store)
        assert "claude body" in material
        assert "codex body" in material
        assert "must not appear" not in material

    def test_empty_when_no_history_imported(self, store):
        store.remember("agent", "not history", "irrelevant")
        assert pp._gather_source_material(store) == ""

    def test_respects_a_char_budget(self, store):
        for i in range(50):
            store.remember("claude_history", f"S{i} [{i:08d}]", "x" * 500)
        material = pp._gather_source_material(store, max_chars=2000)
        assert len(material) <= 2500  # a little slack for headers, but bounded


class TestExtractJson:
    def test_plain_json(self):
        assert pp._extract_json('{"a": 1}') == {"a": 1}

    def test_json_in_code_fence(self):
        assert pp._extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_surrounding_prose(self):
        assert pp._extract_json('Sure, here it is:\n{"a": 1}\nHope that helps!') == {"a": 1}

    def test_garbage_returns_none(self):
        assert pp._extract_json("not json at all") is None


class TestGenerateProfile:
    def test_runs_once_and_refuses_a_second_time(self, store):
        _seed_history(store)
        client = _FakeClient(content=_GOOD_JSON)
        r1 = pp.generate_profile(store, client=client, config=_cfg())
        assert r1["ok"] is True
        assert client.chat.completions.calls  # the LLM was actually called once

        client2 = _FakeClient(content=_GOOD_JSON)
        r2 = pp.generate_profile(store, client=client2, config=_cfg())
        assert r2 == {"ok": False, "reason": "already_generated"}
        assert client2.chat.completions.calls == []  # never even attempted

    def test_force_allows_a_deliberate_rerun(self, store):
        _seed_history(store)
        pp.generate_profile(store, client=_FakeClient(content=_GOOD_JSON), config=_cfg())
        r2 = pp.generate_profile(
            store, client=_FakeClient(content=_GOOD_JSON), config=_cfg(), force=True
        )
        assert r2["ok"] is True

    def test_no_source_material_refuses_honestly(self, store):
        """No history imported yet -- must not call the LLM on nothing,
        and must not fabricate a profile out of thin air (Rule 2.2)."""
        client = _FakeClient(content=_GOOD_JSON)
        result = pp.generate_profile(store, client=client, config=_cfg())
        assert result == {"ok": False, "reason": "no_source_material"}
        assert client.chat.completions.calls == []

    def test_stores_a_readable_fact_with_all_four_categories(self, store):
        _seed_history(store)
        pp.generate_profile(store, client=_FakeClient(content=_GOOD_JSON), config=_cfg())
        fact = store.get(pp.PROFILE_SOURCE, pp.PROFILE_TITLE)
        assert fact is not None
        body = fact["body"]
        assert "Working style:" in body
        assert "Preferences:" in body
        assert "Communication preferences:" in body
        assert "General interests:" in body
        assert "Iterates quickly" in body

    def test_llm_failure_is_honest_not_a_crash(self, store):
        _seed_history(store)
        client = _FakeClient(exc=RuntimeError("NVIDIA unreachable"))
        result = pp.generate_profile(store, client=client, config=_cfg())
        assert result["ok"] is False
        assert "llm_call_failed" in result["reason"]
        assert pp.has_profile(store) is False  # nothing stored on failure

    def test_max_tokens_is_generous_not_tight(self, store):
        """Regression: traced live at max_tokens=900, this backend
        (nvidia/nemotron) visibly reasons in `content` before answering
        and got cut off mid-reasoning -- "Now produce JSON... Let's
        craft: Working style: \"Working style: The user tends to..." --
        which the honest-degrade fallback then stored as if it were the
        real answer. This is a single one-time call, not a hot path, so
        there is no cost reason to cap it tight."""
        _seed_history(store)
        client = _FakeClient(content=_GOOD_JSON)
        pp.generate_profile(store, client=client, config=_cfg())
        kwargs = client.chat.completions.calls[0]
        assert kwargs["max_tokens"] >= 3000

    def test_unparseable_response_still_stores_something_not_nothing(self, store):
        """A model that ignores the JSON instruction shouldn't burn the
        one-shot budget for nothing -- store the raw prose rather than
        silently discard a real (if malformed) answer."""
        _seed_history(store)
        client = _FakeClient(content="I cannot determine a working style from this.")
        result = pp.generate_profile(store, client=client, config=_cfg())
        assert result["ok"] is True
        fact = store.get(pp.PROFILE_SOURCE, pp.PROFILE_TITLE)
        assert "cannot determine" in fact["body"]

    def test_system_prompt_forbids_biographical_data(self):
        """Structural guard, not just a vibe check: the instruction must
        be unambiguous, since there is no mechanical filter behind it."""
        text = pp._SYSTEM_PROMPT.lower()
        for term in ("name", "age", "location", "employer"):
            assert term in text
        assert "no biographical" in text or "identifying" in text

    def test_schema_has_no_field_that_could_hold_a_name_or_address(self):
        """The output shape itself is a second line of defense: even a
        model that ignores the prose instruction has nowhere structured
        to put a name or address."""
        fields = set(pp._PROFILE_SCHEMA["properties"])
        assert fields == {
            "working_style", "preferences", "communication_preferences", "general_interests",
        }


def _cfg():
    from dourmouse.config import NvidiaConfig

    return NvidiaConfig(api_key="k", base_url="https://example.invalid", model="test-model")
