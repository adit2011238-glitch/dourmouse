"""Phase I2, server side of crash recovery: the run marker (finding #171).

``RunMarker`` tells the next start whether the last server run ended cleanly. These
tests use real files, a real advisory lock and real child processes that are really
killed (SIGKILL cannot be caught, so a handler could never write the marker; the
lock plus the leftover ``running`` record is what the next start reads).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from dourmouse import webui
from dourmouse.state_store import StateStore

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the marker needs fcntl")

ROOT = Path(__file__).resolve().parents[2]

CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from dourmouse import webui
    marker = webui.RunMarker(Path(sys.argv[1]))
    previous = marker.arm()
    if sys.argv[2] == "handlers":
        webui._install_clean_stop_handlers(marker)
    print("ARMED", previous is None, flush=True)
    time.sleep(60)
    """
)


def _spawn(path: Path, mode: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(path), mode],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().startswith("ARMED")
    return proc


class TestMarkerFile:
    def test_a_first_run_has_no_earlier_crash_and_writes_running(self, tmp_path):
        marker = webui.RunMarker(tmp_path / "m.json")
        assert marker.arm() is None
        record = json.loads((tmp_path / "m.json").read_text())
        assert record["state"] == "running" and record["pid"] == os.getpid()
        marker.clean()

    def test_a_clean_stop_empties_the_file_so_the_next_start_reports_nothing(self, tmp_path):
        first = webui.RunMarker(tmp_path / "m.json")
        first.arm()
        first.clean()
        assert (tmp_path / "m.json").read_text() == ""
        second = webui.RunMarker(tmp_path / "m.json")
        assert second.arm() is None
        second.clean()

    def test_a_run_that_never_cleaned_up_is_reported_exactly_once(self, tmp_path):
        first = webui.RunMarker(tmp_path / "m.json")
        first.arm()
        first._release()  # the process is gone and nothing was written: the kernel drops the lock
        second = webui.RunMarker(tmp_path / "m.json")
        earlier = second.arm()
        assert earlier is not None and earlier["state"] == "running"
        second.clean()
        third = webui.RunMarker(tmp_path / "m.json")
        assert third.arm() is None, "the notice is once only"
        third.clean()

    def test_a_crash_carries_its_reason_to_the_next_start(self, tmp_path):
        first = webui.RunMarker(tmp_path / "m.json")
        first.arm()
        first.crashed("RuntimeError: the disk is full")
        second = webui.RunMarker(tmp_path / "m.json")
        earlier = second.arm()
        assert earlier is not None and earlier["state"] == "crashed"
        assert earlier["reason"] == "RuntimeError: the disk is full"
        second.clean()

    def test_a_second_live_server_does_not_overwrite_the_first_ones_marker(self, tmp_path):
        first = webui.RunMarker(tmp_path / "m.json")
        first.arm()
        before = (tmp_path / "m.json").read_text()
        second = webui.RunMarker(tmp_path / "m.json")
        assert second.arm() is None
        second.clean()  # nothing armed: must not touch the first run's file
        assert (tmp_path / "m.json").read_text() == before
        first.clean()

    def test_an_unreadable_marker_counts_as_an_unclean_run_not_a_silent_pass(self, tmp_path):
        (tmp_path / "m.json").write_text("{not json")
        marker = webui.RunMarker(tmp_path / "m.json")
        earlier = marker.arm()
        assert earlier is not None
        marker.clean()

    def test_an_unwritable_folder_never_stops_the_server(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        marker = webui.RunMarker(blocker / "m.json")
        assert marker.arm() is None  # the parent is a file: no marker, no exception
        marker.clean()
        marker.crashed("x")


class TestRealProcesses:
    def test_a_killed_server_leaves_a_report_for_the_next_start(self, tmp_path):
        path = tmp_path / "m.json"
        child = _spawn(path, "handlers")
        try:
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
        earlier = webui.RunMarker(path)
        record = earlier.arm()
        assert record is not None and record["state"] == "running" and record["pid"] == child.pid
        earlier.clean()

    def test_a_terminated_server_is_a_deliberate_stop_and_reports_nothing(self, tmp_path):
        path = tmp_path / "m.json"
        child = _spawn(path, "handlers")
        try:
            child.send_signal(signal.SIGTERM)
            assert child.wait(timeout=10) == -signal.SIGTERM, "it still dies by the signal, as before"
        finally:
            if child.poll() is None:
                child.kill()
        assert path.read_text() == ""
        later = webui.RunMarker(path)
        assert later.arm() is None
        later.clean()

    def test_the_lock_is_dropped_by_the_kernel_not_by_the_child(self, tmp_path):
        path = tmp_path / "m.json"
        child = _spawn(path, "none")
        try:
            blocked = webui.RunMarker(path)
            assert blocked.arm() is None, "a live server holds the lock"
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
        deadline = time.time() + 5
        record = None
        while time.time() < deadline and record is None:
            probe = webui.RunMarker(path)
            record = probe.arm()
            probe.clean()
        assert record is not None and record["state"] == "running"


class TestTheNotice:
    def _server(self, tmp_path):
        class _Hub:
            def __init__(self):
                self.events = []

            def broadcast(self, evt):
                self.events.append(evt)

        class _Server:
            pass

        server = _Server()
        server.state = StateStore(tmp_path / "state.db")
        server.events_broadcast = _Hub()
        return server

    def test_it_becomes_one_system_alert_with_the_reason_and_the_log(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_LOG", "/tmp/x/server.log")
        server = self._server(tmp_path)
        ok = webui._report_previous_crash(server, {"state": "crashed", "started": "2026-10-05T09:00:00", "reason": "MemoryError"})
        assert ok is True
        alerts = server.state.alerts("*")
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["kind"] == "system" and alert["severity"] == "med"
        assert "restarted" in alert["title"]
        assert "2026-10-05 09:00:00" in alert["detail"] and "MemoryError" in alert["detail"]
        assert "/tmp/x/server.log" in alert["detail"]
        assert "\u2014" not in alert["title"] + alert["detail"]
        assert server.events_broadcast.events == [{"type": "state_change", "section": "alerts", "owner": "*"}]

    def test_a_server_without_a_state_store_is_not_an_error(self):
        class _Bare:
            pass

        assert webui._report_previous_crash(_Bare(), {"state": "running"}) is False

    def test_a_failing_store_never_stops_the_server(self, tmp_path):
        class _Broken:
            def add_alert(self, **_kw):
                raise RuntimeError("db locked")

        class _Server:
            state = _Broken()

        assert webui._report_previous_crash(_Server(), {"state": "running"}) is False
