"""Core record for the structured research pipeline (Domain G). Pure
logic: no I/O, no clock, no model -- same discipline as research_mesh/
core.py (finding #033) and every other state-machine record in this
codebase. A ResearchRecord is a resumable, auditable unit: nothing is ever
deleted from it, only appended or status-transitioned, so a killed run
resumes cleanly and a rejected hypothesis or superseded claim stays
visible in the real record rather than vanishing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto


class Stage(Enum):
    """Real stages this pipeline currently implements. Hypothesis
    generation, experimental design, and a dedicated criticism/revision
    pass (named in this domain's own methodological reference, AI-
    Scientist, and in the founding spec's own research-network workstream)
    are real, separate, not-yet-built follow-on -- not silently folded
    into SYNTHESIZED, and not claimed here."""

    PLANNED = auto()
    SOURCES_DISCOVERED = auto()
    EVIDENCE_EXTRACTED = auto()
    SYNTHESIZED = auto()


@dataclass(frozen=True)
class Claim:
    """One real, sourced assertion. Field names match this domain's own
    spec verbatim: {claim, source_id, url, document_hash, location,
    passage, retrieved_at, agent} -- never a bare assertion with no
    traceable source. `passage` is the exact quoted text the claim was
    drawn from, never a paraphrase -- a paraphrase cannot be independently
    checked against the source the way a real quote can."""

    claim: str
    source_id: str
    url: str
    document_hash: str
    location: str
    passage: str
    retrieved_at: float
    agent: str
    # REJECTED means a later pass judged this claim unsupported or
    # superseded -- it is never deleted, only marked, so the record stays
    # honest about what was once believed and why it changed.
    status: str = "ACTIVE"  # "ACTIVE" | "REJECTED"
    # Real gap closed (2026-09-20): contradiction detection (harsh
    # acceptance test 2) needs to compare claims answering the SAME real
    # sub-question -- this was never recorded on the Claim itself, only
    # passed as an ephemeral index into extract_evidence(). Defaults to ""
    # so every pre-existing call site (tests, `reject_claim`'s own copy
    # below) keeps working unchanged; real callers set it going forward.
    sub_question: str = ""
    # R0-5 (finding #089): the URL the content actually came from after
    # redirects. `url` stays the discovered source; "" on claims made
    # before this existed.
    final_url: str = ""
    # R3 (finding #096): the Task whose work produced this claim. "" for the
    # original pipeline pass; a follow-up task's id for evidence gathered to
    # settle a contradiction after synthesis.
    task_id: str = ""


@dataclass(frozen=True)
class Contradiction:
    """Two real claims that answer the same real sub-question
    incompatibly. Surfaced in synthesis rather than one side being
    silently dropped -- this domain's own harsh acceptance test 2."""

    claim_a_id: str  # Claim.source_id + claim text hash, see store.py's fingerprint
    claim_b_id: str
    sub_question: str
    note: str = ""


_STAGE_ORDER = {s: i for i, s in enumerate(Stage)}


@dataclass(frozen=True)
class Task:
    """A unit of research work (R3, finding #096). The spec's backward edge,
    "contradiction discovered, new research task, new evidence, revised
    synthesis", is a Task spawned by a Contradiction, not the record's own
    stage moving backwards: the project only moves forward, the work grows."""

    task_id: str
    title: str
    stage: str  # the loop stage this task works at, e.g. "SOURCE_DISCOVERY"
    status: str = "OPEN"  # "OPEN" | "DONE"
    sub_question: str = ""
    spawned_by: str = ""  # contradiction_key() of the contradiction that caused it


def contradiction_key(c: Contradiction) -> str:
    return f"{c.claim_a_id}|{c.claim_b_id}|{c.sub_question}"


@dataclass
class ResearchRecord:
    """The persisted, resumable state of one research question."""

    question: str
    stage: Stage = Stage.PLANNED
    plan: tuple[str, ...] = ()  # real sub-questions, from the plan stage
    sources: tuple[str, ...] = ()  # real URLs discovered, from source discovery
    claims: tuple[Claim, ...] = ()
    contradictions: tuple[Contradiction, ...] = ()
    synthesis: str = ""
    tasks: tuple[Task, ...] = ()
    # Every synthesis this record has ever had, oldest first; `synthesis`
    # is the current one. A revised synthesis never erases the earlier one.
    synthesis_history: tuple[str, ...] = ()

    def _advance(self, stage: Stage) -> None:
        """Stages only move forward (finding #096). Evidence added for a
        follow-up task after synthesis must not drag the record back."""
        if _STAGE_ORDER[stage] > _STAGE_ORDER[self.stage]:
            self.stage = stage

    def open_tasks(self) -> tuple[Task, ...]:
        return tuple(t for t in self.tasks if t.status == "OPEN")

    def _task(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        raise ValueError(f"no task {task_id!r}")

    def spawn_task_for(self, contradiction: Contradiction) -> Task:
        """The backward edge: one follow-up task per contradiction, spawned
        to find evidence that settles it. Idempotent."""
        key = contradiction_key(contradiction)
        if contradiction not in self.contradictions:
            raise ValueError("can only spawn work for a contradiction on this record")
        for t in self.tasks:
            if t.spawned_by == key:
                return t
        task = Task(
            task_id=f"followup-{len(self.tasks) + 1}",
            title=f"Settle the contradiction: {contradiction.note or contradiction.sub_question}",
            stage="SOURCE_DISCOVERY",
            sub_question=contradiction.sub_question,
            spawned_by=key,
        )
        self.tasks = self.tasks + (task,)
        return task

    def complete_task(self, task_id: str) -> None:
        self._task(task_id)  # raises for an unknown task
        self.tasks = tuple(replace(t, status="DONE") if t.task_id == task_id else t for t in self.tasks)

    def set_plan(self, sub_questions: list[str]) -> None:
        if self.stage is not Stage.PLANNED:
            raise ValueError(f"cannot set plan from stage {self.stage.name}")
        if not sub_questions:
            raise ValueError("plan must contain at least one real sub-question")
        self.plan = tuple(sub_questions)

    def add_sources(self, urls: list[str]) -> None:
        if not self.plan:
            raise ValueError("cannot discover sources before a real plan exists")
        seen = set(self.sources)
        new: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)  # dedupe within THIS batch too, not just against prior sources
                new.append(u)
        self.sources = self.sources + tuple(new)
        self._advance(Stage.SOURCES_DISCOVERED)

    def add_claim(self, claim: Claim) -> None:
        if claim.task_id:
            # Follow-up evidence: allowed at any stage, but only for a task
            # that is really open on this record.
            if self._task(claim.task_id).status != "OPEN":
                raise ValueError(f"task {claim.task_id} is not open")
        elif self.stage not in (Stage.SOURCES_DISCOVERED, Stage.EVIDENCE_EXTRACTED):
            raise ValueError(f"cannot add a claim from stage {self.stage.name}")
        self.claims = self.claims + (claim,)
        self._advance(Stage.EVIDENCE_EXTRACTED)

    def reject_claim(self, index: int, reason: str) -> None:
        """Never deletes -- replaces the claim at `index` with a REJECTED
        copy carrying the same real provenance, so the record stays
        honest about what was once claimed and why it was dropped."""
        # replace(), not a hand-listed copy: a field added to Claim later
        # (final_url, finding #089) must survive rejection too.
        rejected = replace(self.claims[index], status="REJECTED")
        self.claims = self.claims[:index] + (rejected,) + self.claims[index + 1:]

    def add_contradiction(self, contradiction: Contradiction) -> None:
        """Idempotent, in either claim order: re-running detection must not
        record the same disagreement twice (finding #096)."""
        flipped = replace(contradiction, claim_a_id=contradiction.claim_b_id, claim_b_id=contradiction.claim_a_id)
        known = {contradiction_key(c) for c in self.contradictions}
        if contradiction_key(contradiction) in known or contradiction_key(flipped) in known:
            return
        self.contradictions = self.contradictions + (contradiction,)

    def set_synthesis(self, text: str) -> None:
        """First synthesis from EVIDENCE_EXTRACTED; a REVISED synthesis from
        SYNTHESIZED once no follow-up task is still open (the end of the
        backward edge). Earlier syntheses are kept in synthesis_history."""
        if self.stage is Stage.SYNTHESIZED:
            if self.open_tasks():
                raise ValueError("cannot revise the synthesis while follow-up tasks are still open")
        elif self.stage is not Stage.EVIDENCE_EXTRACTED:
            raise ValueError(f"cannot synthesize from stage {self.stage.name}")
        if not text.strip():
            raise ValueError("synthesis must be real, non-empty text")
        self.synthesis = text
        self.synthesis_history = self.synthesis_history + (text,)
        self.stage = Stage.SYNTHESIZED

    def active_claims(self) -> tuple[Claim, ...]:
        return tuple(c for c in self.claims if c.status == "ACTIVE")
