"""scripts/perf_check.py (phase I2, finding #171): the parts that can be tested without
launching an app. The launch itself is exercised live against an isolated copy and the
numbers are recorded in ~/Documents/DOURMOUSE/PERF_BUDGET.md.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "perf_check.py"
spec = importlib.util.spec_from_file_location("perf_check", SCRIPT)
assert spec and spec.loader
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

PS = """\
  100     1  90000 /Users/x/Applications/Dourmouse.app/Contents/MacOS/Electron
  101   100  40000 /Users/x/Applications/Dourmouse.app/Contents/Frameworks/Electron Helper.app/Contents/MacOS/Electron Helper --type=gpu-process
  102   100  120000 /Users/x/Applications/Dourmouse.app/Contents/Frameworks/Electron Helper (Renderer).app/Contents/MacOS/Electron Helper (Renderer) --type=renderer
  103   100  512000 /opt/homebrew/Cellar/python@3.14/Python.app/Contents/MacOS/Python -m dourmouse.webui
  104   103  10240 /usr/bin/ffmpeg -i x
  200     1  70000 /Users/x/Applications/Dourmouse.app/Contents/MacOS/Electron
  201   200  99999 /opt/homebrew/Cellar/python@3.14/Python.app/Contents/MacOS/Python -m dourmouse.webui
  garbage line
  abc   1  2 not numbers
"""


class TestPorts:
    @pytest.mark.parametrize("port", [8765, 9333, 9334])
    def test_the_owners_ports_are_refused_in_any_position(self, port):
        with pytest.raises(SystemExit, match="owner"):
            pc.refuse_owner_ports(18890, port, 19391)
        with pytest.raises(SystemExit, match="owner"):
            pc.refuse_owner_ports(port, 18890, 19391)

    def test_out_of_range_and_repeated_ports_are_refused(self):
        with pytest.raises(SystemExit, match="outside"):
            pc.refuse_owner_ports(80, 18890, 19391)
        with pytest.raises(SystemExit, match="twice"):
            pc.refuse_owner_ports(18890, 18890, 19391)

    def test_good_ports_pass(self):
        pc.refuse_owner_ports(18890, 19390, 19391)

    def test_nothing_is_launched_when_a_port_is_the_owners(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("a process was started")

        monkeypatch.setattr(pc.subprocess, "run", boom)
        monkeypatch.setattr(pc.subprocess, "Popen", boom)
        for argv in (["--ui-port", "8765"], ["--cdp-port", "9333"], ["--pane-port", "9334"]):
            with pytest.raises(SystemExit, match="owner"):
                pc.main(argv)

    def test_a_port_that_already_has_a_listener_is_refused_before_launch(self, monkeypatch, tmp_path):
        app = tmp_path / "Fake.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        monkeypatch.setattr(pc, "port_in_use", lambda port: port == 19390)
        monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("launched")))
        with pytest.raises(SystemExit, match="19390"):
            pc.measure(app, 18890, 19390, 19391, 0)


class TestProcessMath:
    def test_ps_rows_skip_lines_that_are_not_processes(self):
        rows = pc.parse_ps(PS)
        assert [r["pid"] for r in rows] == [100, 101, 102, 103, 104, 200, 201]

    def test_a_tree_is_found_through_parent_ids_only(self):
        rows = pc.parse_ps(PS)
        assert sorted(r["pid"] for r in pc.descendants(rows, 100)) == [100, 101, 102, 103, 104]
        assert sorted(r["pid"] for r in pc.descendants(rows, 200)) == [200, 201]

    def test_memory_is_split_into_server_and_app_and_ignores_the_other_copy(self):
        mem = pc.split_memory(pc.parse_ps(PS), 100)
        assert mem["server_pids"] == [103, 104], "the server and its own child"
        assert mem["server_rss_mb"] == round((512000 + 10240) / 1024, 1)
        assert mem["app_rss_mb"] == round((90000 + 40000 + 120000) / 1024, 1)
        assert mem["app_process_count"] == 3

    def test_a_cycle_in_the_table_cannot_loop_forever(self):
        rows = [{"pid": 1, "ppid": 2, "rss_kb": 1, "command": "a"}, {"pid": 2, "ppid": 1, "rss_kb": 1, "command": "b"}]
        assert sorted(r["pid"] for r in pc.descendants(rows, 1)) == [1, 2]

    def test_lsof_output_is_read_as_process_ids(self):
        assert pc.parse_lsof_pids("52753\n52754\n") == [52753, 52754]
        assert pc.parse_lsof_pids("") == []


class TestStopping:
    def test_only_processes_from_the_snapshot_are_ever_signalled(self, monkeypatch):
        rows = pc.parse_ps(PS)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(pc, "_ps_rows", lambda: rows)
        monkeypatch.setattr(pc.os, "kill", lambda pid, sig: killed.append((pid, sig)))
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        forced = pc._stop_tree(100, grace=0.0)
        pids = {pid for pid, _ in killed}
        assert pids <= {100, 101, 102, 103, 104}, "the other copy (200, 201) is never touched"
        assert {(100, 15)} <= set(killed)
        assert sorted(forced) == [100, 101, 102, 103, 104]

    def test_nothing_is_signalled_without_a_main_process(self, monkeypatch):
        monkeypatch.setattr(pc.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("signalled")))
        assert pc._stop_tree(None) == []

    def test_a_process_that_quit_by_itself_is_not_forced(self, monkeypatch):
        state = {"calls": 0}

        def rows():
            state["calls"] += 1
            return pc.parse_ps(PS) if state["calls"] == 1 else []

        killed: list[int] = []
        monkeypatch.setattr(pc, "_ps_rows", rows)
        monkeypatch.setattr(pc.os, "kill", lambda pid, sig: killed.append(pid))
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        assert pc._stop_tree(100, grace=5.0) == []
        assert killed == [100], "only the polite quit request was sent"


class TestBudget:
    def _result(self, **over):
        base = {
            "server_ready_s": 3.0,
            "first_screen_ready_s": 5.0,
            "screen_switch_ms": {"HOME": {"cold": 100, "warm": 50}, "BROWSER": {"cold": 700, "warm": 120}},
            "memory_mb": {"server_rss_mb": 300, "app_rss_mb": 700},
        }
        base.update(over)
        return base

    def test_numbers_inside_the_budget_flag_nothing(self):
        assert pc.over_budget(self._result()) == []

    def test_each_kind_of_number_is_checked(self):
        slow = self._result(
            server_ready_s=99,
            first_screen_ready_s=99,
            screen_switch_ms={"HOME": {"cold": 9000, "warm": 9000}},
            memory_mb={"server_rss_mb": 99999, "app_rss_mb": 99999},
        )
        assert pc.over_budget(slow) == sorted(pc.BUDGET)

    def test_a_missing_number_is_not_blamed_and_a_timeout_is_not_a_fast_screen(self):
        assert pc.over_budget({}) == []
        timed_out = self._result(screen_switch_ms={"HOME": {"cold": -1, "warm": -1}})
        assert pc.over_budget(timed_out) == []

    def test_check_flag_sets_the_exit_code(self, monkeypatch, capsys):
        monkeypatch.setattr(pc, "measure", lambda *a, **k: self._result(server_ready_s=99))
        assert pc.main(["--idle", "0"]) == 0
        assert pc.main(["--idle", "0", "--check"]) == 1
        out = capsys.readouterr().out
        assert "server_ready_s" in out

    def test_json_output_carries_the_budget_and_the_flags(self, monkeypatch, capsys):
        monkeypatch.setattr(pc, "measure", lambda *a, **k: self._result(server_ready_s=99))
        pc.main(["--idle", "0", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["over_budget"] == ["server_ready_s"] and data["budget"] == pc.BUDGET

    def test_the_default_app_is_the_installed_one(self):
        assert Path.home().joinpath("Applications", "Dourmouse.app") == pc.DEFAULT_APP
