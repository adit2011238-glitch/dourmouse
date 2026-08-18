"""A cache that serves a wrong answer is worse than no cache.

These tests weight correctness over hit rate: what must never be cached, what
must never collide, and what must expire.
"""

from __future__ import annotations

import time

import pytest

from dourmouse import cache


@pytest.fixture(autouse=True)
def _cache_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_CACHE_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("DOURMOUSE_CACHE_ENABLED", "1")
    monkeypatch.delenv("DOURMOUSE_CACHE_TTL", raising=False)
    return tmp_path


MSGS = [{"role": "user", "content": "What is the capital of Japan?"}]


# --------------------------------------------------------------------------- #
# is_cacheable — the safety gate
# --------------------------------------------------------------------------- #

def test_tool_carrying_calls_are_never_cached():
    """A replayed tool decision could repeat a side effect like gmail_send."""
    assert not cache.is_cacheable(
        messages=MSGS, tools=[{"type": "function", "function": {"name": "delete_path"}}]
    )


def test_streaming_calls_are_never_cached():
    assert not cache.is_cacheable(messages=MSGS, tools=None, stream=True)


@pytest.mark.parametrize("temp", [0.7, 1.0, 0.01])
def test_nonzero_temperature_is_never_cached(temp):
    """A caller asking for variety must not silently get a stored answer."""
    assert not cache.is_cacheable(messages=MSGS, tools=None, temperature=temp)


def test_deterministic_toolless_call_is_cacheable():
    assert cache.is_cacheable(messages=MSGS, tools=None, temperature=0)
    assert cache.is_cacheable(messages=MSGS, tools=[], temperature=None)


def test_empty_messages_are_not_cacheable():
    assert not cache.is_cacheable(messages=[], tools=None)


def test_disable_switch_turns_everything_off(monkeypatch):
    monkeypatch.delenv("DOURMOUSE_CACHE_ENABLED", raising=False)
    assert not cache.is_cacheable(messages=MSGS, tools=None)
    cache.put("k", "v", model="m")
    assert cache.get("k") is None


# --------------------------------------------------------------------------- #
# make_key — a missing field here is a wrong-answer bug
# --------------------------------------------------------------------------- #

def test_same_request_gives_the_same_key():
    a = cache.make_key(model="m", messages=MSGS)
    b = cache.make_key(model="m", messages=list(MSGS))
    assert a == b


def test_key_is_insensitive_to_dict_ordering():
    """Canonical JSON: field order must not create a spurious miss."""
    a = cache.make_key(model="m", messages=[{"role": "user", "content": "x"}])
    b = cache.make_key(model="m", messages=[{"content": "x", "role": "user"}])
    assert a == b


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model": "other-model"},
        {"messages": [{"role": "user", "content": "different"}]},
        {"temperature": 0.5},
        {"max_tokens": 99},
    ],
    ids=["model", "messages", "temperature", "max_tokens"],
)
def test_any_answer_changing_field_changes_the_key(kwargs):
    base = dict(model="m", messages=MSGS, temperature=0, max_tokens=512)
    assert cache.make_key(**base) != cache.make_key(**{**base, **kwargs})


def test_conversation_history_is_part_of_the_key():
    """Same final question, different history, must not collide."""
    one = [{"role": "user", "content": "Who is she?"}]
    two = [
        {"role": "user", "content": "Tell me about Ada Lovelace."},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "Who is she?"},
    ]
    assert cache.make_key(model="m", messages=one) != cache.make_key(model="m", messages=two)


def test_message_order_matters():
    a = [{"role": "user", "content": "A"}, {"role": "user", "content": "B"}]
    b = [{"role": "user", "content": "B"}, {"role": "user", "content": "A"}]
    assert cache.make_key(model="m", messages=a) != cache.make_key(model="m", messages=b)


# --------------------------------------------------------------------------- #
# get / put
# --------------------------------------------------------------------------- #

def test_roundtrip_hit():
    k = cache.make_key(model="m", messages=MSGS)
    cache.put(k, "Tokyo", model="m")
    assert cache.get(k) == "Tokyo"


def test_miss_returns_none():
    assert cache.get("never-stored") is None


def test_entry_expires_after_ttl():
    """Live answers go stale; a stale right-looking answer is the danger."""
    cache.put("k", "old news", model="m", ttl=1)
    assert cache.get("k") == "old news"
    time.sleep(1.1)
    assert cache.get("k") is None


def test_zero_ttl_stores_nothing():
    cache.put("k", "v", model="m", ttl=0)
    assert cache.get("k") is None


def test_empty_content_is_not_stored():
    cache.put("k", "", model="m")
    assert cache.get("k") is None


def test_survives_a_process_restart(tmp_path, monkeypatch):
    """SQLite not a dict, so a restart keeps the warm cache."""
    cache.put("k", "persisted", model="m")
    import importlib

    importlib.reload(cache)
    monkeypatch.setenv("DOURMOUSE_CACHE_DB", str(tmp_path / "c.db"))
    assert cache.get("k") == "persisted"


# --------------------------------------------------------------------------- #
# maintenance
# --------------------------------------------------------------------------- #

def test_hits_are_counted():
    cache.put("k", "v", model="m")
    cache.get("k")
    cache.get("k")
    assert cache.stats()["total_hits"] == 2


def test_invalidate_by_model_leaves_others():
    cache.put("a", "1", model="keep")
    cache.put("b", "2", model="drop")

    assert cache.invalidate(model="drop") == 1
    assert cache.get("a") == "1"
    assert cache.get("b") is None


def test_clear_removes_everything():
    cache.put("a", "1", model="m")
    cache.put("b", "2", model="m")
    assert cache.clear() == 2
    assert cache.stats()["entries"] == 0


def test_purge_expired_reclaims_space():
    cache.put("live", "v", model="m", ttl=600)
    cache.put("dead", "v", model="m", ttl=1)
    time.sleep(1.1)

    assert cache.purge_expired() == 1
    assert cache.get("live") == "v"


def test_cache_is_off_unless_explicitly_enabled(monkeypatch):
    """Caching fixes repeat answers for the whole TTL — an operator decision."""
    monkeypatch.delenv("DOURMOUSE_CACHE_ENABLED", raising=False)
    assert not cache.is_cacheable(messages=MSGS, tools=None)


def test_stats_reports_configuration():
    s = cache.stats()
    assert s["enabled"] is True
    assert s["ttl_seconds"] == 3600
    assert "db" in s


def test_ttl_is_configurable(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_CACHE_TTL", "60")
    assert cache.ttl_seconds() == 60


def test_bad_ttl_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_CACHE_TTL", "not-a-number")
    assert cache.ttl_seconds() == 3600


def test_unwritable_db_degrades_to_no_cache(monkeypatch, tmp_path):
    """A broken cache must never fail a request."""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("DOURMOUSE_CACHE_DB", str(blocker / "sub" / "c.db"))

    cache.put("k", "v", model="m")   # must not raise
    assert cache.get("k") is None    # must not raise
