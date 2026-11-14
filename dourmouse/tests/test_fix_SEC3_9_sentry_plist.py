"""SEC3-9 (high): one malformed file in a LaunchAgents folder made every
sentry scan fail, silently and permanently.

get_persistence_items caught only OSError, InvalidFileException and
ValueError, but a plist whose root is an array raised AttributeError,
truncated XML raised ExpatError and a numeric ProgramArguments raised
TypeError. collect_state then raised, run_scan never ran, and
SentryRuntime._loop swallowed the error with ``pass``: no alert, no counter.
"""

from __future__ import annotations

import plistlib
import time

import pytest

from dourmouse.security import mac_telemetry as mt
from dourmouse.security import sentry
from dourmouse.security.mac_detectors import detect_mac_findings
from dourmouse.security.sentry import SentryRuntime, SentryStore, run_scan

_BAD_PAYLOADS = {
    "array_root.plist": plistlib.dumps(["not", "a", "dict"]),
    "truncated.plist": b'<?xml version="1.0"?><plist><dict><key>a</key>',
    "numeric_args.plist": plistlib.dumps({"Label": "x", "ProgramArguments": 5}),
    "args_not_strings.plist": plistlib.dumps({"Label": "x", "ProgramArguments": [1, 2]}),
    "label_not_string.plist": plistlib.dumps({"Label": ["x"], "Program": "/bin/true"}),
    "program_not_string.plist": plistlib.dumps({"Label": "x", "Program": 7}),
}


@pytest.mark.parametrize("name", sorted(_BAD_PAYLOADS))
def test_a_bad_plist_is_an_error_item_not_an_exception(tmp_path, monkeypatch, name):
    (tmp_path / name).write_bytes(_BAD_PAYLOADS[name])
    monkeypatch.setattr(mt, "persistence_dirs", lambda: [tmp_path])
    out = mt.get_persistence_items()
    assert out["available"] is True
    [item] = out["items"]
    assert item["path"].endswith(name)
    assert item["error"].startswith("unreadable:")


def test_the_attackers_own_plist_next_to_a_bad_one_is_still_read(tmp_path, monkeypatch):
    for name, data in _BAD_PAYLOADS.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "zz.evil.plist").write_bytes(plistlib.dumps(
        {"Label": "zz.evil", "ProgramArguments": ["/tmp/evil", "--quiet"], "RunAtLoad": True}))
    monkeypatch.setattr(mt, "persistence_dirs", lambda: [tmp_path])
    items = mt.get_persistence_items()["items"]
    good = [i for i in items if "error" not in i]
    assert [i["label"] for i in good] == ["zz.evil"]
    assert good[0]["program"] == "/tmp/evil" and good[0]["run_at_load"] is True
    assert len([i for i in items if "error" in i]) == len(_BAD_PAYLOADS)


def test_an_oversized_file_is_not_read_into_memory(tmp_path, monkeypatch):
    big = tmp_path / "huge.plist"
    big.write_bytes(b"x" * (mt.MAX_LAUNCHD_PLIST_BYTES + 1))
    monkeypatch.setattr(mt, "persistence_dirs", lambda: [tmp_path])
    [item] = mt.get_persistence_items()["items"]
    assert "too large" in item["error"]


def test_parse_rejects_shapes_launchd_would_refuse():
    with pytest.raises(ValueError, match="not a dictionary"):
        mt.parse_launchd_plist(plistlib.dumps([1]))
    with pytest.raises(ValueError, match="ProgramArguments"):
        mt.parse_launchd_plist(plistlib.dumps({"ProgramArguments": "/bin/sh -c x"}))
    # Unchanged for a normal job, including KeepAlive as a dictionary of conditions.
    assert mt.parse_launchd_plist(plistlib.dumps(
        {"Label": "a", "ProgramArguments": [], "KeepAlive": {"SuccessfulExit": False}})) == {
        "label": "a", "program": None, "run_at_load": False, "keep_alive": True}


def test_an_unreadable_startup_file_is_a_finding(tmp_path, monkeypatch):
    (tmp_path / "zz-marker.plist").write_bytes(_BAD_PAYLOADS["truncated.plist"])
    monkeypatch.setattr(mt, "persistence_dirs", lambda: [tmp_path])
    state = {"persistence": mt.get_persistence_items()}
    found = [f for f in detect_mac_findings(state, []) if f.kind == "persistence_unreadable"]
    assert len(found) == 1
    assert found[0].severity == "med"
    assert "zz-marker.plist" in found[0].title


def test_a_scan_with_a_bad_plist_completes(tmp_path, monkeypatch):
    (tmp_path / "zz-marker.plist").write_bytes(_BAD_PAYLOADS["array_root.plist"])
    monkeypatch.setattr(mt, "persistence_dirs", lambda: [tmp_path])

    def state_fn():
        return {"firewall": {"available": True, "enabled": True},
                "listening_ports": {"available": True, "listening_ports": []},
                "arp_neighbors": {"available": True, "neighbors": []},
                "persistence": mt.get_persistence_items()}

    result = run_scan(state_fn=state_fn, store=SentryStore(tmp_path / "s.db"), write_alerts=False)
    assert any(f.kind == "persistence_unreadable" for f in result.all_findings)


class TestFailedScanAccounting:
    def _broken_runtime(self, tmp_path):
        def state_fn():
            raise AttributeError("'list' object has no attribute 'get'")

        return SentryRuntime(store=SentryStore(tmp_path / "s.db"), state_fn=state_fn)

    def test_three_failed_scans_raise_one_high_alert(self, tmp_path, monkeypatch):
        alerts = []
        monkeypatch.setattr(sentry, "_write_alert", lambda f: alerts.append(f) or True)
        rt = self._broken_runtime(tmp_path)
        for expected in (1, 2, 3, 4, 5):
            with pytest.raises(AttributeError):
                rt.run_one_tick_now()
            assert rt.consecutive_failed_scans == expected
        assert "AttributeError" in rt.last_scan_error
        assert len(alerts) == 1  # once per run of failures, not once per tick
        assert alerts[0].severity == "high"
        assert alerts[0].kind == "sentry_scan_failing"
        assert "cannot scan" in alerts[0].title
        assert "AttributeError" in alerts[0].detail

    def test_a_good_scan_resets_the_count_and_rearms_the_alert(self, tmp_path, monkeypatch):
        alerts = []
        monkeypatch.setattr(sentry, "_write_alert", lambda f: alerts.append(f) or True)
        broken = {"on": True}

        def state_fn():
            if broken["on"]:
                raise TypeError("'int' object is not subscriptable")
            return {"firewall": {"available": True, "enabled": True},
                    "listening_ports": {"available": True, "listening_ports": []},
                    "arp_neighbors": {"available": True, "neighbors": []}}

        rt = SentryRuntime(store=SentryStore(tmp_path / "s.db"), state_fn=state_fn)
        for _ in range(3):
            with pytest.raises(TypeError):
                rt.run_one_tick_now()
        broken["on"] = False
        rt.run_one_tick_now()
        assert rt.consecutive_failed_scans == 0 and rt.last_scan_error is None
        broken["on"] = True
        for _ in range(3):
            with pytest.raises(TypeError):
                rt.run_one_tick_now()
        assert len([a for a in alerts if a.kind == "sentry_scan_failing"]) == 2

    def test_the_loop_counts_failures_instead_of_hiding_them(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sentry, "_write_alert", lambda f: True)
        rt = self._broken_runtime(tmp_path)
        rt._interval = 0.02  # bypass the 60 s floor for this test only
        rt.start()
        try:
            deadline = time.time() + 5.0
            while rt.consecutive_failed_scans < 3 and time.time() < deadline:
                time.sleep(0.02)
            assert rt.consecutive_failed_scans >= 3
        finally:
            rt.stop()
