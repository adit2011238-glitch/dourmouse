"""Tests for the local-first Supabase sync layer (dourmouse/supabase_sync.py).

Every real network call is replaced with an injected fake ``transport`` (see
``Transport`` in the module under test); every real store is a small fake
implementing the ``_Store`` protocol. No test touches the network or a real
Supabase project. RLS/RPC correctness against the LIVE project was verified
separately with rolled-back transactions (recorded in this commit's message):
a signed-in user sees exactly their own facts row through match_documents,
and current_user()-style isolation held for two seeded users.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from dourmouse.supabase_sync import (
    SupabaseSync,
    SyncOutbox,
    SyncOutcome,
    body_hash,
    to_utc_iso,
)


class _FakeStore:
    """Minimal in-memory stand-in for MemoryStore / RemoteMemoryStore."""

    def __init__(self, facts: list[dict] | None = None, *, raise_on_all_facts: bool = False):
        self._facts: dict[tuple[str, str], dict] = {}
        for f in facts or []:
            self._facts[(f["source"], f["title"])] = dict(f)
        self._raise_on_all_facts = raise_on_all_facts
        self.remembered: list[dict] = []

    def all_facts(self):
        if self._raise_on_all_facts:
            raise RuntimeError("remote store unreachable")
        return list(self._facts.values())

    def get(self, source, title):
        return self._facts.get((source, title))

    def remember(self, source, title, body):
        row = {"source": source, "title": title, "body": body, "updated_at": to_utc_iso(None)}
        self._facts[(source, title)] = row
        self.remembered.append(row)
        return "ok"


@pytest.fixture
def tmp_outbox_path(tmp_path):
    return tmp_path / "outbox.db"


def _transport(responses):
    """responses: list of (status, body_obj_or_bytes) consumed in call order."""
    calls = []
    it = iter(responses)

    def run(method, url, headers, body):
        calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        status, payload = next(it)
        raw = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
        return status, raw

    run.calls = calls
    return run


def _offline_transport():
    def run(method, url, headers, body):
        raise OSError("Network is unreachable")

    return run


class TestTimezoneNormalisation:
    """The specific bug the module's own docstring documents: a naive local
    timestamp cast to timestamptz under Postgres' UTC session TimeZone lands
    wrong by the local UTC offset."""

    def test_naive_local_string_is_treated_as_utc_not_reinterpreted(self):
        # MemoryStore.remember() stamps with datetime.now().isoformat(seconds)
        # -- no offset at all. to_utc_iso must not silently assume the wrong
        # zone; it must produce a real, offset-carrying UTC string.
        out = to_utc_iso("2026-09-04T12:00:00")
        assert out.endswith("+00:00") or out.endswith("Z")

    def test_an_already_offset_string_is_converted_correctly(self):
        out = to_utc_iso("2026-09-04T12:00:00+04:00")
        assert "08:00:00" in out  # 12:00 at +04:00 is 08:00 UTC

    def test_output_is_directly_comparable_as_a_string(self):
        """The `>` conflict rule compares these as plain strings, so the
        format must be consistently zero-padded and zoned."""
        a = to_utc_iso("2026-09-04T08:00:00+00:00")
        b = to_utc_iso("2026-09-04T09:00:00+00:00")
        assert a < b


class TestBodyHash:
    def test_identical_content_hashes_identically(self):
        assert body_hash("same text") == body_hash("same text")

    def test_different_content_hashes_differently(self):
        assert body_hash("a") != body_hash("b")


class TestSyncOutbox:
    def test_enqueue_then_pending_round_trips(self, tmp_outbox_path):
        ob = SyncOutbox(tmp_outbox_path)
        ob.enqueue("s", "t", "body", "2026-09-04T08:00:00+00:00")
        pending = ob.pending()
        assert len(pending) == 1
        assert pending[0]["body"] == "body"
        ob.close()

    def test_a_newer_enqueue_replaces_an_older_queued_one(self, tmp_outbox_path):
        """Editing a fact five times offline queues ONE row with the final
        body, not a replay of the whole edit history."""
        ob = SyncOutbox(tmp_outbox_path)
        ob.enqueue("s", "t", "v1", "2026-09-04T08:00:00+00:00")
        ob.enqueue("s", "t", "v2", "2026-09-04T09:00:00+00:00")
        ob.enqueue("s", "t", "v3", "2026-09-04T10:00:00+00:00")
        pending = ob.pending()
        assert len(pending) == 1
        assert pending[0]["body"] == "v3"
        ob.close()

    def test_an_older_enqueue_does_not_overwrite_a_newer_queued_one(self, tmp_outbox_path):
        """The same strictly-newer-wins rule sync_facts applies server-side,
        applied here too so the local queue cannot itself reorder a fact."""
        ob = SyncOutbox(tmp_outbox_path)
        ob.enqueue("s", "t", "new", "2026-09-04T10:00:00+00:00")
        ob.enqueue("s", "t", "stale-replay", "2026-09-04T08:00:00+00:00")
        pending = ob.pending()
        assert pending[0]["body"] == "new"
        ob.close()

    def test_clear_removes_only_the_named_keys(self, tmp_outbox_path):
        ob = SyncOutbox(tmp_outbox_path)
        ob.enqueue("s", "keep", "b", "2026-09-04T08:00:00+00:00")
        ob.enqueue("s", "drop", "b", "2026-09-04T08:00:00+00:00")
        ob.clear([("s", "drop")])
        remaining = {r["title"] for r in ob.pending()}
        assert remaining == {"keep"}
        ob.close()

    def test_record_failure_keeps_the_row_queued_with_a_reason(self, tmp_outbox_path):
        ob = SyncOutbox(tmp_outbox_path)
        ob.enqueue("s", "t", "b", "2026-09-04T08:00:00+00:00")
        ob.record_failure([("s", "t")], "HTTP 503: backend down")
        assert ob.depth() == 1  # never silently dropped
        ob.close()

    def test_mark_synced_and_state_round_trip(self, tmp_outbox_path):
        ob = SyncOutbox(tmp_outbox_path)
        ob.mark_synced("s", "t", "2026-09-04T08:00:00+00:00", "the body")
        state = ob.state("s", "t")
        assert state is not None
        assert state["body_sha"] == body_hash("the body")
        ob.close()

    def test_reopening_the_same_path_preserves_the_queue(self, tmp_outbox_path):
        """A dropped outbox file must be a harmless resync-from-scratch, and
        conversely a kept one must genuinely persist across process restarts."""
        ob1 = SyncOutbox(tmp_outbox_path)
        ob1.enqueue("s", "t", "b", "2026-09-04T08:00:00+00:00")
        ob1.close()
        ob2 = SyncOutbox(tmp_outbox_path)
        assert ob2.depth() == 1
        ob2.close()


class TestConfiguredGate:
    def test_unconfigured_with_nothing_set(self, tmp_outbox_path):
        sync = SupabaseSync(None, outbox_path=tmp_outbox_path)
        assert sync.configured is False
        sync.close()

    def test_configured_only_once_url_key_token_and_store_all_present(self, tmp_outbox_path):
        store = _FakeStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path,
        )
        assert sync.configured is True
        sync.close()

    def test_missing_access_token_alone_is_unconfigured(self, tmp_outbox_path):
        """sync_facts itself rejects an anon-key-only call server-side --
        this must not even try."""
        store = _FakeStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="",
            outbox_path=tmp_outbox_path,
        )
        assert sync.configured is False
        sync.close()

    def test_unconfigured_methods_report_it_rather_than_erroring(self, tmp_outbox_path):
        sync = SupabaseSync(None, outbox_path=tmp_outbox_path)
        for outcome in (sync.push(), sync.pull(), sync.sync()):
            assert outcome.configured is False
            assert outcome.ok is True
        sync.close()


class TestQueue:
    def test_queue_never_touches_the_network(self, tmp_outbox_path):
        store = _FakeStore()
        run = _transport([])  # would raise StopIteration if ever called
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path, transport=run,
        )
        outcome = sync.queue("s", "t", "body")
        assert outcome.queued == 1
        assert run.calls == []
        sync.close()

    def test_queue_all_local_skips_facts_already_confirmed_unchanged(self, tmp_outbox_path):
        """Comparing HASHES, not timestamps -- the echo-loop guard: a fact
        just pulled and re-stamped by MemoryStore.remember() must not
        immediately re-queue itself as a local edit."""
        store = _FakeStore([{"source": "s", "title": "t", "body": "same", "updated_at": "x"}])
        sync = SupabaseSync(store, outbox_path=tmp_outbox_path)
        sync.outbox.mark_synced("s", "t", "2026-09-04T08:00:00+00:00", "same")
        outcome = sync.queue_all_local()
        assert outcome.detail["newly_queued"] == 0
        sync.close()

    def test_queue_all_local_queues_a_genuinely_changed_fact(self, tmp_outbox_path):
        store = _FakeStore([{"source": "s", "title": "t", "body": "changed", "updated_at": "x"}])
        sync = SupabaseSync(store, outbox_path=tmp_outbox_path)
        sync.outbox.mark_synced("s", "t", "2026-09-04T08:00:00+00:00", "original")
        outcome = sync.queue_all_local()
        assert outcome.detail["newly_queued"] == 1
        sync.close()

    def test_queue_all_local_degrades_honestly_when_the_store_raises(self, tmp_outbox_path):
        """RemoteMemoryStore.all_facts() genuinely raises (it is one of the
        _unsupported() operations) -- this must report, never propagate."""
        store = _FakeStore(raise_on_all_facts=True)
        sync = SupabaseSync(store, outbox_path=tmp_outbox_path)
        outcome = sync.queue_all_local()
        assert outcome.ok is False
        assert "cannot enumerate" in outcome.error
        sync.close()


class TestPush:
    def test_push_calls_sync_facts_and_clears_the_batch_on_success(self, tmp_outbox_path):
        store = _FakeStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            device_id="dev-1", outbox_path=tmp_outbox_path,
            transport=_transport([(200, [{"synced": 1, "skipped": 0}])]),
        )
        sync.queue("s", "t", "body")
        outcome = sync.push()
        assert outcome.pushed == 1
        assert sync.outbox.depth() == 0  # batch cleared
        sync.close()

    def test_push_hits_the_real_rpc_path_with_the_device_id(self, tmp_outbox_path):
        store = _FakeStore()
        run = _transport([(200, [{"synced": 1, "skipped": 0}])])
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            device_id="dev-1", outbox_path=tmp_outbox_path, transport=run,
        )
        sync.queue("s", "t", "body")
        sync.push()
        assert run.calls[0]["url"].endswith("/rest/v1/rpc/sync_facts")
        sent = json.loads(run.calls[0]["body"])
        assert sent["p_device_id"] == "dev-1"
        assert sent["p_facts"][0]["source"] == "s"
        sync.close()

    def test_skipped_rows_are_also_cleared_not_retried_forever(self, tmp_outbox_path):
        """skipped means 'the cloud is already at least this current OR the
        row was malformed' -- neither is fixed by retrying, so it must not
        stay queued."""
        store = _FakeStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path,
            transport=_transport([(200, [{"synced": 0, "skipped": 1}])]),
        )
        sync.queue("s", "t", "body")
        outcome = sync.push()
        assert outcome.skipped == 1
        assert sync.outbox.depth() == 0
        sync.close()

    def test_an_empty_outbox_never_calls_the_network(self, tmp_outbox_path):
        store = _FakeStore()
        run = _transport([])
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path, transport=run,
        )
        outcome = sync.push()
        assert outcome.queued == 0
        assert run.calls == []
        sync.close()

    def test_offline_leaves_the_batch_queued_and_records_the_reason(self, tmp_outbox_path):
        """OFFLINE IS A NORMAL STATE, NOT AN ERROR -- ok stays true-shaped and
        nothing is lost."""
        store = _FakeStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path, transport=_offline_transport(),
        )
        sync.queue("s", "t", "body")
        outcome = sync.push()
        assert outcome.offline is True
        assert sync.outbox.depth() == 1  # nothing lost
        state = sync.outbox.pending()
        assert state  # still there for the next attempt
        sync.close()

    def test_a_5xx_is_treated_as_retriable_offline_row_stays_queued(self, tmp_outbox_path):
        """The server is up but broken. Deliberately treated like a network
        outage -- keep the work queued and retry, rather than discarding a
        real local change over a transient 502/503."""
        store = _FakeStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path,
            transport=_transport([(500, {"error": "internal"})]),
        )
        sync.queue("s", "t", "body")
        outcome = sync.push()
        assert outcome.offline is True
        assert sync.outbox.depth() == 1
        sync.close()

    def test_a_4xx_is_a_real_failure_not_offline(self, tmp_outbox_path):
        """A 400/401/403 means the request itself is wrong (bad payload, bad
        token) -- retrying it unchanged would never succeed, so unlike a 5xx
        this is reported as a genuine failure."""
        store = _FakeStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path,
            transport=_transport([(401, {"error": "invalid token"})]),
        )
        sync.queue("s", "t", "body")
        outcome = sync.push()
        assert outcome.ok is False
        assert outcome.offline is False
        assert sync.outbox.depth() == 1
        sync.close()


class TestPull:
    def test_a_newer_remote_row_is_applied_locally(self, tmp_outbox_path):
        store = _FakeStore([{"source": "s", "title": "t", "body": "old", "updated_at": "2026-09-04T08:00:00+00:00"}])
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path,
            transport=_transport([(200, [
                {"source": "s", "title": "t", "body": "new", "updated_at": "2026-09-04T09:00:00+00:00"},
            ])]),
        )
        outcome = sync.pull()
        assert outcome.pulled == 1
        assert store.remembered[-1]["body"] == "new"
        sync.close()

    def test_a_remote_row_that_is_not_strictly_newer_is_skipped(self, tmp_outbox_path):
        """The exact rule sync_facts applies server-side: `>`, not `>=`, so
        two devices whose clocks agree to the second do not flap forever."""
        store = _FakeStore([{"source": "s", "title": "t", "body": "local", "updated_at": "2026-09-04T09:00:00+00:00"}])
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path,
            transport=_transport([(200, [
                {"source": "s", "title": "t", "body": "remote", "updated_at": "2026-09-04T09:00:00+00:00"},
            ])]),
        )
        outcome = sync.pull()
        assert outcome.pulled == 0
        assert outcome.skipped == 1
        assert store.remembered == []
        sync.close()

    def test_identical_content_is_not_rewritten_even_with_a_newer_stamp(self, tmp_outbox_path):
        """Avoids churning the local FTS index (remember() re-indexes and
        drops the cached embedding on every call) for a no-op change."""
        store = _FakeStore([{"source": "s", "title": "t", "body": "same", "updated_at": "2026-09-04T08:00:00+00:00"}])
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path,
            transport=_transport([(200, [
                {"source": "s", "title": "t", "body": "same", "updated_at": "2026-09-04T09:00:00+00:00"},
            ])]),
        )
        outcome = sync.pull()
        assert outcome.pulled == 0
        assert store.remembered == []
        sync.close()

    def test_offline_pull_is_reported_as_offline_not_a_failure(self, tmp_outbox_path):
        store = _FakeStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path, transport=_offline_transport(),
        )
        outcome = sync.pull()
        assert outcome.offline is True
        sync.close()

    def test_a_store_that_raises_on_get_is_skipped_not_propagated(self, tmp_outbox_path):
        """RemoteMemoryStore.get() genuinely raises when unreachable mid-pull
        -- this row must be skipped, not take the whole pull down."""

        class RaisingStore(_FakeStore):
            def get(self, source, title):
                raise RuntimeError("remote store unreachable")

        store = RaisingStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path,
            transport=_transport([(200, [
                {"source": "s", "title": "t", "body": "x", "updated_at": "2026-09-04T09:00:00+00:00"},
            ])]),
        )
        outcome = sync.pull()  # must not raise
        assert outcome.skipped == 1
        sync.close()


class TestSync:
    def test_push_runs_before_pull(self, tmp_outbox_path):
        """Push first so local work is never overwritten by a pull before it
        has had its chance at the conflict rule."""
        store = _FakeStore()
        run = _transport([
            (200, [{"synced": 1, "skipped": 0}]),  # push
            (200, []),                              # pull
        ])
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path, transport=run,
        )
        sync.queue("s", "t", "body")
        sync.sync()
        assert run.calls[0]["url"].endswith("sync_facts")
        assert "/facts" in run.calls[1]["url"]
        sync.close()

    def test_sync_on_an_unconfigured_instance_is_a_clean_no_op(self, tmp_outbox_path):
        sync = SupabaseSync(None, outbox_path=tmp_outbox_path)
        outcome = sync.sync()
        assert outcome.configured is False
        assert outcome.ok is True
        sync.close()


class TestNeverRaisesIntoACallTurn:
    """The specific, twice-already-hit bug this module's docstring names:
    RemoteMemoryStore genuinely raises where MemoryStore never does, and one
    such exception already escaped into a live request and dropped a
    connection outright. Every public entry point must be exception-safe."""

    @pytest.mark.parametrize("method_name", ["push", "pull", "sync", "queue_all_local"])
    def test_a_store_that_raises_on_every_call_never_escapes(self, tmp_outbox_path, method_name):
        class ExplodingStore:
            def all_facts(self):
                raise RuntimeError("boom")

            def get(self, source, title):
                raise RuntimeError("boom")

            def remember(self, source, title, body):
                raise RuntimeError("boom")

        sync = SupabaseSync(
            ExplodingStore(), url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path,
            transport=_transport([(200, [{"synced": 0, "skipped": 0}]), (200, [])]),
        )
        result = getattr(sync, method_name)()  # must not raise
        assert isinstance(result, SyncOutcome)
        sync.close()

    def test_a_transport_that_raises_arbitrary_exceptions_never_escapes(self, tmp_outbox_path):
        """Any transport-level exception (DNS, TLS, socket) is treated the
        same as a network outage -- reported as offline, work stays queued,
        and above all the exception never reaches the caller."""
        def flaky_transport(method, url, headers, body):
            raise ValueError("something truly unexpected")

        store = _FakeStore()
        sync = SupabaseSync(
            store, url="https://x.supabase.co", anon_key="k", access_token="t",
            outbox_path=tmp_outbox_path, transport=flaky_transport,
        )
        sync.queue("s", "t", "body")
        result = sync.push()  # must not raise
        assert result.offline is True
        assert sync.outbox.depth() == 1
        sync.close()


class TestSummary:
    def test_unconfigured_summary_is_explicit(self):
        assert "NOT CONFIGURED" in SyncOutcome(configured=False).summary()

    def test_offline_summary_says_nothing_was_lost(self):
        text = SyncOutcome(offline=True, queued=3).summary()
        assert "3" in text
        assert "Nothing was lost" in text

    def test_failure_summary_carries_the_real_error(self):
        text = SyncOutcome(ok=False, error="HTTP 500: internal").summary()
        assert "HTTP 500" in text
