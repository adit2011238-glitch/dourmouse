"""Qualification pipeline: orchestrates study -> test -> remediate -> qualify.

One `step()` performs exactly one atomic state transition and persists it, so
the pipeline can be killed at any point and resumed from the store. `run()`
loops steps until the agent reaches a terminal state (QUALIFIED /
NOT_QUALIFIED) or a step budget is exhausted.

Two run modes:

- Strict (default): study excludes every not-yet-passed paper and its key, so
  an agent can never ingest the answers to an exam it hasn't taken. A
  regurgitation-only brain therefore fails each first attempt and qualifies
  through the remediation loop — which is exactly what the strict rule should
  do, and it exercises the fail -> remediate -> retry path for real.
- Open-book (--open-book, demo only): study sources include the full corpus so
  a mock brain can qualify cleanly on first attempts. This exists purely to
  dry-run the state machine end-to-end; a real backend qualifies in strict
  mode by reasoning, not regurgitation.

CLI:

    python -m research_mesh.agents.pipeline --db /tmp/q.db \\
        --domain "Mathematics" \\
        --field "Algebra (abstract & commutative)" --mock
    python -m research_mesh.agents.pipeline --list --db /tmp/q.db
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from .brain import Brain, BrainNotConfigured, MockBrain, NotConfiguredBrain
from .core import MAX_ATTEMPTS, AgentRecord, Status
from .exams import pending_iterations, take_exam
from .store import AgentStore
from .study import FieldCorpus, StudyEngine, load_corpus

# Papers root: jarvis/research_mesh/fields/exams/papers
PAPERS_ROOT = Path(__file__).resolve().parent.parent.parent / "research_mesh" / "fields" / "exams" / "papers"

DEFAULT_DB = PAPERS_ROOT / "qualification.db"


class QualificationPipeline:
    """Step-driven orchestrator. Injectable clock + brain for hermetic tests."""

    def __init__(self, store: AgentStore, brain: Brain, corpus: FieldCorpus,
                 now: Callable[[], float] = time.time,
                 open_book: bool = False) -> None:
        self.store = store
        self.brain = brain
        self.corpus = corpus
        self.now = now
        self.open_book = open_book
        self.study_engine = StudyEngine(corpus, brain)

    # -- one atomic step --------------------------------------------------

    def step(self, record: AgentRecord) -> Status:
        """Advance the record by exactly one transition; persists on the way out."""
        if record.status.terminal:
            return record.status

        if record.status is Status.UNLEARNED:
            record.start_study()
            if self.open_book:
                self._open_book_study(record)
            else:
                self.study_engine.run(record, self.now())
            # study_engine.run() calls record.complete_study() -> READY

        elif record.status is Status.READY:
            pending = pending_iterations(self.corpus, set(record.passed_iterations))
            if not pending:
                record.qualify()
            else:
                record.start_iteration(pending[0].paper.id)

        elif record.status is Status.TESTING:
            assert record.current_iteration is not None
            it = next(x for x in pending_iterations(
                self.corpus, set(record.passed_iterations))
                if x.paper.id == record.current_iteration)
            attempt = take_exam(self.corpus, it, self.brain,
                                record.attempts_on_iteration + 1)
            if attempt.passed:
                record.record_pass(attempt)
            else:
                record.record_fail(attempt)

        elif record.status is Status.FAILED:
            if record.attempts_on_iteration >= MAX_ATTEMPTS:
                record.exclude()
            else:
                record.start_remediation()

        elif record.status is Status.REMEDIATING:
            assert record.current_iteration is not None
            paper = self.corpus.paper_by_id(record.current_iteration)
            sources = []
            if paper is not None:
                sources.append(paper.path)
                if paper.key_path is not None:
                    sources.append(paper.key_path)
            feedback = record.history[-1].feedback if record.history else ""
            self.brain.remediate(feedback, sources)
            record.finish_remediation()

        elif record.status is Status.PASSED:
            pending = pending_iterations(self.corpus, set(record.passed_iterations))
            if not pending:
                record.qualify()
            else:
                record.status = Status.READY  # internal: next step starts the exam

        self.store.save(record, self.now())
        return record.status

    # -- batch ------------------------------------------------------------

    def run(self, record: AgentRecord, max_steps: int = 1000) -> Status:
        if not self.brain.configured:
            raise BrainNotConfigured(
                "brain not configured: nothing was run or persisted")
        for _ in range(max_steps):
            if record.status.terminal:
                return record.status
            self.step(record)
        raise RuntimeError(
            f"step budget exhausted for {record.field} at {record.status.name}; "
            "resume with a higher budget or inspect the record")

    # -- open-book demo study ---------------------------------------------

    def _open_book_study(self, record: AgentRecord) -> None:
        """Demo mode: study the full corpus so a mock can pass first-try."""
        self.study_engine.open_book_study(record)


def _load_all_fields() -> list[tuple[str, str]]:
    m = json.loads((PAPERS_ROOT / "MANIFEST.json").read_text())
    return [(f["domain"], f["field"]) for f in m["fields"]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Research-mesh qualification pipeline")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--domain")
    ap.add_argument("--field")
    ap.add_argument("--mock", action="store_true",
                    help="use the deterministic MockBrain (no LLM needed)")
    ap.add_argument("--open-book", action="store_true",
                    help="demo: let study include the full corpus")
    ap.add_argument("--list", action="store_true", help="list all fields")
    args = ap.parse_args(argv)

    if args.list:
        for d, f in _load_all_fields():
            print(f"{d} / {f}")
        return 0
    if not args.domain or not args.field:
        ap.error("--domain and --field are required (or --list)")

    store = AgentStore(args.db)
    brain: Brain = MockBrain() if args.mock else NotConfiguredBrain()
    corpus = load_corpus(PAPERS_ROOT, args.domain, args.field)
    if not corpus.papers and not corpus.landing_pages:
        print(f"no corpus for {args.domain} / {args.field} "
              "(field has no papers and no landing pages)")
        return 1

    record = store.load(args.domain, args.field) or AgentRecord(
        domain=args.domain, field=args.field)
    pipe = QualificationPipeline(store, brain, corpus, open_book=args.open_book)
    try:
        final = pipe.run(record)
    except BrainNotConfigured as exc:
        print(f"NOT CONFIGURED: {exc}")
        print("run with --mock for an offline dry-run, or subclass Brain for a real backend")
        return 2

    print(f"\n=== {args.domain} / {args.field} ===")
    print(f"final status: {final.name}")
    print(f"iterations passed: {len(record.passed_iterations)}/{len(record.passed_iterations) + len(pending_iterations(corpus, set(record.passed_iterations)))}")
    print(f"study minutes used: {record.study_minutes_used}")
    for a in record.history:
        print(f"  [{'PASS' if a.passed else 'FAIL'}] {a.iteration_id} "
              f"score={a.score:.2f} citations_verified={a.citations_verified}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
