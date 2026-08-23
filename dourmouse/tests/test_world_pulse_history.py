"""Hermetic tests for the world-pulse time-scrubber history store.

No network involved at all (this module never makes an HTTP call), but the
house convention still applies: nothing touches the real workspace or real
filesystem outside a pytest ``tmp_path``. Every test points
``DOURMOUSE_WORKSPACE`` at a fresh ``tmp_path`` so the JSONL file lives
somewhere disposable.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dourmouse import world_pulse_history as wph


def _geo(score: int = 50, **counts: int) -> dict:
    return {
        "generated_at": "2026-08-23T00:00:00+00:00",
        "pulse_score": score,
        "layers": {k: [{"lat": 0, "lon": 0}] * v for k, v in counts.items()},
        "counts": counts,
    }


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    """Point the workspace root at a throwaway dir for every test.

    Mirrors the existing convention (``DOURMOUSE_WORKSPACE``) so this module
    resolves its file exactly the way live_feeds._tasks_path() does, but
    never near the real workspace/ directory.
    """
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("DOURMOUSE_WORLD_HISTORY_FILE", raising=False)
    yield tmp_path


def _write_raw_records(path: Path, records: list[dict]) -> None:
    """Bypass record_snapshot's min-interval gate to seed the file directly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r) for r in records)
    if body:
        body += "\n"
    path.write_text(body, encoding="utf-8")


class TestPathResolution:
    def test_history_file_lives_under_workspace(self, tmp_path):
        path = wph._history_file()
        assert path == (tmp_path / "world_history.jsonl").expanduser()

    def test_dedicated_env_var_overrides_workspace(self, tmp_path, monkeypatch):
        override = tmp_path / "elsewhere" / "custom_history.jsonl"
        monkeypatch.setenv("DOURMOUSE_WORLD_HISTORY_FILE", str(override))
        assert wph._history_file() == override.expanduser()


class TestRecordAndRetrieve:
    """The core round trip: record a geo snapshot, get it back later."""

    def test_record_then_range_lists_it(self):
        wph.record_snapshot(_geo(quakes=2, flights=5))
        rows = wph.history_range(hours=24)
        assert len(rows) == 1
        assert "at" in rows[0]
        assert rows[0]["counts"] == {"quakes": 2, "flights": 5}

    def test_record_then_history_at_returns_the_snapshot(self):
        wph.record_snapshot(_geo(quakes=1))
        got = wph.history_at(minutes_ago=0)
        assert got is not None
        assert got["geo"]["counts"] == {"quakes": 1}

    def test_history_range_oldest_first(self):
        """Seed three out-of-order records directly and check the ordering
        contract (oldest first) rather than relying on real-time recording,
        which would make the test slow and timing-flaky.
        """
        now = datetime.now(timezone.utc)
        recs = [
            {"at": (now - timedelta(minutes=5)).isoformat(timespec="seconds"), "geo": _geo(a=1)},
            {"at": (now - timedelta(minutes=30)).isoformat(timespec="seconds"), "geo": _geo(a=2)},
            {"at": (now - timedelta(minutes=15)).isoformat(timespec="seconds"), "geo": _geo(a=3)},
        ]
        _write_raw_records(wph._history_file(), recs)
        rows = wph.history_range(hours=1)
        ats = [r["at"] for r in rows]
        assert ats == sorted(ats)

    def test_record_snapshot_never_raises_on_bad_input(self):
        # Not a dict at all — the function must swallow this quietly.
        wph.record_snapshot(None)  # type: ignore[arg-type]
        wph.record_snapshot([])  # type: ignore[arg-type]
        assert wph.history_range() == []


class TestMinInterval:
    """A poller is expected to call record_snapshot on every tick — the
    min-interval gate is what stops that from writing a line every few
    seconds.
    """

    def test_rapid_repeat_calls_write_only_once(self):
        wph.record_snapshot(_geo(quakes=1))
        wph.record_snapshot(_geo(quakes=2))  # immediate repeat: no-op
        wph.record_snapshot(_geo(quakes=3))  # still within the window
        rows = wph.history_range(hours=24)
        assert len(rows) == 1
        # The first write wins; later no-op calls must not overwrite it.
        assert rows[0]["counts"] == {"quakes": 1}

    def test_call_after_interval_elapses_writes_again(self, monkeypatch):
        # Shrink the gate so the test doesn't need to sleep 90 real seconds.
        monkeypatch.setattr(wph, "_MIN_INTERVAL_SECONDS", 0.05)
        wph.record_snapshot(_geo(quakes=1))
        time.sleep(0.12)
        wph.record_snapshot(_geo(quakes=2))
        rows = wph.history_range(hours=24)
        assert len(rows) == 2


class TestEmptyHistory:
    """Nothing recorded yet must be an honest empty answer, never a crash
    or a fabricated snapshot.
    """

    def test_history_range_with_nothing_recorded(self):
        assert wph.history_range(hours=24) == []

    def test_history_at_with_nothing_recorded_returns_none(self):
        assert wph.history_at(minutes_ago=10) is None

    def test_prune_old_with_nothing_recorded(self):
        assert wph.prune_old(hours=24) == 0


class TestHistoryAtNearestMatch:
    def test_picks_the_closer_of_two_candidates(self):
        now = datetime.now(timezone.utc)
        recs = [
            {"at": (now - timedelta(minutes=10)).isoformat(timespec="seconds"), "geo": _geo(a=10)},
            {"at": (now - timedelta(minutes=60)).isoformat(timespec="seconds"), "geo": _geo(a=60)},
        ]
        _write_raw_records(wph._history_file(), recs)
        got = wph.history_at(minutes_ago=8)
        assert got["geo"]["counts"] == {"a": 10}

    def test_never_fabricates_returns_real_recorded_snapshot_even_if_far(self):
        """The nearest REAL snapshot must come back even when it's a poor
        match for the request — the contract explicitly forbids inventing
        an interpolated point instead.
        """
        now = datetime.now(timezone.utc)
        recs = [{"at": (now - timedelta(hours=5)).isoformat(timespec="seconds"), "geo": _geo(a=99)}]
        _write_raw_records(wph._history_file(), recs)
        got = wph.history_at(minutes_ago=1)
        assert got is not None
        assert got["geo"]["counts"] == {"a": 99}


class TestPruneOld:
    def test_prune_removes_only_entries_past_the_window(self):
        now = datetime.now(timezone.utc)
        recs = [
            {"at": (now - timedelta(hours=1)).isoformat(timespec="seconds"), "geo": _geo(a=1)},
            {"at": (now - timedelta(hours=30)).isoformat(timespec="seconds"), "geo": _geo(a=2)},
            {"at": (now - timedelta(hours=48)).isoformat(timespec="seconds"), "geo": _geo(a=3)},
        ]
        _write_raw_records(wph._history_file(), recs)
        removed = wph.prune_old(hours=24)
        assert removed == 2
        remaining = wph.history_range(hours=999)
        assert len(remaining) == 1
        assert remaining[0]["counts"] == {"a": 1}

    def test_prune_never_raises_on_corrupt_file(self):
        path = wph._history_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all\n{broken\n", encoding="utf-8")
        assert wph.prune_old(hours=24) == 0  # nothing PARSEABLE to remove


class TestHardCap:
    """Nothing calling record_snapshot in a tight loop may grow the file
    without bound — the hard cap must win even with the min-interval gate
    effectively disabled.
    """

    def test_cap_is_enforced_across_many_rapid_writes(self, monkeypatch):
        monkeypatch.setattr(wph, "_MIN_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr(wph, "_MAX_LINES", 5)
        for i in range(20):
            wph.record_snapshot(_geo(a=i))
            time.sleep(0.001)
        rows = wph.history_range(hours=999)
        assert len(rows) <= 5
        # The cap keeps the MOST RECENT entries, not the oldest.
        assert rows[-1]["counts"] == {"a": 19}
