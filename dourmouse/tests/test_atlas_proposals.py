"""Tests for dourmouse/atlas_proposals.py (v8.16 — LLM-authored strategy
code, human-gated). Workspace isolation is automatic (see conftest.py's
autouse _workspace_isolated fixture) — no test here touches the real
workspace/atlas_lab/proposals.json.
"""

from __future__ import annotations

import json
import time

import pytest

from dourmouse import atlas_proposals as ap
from dourmouse.tests.test_webui import server  # noqa: F401 — shared server fixture


# --------------------------------------------------------------------------- #
# Layer 1 — static safety pre-filter
# --------------------------------------------------------------------------- #

_GOOD_CODE = """
import pandas as pd

def run(load, params):
    df = load("fx:EURUSD:d1")
    return {"mean_return": 0.001, "std_dev": 0.01, "n_obs": len(df)}
"""

_GOOD_CODE_NO_DATA = """
def run(load, params):
    return {"mean_return": 0.0, "std_dev": 0.0, "n_obs": 0, "note": "test"}
"""


class TestStaticSafetyCheck:
    def test_clean_code_passes(self):
        assert ap._static_safety_check(_GOOD_CODE) == ""

    def test_clean_code_with_no_load_call_passes(self):
        assert ap._static_safety_check(_GOOD_CODE_NO_DATA) == ""

    def test_syntax_error_refused(self):
        note = ap._static_safety_check("def run(load, params):\n    return {\n")
        assert "does not parse" in note

    def test_missing_run_function_refused(self):
        note = ap._static_safety_check("def other(load, params):\n    return {}\n")
        assert "no top-level" in note

    @pytest.mark.parametrize(
        "bad_import",
        ["import os", "import subprocess", "import socket", "from os import path",
         "import shutil", "import requests"],
    )
    def test_disallowed_import_refused(self, bad_import):
        code = f"{bad_import}\ndef run(load, params):\n    return {{}}\n"
        note = ap._static_safety_check(code)
        assert "not in the allowed list" in note

    @pytest.mark.parametrize("allowed_import", ["pandas", "numpy", "math", "statistics", "datetime", "json"])
    def test_allowed_import_passes(self, allowed_import):
        code = f"import {allowed_import}\ndef run(load, params):\n    return {{'mean_return': 0.0, 'std_dev': 0.0, 'n_obs': 0}}\n"
        assert ap._static_safety_check(code) == ""

    @pytest.mark.parametrize(
        "hostile",
        [
            "def run(load, params):\n    return {}.__class__.__base__.__subclasses__()\n",
            "def run(load, params):\n    eval('1')\n    return {}\n",
            "def run(load, params):\n    exec('pass')\n    return {}\n",
            "def run(load, params):\n    open('/etc/passwd')\n    return {}\n",
            "def run(load, params):\n    __import__('os')\n    return {}\n",
        ],
    )
    def test_sandbox_escape_vectors_refused(self, hostile):
        note = ap._static_safety_check(hostile)
        assert note != ""


# --------------------------------------------------------------------------- #
# LLM -> proposal (mocked LLM, real safety check + real persistence)
# --------------------------------------------------------------------------- #

def _mock_llm(strategy_name="Test Strategy", code=_GOOD_CODE):
    return json.dumps({
        "strategy_name": strategy_name,
        "explanation": "A test strategy that does nothing real.",
        "params": {"pair": "EURUSD", "lookback": 20},
        "code": code,
    })


class TestProposeFromIdea:
    def test_safe_idea_lands_pending(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm())
        prop = ap.propose_from_idea("buy EURUSD on Mondays")
        assert prop["status"] == "pending"
        assert prop["strategy_name"] == "Test Strategy"
        assert prop["source"] == "chat"
        assert prop["safety_note"] == ""

    def test_generator_source_is_recorded(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm())
        prop = ap.propose_from_idea("improve on the momentum strategy", source="generator")
        assert prop["source"] == "generator"

    def test_unsafe_code_never_reaches_pending(self, monkeypatch):
        hostile = "import os\ndef run(load, params):\n    return {}\n"
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=hostile))
        prop = ap.propose_from_idea("do something sketchy")
        assert prop["status"] == "rejected_unsafe"
        assert prop["safety_note"] != ""
        # And it's genuinely unrecoverable — approve_and_run must refuse it.
        with pytest.raises(ValueError, match="safety pre-filter"):
            ap.approve_and_run(prop["id"], target="local")

    def test_empty_prompt_rejected_before_any_llm_call(self, monkeypatch):
        called = []
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": called.append(1) or _mock_llm())
        with pytest.raises(ValueError):
            ap.propose_from_idea("   ")
        assert not called

    def test_bad_llm_json_raises_with_raw_excerpt(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": "not json at all")
        with pytest.raises(RuntimeError, match="did not return valid JSON"):
            ap.propose_from_idea("test")

    def test_missing_required_field_raises(self, monkeypatch):
        monkeypatch.setattr(
            ap, "_llm_chat",
            lambda prompt, system="": json.dumps({"strategy_name": "x", "code": _GOOD_CODE}),
        )
        with pytest.raises(RuntimeError, match="explanation"):
            ap.propose_from_idea("test")

    def test_retries_on_malformed_json_then_succeeds(self, monkeypatch):
        calls = []

        def flaky(prompt, system=""):
            calls.append(1)
            if len(calls) < 2:
                return "not json at all"
            return _mock_llm()

        monkeypatch.setattr(ap, "_llm_chat", flaky)
        prop = ap.propose_from_idea("an idea")
        assert prop["status"] == "pending"
        assert len(calls) == 2

    def test_retries_on_missing_field_then_succeeds(self, monkeypatch):
        calls = []

        def flaky(prompt, system=""):
            calls.append(1)
            if len(calls) < 3:
                return json.dumps({"strategy_name": "x", "code": _GOOD_CODE_NO_DATA})  # missing explanation
            return _mock_llm()

        monkeypatch.setattr(ap, "_llm_chat", flaky)
        prop = ap.propose_from_idea("an idea")
        assert prop["status"] == "pending"
        assert len(calls) == 3

    def test_gives_up_after_max_attempts_with_clear_message(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": (calls.append(1), "still not json")[1])
        with pytest.raises(RuntimeError, match=f"{ap._CODEGEN_ATTEMPTS} times in a row"):
            ap.propose_from_idea("an idea")
        assert len(calls) == ap._CODEGEN_ATTEMPTS

    def test_persisted_across_reload(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm())
        prop = ap.propose_from_idea("test persistence")
        # list_proposals re-reads from disk each call — proves persistence,
        # not just an in-memory cache.
        found = ap.get_proposal(prop["id"])
        assert found is not None
        assert found["strategy_name"] == "Test Strategy"


# --------------------------------------------------------------------------- #
# Review queue CRUD + decision invariants
# --------------------------------------------------------------------------- #

class TestReviewQueue:
    def _make_pending(self, monkeypatch, **kw):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(**kw))
        return ap.propose_from_idea("an idea")

    def test_list_proposals_filters_by_status(self, monkeypatch):
        p1 = self._make_pending(monkeypatch)
        ap.reject_proposal(p1["id"], "not interesting")
        p2 = self._make_pending(monkeypatch)
        pending = ap.list_proposals(status="pending")
        rejected = ap.list_proposals(status="rejected")
        assert {p["id"] for p in pending} == {p2["id"]}
        assert {p["id"] for p in rejected} == {p1["id"]}

    def test_reject_records_reason(self, monkeypatch):
        p = self._make_pending(monkeypatch)
        ap.reject_proposal(p["id"], "already tried this, failed")
        got = ap.get_proposal(p["id"])
        assert got["status"] == "rejected"
        assert got["reviewer_note"] == "already tried this, failed"

    def test_reject_does_not_overwrite_prior_decision(self, monkeypatch):
        """A proposal already approved (or rejected) is a closed decision —
        a second reject call must not silently flip it."""
        p = self._make_pending(monkeypatch)
        ap.reject_proposal(p["id"], "first reason")
        ap.reject_proposal(p["id"], "second reason — should not apply")
        got = ap.get_proposal(p["id"])
        assert got["reviewer_note"] == "first reason"

    def test_reject_unknown_id_returns_none(self):
        assert ap.reject_proposal("prop_does_not_exist") is None

    def test_get_unknown_id_returns_none(self):
        assert ap.get_proposal("prop_does_not_exist") is None


# --------------------------------------------------------------------------- #
# approve_and_run — the gate itself
# --------------------------------------------------------------------------- #

class TestApproveAndRun:
    def test_unknown_proposal_raises_keyerror(self):
        with pytest.raises(KeyError):
            ap.approve_and_run("prop_nope", target="local")

    def test_desktop_target_is_honest_not_configured(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=_GOOD_CODE_NO_DATA))
        p = ap.propose_from_idea("idea")
        run = ap.approve_and_run(p["id"], target="desktop")
        assert run["status"] == "failed"
        assert "NOT CONFIGURED" in run["error"]
        # And the proposal itself is still marked approved — the GATE did
        # its job (code only ran, or tried to, after approval), the failure
        # is purely about the desktop backend not existing yet.
        assert ap.get_proposal(p["id"])["status"] == "approved"

    def test_local_execution_end_to_end_real_sandbox(self, monkeypatch):
        """No LLM mocking of the sandbox itself — this really shells out
        through sandbox.run_sandboxed on whatever this machine actually
        has. Skips honestly if sandbox-exec isn't available here (Rule 2.2
        — matches how test_system_access.py's own run_command test skips)."""
        if not ap.sandbox_available():
            pytest.skip("sandbox-exec not available on this machine")
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=_GOOD_CODE_NO_DATA))
        monkeypatch.setattr(ap, "_explain_run", lambda pid, run: "mocked explanation")
        p = ap.propose_from_idea("a strategy with no real data need")
        run = ap.approve_and_run(p["id"], target="local")
        assert run["status"] == "done", run.get("error")
        assert run["metrics"]["n_obs"] == 0
        assert run["verdict"] == "NO DATA"
        assert run["explanation"] == "mocked explanation"
        # The run is now findable both standalone and via the proposal.
        assert run["id"] in ap.get_proposal(p["id"])["runs"]
        assert any(r["id"] == run["id"] for r in ap.list_runs(proposal_id=p["id"]))

    def test_local_execution_real_load_call_honest_not_configured(self, monkeypatch):
        """A strategy that DOES call load() locally must get the honest
        "local data registry not configured" error, not a crash or a
        fabricated result — this is the harness's _LOCAL_LOADER_BODY."""
        if not ap.sandbox_available():
            pytest.skip("sandbox-exec not available on this machine")
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=_GOOD_CODE))
        p = ap.propose_from_idea("a strategy that wants real fx data")
        run = ap.approve_and_run(p["id"], target="local")
        assert run["status"] == "failed"
        assert "not configured" in run["error"].lower()

    def test_rejected_unsafe_cannot_be_approved(self, monkeypatch):
        monkeypatch.setattr(
            ap, "_llm_chat",
            lambda prompt, system="": _mock_llm(code="import socket\ndef run(load, params):\n    return {}\n"),
        )
        p = ap.propose_from_idea("idea")
        assert p["status"] == "rejected_unsafe"
        with pytest.raises(ValueError):
            ap.approve_and_run(p["id"], target="local")

    def test_already_rejected_cannot_be_approved(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=_GOOD_CODE_NO_DATA))
        p = ap.propose_from_idea("idea")
        ap.reject_proposal(p["id"], "no")
        with pytest.raises(ValueError, match="not approvable"):
            ap.approve_and_run(p["id"], target="local")


# --------------------------------------------------------------------------- #
# Harness output parsing + verdicts (pure functions, no sandbox needed)
# --------------------------------------------------------------------------- #

class TestApproveAndRunAsync:
    def _poll_until_done(self, run_id, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = ap.get_run(run_id)
            if run and run["status"] != "running":
                return run
            time.sleep(0.05)
        pytest.fail(f"run {run_id} did not finish within {timeout}s")

    def test_returns_running_placeholder_immediately(self, monkeypatch):
        if not ap.sandbox_available():
            pytest.skip("sandbox-exec not available on this machine")
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=_GOOD_CODE_NO_DATA))
        p = ap.propose_from_idea("idea")
        placeholder = ap.approve_and_run_async(p["id"], target="local")
        assert placeholder["status"] == "running"
        assert placeholder["id"].startswith("run_")
        # The proposal is approved synchronously, before the thread even
        # needs to finish — the gate decision isn't part of the latency.
        assert ap.get_proposal(p["id"])["status"] == "approved"

    def test_placeholder_updates_in_place_to_done(self, monkeypatch):
        if not ap.sandbox_available():
            pytest.skip("sandbox-exec not available on this machine")
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=_GOOD_CODE_NO_DATA))
        monkeypatch.setattr(ap, "_explain_run", lambda pid, run: "mocked explanation")
        p = ap.propose_from_idea("idea")
        placeholder = ap.approve_and_run_async(p["id"], target="local")
        final = self._poll_until_done(placeholder["id"])
        assert final["status"] == "done"
        assert final["id"] == placeholder["id"]  # same record, updated in place
        assert final["metrics"]["n_obs"] == 0

    def test_get_run_unknown_id_returns_none(self):
        assert ap.get_run("run_does_not_exist") is None

    def test_rejected_unsafe_cannot_be_approved_async(self, monkeypatch):
        monkeypatch.setattr(
            ap, "_llm_chat",
            lambda prompt, system="": _mock_llm(code="import socket\ndef run(load, params):\n    return {}\n"),
        )
        p = ap.propose_from_idea("idea")
        with pytest.raises(ValueError):
            ap.approve_and_run_async(p["id"], target="local")


class TestParseHarnessOutput:
    def test_parses_result_marker(self):
        out = '===RESULT===\n{"mean_return": 0.01, "n_obs": 5}\n'
        metrics, err = ap._parse_harness_output(out)
        assert err == ""
        assert metrics == {"mean_return": 0.01, "n_obs": 5}

    def test_parses_error_marker(self):
        out = "===ERROR===\nsomething broke\n"
        metrics, err = ap._parse_harness_output(out)
        assert metrics == {}
        assert "something broke" in err

    def test_neither_marker_is_honest_failure(self):
        metrics, err = ap._parse_harness_output("garbage, no markers at all")
        assert metrics == {}
        assert "no recognizable output" in err

    def test_unparseable_result_json_is_honest_failure(self):
        metrics, err = ap._parse_harness_output("===RESULT===\nnot{json")
        assert metrics == {}
        assert "unparseable" in err


class TestWebRoutes:
    """Real server fixture (same shape as TestAtlasLabRoutes in
    test_atlas_lab.py) — proves the routes are actually wired in webui.py,
    not just that the module functions work in isolation."""

    def _get(self, server, path: str):
        import http.client

        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        status, body = resp.status, resp.read()
        conn.close()
        return status, json.loads(body.decode("utf-8"))

    def _post(self, server, path: str, body: dict):
        import http.client

        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status, data = resp.status, json.loads(resp.read())
        conn.close()
        return status, data

    def test_empty_proposals_list_is_ok(self, server):
        status, data = self._get(server, "/api/atlas-lab/proposals")
        assert status == 200
        assert data["ok"] is True
        assert data["proposals"] == []

    def test_propose_then_list_then_get(self, server, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=_GOOD_CODE_NO_DATA))
        status, data = self._post(server, "/api/atlas-lab/proposals", {"prompt": "test idea"})
        assert status == 200, data
        assert data["ok"] is True
        pid = data["proposal"]["id"]
        assert data["proposal"]["status"] == "pending"

        status, data = self._get(server, "/api/atlas-lab/proposals")
        assert any(p["id"] == pid for p in data["proposals"])

        status, data = self._get(server, f"/api/atlas-lab/proposals/{pid}")
        assert status == 200
        assert data["proposal"]["id"] == pid

    def test_propose_missing_prompt_is_400(self, server):
        status, data = self._post(server, "/api/atlas-lab/proposals", {})
        assert status == 400
        assert data["ok"] is False

    def test_get_unknown_proposal_is_404(self, server):
        status, data = self._get(server, "/api/atlas-lab/proposals/prop_nope")
        assert status == 404

    def test_reject_unknown_proposal_is_404(self, server):
        status, data = self._post(server, "/api/atlas-lab/proposals/prop_nope/reject", {"reason": "no"})
        assert status == 404

    def test_full_loop_propose_reject(self, server, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=_GOOD_CODE_NO_DATA))
        _, data = self._post(server, "/api/atlas-lab/proposals", {"prompt": "test idea"})
        pid = data["proposal"]["id"]

        status, data = self._post(server, f"/api/atlas-lab/proposals/{pid}/reject", {"reason": "not now"})
        assert status == 200
        assert data["proposal"]["status"] == "rejected"
        assert data["proposal"]["reviewer_note"] == "not now"

    def test_full_loop_propose_approve_poll_run(self, server, monkeypatch):
        if not ap.sandbox_available():
            pytest.skip("sandbox-exec not available on this machine")
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": _mock_llm(code=_GOOD_CODE_NO_DATA))
        monkeypatch.setattr(ap, "_explain_run", lambda pid, run: "mocked explanation")

        _, data = self._post(server, "/api/atlas-lab/proposals", {"prompt": "test idea"})
        pid = data["proposal"]["id"]

        status, data = self._post(server, f"/api/atlas-lab/proposals/{pid}/approve", {"target": "local"})
        assert status == 200, data
        run_id = data["run"]["id"]
        assert data["run"]["status"] == "running"

        deadline = time.time() + 10
        final = None
        while time.time() < deadline:
            status, data = self._get(server, f"/api/atlas-lab/runs/{run_id}")
            if data["run"]["status"] != "running":
                final = data["run"]
                break
            time.sleep(0.05)
        assert final is not None, "run never left running state"
        assert final["status"] == "done"
        assert final["metrics"]["n_obs"] == 0

        status, data = self._get(server, f"/api/atlas-lab/runs?proposal_id={pid}")
        assert any(r["id"] == run_id for r in data["runs"])

    def test_approve_unknown_proposal_is_404(self, server):
        status, data = self._post(server, "/api/atlas-lab/proposals/prop_nope/approve", {"target": "local"})
        assert status == 404

    def test_get_unknown_run_is_404(self, server):
        status, data = self._get(server, "/api/atlas-lab/runs/run_nope")
        assert status == 404


class TestVerdictFromMetrics:
    def test_zero_observations_is_no_data(self):
        assert ap._verdict_from_metrics({"n_obs": 0}) == "NO DATA"

    def test_high_sharpe_is_candidate(self):
        assert ap._verdict_from_metrics({"n_obs": 100, "sharpe": 0.8}) == "CANDIDATE"

    def test_low_positive_sharpe_is_hold(self):
        assert ap._verdict_from_metrics({"n_obs": 100, "sharpe": 0.1}) == "HOLD"

    def test_negative_sharpe_is_failed(self):
        assert ap._verdict_from_metrics({"n_obs": 100, "sharpe": -0.2}) == "FAILED"

    def test_no_sharpe_falls_back_to_mean_return_sign(self):
        assert ap._verdict_from_metrics({"n_obs": 100, "mean_return": 0.01}) == "CANDIDATE"
        assert ap._verdict_from_metrics({"n_obs": 100, "mean_return": -0.01}) == "FAILED"

    def test_nothing_usable_is_inconclusive(self):
        assert ap._verdict_from_metrics({"n_obs": 100}) == "INCONCLUSIVE"
