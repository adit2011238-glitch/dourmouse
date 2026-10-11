"""Fix agent FD1: P3-17, a result records whether the answering model also graded it."""

from __future__ import annotations

import json
import types

import dourmouse.eval_harness as eh


class _Completions:
    def create(self, **kw):
        msg = types.SimpleNamespace(content=json.dumps({"score": 0.9, "failure_type": "correct", "feedback": "",
                                                        "strengths": [], "weaknesses": []}))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


class _Client:
    chat = types.SimpleNamespace(completions=_Completions())


def _run(monkeypatch, answerer, grader_env):
    monkeypatch.setattr(eh, "_answerer_model", lambda: answerer)
    cfg = types.SimpleNamespace(model=answerer)
    monkeypatch.setattr("dourmouse.backend_fallback.load_llm_config_with_fallback", lambda: cfg)
    monkeypatch.setattr("dourmouse.orchestrator._build_client", lambda c: _Client())
    if grader_env is None:
        monkeypatch.delenv("DOURMOUSE_EVAL_GRADER_MODEL", raising=False)
    else:
        monkeypatch.setenv("DOURMOUSE_EVAL_GRADER_MODEL", grader_env)
    monkeypatch.setattr("dourmouse.dispatch.run_dispatch",
                        lambda q, r, client=None: {"final_text": "an answer", "transcript": []})
    return eh.run_question({"id": "q1", "question": "q", "ideal_answer": "a"}, registry=object())


def test_default_grader_is_flagged_as_self_graded(monkeypatch):
    r = _run(monkeypatch, "model-a", None)
    assert r.self_graded is True
    assert r.answerer_model == "model-a" and r.grader_model == "model-a"
    assert r.to_dict()["self_graded"] is True


def test_separate_grader_model_is_not_self_graded(monkeypatch):
    r = _run(monkeypatch, "model-a", "model-b")
    assert r.self_graded is False
    assert r.grader_model == "model-b"


def test_summary_line_warns_when_self_graded():
    a = eh.GradedResult(id="x", question="q", answer="a", tool_trace=[], score=1.0, failure_type="c",
                        feedback="", self_graded=True)
    b = eh.GradedResult(id="y", question="q", answer="a", tool_trace=[], score=1.0, failure_type="c",
                        feedback="", self_graded=False)
    assert "self-graded" in eh.independence_note([a, b]).lower()
    assert eh.independence_note([b]) == ""
