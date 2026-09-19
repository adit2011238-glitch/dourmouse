"""Exam-engine tests: deterministic grading and the citation gate."""

from __future__ import annotations

from ..brain import BrainAnswer, MockBrain
from ..exams import build_iterations, grade_iteration, take_exam
from ..study import grade_answer


def test_grade_answer_exact_and_partial() -> None:
    assert grade_answer("homomorphisms kernels quotient groups",
                        "homomorphisms kernels quotient groups") == 1.0
    # Partial coverage: 2 of the key's 4 significant tokens present.
    s = grade_answer("homomorphisms groups", "homomorphisms kernels quotient groups")
    assert s == 0.5
    # Case/punctuation-insensitive.
    assert grade_answer("HOMOMORPHISMS, Kernels! quotient GROUPS",
                        "homomorphisms kernels quotient groups") == 1.0
    # Empty answer never passes a keyed exam.
    assert grade_answer("", "homomorphisms kernels") == 0.0


def test_fabricated_citation_fails_despite_good_text(corpus) -> None:
    it = build_iterations(corpus)[0]
    answer = BrainAnswer(
        text="homomorphisms kernels quotient groups",
        citations=("made-up-paper.pdf",),  # not in the corpus
    )
    grade = grade_iteration(corpus, it, answer)
    assert grade.fabricated_citations == 1
    assert not grade.citations_verified
    assert not grade.passed  # citation gate is hard


def test_no_citations_fails_keyed_exam(corpus) -> None:
    it = build_iterations(corpus)[0]
    grade = grade_iteration(corpus, it, BrainAnswer(
        text="homomorphisms kernels quotient groups", citations=()))
    assert not grade.citations_verified
    assert not grade.passed


def test_verified_keyed_answer_passes(corpus) -> None:
    it = build_iterations(corpus)[0]
    grade = grade_iteration(corpus, it, BrainAnswer(
        text="homomorphisms kernels quotient groups",
        citations=(it.paper.id,)))
    assert grade.passed and grade.score >= 0.9


def test_no_key_mode_requires_completeness_and_citations(no_key_corpus) -> None:
    it = build_iterations(no_key_corpus)[0]
    # Short answer: completeness floor fails.
    short = grade_iteration(no_key_corpus, it, BrainAnswer(
        text="short", citations=(it.paper.id,)))
    assert not short.passed
    # Long answer with verified citation passes (honestly labeled no-key).
    long = grade_iteration(no_key_corpus, it, BrainAnswer(
        text="module theory " * 30, citations=(it.paper.id,)))
    assert long.passed
    assert "no official key" in long.feedback
    # Long answer but fabricated citation: still fails.
    bad = grade_iteration(no_key_corpus, it, BrainAnswer(
        text="module theory " * 30, citations=("fake.pdf",)))
    assert not bad.passed


def test_take_exam_records_transcript(corpus) -> None:
    brain = MockBrain()
    it = build_iterations(corpus)[0]
    # Brain has not studied: cannot answer -> failed attempt, honest feedback.
    attempt = take_exam(corpus, it, brain, attempt_no=1)
    assert not attempt.passed
    assert attempt.iteration_id == it.paper.id
    assert len(attempt.transcript) == 3
