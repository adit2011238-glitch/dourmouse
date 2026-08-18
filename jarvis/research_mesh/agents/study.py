"""The 5-hour study window: phases, held-out enforcement, dossier output.

Why this shape:

- The study window is timeboxed (STUDY_BUDGET_MIN = 300 study-minutes) and
  split into the five phases defined in core.py. Each phase runs at most once,
  in order, so a killed pipeline resumes without redoing completed phases
  (record.phase marks the current position).
- *Held-out enforcement*: study sources are the field's real files, minus every
  paper (and its key) that will be used as an exam iteration and has not yet
  been passed. An agent can therefore never ingest the answers to an exam it
  has not yet taken. Papers already passed may be studied post-hoc — that is
  how real students learn from graded exams and how remediation works.
- The output is a durable StudyDossier whose concept cards, practice scores and
  held-out list are all derived from real inputs — nothing is fabricated.

The engine is deterministic and clock/brain-injectable: a real backend simply
performs the same study() calls against real sources; the MockBrain does it
instantly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .brain import Brain
from .core import (
    PHASE_ORDER,
    ConceptCard,
    Status,
    StudyDossier,
    StudyPhase,
    STUDY_BUDGET_MIN,
)

_YEAR = re.compile(r"(19|20)\d{2}")


@dataclass(frozen=True)
class Paper:
    """One real exam paper in a field's corpus."""

    id: str                 # readable filename (also used as iteration id)
    path: Path
    url: str
    has_key: bool
    key_path: Path | None


@dataclass
class FieldCorpus:
    domain: str
    field: str
    folder: Path
    papers: tuple[Paper, ...] = ()
    landing_pages: tuple[Path, ...] = ()

    def paper_by_id(self, paper_id: str) -> Paper | None:
        for p in self.papers:
            if p.id == paper_id:
                return p
        return None


_KEY_SUFFIX = re.compile(
    r"(?:[-_\s]*)(?:sol|sols|solution|solutions|key|keys|answer|answers|official\s*key|answer\s*key)$",
    re.I,
)


def _stem(name: str) -> str:
    """Normalized stem of a filename: lowercase alnum tokens, solution suffixes cut."""
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    stem = _KEY_SUFFIX.sub("", stem)
    return re.sub(r"[^a-z0-9]+", "", stem.lower())


def load_corpus(papers_root: Path, domain: str, field: str) -> FieldCorpus:
    """Load a field's materialized corpus (tests/ + keys/ folders).

    A paper is a tests/*.pdf file; its key is the matching file in keys/ whose
    normalized stem equals the paper's (so 'AlgebraFall2025.pdf' pairs with
    'AlgebraFall2025Sols.pdf'). Landing pages (.html) are kept as studyable
    non-answer material.
    """
    folder = papers_root / domain / field
    tests_dir, keys_dir = folder / "tests", folder / "keys"
    def _real(p: Path) -> bool:
        return not p.name.startswith("._")  # skip macOS AppleDouble companions

    papers: list[Paper] = []
    if tests_dir.exists():
        key_by_stem: dict[str, Path] = {}
        if keys_dir.exists():
            for k in keys_dir.glob("*.pdf"):
                if _real(k):
                    key_by_stem.setdefault(_stem(k.name), k)
        for t in sorted(tests_dir.glob("*.pdf")):
            if not _real(t):
                continue
            key = key_by_stem.get(_stem(t.name))
            papers.append(Paper(
                id=t.name, path=t, url="", has_key=key is not None, key_path=key,
            ))
    landing = tuple(sorted(p for p in folder.glob("*.html")))
    return FieldCorpus(domain=domain, field=field, folder=folder,
                       papers=tuple(papers), landing_pages=landing)


def iteration_order(papers: list[Paper]) -> list[Paper]:
    """Chronological order: oldest first, so later papers stay held out longest.

    The year is read from the filename when present; files without a year sort
    after dated ones, alphabetically.
    """
    def key(p: Paper) -> tuple[int, int, str]:
        m = _YEAR.search(p.id)
        return (0, int(m.group(0)), p.id) if m else (1, 0, p.id)

    return sorted(papers, key=key)


class StudyEngine:
    """Runs (or resumes) the study window for one field-agent."""

    def __init__(self, corpus: FieldCorpus, brain: Brain,
                 budget_min: int = STUDY_BUDGET_MIN) -> None:
        self.corpus = corpus
        self.brain = brain
        self.budget_min = budget_min
        self._practice_scores: list[float] = []

    def study_sources(self, passed_ids: set[str]) -> tuple[list[Path], list[str]]:
        """Sources allowed for study given the iterations already passed.

        Held out: every paper (and key) not yet passed. Allowed: landing pages
        (never answer material), passed papers and their keys (post-hoc), and
        the concept list.
        """
        sources = list(self.corpus.landing_pages)
        for p in self.corpus.papers:
            if p.id in passed_ids:
                sources.append(p.path)
                if p.key_path is not None:
                    sources.append(p.key_path)
        concepts = [self.corpus.field]
        return sources, concepts

    def run(self, record, now: float) -> StudyDossier | None:
        """Execute any not-yet-run phases; returns the dossier when complete.

        `record` is an AgentRecord in STUDYING status; its .phase marks the
        current phase. After the final phase the dossier is built and the
        record is left READY via record.complete_study().
        """
        if record.status is not Status.STUDYING:
            raise ValueError(f"study engine requires STUDYING, got {record.status.name}")

        passed_ids = set(record.passed_iterations)
        sources, concepts = self.study_sources(passed_ids)
        held_out = tuple(p.id for p in self.corpus.papers if p.id not in passed_ids)

        # Phases that have not run yet, in order.
        start = record.phase or StudyPhase.CURRICULUM
        todo = PHASE_ORDER[PHASE_ORDER.index(start):]

        for phase in todo:
            record.phase = phase
            if phase is StudyPhase.CURRICULUM:
                self.brain.study([], concepts)
            elif phase is StudyPhase.INGESTION:
                self.brain.study(sources, concepts)
            elif phase is StudyPhase.PRACTICE:
                self._practice(record, passed_ids)
            elif phase is StudyPhase.GAP_CLOSURE:
                self._gap_closure(record, sources)
            elif phase is StudyPhase.CONSOLIDATION:
                pass  # dossier assembled below
            record.study_minutes_used = min(
                record.study_minutes_used + phase.budget_min, self.budget_min)

        record.study_minutes_used = self.budget_min
        dossier = self._build_dossier(held_out)
        record.complete_study(dossier)
        return dossier

    def open_book_study(self, record) -> StudyDossier:
        """Demo mode: study the full corpus (including all keys), then quiz on
        every paper. Held-out list is empty because nothing is withheld — this
        is explicitly NOT the strict qualification rule; it exists so a mock
        brain can dry-run the full state machine to QUALIFIED."""
        sources: list[Path] = list(self.corpus.landing_pages)
        for p in self.corpus.papers:
            sources.append(p.path)
            if p.key_path is not None:
                sources.append(p.key_path)
        self.brain.study(sources, [self.corpus.field])
        self._practice_all()
        record.study_minutes_used = self.budget_min
        record.complete_study(self._build_dossier(()))
        return record.dossier  # type: ignore[return-value]

    # -- phase internals --------------------------------------------------

    def _practice(self, record, passed_ids: set[str]) -> None:
        """Self-quiz on already-passed papers; scores go into the dossier."""
        for p in self.corpus.papers:
            if p.id not in passed_ids or p.key_path is None:
                continue
            ans = self.brain.answer(p.id)
            if not ans.text:
                continue
            key_text = p.key_path.read_text(errors="replace")
            self._practice_scores.append(grade_answer(ans.text, key_text))

    def _practice_all(self) -> None:
        """Self-quiz on every keyed paper (open-book demo mode)."""
        for p in self.corpus.papers:
            if p.key_path is None:
                continue
            ans = self.brain.answer(p.id)
            if ans.text:
                key_text = p.key_path.read_text(errors="replace")
                self._practice_scores.append(grade_answer(ans.text, key_text))

    def _gap_closure(self, record, sources: list[Path]) -> None:
        # Re-ingest the same allowed sources: cheap for the mock, targeted
        # re-retrieval for a real backend.
        self.brain.study(sources, [self.corpus.field])

    def _build_dossier(self, held_out: tuple[str, ...]) -> StudyDossier:
        cards = (ConceptCard(
            concept=self.corpus.field,
            summary=f"studied from {len(self.corpus.landing_pages)} program pages "
                    f"and {len(self.corpus.papers)} papers ({len(held_out)} held out)",
            confidence=0.0,  # never guessed; set post-practice in real backends
        ),)
        return StudyDossier(
            field=self.corpus.field, domain=self.corpus.domain,
            concept_cards=cards,
            canonical_literature=tuple(p.id for p in self.corpus.papers),
            practice_scores=tuple(self._practice_scores),
            held_out=held_out,
            complete=True,
        )


def grade_answer(answer: str, expected: str) -> float:
    """Deterministic answer-vs-key score in [0, 1].

    Normalizes both texts (lowercase, alnum only, collapsed spaces) and scores
    the fraction of the key's significant tokens present in the answer. This is
    a strict, explainable grader — no LLM judgment on pass/fail.
    """
    import re as _re

    def norm(s: str) -> set[str]:
        toks = _re.findall(r"[a-z0-9]+", s.lower())
        stop = {"the", "and", "for", "are", "was", "with", "that", "this",
                "from", "have", "has", "not", "its", "all", "but", "you",
                "your", "will", "can", "may", "of", "to", "in", "on", "at"}
        return {t for t in toks if t not in stop and len(t) > 1}

    a, e = norm(answer), norm(expected)
    if not e:
        return 1.0 if not answer.strip() else 0.0
    return len(a & e) / len(e)
