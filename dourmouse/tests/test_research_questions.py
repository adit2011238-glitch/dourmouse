"""PL-10 T1 step 5: questions set after reading, edited and approved by the owner."""

from __future__ import annotations

import json

import pytest

from dourmouse.research_pipeline import questions as Q
from dourmouse.research_pipeline.cards import Finding, Grade, PaperCard
from dourmouse.research_pipeline.papers import Paper


def _card(cid, title, finding=True):
    g = Grade(70, "B", "journal", 10, 1.0, 2020, "pdf")
    fs = (Finding("f-1", "Spacing improved recall by 10 points.", "spacing improved recall by ten points", "Results"),) if finding else ()
    return PaperCard(cid, Paper(title=title, year=2020, doi=f"10.1/{cid}"), "pdf", "", "", "", "", fs, (), "", g, 1)


CARDS = [_card("card-a", "Spacing in classrooms"), _card("card-b", "Spacing in the lab")]


def _complete(reply):
    calls = []

    def complete(prompt):
        calls.append(prompt)
        return json.dumps(reply) if isinstance(reply, dict) else reply

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def test_derive_keeps_known_cards_and_flags_a_short_list():
    reply = {"question": "How should students space their revision?", "goals": ["Practical schedule"],
             "sub_questions": [{"text": "Does spacing help in real classrooms?", "why": "lab and class disagree",
                                "cards": ["card-a", "card-zzz"]},
                               "What gap length is best for a test one week away?"]}
    complete = _complete(reply)
    qs = Q.derive_questions("spacing effect", None, CARDS, complete)
    assert qs.question == "How should students space their revision?" and qs.status == "draft"
    assert qs.sub_questions[0].cards == ("card-a",)
    assert qs.sub_questions[1].text.startswith("What gap length")
    assert "add more before approving" in qs.note
    assert "[card-a] Spacing in classrooms (2020), grade B" in complete.calls[0]


def test_derive_caps_at_six_and_says_when_there_is_no_grounding():
    reply = {"question": "q", "sub_questions": [f"sub {i}" for i in range(9)]}
    qs = Q.derive_questions("t", None, [_card("card-a", "x", finding=False)], _complete(reply))
    assert len(qs.sub_questions) == 6
    assert "first 6 were kept" in qs.note and "rest on the brief alone" in qs.note


def test_owner_edit_validates_and_keeps_known_reasons():
    qs = Q.derive_questions("t", None, CARDS, _complete(
        {"question": "q", "sub_questions": [{"text": "A?", "why": "gap"}, {"text": "B?"}, {"text": "C?"}]}))
    edited = Q.edit(qs, {"question": "My question?", "goals": ["g1"], "sub_questions": ["A?", "D?", "A?"]})
    assert edited.edited_by_owner and [s.text for s in edited.sub_questions] == ["A?", "D?"]
    assert edited.sub_questions[0].why == "gap" and edited.sub_questions[1].why == "added by the owner"
    for bad in ({"question": "", "sub_questions": ["x"]}, {"question": "q", "sub_questions": []},
                {"question": "q", "sub_questions": ["x"] * 2 + [str(i) for i in range(6)]},
                {"question": "q" * 401, "sub_questions": ["x"]},
                {"question": "q", "sub_questions": ["x"], "goals": ["g"] * 6}):
        with pytest.raises(ValueError):
            Q.edit(qs, bad)


def test_approve_freezes_the_set():
    qs = Q.derive_questions("t", None, CARDS, _complete({"question": "q", "sub_questions": ["a", "b", "c"]}))
    approved = Q.approve(qs, clock=lambda: 7.0)
    assert approved.status == "approved" and approved.approved_at == 7.0
    with pytest.raises(ValueError, match="already approved"):
        Q.edit(approved, {"question": "x", "sub_questions": ["y"]})
    with pytest.raises(ValueError):
        Q.approve(Q.QuestionSet(question="q"))
    assert Q.QuestionSet.from_dict(json.loads(json.dumps(approved.to_dict()))) == approved
