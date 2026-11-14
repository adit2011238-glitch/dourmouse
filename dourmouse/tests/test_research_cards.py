"""PL-10 T1 step 3: paper cards with verified quotes and deterministic grades."""

from __future__ import annotations

import json

from dourmouse.research_pipeline import cards as C
from dourmouse.research_pipeline.library_port import InterimLibrary
from dourmouse.research_pipeline.papers import Paper
from dourmouse.research_pipeline.reading import PaperText, Section

BODY = (
    "We asked whether the optimal gap between study sessions depends on how long information must be retained. "
    "Participants (N = 1354) studied 32 obscure facts and were tested after delays of up to one year. "
    "Learners were assigned to one of 26 gap and retention interval conditions in an online experiment. "
    "The optimal gap increased as the test delay increased, and final recall was 64% with the best gap. "
    "A limitation is that the facts were trivia items rather than classroom material."
)
READING = PaperText("doi:10.1/x", "pdf", "https://example.org/x.pdf", text=BODY,
                    sections=(Section("Results", 0, len(BODY)),), pages=((1, 0),))
PAPER = Paper(title="Spacing effects in learning", authors=("Nicholas J. Cepeda",), year=2008, doi="10.1/x",
              venue="Psychological Science", venue_type="journal", cited_by=900)

REPLY = {
    "question": {"text": "Does the optimal gap between study sessions depend on the retention interval?",
                 "quote": "whether the optimal gap between study sessions depends on how long information must be retained"},
    "method": {"text": "A laboratory eye-tracking study.", "quote": "participants wore eye trackers in the lab"},
    "data": {"text": "1354 participants learning 32 facts."},
    "findings": [
        {"text": "The optimal gap grew with the test delay and final recall reached 64%.",
         "quote": "The optimal gap increased as the test delay increased, and final recall was 64% with the best gap."},
        {"text": "Spacing helps.", "quote": "the"},
    ],
    "limitations": [{"text": "Trivia items, not classroom material.",
                     "quote": "the facts were trivia items rather than classroom material"}],
    "bearing": "Directly relevant: it measures how the best spacing depends on the retention interval.",
}


def _complete(*replies):
    calls: list[str] = []
    queue = list(replies)

    def complete(prompt: str) -> str:
        calls.append(prompt)
        return queue.pop(0)

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def test_card_keeps_only_fields_whose_quotes_are_in_the_paper():
    complete = _complete(json.dumps(REPLY))
    card, calls = C.make_card(PAPER, READING, "spacing effect", complete, now_year=2026, clock=lambda: 5.0)
    assert calls == 1
    assert card.question.startswith("Does the optimal gap")
    assert card.method == "" and card.data == ""
    assert len(card.findings) == 1
    f = card.findings[0]
    assert "64%" in f.text and f.location == "Results, PDF page 1"
    assert any("method: quote not found" in r for r in card.rejected)
    assert any("data: no quote given" in r for r in card.rejected)
    assert any("finding 2" in r and "too short" in r for r in card.rejected)
    assert card.quotes_checked == 6
    assert card.limitations[0].startswith("Trivia items")
    assert "TOPIC THE READER IS RESEARCHING: spacing effect" in complete.calls[0]


def test_grade_arithmetic_and_reasons():
    g = C.grade_paper(PAPER, read_from="pdf", findings=1, now_year=2026)
    # 40 + 20 journal + min(20, 6*log2(1+900/19)) = 20 + 0 (published 2008, 18 years) + 10 pdf
    assert (g.score, g.letter) == (90, "A")
    assert any("Psychological Science, a journal" in r for r in g.reasons)
    pre = Paper(title="t", year=2025, arxiv_id="2501.00001", venue_type="repository", cited_by=3)
    gp = C.grade_paper(pre, read_from="abstract", findings=0, now_year=2026)
    # 40 + 0 preprint + round(6*log2(1+1.5)) = 8 + 10 recent - 10 abstract - 15 no finding
    assert (gp.score, gp.letter, gp.status) == (33, "D", "preprint")
    unknown = C.grade_paper(Paper(title="t"), read_from="none", findings=0, now_year=2026)
    assert unknown.score == 0 and "citation count unknown" in " ".join(unknown.reasons)


def test_agreement_moves_the_grade_and_says_why():
    card, _ = C.make_card(PAPER, READING, "spacing", _complete(json.dumps(REPLY)), now_year=2026)
    up = C.regrade(card, agrees=2, disputes=0, now_year=2026)
    down = C.regrade(card, agrees=0, disputes=4, now_year=2026)
    assert up.grade.score == min(100, card.grade.score + 6)
    assert down.grade.score == card.grade.score - 15
    assert any("disputed by 4" in r for r in down.grade.reasons)


def test_abstract_only_and_preprint_limitations_are_added_by_the_platform():
    abstract = PaperText("arxiv:2501.00001", "abstract", "https://arxiv.org/abs/2501.00001", text=BODY,
                         sections=(Section("Abstract", 0, len(BODY)),))
    pre = Paper(title="t", year=2025, arxiv_id="2501.00001", venue_type="repository")
    card, _ = C.make_card(pre, abstract, "x", _complete(json.dumps({"findings": []})), now_year=2026)
    assert "Read from the abstract only" in card.limitations[0]
    assert "Preprint: not peer reviewed." in card.limitations
    assert "the abstract only" in card.reading_note or card.read_from == "abstract"


def test_a_non_json_reply_is_asked_once_more_then_the_card_is_empty_and_graded_down():
    complete = _complete("Sure, here is the card!", "still not json")
    card, calls = C.make_card(PAPER, READING, "x", complete, now_year=2026)
    assert calls == 2 and card.findings == ()
    assert "Reply with the JSON object only." in complete.calls[1]
    assert any("no finding could be quoted" in r for r in card.grade.reasons)


def test_nothing_to_read_costs_no_model_call():
    empty = PaperText("doi:10.1/z", "none", "https://doi.org/10.1/z", note="no abstract either")
    card, calls = C.make_card(Paper(title="z", doi="10.1/z"), empty, "x", _complete(), now_year=2026)
    assert calls == 0 and card.limitations == ("Nothing could be read: no open-access PDF and no abstract.",)


def test_card_round_trips_and_stores_each_finding_as_a_citable_passage(tmp_path):
    card, _ = C.make_card(PAPER, READING, "spacing", _complete(json.dumps(REPLY)), now_year=2026)
    assert C.PaperCard.from_dict(json.loads(json.dumps(card.to_dict()))) == card
    lib = InterimLibrary(tmp_path / "lib.db")
    stored = C.store_card(card, lib, job_id="g1")
    pid = stored.passage_ids[card.findings[0].id]
    cited = lib.cite(pid)
    assert cited["text"] == card.findings[0].quote
    assert cited["meta"]["kind"] == "finding" and cited["meta"]["job_id"] == "g1"
    assert lib.search("paper card spacing", filters={"meta": {"kind": "paper_card"}})
