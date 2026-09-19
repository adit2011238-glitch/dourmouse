"""Study-engine tests: held-out enforcement, phase completion, dossier."""

from __future__ import annotations

from ..brain import MockBrain
from ..core import AgentRecord, Status, StudyPhase
from ..study import StudyEngine, iteration_order


def test_study_sources_hold_out_unpassed_papers(corpus) -> None:
    """Study sources must never include a not-yet-passed paper or its key."""
    engine = StudyEngine(corpus, MockBrain())
    sources, _ = engine.study_sources(passed_ids=set())
    names = {s.name for s in sources}
    assert names == set()  # nothing passed yet -> no papers allowed

    sources, _ = engine.study_sources(passed_ids={"qual_2023.pdf"})
    names = {s.name for s in sources}
    assert "qual_2023.pdf" in names          # passed paper allowed (post-hoc)
    assert "qual_2024.pdf" not in names      # future paper held out


def test_mock_cannot_answer_held_out_paper(corpus) -> None:
    """A regurgitation brain that studied only allowed sources fails an unseen exam."""
    engine = StudyEngine(corpus, MockBrain())
    brain = engine.brain
    sources, concepts = engine.study_sources(passed_ids=set())
    brain.study(sources, concepts)
    # Question references a held-out paper the brain never saw.
    ans = brain.answer("Solve the problems in 'qual_2023.pdf'")
    assert ans.text == "" and ans.citations == ()


def test_study_run_completes_to_ready_with_dossier(corpus) -> None:
    engine = StudyEngine(corpus, MockBrain())
    r = AgentRecord(domain="Mathematics", field="Algebra")
    r.start_study()
    dossier = engine.run(r, now=0.0)
    assert r.status is Status.READY
    assert r.study_minutes_used == 300
    assert dossier is not None and dossier.complete
    # Both papers were held out during study (nothing was passed yet).
    assert set(dossier.held_out) == {"qual_2023.pdf", "qual_2024.pdf"}


def test_iteration_order_is_chronological(corpus) -> None:
    ids = [p.id for p in iteration_order(list(corpus.papers))]
    assert ids == ["qual_2023.pdf", "qual_2024.pdf"]


def test_open_book_study_teaches_all_papers(corpus) -> None:
    """Demo mode: mock can now answer every paper (keys studied last)."""
    engine = StudyEngine(corpus, MockBrain())
    r = AgentRecord(domain="Mathematics", field="Algebra")
    r.start_study()
    engine.open_book_study(r)
    assert r.status is Status.READY
    assert r.dossier is not None and r.dossier.held_out == ()
    ans = engine.brain.answer("Solve the problems in 'qual_2023.pdf'")
    assert "homomorphisms" in ans.text
    ans2 = engine.brain.answer("Solve the problems in 'qual_2024.pdf'")
    assert "ideals" in ans2.text


def test_phase_progression(corpus) -> None:
    engine = StudyEngine(corpus, MockBrain())
    r = AgentRecord(domain="Mathematics", field="Algebra")
    r.start_study()
    engine.run(r, now=0.0)
    # All five phases ran; the record ends READY with no dangling phase.
    assert r.phase is None
    assert r.study_minutes_used == sum(p.budget_min for p in StudyPhase)
