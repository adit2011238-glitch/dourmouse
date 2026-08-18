"""Core lifecycle for the research-mesh qualification pipeline.

Every field-agent follows one deterministic lifecycle:

    UNLEARNED -> STUDYING -> READY -> TESTING -> PASSED -> TESTING -> ... -> QUALIFIED
                                    \-> FAILED -> REMEDIATING -> TESTING (same iteration)
                                          \-> after MAX_ATTEMPTS: NOT_QUALIFIED

Design notes (why this shape):

- The lifecycle is a *state machine*, not a script: any transition, study action,
  answer and grade is persisted (see store.py) so a killed pipeline resumes from
  the exact state it stopped in. No work is ever redone or double-counted.
- Iterations are taken strictly one at a time in chronological order (oldest
  first), so later papers stay held out longest — the anti-cheating spine of
  the whole system (see study.py for how held-out papers are enforced).
- FAILED is terminal for an attempt, but not for the agent: each failure sends
  it to REMEDIATING for a fixed focused budget, then back to TESTING on the
  *same* iteration. Only MAX_ATTEMPTS consecutive failures exclude the agent
  (NOT_QUALIFIED) — the agent is then removed from routing and its spec flagged.

This module is pure logic: no I/O, no clocks, no LLM. Everything is injectable
so the machine can be tested exhaustively and cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

# Maximum consecutive failed attempts on one iteration before the agent is
# permanently excluded from routing.
MAX_ATTEMPTS = 3
# Fixed focused remediation budget per failed attempt, in study-minutes.
REMEDIATION_BUDGET_MIN = 60
# Total study budget before the agent may start taking exams, in study-minutes.
STUDY_BUDGET_MIN = 300


class Status(Enum):
    UNLEARNED = auto()
    STUDYING = auto()
    READY = auto()
    TESTING = auto()
    FAILED = auto()
    PASSED = auto()
    REMEDIATING = auto()
    QUALIFIED = auto()
    NOT_QUALIFIED = auto()

    @property
    def terminal(self) -> bool:
        return self in (Status.QUALIFIED, Status.NOT_QUALIFIED)


class StudyPhase(Enum):
    """The five timeboxed phases of the 5-hour study window."""

    CURRICULUM = auto()      # 30 min  — build syllabus from field spec
    INGESTION = auto()       # 120 min — retrieve real sources, extract concepts
    PRACTICE = auto()        # 60 min  — self-quiz on held-out-excluded papers
    GAP_CLOSURE = auto()     # 60 min  — re-drill the lowest-confidence concepts
    CONSOLIDATION = auto()   # 30 min  — compile the Study Dossier + exam brief

    @property
    def budget_min(self) -> int:
        return {StudyPhase.CURRICULUM: 30, StudyPhase.INGESTION: 120,
                StudyPhase.PRACTICE: 60, StudyPhase.GAP_CLOSURE: 60,
                StudyPhase.CONSOLIDATION: 30}[self]


# All phases in order, with cumulative budgets (used to derive deadlines).
PHASE_ORDER = list(StudyPhase)


@dataclass(frozen=True)
class ConceptCard:
    """One concept the agent claims to have learned, with a confidence score."""

    concept: str
    summary: str
    confidence: float = 0.0  # 0..1, derived from practice scores, never guessed
    sources: tuple[str, ...] = ()  # real source URLs/ids backing this card


@dataclass(frozen=True)
class StudyDossier:
    """Durable, auditable output of the study window."""

    field: str
    domain: str
    concept_cards: tuple[ConceptCard, ...] = ()
    canonical_literature: tuple[str, ...] = ()
    worked_examples: tuple[str, ...] = ()
    practice_scores: tuple[float, ...] = ()
    held_out: tuple[str, ...] = ()  # paper ids excluded from study (tested later)
    complete: bool = False

    @property
    def mean_practice_score(self) -> float:
        if not self.practice_scores:
            return 0.0
        return sum(self.practice_scores) / len(self.practice_scores)


@dataclass
class ExamAttempt:
    """One graded attempt at one exam iteration."""

    iteration_id: str
    attempt: int
    score: float = 0.0            # 0..1 rubric score
    passed: bool = False
    citations_verified: bool = False
    fabricated_citations: int = 0
    feedback: str = ""
    transcript: tuple[str, ...] = ()  # q -> a pairs, persisted for audit


@dataclass
class AgentRecord:
    """The persisted, resumable state of one field-agent."""

    domain: str
    field: str
    status: Status = Status.UNLEARNED
    study_minutes_used: int = 0
    phase: Optional[StudyPhase] = None
    dossier: Optional[StudyDossier] = None
    current_iteration: Optional[str] = None
    attempts_on_iteration: int = 0
    passed_iterations: tuple[str, ...] = ()
    history: tuple[ExamAttempt, ...] = ()
    remediation_budget_left: int = 0

    # -- lifecycle transitions (validated, deterministic) -----------------

    def can_start_study(self) -> bool:
        return self.status is Status.UNLEARNED

    def start_study(self) -> None:
        if not self.can_start_study():
            raise ValueError(f"cannot start study from {self.status.name}")
        self.status = Status.STUDYING
        self.phase = StudyPhase.CURRICULUM

    def complete_study(self, dossier: StudyDossier) -> None:
        if self.status is not Status.STUDYING:
            raise ValueError(f"cannot complete study from {self.status.name}")
        if not dossier.complete:
            raise ValueError("study dossier is not marked complete")
        self.dossier = dossier
        self.phase = None
        self.status = Status.READY

    def can_start_exam(self) -> bool:
        # PASSED is included: passing one iteration flows straight into the next.
        return self.status in (Status.READY, Status.REMEDIATING, Status.PASSED)

    def start_iteration(self, iteration_id: str) -> None:
        if not self.can_start_exam():
            raise ValueError(f"cannot start an iteration from {self.status.name}")
        if iteration_id in self.passed_iterations:
            raise ValueError(f"iteration {iteration_id} already passed")
        # A *new* iteration resets the attempt counter; retrying the *same*
        # iteration after remediation must keep counting toward MAX_ATTEMPTS,
        # otherwise an agent could retry forever without ever being excluded.
        if iteration_id != self.current_iteration:
            self.attempts_on_iteration = 0
            self.current_iteration = iteration_id
        self.status = Status.TESTING

    def record_pass(self, attempt: ExamAttempt) -> None:
        if self.status is not Status.TESTING:
            raise ValueError(f"cannot record a pass from {self.status.name}")
        if not attempt.passed:
            raise ValueError("attempt must be marked passed")
        self._record_attempt(attempt)
        self.passed_iterations = self.passed_iterations + (attempt.iteration_id,)
        self.current_iteration = None
        self.attempts_on_iteration = 0
        self.status = Status.PASSED

    def record_fail(self, attempt: ExamAttempt) -> None:
        if self.status is not Status.TESTING:
            raise ValueError(f"cannot record a fail from {self.status.name}")
        if attempt.passed:
            raise ValueError("attempt must be marked failed")
        self._record_attempt(attempt)
        self.status = Status.FAILED

    def _record_attempt(self, attempt: ExamAttempt) -> None:
        self.history = self.history + (attempt,)
        self.attempts_on_iteration += 1

    def start_remediation(self) -> None:
        if self.status is not Status.FAILED:
            raise ValueError(f"cannot remediate from {self.status.name}")
        if self.attempts_on_iteration >= MAX_ATTEMPTS:
            raise ValueError("max attempts reached; agent must be excluded")
        self.remediation_budget_left = REMEDIATION_BUDGET_MIN
        self.status = Status.REMEDIATING

    def finish_remediation(self) -> None:
        if self.status is not Status.REMEDIATING:
            raise ValueError(f"cannot finish remediation from {self.status.name}")
        self.remediation_budget_left = 0
        # Back to TESTING on the *same* iteration, via READY's path.
        self.status = Status.READY

    def qualify(self) -> None:
        """Called when every available iteration has been passed."""
        if self.status not in (Status.PASSED, Status.READY):
            raise ValueError(f"cannot qualify from {self.status.name}")
        self.current_iteration = None
        self.status = Status.QUALIFIED

    def exclude(self) -> None:
        """Permanent exclusion: max attempts exhausted on an iteration."""
        if self.status is not Status.FAILED:
            raise ValueError(f"cannot exclude from {self.status.name}")
        if self.attempts_on_iteration < MAX_ATTEMPTS:
            raise ValueError("exclusion requires MAX_ATTEMPTS consecutive failures")
        self.current_iteration = None
        self.status = Status.NOT_QUALIFIED
