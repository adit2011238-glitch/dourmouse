"""FI1 fixes: atlas_cli.atlas_version caching (P4-1), atlas_command formatter
(P4-2), atlas_scheduler Memorial Day (P5-10)."""

from __future__ import annotations

import datetime as dt
import threading
import time

import pytest

from dourmouse.atlas import atlas_cli


@pytest.fixture(autouse=True)
def _fresh_version_cache(monkeypatch):
    monkeypatch.setattr(atlas_cli, "_version_cache", {"at": 0.0, "value": None})
    monkeypatch.setattr(atlas_cli, "_version_lock", threading.Lock())


# --------------------------------------------------------------------------- #
# P4-1
# --------------------------------------------------------------------------- #

def test_failed_probe_is_cached_with_a_short_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(atlas_cli, "run_atlas_cli",
                        lambda argv, timeout=0: calls.append(argv) or (1, "", "bad import"))
    assert atlas_cli.atlas_version() is None
    assert atlas_cli.atlas_version() is None
    assert atlas_cli.atlas_version() is None
    assert len(calls) == 1


def test_raising_probe_is_cached_too(monkeypatch):
    calls = []

    def boom(argv, timeout=0):
        calls.append(argv)
        raise OSError("no venv")

    monkeypatch.setattr(atlas_cli, "run_atlas_cli", boom)
    assert atlas_cli.atlas_version() is None
    assert atlas_cli.atlas_version() is None
    assert len(calls) == 1


def test_failed_probe_is_retried_after_the_negative_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(atlas_cli, "run_atlas_cli",
                        lambda argv, timeout=0: calls.append(argv) or (1, "", ""))
    atlas_cli.atlas_version()
    atlas_cli._version_cache["at"] -= atlas_cli._VERSION_FAILURE_TTL_SECONDS + 1
    atlas_cli.atlas_version()
    assert len(calls) == 2


def test_success_is_still_cached_and_returned(monkeypatch):
    calls = []
    monkeypatch.setattr(atlas_cli, "run_atlas_cli",
                        lambda argv, timeout=0: calls.append(argv) or (0, "1.2.3\n", ""))
    assert atlas_cli.atlas_version() == "1.2.3"
    assert atlas_cli.atlas_version() == "1.2.3"
    assert len(calls) == 1


def test_non_blocking_probe_returns_at_once_and_runs_one_probe(monkeypatch):
    release = threading.Event()
    started = []

    def slow(argv, timeout=0):
        started.append(1)
        release.wait(10)
        return 0, "9.9\n", ""

    monkeypatch.setattr(atlas_cli, "run_atlas_cli", slow)
    t0 = time.time()
    assert atlas_cli.atlas_version(wait=False) is None
    assert atlas_cli.atlas_version(wait=False) is None
    assert time.time() - t0 < 2
    time.sleep(0.2)
    assert len(started) == 1
    release.set()
    for _ in range(100):
        if atlas_cli._version_cache["value"] == "9.9":
            break
        time.sleep(0.05)
    assert atlas_cli.atlas_version(wait=False) == "9.9"


def test_panel_snapshot_does_not_wait_for_the_probe(tmp_path, monkeypatch):
    from dourmouse.tests.test_atlas_cli import _fake_atlas

    _fake_atlas(tmp_path, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(atlas_cli, "run_atlas_cli",
                        lambda argv, timeout=0: (release.wait(10), (0, "1\n", ""))[1])
    t0 = time.time()
    payload = atlas_cli.atlas_panel_snapshot()
    assert time.time() - t0 < 5
    assert payload["version"] is None
    release.set()


# --------------------------------------------------------------------------- #
# P4-2: atlas_command standard formatter
# --------------------------------------------------------------------------- #

def _standard_tool(monkeypatch, standard: dict) -> str:
    from dourmouse.atlas import atlas_command as ac

    monkeypatch.setattr(ac, "atlas_standard", lambda: {"configured": True, "error": "", **standard})
    return ac._atlas_standard_tool({})


def _full():
    import copy

    from dourmouse.tests.test_atlas_command import STANDARD
    return copy.deepcopy(STANDARD)


def test_full_standard_still_formats(monkeypatch):
    out = _standard_tool(monkeypatch, _full())
    assert "permutation p: 0.0060 (bar < 0.01: PASS)" in out
    assert "terminal $437.94" in out


def test_exact_zero_p_value_passes(monkeypatch):
    std = _full()
    std["numbers"]["permutation_p"] = 0.0
    out = _standard_tool(monkeypatch, std)
    assert "PASS" in out and "FAIL" not in out


def test_missing_permutation_p_does_not_crash_or_grade(monkeypatch):
    std = _full()
    del std["numbers"]["permutation_p"]
    out = _standard_tool(monkeypatch, std)
    assert "permutation p: n/a" in out
    assert "PASS" not in out and "FAIL" not in out


def test_null_permutation_p_does_not_crash(monkeypatch):
    std = _full()
    std["numbers"]["permutation_p"] = None
    assert "permutation p: n/a" in _standard_tool(monkeypatch, std)


def test_core_without_terminal_does_not_crash(monkeypatch):
    std = _full()
    del std["numbers"]["portfolio_core"]["terminal"]
    out = _standard_tool(monkeypatch, std)
    assert "terminal $n/a" in out


def test_missing_sections_do_not_crash(monkeypatch):
    out = _standard_tool(monkeypatch, {"generated_utc": "x"})
    assert out.startswith("ATLAS STANDARD")
    assert "n/a" in out


def test_non_numeric_leg_t_is_skipped(monkeypatch):
    std = _full()
    std["numbers"]["legs"]["BAD"] = {"t": "n/a"}
    out = _standard_tool(monkeypatch, std)
    assert "HE_8 +5.07" in out
    assert "BAD" not in out


# --------------------------------------------------------------------------- #
# P5-10: Memorial Day is the LAST Monday of May
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("year,expected", [
    (2024, dt.date(2024, 5, 27)),
    (2025, dt.date(2025, 5, 26)),
    (2026, dt.date(2026, 5, 25)),
    (2029, dt.date(2029, 5, 28)),
])
def test_memorial_day_is_last_monday_of_may(year, expected):
    from dourmouse.atlas import atlas_scheduler as sch

    holidays = sch._holidays(year)
    assert expected in holidays
    june_mondays = [d for d in holidays if d.month == 6 and d.weekday() == 0]
    assert june_mondays == []


def test_first_trading_day_of_june_2025_is_not_skipped():
    from dourmouse.atlas import atlas_scheduler as sch

    assert sch.first_trading_day(2025, 6) == dt.date(2025, 6, 2)
    assert sch.last_trading_day(2025, 5) == dt.date(2025, 5, 30)
