"""Exam engine: build iterations, grade attempts, enforce the citation gate.

Grading design (why deterministic, why fail-closed):

- Where the archive publishes an official key, scoring is exact: both the
  answer and the key are normalized (lowercase, alnum tokens, stopwords
  removed) and the score is the fraction of the key's significant tokens
  present in the answer. Pass threshold 0.9. No LLM judgment anywhere in the
  pass/fail decision.
- Every answer also passes through the *citation gate*: each citation must
  resolve to a real file in the field's corpus (by filename or URL substring).
  A fabricated citation fails the attempt no matter how good the prose is —
  this is the anti-hallucination spine of the qualification.
- Where no key exists, exact grading is impossible; the attempt is graded by
  the citation gate plus a completeness floor, and the feedback says plainly
  that no official key was published (honest, not invented).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .brain import BrainAnswer
from .core import ExamAttempt
from .study import FieldCorpus, Paper, grade_answer, iteration_order

# Pass threshold for key-graded iterations (fraction of key tokens covered).
PASS_THRESHOLD = 0.9
# Minimum answer length (chars) for citation-gated iterations with no key.
MIN_ANSWER_CHARS = 120


@dataclass(frozen=True)
class Iteration:
    paper: Paper
    question: str
    has_key: bool


@dataclass(frozen=True)
class Grade:
    score: float
    passed: bool
    citations_verified: bool
    fabricated_citations: int
    feedback: str


def build_iterations(corpus: FieldCorpus) -> list[Iteration]:
    """All exam iterations for a field, oldest paper first.

    The question embeds the paper id so a brain can address the paper by name;
    the mock matches on it, a real backend receives the real exam context.
    """
    out: list[Iteration] = []
    for p in iteration_order(list(corpus.papers)):
        q = f"Solve the problems in the real exam paper '{p.id}'"
        if p.url:
            q += f" (source: {p.url})"
        out.append(Iteration(paper=p, question=q + ".", has_key=p.has_key))
    return out


def pending_iterations(corpus: FieldCorpus, passed_ids: set[str]) -> list[Iteration]:
    return [it for it in build_iterations(corpus) if it.paper.id not in passed_ids]


def verify_citations(corpus: FieldCorpus, answer: BrainAnswer) -> tuple[bool, int]:
    """Every citation must resolve to a real corpus file (name or URL)."""
    if not answer.citations:
        return False, 0  # no citations at all -> gate fails (unless no-key mode)
    known: list[str] = []
    for p in corpus.papers:
        known.append(p.id)
        if p.url:
            known.append(p.url)
    known.extend(str(x) for x in corpus.landing_pages)
    bad = 0
    for c in answer.citations:
        if not any(c and (c in k or k in c) for k in known):
            bad += 1
    return bad == 0, bad


def grade_iteration(corpus: FieldCorpus, it: Iteration,
                    answer: BrainAnswer) -> Grade:
    ok_cites, bad = verify_citations(corpus, answer)
    if it.has_key and it.paper.key_path is not None:
        key_text = it.paper.key_path.read_text(errors="replace")
        score = grade_answer(answer.text, key_text)
        passed = score >= PASS_THRESHOLD and ok_cites and bad == 0
        feedback = (
            f"key-graded: {score:.2f} vs threshold {PASS_THRESHOLD}; "
            f"citations verified={ok_cites} (fabricated={bad})"
        )
        return Grade(score, passed, ok_cites, bad, feedback)

    # No official key: citation gate + completeness floor, honestly labeled.
    complete = len(answer.text.strip()) >= MIN_ANSWER_CHARS
    passed = ok_cites and complete
    score = 1.0 if passed else 0.0
    feedback = (
        "no official key published for this paper; graded by citation gate "
        f"(verified={ok_cites}, fabricated={bad}) and completeness "
        f"({len(answer.text.strip())} chars >= {MIN_ANSWER_CHARS})"
    )
    return Grade(score, passed, ok_cites, bad, feedback)


def take_exam(corpus: FieldCorpus, it: Iteration, brain, attempt_no: int) -> ExamAttempt:
    """Ask the brain, grade, and return the recorded attempt."""
    answer = brain.answer(it.question)
    g = grade_iteration(corpus, it, answer)
    return ExamAttempt(
        iteration_id=it.paper.id,
        attempt=attempt_no,
        score=g.score,
        passed=g.passed,
        citations_verified=g.citations_verified,
        fabricated_citations=g.fabricated_citations,
        feedback=g.feedback,
        transcript=(it.question, answer.text, " | ".join(answer.citations)),
    )
