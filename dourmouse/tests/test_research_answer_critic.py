"""R9 (founding spec item 58): the answer critic. Deterministic support against
stored claims and passages, a model that may only decide partly matching
sentences and must quote a stored passage, and the tool that exposes it. No
network: the model is a fake callable."""

from __future__ import annotations

import json

import pytest

import dourmouse.research_pipeline_tools as rpt
from dourmouse.research_graph import store as graph_store
from dourmouse.research_graph.store import GraphStore
from dourmouse.research_graph.sync import sync_record
from dourmouse.research_pipeline.answer_critic import (
    CITED_UNVERIFIED,
    NO_CITATION,
    NOT_A_CLAIM,
    SUPPORTED,
    UNSUPPORTED,
    critique_answer,
    split_sentences,
)
from dourmouse.research_pipeline.core import Claim, ResearchRecord

PASSAGE_1 = ("A 2019 randomized trial of 120 adults found that 200mg of caffeine cut mean reaction time by 18ms. "
             "The effect faded after four hours.")
PASSAGE_2 = "Habitual coffee drinkers showed a smaller benefit than people who rarely drink coffee, the authors noted."
URL_1 = "https://example.org/caffeine-trial"
URL_2 = "https://example.org/habitual"


def _record() -> ResearchRecord:
    r = ResearchRecord(question="Does caffeine improve reaction time?")
    r.set_plan(["What does the trial evidence say?"])
    r.add_sources([URL_1, URL_2])
    for i, (claim, passage, url) in enumerate((
        ("Caffeine at 200mg cut mean reaction time by 18ms in a 2019 randomized trial.", PASSAGE_1, URL_1),
        ("The benefit is smaller in habitual coffee drinkers.", PASSAGE_2, URL_2),
    )):
        r.add_claim(Claim(claim=claim, source_id=f"s{i}", url=url, document_hash=f"h{i}", location="p1",
                          passage=passage, retrieved_at=1.0, agent="t", sub_question="What does the trial evidence say?"))
    return r


def _by_text(result, needle):
    return next(s for s in result.sentences if needle in s.text)


def test_backed_sentence_is_supported_with_ids():
    result = critique_answer("Caffeine at 200mg cut mean reaction time by 18ms.", _record())
    (s,) = result.sentences
    assert s.verdict == SUPPORTED and s.decided_by == "deterministic"
    assert any(i.startswith("clm-") for i in s.evidence_ids) and any(i.startswith("pas-") for i in s.evidence_ids)
    assert result.support_ratio == 1.0 and result.counts[SUPPORTED] == 1


def test_a_wrong_number_is_not_supported_by_matching_words():
    (s,) = critique_answer("Caffeine at 200mg cut mean reaction time by 40ms.", _record()).sentences
    assert s.verdict == UNSUPPORTED


def test_unsupported_sentence_is_flagged():
    result = critique_answer("Caffeine cures insomnia in teenagers. The effect faded after four hours.", _record())
    assert _by_text(result, "insomnia").verdict == UNSUPPORTED
    assert _by_text(result, "faded").verdict == SUPPORTED
    assert result.support_ratio == 0.5


def test_missing_citation_when_the_format_uses_them():
    answer = f"Caffeine cut mean reaction time by 18ms ({URL_1}). The benefit is smaller in habitual coffee drinkers."
    result = critique_answer(answer, _record())
    assert result.citations_expected is True
    assert _by_text(result, "18ms").verdict == SUPPORTED
    assert _by_text(result, "habitual").verdict == NO_CITATION
    assert result.missing_citations == ("The benefit is smaller in habitual coffee drinkers.",)
    # an answer that never cites is not faulted for it unless the caller says citations are required
    plain = critique_answer("The benefit is smaller in habitual coffee drinkers.", _record())
    assert plain.sentences[0].verdict == SUPPORTED
    required = critique_answer("The benefit is smaller in habitual coffee drinkers.", _record(), require_citations=True)
    assert required.sentences[0].verdict == NO_CITATION


def test_cited_but_unbacked_is_cited_unverified():
    result = critique_answer("Caffeine doubles lifespan in mice [3]. Caffeine cut mean reaction time by 18ms [1].", _record())
    assert result.sentences[0].verdict == CITED_UNVERIFIED
    assert result.sentences[1].verdict == SUPPORTED


def test_a_lone_marker_after_the_full_stop_joins_its_sentence():
    result = critique_answer("Caffeine cut mean reaction time by 18ms. [1]\nCaffeine cures insomnia. [2]", _record())
    assert [s.verdict for s in result.sentences] == [SUPPORTED, CITED_UNVERIFIED]


def test_not_a_claim_sentences():
    answer = ("## Findings\nDoes caffeine really help?\nHowever, the evidence is inconclusive on long-term use.\n"
              "In summary:\nI cannot say more than the sources state.")
    result = critique_answer(answer, _record())
    assert all(s.verdict == NOT_A_CLAIM for s in result.sentences), [(s.text, s.verdict) for s in result.sentences]
    assert result.support_ratio is None and "nothing to verify" in result.summary


def test_empty_and_question_only_answers():
    for text in ("", "   \n\n"):
        result = critique_answer(text, _record())
        assert result.sentences == () and result.support_ratio is None and "empty" in result.summary
    result = critique_answer("What is caffeine? Does it help? Why?", _record())
    assert [s.verdict for s in result.sentences] == [NOT_A_CLAIM] * 3


def test_decimals_and_abbreviations_do_not_split():
    assert [s for s, _ in split_sentences("The dose was 3.5 mg per kg. Dr. Smith et al. found 1,200.5 cases in the U.S. alone. "
                                          "Was it e.g. large? Yes.")] == [
        "The dose was 3.5 mg per kg.", "Dr. Smith et al. found 1,200.5 cases in the U.S. alone.", "Was it e.g. large?", "Yes."]


def test_markdown_lists_and_numbering():
    answer = ("**Summary**\n- The effect faded after four hours.\n* Caffeine cures insomnia.\n"
              "1. The benefit is smaller in **habitual** coffee drinkers.\n2) See [the trial](" + URL_1 + ").")
    parts = split_sentences(answer)
    assert parts[0] == ("Summary", "heading")
    assert [t for t, _ in parts[1:4]] == ["The effect faded after four hours.", "Caffeine cures insomnia.",
                                          "The benefit is smaller in habitual coffee drinkers."]
    assert URL_1 in parts[4][0]
    verdicts = [s.verdict for s in critique_answer(answer, _record()).sentences]
    # the list cites a URL, so the format calls for one on every backed sentence
    assert verdicts[1:4] == [NO_CITATION, UNSUPPORTED, NO_CITATION]


# A sentence that paraphrases enough to overlap partly but not enough to be
# called supported by words alone.
AMBIGUOUS = ("The 2019 trial found 200mg of caffeine cut mean reaction time by 18ms, "
             "and the researchers recommended larger studies.")


def _model(reply):
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return reply if isinstance(reply, str) else json.dumps(reply)
    llm.calls = calls
    return llm


def test_ambiguous_sentence_without_a_model_stays_unsupported():
    (s,) = critique_answer(AMBIGUOUS, _record()).sentences
    assert s.verdict == UNSUPPORTED and "no model" in s.reason


def test_ambiguous_sentence_with_a_verifying_quote_is_supported():
    quote = "A 2019 randomized trial of 120 adults found that 200mg of caffeine cut mean reaction time by 18ms."
    llm = _model({"verdicts": [{"n": 1, "supported": True, "quote": quote}]})
    (s,) = critique_answer(AMBIGUOUS, _record(), llm).sentences
    assert s.verdict == SUPPORTED and s.decided_by == "model" and s.quote == quote
    assert s.evidence_ids and s.evidence_ids[0].startswith("pas-")
    assert "SENTENCE 1" in llm.calls[0] and PASSAGE_1 in llm.calls[0]


def test_ambiguous_sentence_with_a_fabricated_quote_is_demoted():
    llm = _model({"verdicts": [{"n": 1, "supported": True,
                                "quote": "caffeine improved reaction times by twenty percent in every participant"}]})
    (s,) = critique_answer(AMBIGUOUS, _record(), llm).sentences
    assert s.verdict == UNSUPPORTED and s.decided_by == "model_demoted"
    assert "does not appear in any stored passage" in s.reason


def test_a_real_quote_that_does_not_bear_on_the_sentence_is_demoted():
    llm = _model({"verdicts": [{"n": 1, "supported": True, "quote": PASSAGE_2}]})
    (s,) = critique_answer(AMBIGUOUS, _record(), llm).sentences
    assert s.verdict == UNSUPPORTED and s.decided_by == "model_demoted"


def test_model_saying_unsupported_or_failing_never_upgrades():
    (s,) = critique_answer(AMBIGUOUS, _record(), _model({"verdicts": [{"n": 1, "supported": False}]})).sentences
    assert s.verdict == UNSUPPORTED
    (s,) = critique_answer(AMBIGUOUS, _record(), _model("not json at all")).sentences
    assert s.verdict == UNSUPPORTED and s.decided_by == "model"

    def boom(_prompt):
        raise RuntimeError("model down")
    result = critique_answer(f"Caffeine cures insomnia. {AMBIGUOUS} The effect faded after four hours.", _record(), boom)
    assert "model down" in result.model_error and "model check failed" in result.summary
    assert _by_text(result, "faded").verdict == SUPPORTED  # the deterministic verdicts survive


def test_the_model_is_not_asked_about_clear_cases():
    llm = _model({"verdicts": []})
    critique_answer("Caffeine cures insomnia. The effect faded after four hours.", _record(), llm)
    assert llm.calls == []


def test_works_over_the_research_graph(tmp_path):
    g = GraphStore(tmp_path / "g.db")
    ids = sync_record(g, _record())
    result = critique_answer("Caffeine cures insomnia. The effect faded after four hours.", ids["question"], store=g)
    assert [s.verdict for s in result.sentences] == [UNSUPPORTED, SUPPORTED]
    assert any(i.startswith("pas-") for i in result.sentences[1].evidence_ids)
    with pytest.raises(KeyError):
        critique_answer("x y z.", "no-such-question", store=g)


def test_rejected_claims_do_not_back_a_sentence():
    record = _record()
    record.reject_claim(1, "superseded")
    (s,) = critique_answer("The benefit is smaller in habitual coffee drinkers.", record).sentences
    assert s.verdict == UNSUPPORTED


class TestTool:
    @pytest.fixture(autouse=True)
    def _dbs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rpt, "default_db", lambda: tmp_path / "research.db")
        monkeypatch.setattr(graph_store, "default_db", lambda: tmp_path / "research.db")
        monkeypatch.setattr("dourmouse.research_pipeline.hypotheses.default_complete",
                            lambda: (lambda prompt: '{"verdicts": []}'))
        self.g = GraphStore(tmp_path / "research.db")

    def test_registered_read_only_and_regular(self):
        from dourmouse.dispatch import DispatchRegistry, Permission

        tool = next(t for t in rpt.build_research_pipeline_subagent(DispatchRegistry()).tools
                    if t.name == "research_critique_answer")
        assert tool.permission is Permission.REGULAR
        assert tool.parameters["required"] == ["question_id"]

    def test_checks_the_synthesized_answer_by_default(self):
        record = _record()
        record.stage = record.stage.__class__.EVIDENCE_EXTRACTED
        record.set_synthesis(f"Caffeine cut mean reaction time by 18ms ({URL_1}). Caffeine cures insomnia.")
        ids = sync_record(self.g, record)
        out = rpt._research_critique_answer_tool({"question_id": ids["question"]})
        assert "UNSUPPORTED" in out and "insomnia" in out and "cut mean reaction time" not in out.split("\n", 1)[1]

    def test_accepts_a_given_answer_and_a_question_text(self):
        record = _record()
        rpt.ResearchStore(rpt.default_db()).save(record, now=1.0)
        out = rpt._research_critique_answer_tool({"question_id": record.question, "answer": "Caffeine cures insomnia."})
        assert "UNSUPPORTED" in out

    def test_honest_errors(self):
        assert rpt._research_critique_answer_tool({}).startswith("ERROR")
        assert rpt._research_critique_answer_tool({"question_id": "nope"}).startswith("ERROR")
        ids = sync_record(self.g, _record())
        assert rpt._research_critique_answer_tool({"question_id": ids["question"]}).startswith("Not done")
