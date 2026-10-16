"""FI2 P4-67 / P5-64: the generated ATLAS lab harness carries no absolute path and exits non-zero
on an error. (Removal of the run directory after a run is covered by test_fix_FI1_atlas_proposals.py,
test_work_dir_removed_after_successful_run.) Nothing here needs the sandbox or the network."""

from __future__ import annotations

import shutil
import subprocess
import sys

from dourmouse.atlas import atlas_proposals as ap

GOOD = "def run(load, params):\n    return {'n_obs': 1, 'echo': params}\n"
BAD = "def run(load, params):\n    raise ValueError('strategy blew up')\n"


def test_harness_embeds_no_absolute_path(tmp_path):
    work = tmp_path / "some" / "deep" / "run_x"
    harness = ap._write_harness(work, GOOD, {"a": 1})
    text = harness.read_text(encoding="utf-8")
    assert str(work) not in text
    assert str(tmp_path) not in text
    assert "/Users/" not in text


def test_harness_still_runs_after_its_folder_is_moved(tmp_path):
    work = tmp_path / "run_a"
    harness = ap._write_harness(work, GOOD, {"a": 1})
    moved = tmp_path / "elsewhere" / "run_b"
    moved.parent.mkdir()
    shutil.move(str(work), str(moved))
    proc = subprocess.run([sys.executable, str(moved / "harness.py")], capture_output=True, text=True,
                          timeout=30, cwd=str(tmp_path), check=False)
    metrics, err = ap._parse_harness_output(proc.stdout)
    assert err == "", err
    assert metrics["echo"] == {"a": 1}
    assert proc.returncode == 0
    assert harness != moved / "harness.py"


def test_harness_exits_non_zero_when_the_strategy_raises(tmp_path):
    harness = ap._write_harness(tmp_path / "w", BAD, {})
    proc = subprocess.run([sys.executable, str(harness)], capture_output=True, text=True,
                          timeout=30, cwd=str(tmp_path), check=False)
    assert proc.returncode != 0
    assert "===ERROR===" in proc.stdout
    _, err = ap._parse_harness_output(proc.stdout)
    assert "strategy blew up" in err


def test_error_is_still_recognised_through_the_sandbox_output_shape(tmp_path):
    harness = ap._write_harness(tmp_path / "w", BAD, {})
    proc = subprocess.run([sys.executable, str(harness)], capture_output=True, text=True,
                          timeout=30, cwd=str(tmp_path), check=False)
    sandbox_shaped = f"EXIT CODE: {proc.returncode}\nSTDOUT:\n{proc.stdout.strip()}"
    _, err = ap._parse_harness_output(sandbox_shaped)
    assert err.startswith("strategy code raised") and "strategy blew up" in err
