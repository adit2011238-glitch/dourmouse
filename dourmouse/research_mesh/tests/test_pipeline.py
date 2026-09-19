"""Pipeline tests: end-to-end qualification, exclusion, resume, no-backend."""

from __future__ import annotations

import pytest

from ..brain import Brain, BrainNotConfigured, MockBrain, NotConfiguredBrain
from ..core import AgentRecord, Status
from ..store import AgentStore
from ..pipeline import QualificationPipeline


def _run(store, brain, corpus, open_book=False) -> tuple[AgentRecord, QualificationPipeline]:
    rec = store.load("Mathematics", "Algebra") or AgentRecord(
        domain="Mathematics", field="Algebra")
    pipe = QualificationPipeline(store, brain, corpus, open_book=open_book)
    pipe.run(rec)
    return rec, pipe


def test_strict_mode_qualifies_via_remediation(tmp_path, corpus) -> None:
    """Held-out study -> first attempt fails -> remediate -> pass -> QUALIFIED."""
    store = AgentStore(tmp_path / "q.db")
    rec, _ = _run(store, MockBrain(), corpus)
    assert rec.status is Status.QUALIFIED
    assert len(rec.passed_iterations) == 2
    # Every iteration needed exactly one fail + remediation retry (strict).
    fails = [a for a in rec.history if not a.passed]
    assert len(fails) == 2
    # Transcript proves the held-out rule: first attempts were unanswerable.
    assert all("" == a.transcript[1] for a in fails)


def test_open_book_qualifies_first_try(tmp_path, corpus) -> None:
    store = AgentStore(tmp_path / "q.db")
    rec, _ = _run(store, MockBrain(), corpus, open_book=True)
    assert rec.status is Status.QUALIFIED
    # Clean sweep: every attempt passed on the first try.
    assert all(a.passed for a in rec.history)
    assert rec.dossier is not None and rec.dossier.held_out == ()


def test_persistent_defect_excludes_agent(tmp_path, corpus) -> None:
    class UnrecoverableBrain(MockBrain):
        def remediate(self, feedback, sources) -> None:
            pass  # cannot be fixed -> max attempts must exclude the agent

    store = AgentStore(tmp_path / "q.db")
    rec, _ = _run(store, UnrecoverableBrain(defect_filenames={"qual_2023.pdf"}), corpus)
    assert rec.status is Status.NOT_QUALIFIED
    assert rec.attempts_on_iteration == 3


def test_resume_after_interrupt(tmp_path, corpus) -> None:
    """A killed pipeline resumes from the store and still qualifies."""
    store = AgentStore(tmp_path / "q.db")
    rec = AgentRecord(domain="Mathematics", field="Algebra")
    pipe = QualificationPipeline(store, MockBrain(), corpus)
    # Interrupt after a handful of steps (mid first iteration).
    for _ in range(3):
        pipe.step(rec)
    mid_status = rec.status
    assert mid_status not in (Status.QUALIFIED, Status.NOT_QUALIFIED)

    # Fresh process: new store handle, fresh brain, same db.
    store2 = AgentStore(tmp_path / "q.db")
    rec2 = store2.load("Mathematics", "Algebra")
    assert rec2 is not None and rec2.status == mid_status
    pipe2 = QualificationPipeline(store2, MockBrain(), corpus)
    pipe2.run(rec2)
    assert rec2.status is Status.QUALIFIED
    assert len(rec2.passed_iterations) == 2


def test_not_configured_brain_is_honest(tmp_path, corpus) -> None:
    store = AgentStore(tmp_path / "q.db")
    rec = AgentRecord(domain="Mathematics", field="Algebra")
    pipe = QualificationPipeline(store, NotConfiguredBrain(), corpus)
    with pytest.raises(BrainNotConfigured):
        pipe.run(rec)
    # Nothing was persisted as a success: no record exists yet.
    assert store.load("Mathematics", "Algebra") is None


def test_brain_interface_requires_backend(tmp_path, corpus) -> None:
    """A Brain with configured=False must not silently run."""
    class HalfBrain(Brain):
        configured = False

        def study(self, sources, concepts):
            return {}

        def answer(self, question):
            raise AssertionError("should never be asked")

        def remediate(self, feedback, sources):
            pass

    store = AgentStore(tmp_path / "q.db")
    rec = AgentRecord(domain="Mathematics", field="Algebra")
    pipe = QualificationPipeline(store, HalfBrain(), corpus)
    with pytest.raises(BrainNotConfigured):
        pipe.run(rec)
