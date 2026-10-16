"""FI2: research pipeline findings (P4-52, P5-44, P5-45, P3-54, P3-50, P3-53, P3-55)."""

from __future__ import annotations

import hashlib
import json

import pytest

import dourmouse.chat as chat_module
from dourmouse.research_graph.store import GraphStore
from dourmouse.research_pipeline import hypotheses as hy
from dourmouse.research_pipeline.answer_critic import SUPPORTED, UNSUPPORTED, critique_answer
from dourmouse.research_pipeline.core import Claim, Contradiction, ResearchRecord

# ---------------------------------------------------------------- helpers


class _FakeSession:
    calls: list[str] = []
    responses: list[object] = []

    def __init__(self, registry, session_file=None):
        pass

    def ask(self, prompt, force_plain_dispatch=False):
        type(self).calls.append(prompt)
        r = type(self).responses
        return {"final_text": r[0] if len(r) == 1 else r.pop(0)}


def _chat(monkeypatch, responses):
    _FakeSession.calls = []
    _FakeSession.responses = list(responses)
    monkeypatch.setattr(chat_module, "ChatSession", _FakeSession)


def _fake_doc(url, body):
    from dourmouse.research_pipeline.acquire import FetchedDocument

    return FetchedDocument(
        requested_url=url, final_url=url, redirect_chain=(), status=200,
        content_type="text/plain", charset="utf-8", charset_source="header",
        fetched_at=1.0, raw_sha256=hashlib.sha256(body.encode()).hexdigest(),
        raw_bytes=len(body), truncated=False, kind="text", text=body)


def _fetch(monkeypatch, pages):
    import urllib.error

    from dourmouse.research_pipeline import acquire

    def fake(url, **kw):
        if url not in pages:
            raise urllib.error.URLError("unreachable in test")
        return _fake_doc(url, pages[url])

    monkeypatch.setattr(acquire, "fetch_document", fake)


def _claim(text="water is wet", source="s1", sub_question="") -> Claim:
    return Claim(claim=text, source_id=source, url=f"https://example.com/{source}",
                 document_hash="abc", location="p.1", passage="Water is wet.",
                 retrieved_at=1.0, agent="research_info", sub_question=sub_question)


# ---------------------------------------------------------------- P4-52

def _vit_d_record(claim_text: str, passage: str) -> ResearchRecord:
    r = ResearchRecord(question="Does vitamin D prevent fractures?")
    r.set_plan(["What do trials show?"])
    r.add_sources(["https://example.org/vd"])
    r.add_claim(Claim(claim=claim_text, source_id="s1", url="https://example.org/vd",
                      document_hash="h1", location="p1", passage=passage, retrieved_at=1.0,
                      agent="t", sub_question="What do trials show?"))
    return r


NEG_CLAIM = "Vitamin D supplementation does not reduce the risk of fractures in adults."


def test_opposite_polarity_sentence_is_not_supported():
    r = _vit_d_record(NEG_CLAIM, NEG_CLAIM)
    (s,) = critique_answer("Vitamin D supplementation reduces the risk of fractures in adults.", r).sentences
    assert s.verdict == UNSUPPORTED
    assert "negat" in s.reason.lower()


def test_negated_claim_vs_positive_evidence_is_not_supported():
    pos = "Vitamin D supplementation reduces the risk of fractures in adults."
    r = _vit_d_record(pos, pos)
    (s,) = critique_answer("Vitamin D supplementation does not reduce the risk of fractures in adults.", r).sentences
    assert s.verdict == UNSUPPORTED


def test_matching_negation_is_still_supported():
    r = _vit_d_record(NEG_CLAIM, NEG_CLAIM)
    (s,) = critique_answer("Vitamin D supplementation does not reduce fracture risk in adults.", r).sentences
    assert s.verdict == SUPPORTED


@pytest.mark.parametrize("neg", ["isn't", "never", "cannot", "without"])
def test_other_negation_forms_count(neg):
    r = _vit_d_record("Vitamin D supplementation reduces the risk of fractures in adults.",
                      "Vitamin D supplementation reduces the risk of fractures in adults.")
    text = f"Vitamin D supplementation {neg} reduces the risk of fractures in adults."
    (s,) = critique_answer(text, r).sentences
    assert s.verdict == UNSUPPORTED


def test_model_quote_with_opposite_polarity_is_demoted():
    r = _vit_d_record(NEG_CLAIM, NEG_CLAIM)
    # A sentence that only partly matches goes to the model; the model quotes the negated
    # fragment, which says the opposite of the (positive) sentence.
    sentence = "Vitamin D reduces fracture risk."
    llm = lambda prompt: json.dumps({"verdicts": [{"n": 1, "supported": True,  # noqa: E731
                                                   "quote": "does not reduce the risk of fractures"}]})
    (s,) = critique_answer(sentence, r, llm).sentences
    assert s.verdict == UNSUPPORTED


# ---------------------------------------------------------------- P5-44 / P5-45

def _graph(tmp_path):
    g = GraphStore(tmp_path / "g.db")
    g.put("research_question", "q1", {"text": "Does caffeine improve reaction time?"}, created_by="t")
    g.put("claim", "c0", {"text": "A trial found caffeine cut reaction time."}, created_by="t")
    g.link(("claim", "c0"), "answers", ("research_question", "q1"), created_by="t")
    return g


def _model(payload):
    return lambda prompt: json.dumps(payload)


def test_rerun_with_different_rationale_does_not_crash(tmp_path):
    g = _graph(tmp_path)
    a = {"hypotheses": [{"statement": "Caffeine speeds reaction", "rationale": "adenosine", "claims": [1]}]}
    b = {"hypotheses": [{"statement": "Caffeine speeds reaction", "rationale": "arousal", "claims": [1]},
                        {"statement": "Effect fades", "rationale": "half life", "claims": [1]}]}
    first = hy.generate_hypotheses("q1", complete=_model(a), store=g)
    second = hy.generate_hypotheses("q1", complete=_model(b), store=g)
    assert first["ok"] and second["ok"]
    assert len(second["hypotheses"]) == 2
    existing = [h for h in second["hypotheses"] if h.get("existing")]
    assert len(existing) == 1
    assert g.count("hypothesis") == 2
    # the stored rationale is the original, not silently overwritten
    assert g.get("hypothesis", first["hypotheses"][0]["id"]).body["rationale"] == "adenosine"


def test_same_statement_twice_in_one_reply(tmp_path):
    g = _graph(tmp_path)
    p = {"hypotheses": [{"statement": "Same", "rationale": "one", "claims": [1]},
                        {"statement": "same ", "rationale": "two", "claims": [1]}]}
    r = hy.generate_hypotheses("q1", complete=_model(p), store=g)
    assert r["ok"] and g.count("hypothesis") == 1


def test_zero_variance_equal_to_null_is_not_significant():
    s = hy.describe_samples([0.5] * 100, null_mean=0.5)
    assert s["t"] == 0 and s["p_two_sided"] == 1.0


def test_zero_variance_different_from_null_is_maximally_significant():
    s = hy.describe_samples([0.7] * 10, null_mean=0.5)
    assert s["p_two_sided"] == 0.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_null_is_rejected(bad):
    s = hy.describe_samples([1.0, 2.0, 3.0], null_mean=bad)
    assert "error" in s and "p_two_sided" not in s


def test_design_and_run_ignores_a_non_finite_null_value(tmp_path):
    g = GraphStore(tmp_path / "g.db")
    g.put("hypothesis", "h1", {"statement": "constant samples"}, created_by="t")
    code = ("import json, os\nxs=[0.5]*50\nos.makedirs('out', exist_ok=True)\n"
            "json.dump({'samples': xs}, open('out/data.json','w'))\n"
            "json.dump({'n': len(xs)}, open('out/metrics.json','w'))")
    raw = ('{"protocol": "constant", "code": ' + json.dumps(code) + ', "null_value": NaN}')
    r = hy.design_and_run("h1", complete=lambda p: raw, store=g, wait_s=60)
    assert r["ok"]
    assert r["null_value"] is None
    stats = r.get("statistics", {})
    assert "p_two_sided" not in stats  # description only: no usable null


# ---------------------------------------------------------------- P3-53

def test_plan_strips_dash_from_indented_bullets(monkeypatch):
    from dourmouse.research_pipeline.stages import plan

    _chat(monkeypatch, ["Sub-questions:\n  - What is X?\n    - Why does X matter?\n- Plain one"])
    r = plan(ResearchRecord(question="X?"))
    assert r.plan == ("What is X?", "Why does X matter?", "Plain one")


# ---------------------------------------------------------------- P3-55

def _extract(monkeypatch, page, reply):
    from dourmouse.research_pipeline.stages import extract_evidence

    r = ResearchRecord(question="What is MCP?")
    r.set_plan(["What are the parts?"])
    r.add_sources(["https://example.com/p"])
    _fetch(monkeypatch, {"https://example.com/p": page})
    _chat(monkeypatch, [reply])
    extract_evidence(r, None)
    return r


PAGE = "The protocol has three core parts: hosts, servers, and clients. The the a of."


@pytest.mark.parametrize("passage", ["the", "a", "of", "The the"])
def test_tiny_passage_is_rejected(monkeypatch, passage):
    with pytest.raises(ValueError, match="passage validation"):
        _extract(monkeypatch, PAGE, f"CLAIM: The protocol has three parts.\nPASSAGE: {passage}\nLOCATION: top")


def test_unrelated_passage_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="passage validation"):
        _extract(monkeypatch, PAGE,
                 "CLAIM: Tomatoes are botanically fruit.\n"
                 "PASSAGE: hosts, servers, and clients\nLOCATION: top")


def test_real_related_passage_is_accepted(monkeypatch):
    r = _extract(monkeypatch, PAGE,
                 "CLAIM: The protocol has three parts.\n"
                 "PASSAGE: The protocol has three core parts: hosts, servers, and clients.\nLOCATION: top")
    assert len(r.claims) == 1


# ---------------------------------------------------------------- P3-50

def test_reject_claim_keeps_the_reason():
    r = ResearchRecord(question="q")
    r.set_plan(["s"])
    r.add_sources(["https://example.com/s1"])
    r.add_claim(_claim())
    r.reject_claim(0, "superseded by a larger trial")
    assert r.claims[0].status == "REJECTED"
    assert r.claims[0].rejected_reason == "superseded by a larger trial"


@pytest.mark.parametrize("idx", [-1, 1, 5])
def test_reject_claim_validates_the_index(idx):
    r = ResearchRecord(question="q")
    r.set_plan(["s"])
    r.add_sources(["https://example.com/s1"])
    r.add_claim(_claim())
    with pytest.raises(IndexError):
        r.reject_claim(idx, "x")
    assert r.claims[0].status == "ACTIVE"


def test_rejected_reason_survives_save_and_load(tmp_path):
    from dourmouse.research_pipeline.store import ResearchStore

    r = ResearchRecord(question="q")
    r.set_plan(["s"])
    r.add_sources(["https://example.com/s1"])
    r.add_claim(_claim())
    r.reject_claim(0, "why")
    store = ResearchStore(tmp_path / "r.db")
    store.save(r, now=1.0)
    assert store.load("q").claims[0].rejected_reason == "why"


# ---------------------------------------------------------------- P3-54

def _synth_with_contradiction() -> ResearchRecord:
    from dourmouse.research_pipeline.stages import _claim_fingerprint

    r = ResearchRecord(question="Does MCP support HTTP?")
    r.set_plan(["Which transports?"])
    r.add_sources(["https://a.example/1", "https://b.example/1"])
    a = _claim("MCP supports HTTP", source="a1", sub_question="Which transports?")
    b = _claim("MCP is stdio only", source="b1", sub_question="Which transports?")
    r.add_claim(a)
    r.add_claim(b)
    r.add_contradiction(Contradiction(_claim_fingerprint(a), _claim_fingerprint(b),
                                      "Which transports?", "HTTP vs stdio only"))
    r.set_synthesis("First answer: unclear.")
    return r


def _discover_one(monkeypatch, url):
    import dourmouse.dispatch as dispatch_module

    def _dispatch(messages, registry, **kw):
        return {"final_text": "ok", "transcript": [
            {"type": "tool_use", "name": "fetch_url", "raw_arguments": json.dumps({"url": url})}]}

    monkeypatch.setattr(dispatch_module, "run_dispatch_messages", _dispatch)


def _registry():
    from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec

    reg = DispatchRegistry()
    reg.register_subagent(Subagent(name="research_info", domain="General", description="d", tools=(
        ToolSpec(name="web_search", description="t", parameters={"type": "object", "properties": {}},
                 handler=lambda a: "x"),
        ToolSpec(name="fetch_url", description="t", parameters={"type": "object", "properties": {}},
                 handler=lambda a: "x"),
    )))
    return reg


def test_follow_up_with_no_evidence_leaves_the_task_open(monkeypatch):
    from dourmouse.research_pipeline.stages import run_backward_edge

    r = _synth_with_contradiction()
    _discover_one(monkeypatch, "https://spec.example/mcp")
    _fetch(monkeypatch, {"https://spec.example/mcp": "The specification defines stdio and Streamable HTTP."})
    # the model quotes a passage that is not on the page: extraction fails
    _chat(monkeypatch, ["CLAIM: The spec defines HTTP.\nPASSAGE: this sentence is not on the page at all\nLOCATION: top"])
    run_backward_edge(r, _registry())
    (task,) = r.tasks
    assert task.status == "OPEN"
    assert len(r.synthesis_history) == 1  # not revised as if settled
    # and the next call really retries it
    _discover_one(monkeypatch, "https://spec.example/other")
    _fetch(monkeypatch, {"https://spec.example/other": "The specification defines stdio and Streamable HTTP."})
    _chat(monkeypatch, [
        "CLAIM: The spec defines HTTP.\nPASSAGE: The specification defines stdio and Streamable HTTP.\nLOCATION: top",
        "CONTRADICTION: no\nNOTE: x",
        "CONTRADICTION: no\nNOTE: x",
        "Revised answer.",
    ])
    run_backward_edge(r, _registry())
    assert r.tasks[0].status == "DONE"
    assert r.synthesis_history[-1] == "Revised answer."


def test_follow_up_with_evidence_still_completes(monkeypatch):
    from dourmouse.research_pipeline.stages import run_follow_up, spawn_follow_ups

    r = _synth_with_contradiction()
    (task,) = spawn_follow_ups(r)
    _discover_one(monkeypatch, "https://spec.example/mcp")
    _fetch(monkeypatch, {"https://spec.example/mcp": "The specification defines stdio and Streamable HTTP."})
    _chat(monkeypatch, ["CLAIM: The spec defines HTTP.\nPASSAGE: The specification defines stdio and Streamable HTTP.\nLOCATION: top"])
    run_follow_up(r, _registry(), task.task_id)
    assert r.tasks[0].status == "DONE"
    assert r.claims[-1].task_id == task.task_id


def test_unknown_task_id_is_a_value_error():
    from dourmouse.research_pipeline.stages import run_follow_up

    r = _synth_with_contradiction()
    with pytest.raises(ValueError, match="no task"):
        run_follow_up(r, _registry(), "followup-99")
