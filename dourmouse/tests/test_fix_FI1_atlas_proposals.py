"""FI1 fixes for dourmouse/atlas/atlas_proposals.py and atlas_ui_ops.py
(P3-2, P3-3, P3-4, P3-5, P3-6, P3-69, P4-3, P4-66).

No LLM, no network. The sandbox call is stubbed where a real run is not the
point; the harness itself is executed with a plain subprocess in one test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dourmouse.atlas import atlas_proposals as ap

_NO_DATA = "def run(load, params):\n    return {'mean_return': 0.0, 'std_dev': 0.0, 'n_obs': 0}\n"


def _spec(code):
    return json.dumps({"strategy_name": "S", "explanation": "e", "params": {}, "code": code})


# --------------------------------------------------------------------------- #
# P3-2: params travel as JSON data, not as Python source
# --------------------------------------------------------------------------- #

def test_harness_accepts_json_true_false_null_nan(tmp_path):
    code = "def run(load, params):\n    return {'echo': params, 'n_obs': 1}\n"
    params = {"use_filter": True, "off": False, "nothing": None, "x": float("nan"), "n": 3}
    harness = ap._write_harness(tmp_path / "w", code, params)
    out = subprocess.run([sys.executable, str(harness)], capture_output=True, text=True,
                         timeout=30, cwd=str(tmp_path), check=False).stdout
    metrics, err = ap._parse_harness_output(out)
    assert err == "", err
    assert metrics["echo"]["use_filter"] is True
    assert metrics["echo"]["off"] is False
    assert metrics["echo"]["nothing"] is None
    assert metrics["echo"]["n"] == 3


# --------------------------------------------------------------------------- #
# P3-3: bad metrics / disk errors never leave a run "running"
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("metrics", [
    {"sharpe": "n/a", "mean_return": 0.1, "std_dev": 0.2, "n_obs": 10},
    {"sharpe": [1, 2], "mean_return": 0.1, "n_obs": 10},
    {"sharpe": 1.0, "n_obs": "ten"},
    {"mean_return": {"x": 1}, "n_obs": 5},
    {"sharpe": float("nan"), "mean_return": 0.1, "n_obs": 5},
])
def test_verdict_tolerates_non_numeric_metrics(metrics):
    assert isinstance(ap._verdict_from_metrics(metrics), str)


def test_verdict_string_sharpe_falls_back_to_mean_return():
    assert ap._verdict_from_metrics(
        {"sharpe": "n/a", "mean_return": 0.1, "n_obs": 10}) == "CANDIDATE"


def test_non_dict_harness_result_is_a_failure():
    metrics, err = ap._parse_harness_output("===RESULT===\n[1, 2]\n")
    assert metrics == {} and err


def _approve(monkeypatch, code=_NO_DATA):
    monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _spec(code))
    monkeypatch.setattr(ap, "_explain_run", lambda pid, run: "x")
    return ap.propose_from_idea("idea")


def _wait_run(run_id, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        r = ap.get_run(run_id)
        if r and r["status"] != "running":
            return r
        time.sleep(0.05)
    return ap.get_run(run_id)


def test_async_run_with_disk_error_ends_failed_not_running(monkeypatch):
    p = _approve(monkeypatch)
    monkeypatch.setattr(ap, "sandbox_available", lambda: True)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ap, "_write_harness", boom)
    placeholder = ap.approve_and_run_async(p["id"], target="local")
    done = _wait_run(placeholder["id"])
    assert done["status"] == "failed"
    assert "disk full" in done["error"]
    assert done["finished_at"]


def test_async_run_with_string_metrics_completes(monkeypatch):
    p = _approve(monkeypatch)
    monkeypatch.setattr(ap, "sandbox_available", lambda: True)
    out = '===RESULT===\n{"sharpe": "n/a", "mean_return": 0.1, "std_dev": 0.2, "n_obs": 10}\n'
    monkeypatch.setattr(ap, "run_sandboxed", lambda *a, **k: out)
    placeholder = ap.approve_and_run_async(p["id"], target="local")
    done = _wait_run(placeholder["id"])
    assert done["status"] == "done"
    assert done["verdict"] == "CANDIDATE"


def test_sync_approve_and_run_returns_failed_record_on_executor_error(monkeypatch):
    p = _approve(monkeypatch)
    monkeypatch.setattr(ap, "sandbox_available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("sandbox exploded")

    monkeypatch.setattr(ap, "run_sandboxed", boom)
    run = ap.approve_and_run(p["id"], target="local")
    assert run["status"] == "failed"
    assert "sandbox exploded" in run["error"]


# --------------------------------------------------------------------------- #
# P3-4: a non-string code field is a retryable malformed response
# --------------------------------------------------------------------------- #

def test_list_code_is_retried_then_reported_as_malformed(monkeypatch):
    calls = {"n": 0}

    def llm(prompt, system=""):
        calls["n"] += 1
        return json.dumps({"strategy_name": "S", "explanation": "e", "code": ["def run(l, p):", "  pass"]})

    monkeypatch.setattr(ap, "_llm_chat", llm)
    with pytest.raises(RuntimeError, match="malformed"):
        ap.propose_from_idea("idea")
    assert calls["n"] == ap._CODEGEN_ATTEMPTS


def test_list_code_then_good_code_succeeds_on_retry(monkeypatch):
    answers = iter([
        json.dumps({"strategy_name": "S", "explanation": "e", "code": {"a": 1}}),
        _spec(_NO_DATA),
    ])
    monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": next(answers))
    assert ap.propose_from_idea("idea")["status"] == "pending"


def test_non_object_json_is_a_retryable_malformed_response(monkeypatch):
    monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": "[1, 2, 3]")
    with pytest.raises(RuntimeError, match="malformed"):
        ap.propose_from_idea("idea")


# --------------------------------------------------------------------------- #
# P3-5: the run work directory does not outlive the run
# --------------------------------------------------------------------------- #

def test_work_dir_removed_after_successful_run(monkeypatch):
    p = _approve(monkeypatch)
    monkeypatch.setattr(ap, "sandbox_available", lambda: True)
    monkeypatch.setattr(ap, "run_sandboxed",
                        lambda *a, **k: '===RESULT===\n{"n_obs": 0, "mean_return": 0, "std_dev": 0}\n')
    ap.approve_and_run(p["id"], target="local")
    tmp_root = ap._workspace_root() / "atlas_lab" / "tmp"
    assert not tmp_root.exists() or list(tmp_root.iterdir()) == []


def test_work_dir_removed_even_when_the_sandbox_fails(monkeypatch):
    p = _approve(monkeypatch)
    monkeypatch.setattr(ap, "sandbox_available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(ap, "run_sandboxed", boom)
    ap.approve_and_run(p["id"], target="local")
    tmp_root = ap._workspace_root() / "atlas_lab" / "tmp"
    assert not tmp_root.exists() or list(tmp_root.iterdir()) == []


# --------------------------------------------------------------------------- #
# P3-6: builtins reached by name
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("body", [
    "return __builtins__['eval']('1')",
    "return vars(__builtins__)['eval']('1')",
    "return globals()",
    "return locals()",
    "return type(load)",
    "breakpoint()",
])
def test_builtin_name_tricks_are_refused(body):
    code = f"def run(load, params):\n    {body}\n"
    assert ap._static_safety_check(code) != ""


def test_ordinary_strategy_still_passes():
    code = ("import math\n"
            "def run(load, params):\n"
            "    xs = [1.0, 2.0]\n"
            "    return {'mean_return': sum(xs) / len(xs), 'std_dev': 0.1, 'n_obs': len(xs)}\n")
    assert ap._static_safety_check(code) == ""


# --------------------------------------------------------------------------- #
# P3-69: the local loader reads ATLAS_DATA_PATH instead of always claiming it is unset
# --------------------------------------------------------------------------- #

def _load_error(tmp_path, monkeypatch, data_path):
    if data_path is None:
        monkeypatch.delenv("ATLAS_DATA_PATH", raising=False)
    else:
        monkeypatch.setenv("ATLAS_DATA_PATH", str(data_path))
    code = "def run(load, params):\n    load('fx:EURUSD:d1')\n    return {}\n"
    harness = ap._write_harness(tmp_path / "w", code, {})
    out = subprocess.run([sys.executable, str(harness)], capture_output=True, text=True,
                         timeout=30, cwd=str(tmp_path), check=False).stdout
    _, err = ap._parse_harness_output(out)
    return err


def test_unset_data_path_is_not_configured(tmp_path, monkeypatch):
    err = _load_error(tmp_path, monkeypatch, None)
    assert "not set" in err and "ATLAS_DATA_PATH" in err


def test_set_data_path_is_never_reported_as_not_set(tmp_path, monkeypatch):
    err = _load_error(tmp_path, monkeypatch, tmp_path)
    assert err
    assert "is not set" not in err
    assert str(tmp_path) in err


def test_set_but_missing_data_path_names_the_path(tmp_path, monkeypatch):
    missing = tmp_path / "nope"
    err = _load_error(tmp_path, monkeypatch, missing)
    assert str(missing) in err and "is not set" not in err


# --------------------------------------------------------------------------- #
# P4-66: one workspace resolver
# --------------------------------------------------------------------------- #

def test_workspace_root_is_the_config_workspace(monkeypatch):
    from dourmouse import config

    monkeypatch.delenv("DOURMOUSE_WORKSPACE", raising=False)
    root = ap._workspace_root()
    assert root == config.workspace_dir()
    assert root.parent.name != "dourmouse"  # not the package-internal tree


def test_workspace_root_honours_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    assert ap._workspace_root() == tmp_path / "ws"


# --------------------------------------------------------------------------- #
# P4-3: atlas_ui_ops puts the repo root (not dourmouse/) on sys.path
# --------------------------------------------------------------------------- #

def test_ui_ops_root_is_the_repo_root_and_package_dir_stays_off_sys_path():
    repo = Path(__file__).resolve().parents[2]
    code = (
        "import sys\n"
        "from dourmouse.atlas import atlas_ui_ops as m\n"
        f"assert str(m._ROOT) == {str(repo)!r}, m._ROOT\n"
        f"assert {str(repo / 'dourmouse')!r} not in sys.path\n"
        "print('ok')\n"
    )
    env = {"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin"}
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60, cwd="/", env=env, check=False)
    assert res.stdout.strip() == "ok", res.stderr


# --------------------------------------------------------------------------- #
# H-FI1-1: an unreadable proposals.json is kept aside, never overwritten by an empty store
# --------------------------------------------------------------------------- #

def test_corrupt_store_is_kept_aside_not_overwritten(monkeypatch):
    monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _spec(_NO_DATA))
    first = ap.propose_from_idea("idea one")
    store = ap._store_path()
    store.write_text("{truncated", encoding="utf-8")
    assert ap.list_proposals() == []  # nothing readable right now
    second = ap.propose_from_idea("idea two")  # writes a fresh store
    asides = list(store.parent.glob("proposals.json.corrupt-*"))
    assert len(asides) == 1
    assert "truncated" in asides[0].read_text(encoding="utf-8")
    ids = {p["id"] for p in ap.list_proposals()}
    assert second["id"] in ids and first["id"] not in ids


def test_store_holding_a_list_is_kept_aside_too(monkeypatch):
    store = ap._store_path()
    store.write_text("[1, 2]", encoding="utf-8")
    assert ap.list_proposals() == []
    assert list(store.parent.glob("proposals.json.corrupt-*"))


# --------------------------------------------------------------------------- #
# H-FI1-2: a desktop engine reply that is not an object is a clean failure
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b'{"ok": true, "result": []}',
                                  b'{"ok": true, "result": {"metrics": [1]}}'])
def test_desktop_engine_reply_with_the_wrong_shape_is_an_error(monkeypatch, body):
    monkeypatch.setattr(ap.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    metrics, err = ap._call_desktop_engine("code", {})
    assert metrics == {} and err
