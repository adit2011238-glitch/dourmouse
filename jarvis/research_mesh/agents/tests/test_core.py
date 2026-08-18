"""State-machine tests: transitions, retry accounting, exclusion."""

from __future__ import annotations

import pytest

from ..core import (
    MAX_ATTEMPTS,
    AgentRecord,
    ExamAttempt,
    Status,
    StudyDossier,
)


def _dossier() -> StudyDossier:
    return StudyDossier(field="Algebra", domain="Mathematics", complete=True)


def _rec() -> AgentRecord:
    return AgentRecord(domain="Mathematics", field="Algebra")


def _attempt(iteration: str, passed: bool) -> ExamAttempt:
    return ExamAttempt(iteration_id=iteration, attempt=1, score=1.0 if passed else 0.0,
                       passed=passed, citations_verified=passed,
                       fabricated_citations=0, feedback="ok")


def test_full_happy_path_qualifies() -> None:
    r = _rec()
    assert r.status is Status.UNLEARNED
    r.start_study()
    r.complete_study(_dossier())
    assert r.status is Status.READY
    r.start_iteration("qual_2023.pdf")
    assert r.status is Status.TESTING
    r.record_pass(_attempt("qual_2023.pdf", True))
    assert r.status is Status.PASSED
    assert r.passed_iterations == ("qual_2023.pdf",)
    r.start_iteration("qual_2024.pdf")
    r.record_pass(_attempt("qual_2024.pdf", True))
    r.qualify()
    assert r.status is Status.QUALIFIED
    assert r.status.terminal


def test_fail_then_remediate_keeps_attempt_counter() -> None:
    r = _rec()
    r.start_study()
    r.complete_study(_dossier())
    r.start_iteration("qual_2023.pdf")
    r.record_fail(_attempt("qual_2023.pdf", False))
    assert r.attempts_on_iteration == 1
    r.start_remediation()
    assert r.status is Status.REMEDIATING
    r.finish_remediation()
    assert r.status is Status.READY
    # Retaking the SAME iteration must not reset the counter.
    r.start_iteration("qual_2023.pdf")
    assert r.attempts_on_iteration == 1
    assert r.status is Status.TESTING


def test_exclusion_after_max_attempts() -> None:
    r = _rec()
    r.start_study()
    r.complete_study(_dossier())
    r.start_iteration("qual_2023.pdf")
    # Two fail -> remediate -> retake cycles, then a third fail.
    for _ in range(MAX_ATTEMPTS - 1):
        r.record_fail(_attempt("qual_2023.pdf", False))
        r.start_remediation()
        r.finish_remediation()
        r.start_iteration("qual_2023.pdf")
        assert r.attempts_on_iteration == _ + 1  # counter survives remediation
    r.record_fail(_attempt("qual_2023.pdf", False))
    assert r.attempts_on_iteration == MAX_ATTEMPTS
    # Three consecutive fails -> remediation is illegal, exclusion is the only move.
    with pytest.raises(ValueError):
        r.start_remediation()
    r.exclude()
    assert r.status is Status.NOT_QUALIFIED
    assert r.status.terminal


def test_new_iteration_resets_attempts() -> None:
    r = _rec()
    r.start_study()
    r.complete_study(_dossier())
    r.start_iteration("qual_2023.pdf")
    r.record_fail(_attempt("qual_2023.pdf", False))
    r.start_remediation()
    r.finish_remediation()
    r.start_iteration("qual_2023.pdf")
    assert r.attempts_on_iteration == 1
    # Now pass it; a NEW iteration starts fresh.
    r.record_pass(_attempt("qual_2023.pdf", True))
    r.start_iteration("qual_2024.pdf")
    assert r.attempts_on_iteration == 0


def test_invalid_transitions_raise() -> None:
    r = _rec()
    with pytest.raises(ValueError):
        r.complete_study(_dossier())  # not studying
    r.start_study()
    with pytest.raises(ValueError):
        r.start_study()  # already studying
    with pytest.raises(ValueError):
        r.start_iteration("x.pdf")  # not ready
    r.complete_study(_dossier())
    r.start_iteration("a.pdf")
    r.record_pass(_attempt("a.pdf", True))
    with pytest.raises(ValueError):
        r.start_iteration("a.pdf")  # already passed


def test_cannot_record_pass_on_failed_attempt() -> None:
    r = _rec()
    r.start_study()
    r.complete_study(_dossier())
    r.start_iteration("a.pdf")
    with pytest.raises(ValueError):
        r.record_pass(_attempt("a.pdf", False))  # passed flag must match
