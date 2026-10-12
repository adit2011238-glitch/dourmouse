"""FIX-W2 P4-50: LocalFallbackMemoryStore.all_facts() and delete() act on the store that remember() writes to.
Remote reachable: remember() goes to the remote, which cannot be listed or deleted from, so both raise the
typed 'unavailable' error (callers already handle it) instead of quietly answering from a different store.
Remote down: remember() goes to the local store, and so do all_facts() and delete()."""
from __future__ import annotations

import pytest

from dourmouse.memory_store import (
    LocalFallbackMemoryStore,
    MemoryStore,
    RemoteMemoryStore,
    RemoteMemoryStoreUnavailable,
)


class Remote(RemoteMemoryStore):
    """A reachable remote: remember/count work, the unsupported operations raise as the real class does."""

    def __init__(self):
        super().__init__("http://127.0.0.1:1", timeout=0.2)
        self.up = True
        self.remembered = []

    def remember(self, source, title, body):
        if not self.up:
            raise RemoteMemoryStoreUnavailable("down")
        self.remembered.append((source, title))
        return "remembered"

    def count(self, source=None):
        if not self.up:
            raise RemoteMemoryStoreUnavailable("down")
        return len(self.remembered)


@pytest.fixture
def pair(tmp_path):
    local = MemoryStore(tmp_path / "l.db")
    remote = Remote()
    fb = LocalFallbackMemoryStore(remote, lambda: local)
    yield fb, remote, local
    local.close()


def test_with_the_remote_up_all_facts_is_refused_not_answered_from_the_local_store(pair):
    fb, remote, local = pair
    local.remember("s", "old local fact", "x")
    fb.remember("s", "new fact", "y")
    assert remote.remembered == [("s", "new fact")]
    with pytest.raises(RemoteMemoryStoreUnavailable) as exc:
        fb.all_facts()
    assert "remote" in str(exc.value).lower()


def test_with_the_remote_up_delete_is_refused_and_touches_nothing(pair):
    fb, remote, local = pair
    local.remember("s", "t", "x")
    with pytest.raises(RemoteMemoryStoreUnavailable):
        fb.delete("s", "t")
    assert local.get("s", "t") is not None


def test_with_the_remote_down_both_act_on_the_local_store_that_remember_writes_to(pair):
    fb, remote, local = pair
    remote.up = False
    fb.remember("s", "offline fact", "z")
    facts = fb.all_facts()
    assert [f["title"] for f in facts] == ["[PENDING SYNC] offline fact"]
    assert fb.delete("s", "[PENDING SYNC] offline fact") is True
    assert fb.all_facts() == []
    assert fb.last_used_local_fallback is True


def test_the_answer_follows_the_remote_coming_back(pair):
    fb, remote, local = pair
    remote.up = False
    fb.remember("s", "offline fact", "z")
    assert len(fb.all_facts()) == 1
    remote.up = True
    fb._probe_at = 0.0  # the reachability answer is cached for a few seconds; expire it
    with pytest.raises(RemoteMemoryStoreUnavailable):
        fb.all_facts()


def test_the_reachability_probe_is_cached_so_a_prune_loop_does_not_hit_the_remote_each_time(pair):
    fb, remote, local = pair
    calls = []
    real = remote.count
    remote.count = lambda source=None: calls.append(1) or real(source)
    for _ in range(5):
        with pytest.raises(RemoteMemoryStoreUnavailable):
            fb.delete("s", "t")
    assert len(calls) == 1
