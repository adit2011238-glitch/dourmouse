"""Tests for atlas-strategy-lab/scripts/custom_backtest.py (v8.16) — the
desktop engine_api.py's /api/backtest/custom logic.

Lives in dourmouse's own test suite rather than inside the atlas-strategy-
lab submodule: that submodule has no pytest convention of its own (no
tests/ dir, no local venv) and a second real GitHub contributor — adding
project structure there is a bigger, separate decision than testing one
new file. custom_backtest.py has zero dourmouse dependency (stdlib-only,
by design — see its own docstring), so it's imported here by path.

This tests the SAME logic that will run on the desktop, just against
whatever data_dir is passed in — no real market data needed for any test
here, since none of these strategies call load() except the ones
specifically checking the "not configured" honest-failure path.

custom_backtest.py is read from the submodule's WORKING TREE (not pinned
to any particular commit) — whatever branch/commit atlas-strategy-lab
happens to be checked out to. That's a real, known fragility (another
contributor's checkout state can make this file briefly absent — it
already happened once live during this feature's own build: switching
that submodule back to `main` removed the file, since it only exists on a
feature branch there). Skipped, not a hard collection error, if it's not
importable — same convention as this codebase's own
pytest.importorskip("pandas") in test_forex_ops.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "atlas-strategy-lab" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

cb = pytest.importorskip(
    "custom_backtest",
    reason="atlas-strategy-lab submodule not checked out to a commit/branch "
           "containing custom_backtest.py right now",
)


_GOOD_CODE_NO_DATA = """
def run(load, params):
    return {"mean_return": 0.0, "std_dev": 0.0, "n_obs": 0, "note": "test"}
"""

_GOOD_CODE_WITH_DATA = """
def run(load, params):
    df = load("fx:EURUSD:d1")
    return {"mean_return": 0.001, "std_dev": 0.01, "n_obs": len(df)}
"""


class TestStaticSafetyCheck:
    """Deliberately mirrors dourmouse/tests/test_atlas_proposals.py's own
    TestStaticSafetyCheck — same rules, ported implementation, both must
    hold. If one file's check changes, this test class and that one should
    both be revisited (see custom_backtest.py's module docstring)."""

    def test_clean_code_passes(self):
        assert cb.static_safety_check(_GOOD_CODE_NO_DATA) == ""

    def test_syntax_error_refused(self):
        assert "does not parse" in cb.static_safety_check("def run(:\n")

    def test_missing_run_function_refused(self):
        note = cb.static_safety_check("def other(load, params):\n    return {}\n")
        assert "no top-level" in note

    @pytest.mark.parametrize("bad_import", ["import os", "import subprocess", "import socket"])
    def test_disallowed_import_refused(self, bad_import):
        code = f"{bad_import}\ndef run(load, params):\n    return {{}}\n"
        assert "not in the allowed list" in cb.static_safety_check(code)

    @pytest.mark.parametrize(
        "hostile",
        [
            "def run(load, params):\n    return {}.__class__.__base__.__subclasses__()\n",
            "def run(load, params):\n    eval('1')\n    return {}\n",
            "def run(load, params):\n    open('/etc/passwd')\n    return {}\n",
        ],
    )
    def test_escape_vectors_refused(self, hostile):
        assert cb.static_safety_check(hostile) != ""


class TestParseHarnessOutput:
    def test_parses_result(self):
        metrics, err = cb.parse_harness_output('===RESULT===\n{"n_obs": 5}\n')
        assert err == ""
        assert metrics == {"n_obs": 5}

    def test_parses_error(self):
        metrics, err = cb.parse_harness_output("===ERROR===\nboom\n")
        assert metrics == {}
        assert "boom" in err

    def test_neither_marker_is_honest_failure(self):
        _, err = cb.parse_harness_output("nonsense")
        assert "no recognizable output" in err


class TestRunCustomBacktest:
    def test_refuses_unsafe_code_before_executing(self, tmp_path):
        with pytest.raises(RuntimeError, match="refused before execution"):
            cb.run_custom_backtest("import os\ndef run(load, params):\n    return {}\n", {}, tmp_path)

    def test_real_subprocess_execution_no_data_needed(self, tmp_path):
        """Real end-to-end: writes the strategy + harness to disk, actually
        shells out via subprocess.run, parses real stdout. No mocking."""
        result = cb.run_custom_backtest(_GOOD_CODE_NO_DATA, {}, tmp_path)
        assert result["metrics"]["n_obs"] == 0
        assert "runtime_s" in result

    def test_load_call_without_real_data_registry_is_honest_not_configured(self, tmp_path):
        """tmp_path has no data_registry.py in it — this proves the harness
        reports that honestly instead of crashing unrecognizably or
        fabricating a result."""
        with pytest.raises(RuntimeError, match="not importable|not configured|NOT CONFIGURED"):
            cb.run_custom_backtest(_GOOD_CODE_WITH_DATA, {}, tmp_path)

    def test_strategy_returning_non_dict_is_honest_failure(self, tmp_path):
        code = "def run(load, params):\n    return 42\n"
        with pytest.raises(RuntimeError, match="expected dict"):
            cb.run_custom_backtest(code, {}, tmp_path)

    def test_strategy_exception_is_reported_not_swallowed(self, tmp_path):
        code = "def run(load, params):\n    raise ValueError('deliberate test failure')\n"
        with pytest.raises(RuntimeError, match="deliberate test failure"):
            cb.run_custom_backtest(code, {}, tmp_path)

    def test_timeout_is_enforced(self, tmp_path):
        # "import time" is not in ALLOWED_IMPORTS (would be refused before
        # ever running) — a plain busy-loop tests the timeout path without
        # needing a disallowed import.
        code = "def run(load, params):\n    x = 0\n    while True:\n        x += 1\n"
        with pytest.raises(RuntimeError, match="timed out"):
            cb.run_custom_backtest(code, {}, tmp_path, timeout=1)

    def test_params_reach_the_strategy(self, tmp_path):
        code = "def run(load, params):\n    return {'mean_return': 0.0, 'std_dev': 0.0, 'n_obs': params.get('n', -1)}\n"
        result = cb.run_custom_backtest(code, {"n": 7}, tmp_path)
        assert result["metrics"]["n_obs"] == 7
