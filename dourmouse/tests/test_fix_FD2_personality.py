"""FD2 fix P5-38: an unparseable or truncated profile reply is never stored."""

from __future__ import annotations

import pytest

import dourmouse.personality_profile as pp
from dourmouse.memory_store import MemoryStore
from dourmouse.tests.test_personality_profile import (
    _GOOD_JSON,
    _cfg,
    _FakeClient,
    _seed_history,
)


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem" / "test.db")
    yield s
    s.close()


def test_prose_reply_is_not_stored_and_does_not_burn_the_once_only_slot(store):
    _seed_history(store)
    client = _FakeClient(content="Alice at Acme works on the Foo project a lot.")
    result = pp.generate_profile(store, client=client, config=_cfg())
    assert result == {"ok": False, "reason": "unparseable_output"}
    assert pp.has_profile(store) is False
    # the slot is still free: a later good run succeeds without force
    again = pp.generate_profile(store, client=_FakeClient(content=_GOOD_JSON), config=_cfg())
    assert again["ok"] is True


def test_truncated_reply_is_not_stored_even_if_it_looks_like_json(store):
    _seed_history(store)
    client = _FakeClient(content=_GOOD_JSON)
    client.chat.completions._response.choices[0].finish_reason = "length"
    result = pp.generate_profile(store, client=client, config=_cfg())
    assert result == {"ok": False, "reason": "unparseable_output"}
    assert pp.has_profile(store) is False


def test_empty_reply_is_not_stored(store):
    _seed_history(store)
    result = pp.generate_profile(store, client=_FakeClient(content=""), config=_cfg())
    assert result == {"ok": False, "reason": "unparseable_output"}
    assert pp.has_profile(store) is False


def test_json_missing_a_required_key_is_not_stored(store):
    _seed_history(store)
    result = pp.generate_profile(
        store, client=_FakeClient(content='{"working_style": "x"}'), config=_cfg()
    )
    assert result == {"ok": False, "reason": "unparseable_output"}
    assert pp.has_profile(store) is False
