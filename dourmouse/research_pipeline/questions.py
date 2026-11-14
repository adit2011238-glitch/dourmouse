"""Research question, goals and sub-questions (PL-10 T1, step 5).

Set AFTER reading, not before (the owner's method, step 3): the model is shown
the context brief (with the owner's corrections first) and every paper card,
and proposes one research question, its goals, and three to six sub-questions,
each saying which gap or disagreement among the cards it addresses and which
cards bear on it. Card ids it names that do not exist are dropped.

Nothing runs on the proposal until the owner approves it. The owner can rewrite
any of it first; ``edit`` validates the owner's version (a question, one to six
sub-questions, at most five goals, sane lengths) and ``approve`` freezes it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .brief import Brief
from .cards import PaperCard

Complete = Callable[[str], str]

MIN_SUBQ, MAX_SUBQ, MAX_GOALS = 3, 6, 5
MAX_QUESTION_CHARS, MAX_SUBQ_CHARS, MAX_GOAL_CHARS = 400, 300, 240


@dataclass(frozen=True)
class SubQuestion:
    text: str
    why: str = ""
    cards: tuple[str, ...] = ()


@dataclass
class QuestionSet:
    question: str
    goals: list[str] = field(default_factory=list)
    sub_questions: list[SubQuestion] = field(default_factory=list)
    status: str = "draft"  # "draft" | "approved"
    edited_by_owner: bool = False
    note: str = ""
    model_calls: int = 0
    approved_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> QuestionSet:
        qs = cls(**{k: v for k, v in d.items() if k != "sub_questions"})
        qs.sub_questions = [SubQuestion(s["text"], s.get("why", ""), tuple(s.get("cards") or ()))
                            for s in d.get("sub_questions") or []]
        return qs


_PROMPT = """You have read the context brief and the paper cards below for a student's research.
Now set the research: one research question, its goals, and {lo} to {hi} sub-questions.
Each sub-question must be answerable from research papers, must address a GAP (something
the cards leave open) or a DISAGREEMENT (cards that conflict), and must name the cards
(by card id) that bear on it. Prefer specific, checkable sub-questions over broad ones.

TOPIC: {topic}

CONTEXT BRIEF:
{brief}

PAPER CARDS:
{cards}

Reply with one JSON object and nothing else:
{{"question": "the research question, one sentence",
 "goals": ["what answering it should achieve, short"],
 "sub_questions": [{{"text": "...", "why": "the gap or disagreement it addresses", "cards": ["card-..."]}}]}}"""


def _card_block(c: PaperCard) -> str:
    lines = [f"[{c.card_id}] {c.paper.title} ({c.paper.year or 'n.d.'}), grade {c.grade.letter}, read: {c.read_from}"]
    for label, value in (("question", c.question), ("method", c.method), ("data", c.data)):
        if value:
            lines.append(f"  {label}: {value}")
    lines += [f"  finding: {f.text}" for f in c.findings]
    lines += [f"  limitation: {x}" for x in c.limitations]
    return "\n".join(lines)


def derive_questions(topic: str, brief: Brief | None, cards: list[PaperCard], complete: Complete) -> QuestionSet:
    from .hypotheses import _json

    prompt = _PROMPT.format(lo=MIN_SUBQ, hi=MAX_SUBQ, topic=topic,
                            brief=brief.context_for_prompt() if brief else "(no brief)",
                            cards="\n\n".join(_card_block(c) for c in cards) or "(no cards)")
    reply: dict[str, Any] | None = None
    calls = 0
    for attempt in range(2):
        calls += 1
        reply = _json(complete(prompt if attempt == 0 else prompt + "\n\nReply with the JSON object only.") or "")
        if reply is not None:
            break
    reply = reply or {}
    known = {c.card_id for c in cards}
    subs: list[SubQuestion] = []
    for raw in list(reply.get("sub_questions") or []):
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get("text") or "").split())[:MAX_SUBQ_CHARS]
        if text and all(s.text != text for s in subs):
            subs.append(SubQuestion(text, " ".join(str(raw.get("why") or "").split())[:MAX_SUBQ_CHARS],
                                    tuple(c for c in (raw.get("cards") or []) if c in known)))
    notes: list[str] = []
    if len(subs) > MAX_SUBQ:
        notes.append(f"the model proposed {len(subs)} sub-questions; the first {MAX_SUBQ} were kept")
        subs = subs[:MAX_SUBQ]
    if len(subs) < MIN_SUBQ:
        notes.append(f"the model proposed {len(subs)} sub-question(s); add more before approving if needed")
    if not any(c.findings for c in cards):
        notes.append("no paper card has a quoted finding, so these questions rest on the brief alone")
    question = " ".join(str(reply.get("question") or "").split())[:MAX_QUESTION_CHARS] or topic
    goals = [" ".join(str(g).split())[:MAX_GOAL_CHARS] for g in list(reply.get("goals") or []) if str(g).strip()]
    return QuestionSet(question=question, goals=goals[:MAX_GOALS], sub_questions=subs, note="; ".join(notes),
                       model_calls=calls)


def edit(qs: QuestionSet, payload: dict[str, Any]) -> QuestionSet:
    """The owner's version. Raises ValueError with what is wrong; an approved
    set cannot be edited (the answer cycle is already working from it)."""
    if qs.status == "approved":
        raise ValueError("these questions are already approved")
    question = " ".join(str(payload.get("question") or "").split())
    if not question:
        raise ValueError("the research question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"the research question is longer than {MAX_QUESTION_CHARS} characters")
    raw_subs = [" ".join(str(s.get("text") if isinstance(s, dict) else s).split())
                for s in list(payload.get("sub_questions") or [])]
    raw_subs = [s for s in raw_subs if s]
    if not 1 <= len(raw_subs) <= MAX_SUBQ:
        raise ValueError(f"give between 1 and {MAX_SUBQ} sub-questions")
    if any(len(s) > MAX_SUBQ_CHARS for s in raw_subs):
        raise ValueError(f"a sub-question is longer than {MAX_SUBQ_CHARS} characters")
    goals = [" ".join(str(g).split()) for g in list(payload.get("goals") or []) if str(g).strip()]
    if len(goals) > MAX_GOALS or any(len(g) > MAX_GOAL_CHARS for g in goals):
        raise ValueError(f"at most {MAX_GOALS} goals of {MAX_GOAL_CHARS} characters each")
    old = {s.text: s for s in qs.sub_questions}
    subs = [old.get(t, SubQuestion(t, "added by the owner")) for t in dict.fromkeys(raw_subs)]
    return QuestionSet(question=question, goals=goals, sub_questions=subs, status="draft", edited_by_owner=True,
                       note=qs.note, model_calls=qs.model_calls)


def approve(qs: QuestionSet, *, clock: Callable[[], float] = time.time) -> QuestionSet:
    if not qs.question.strip() or not qs.sub_questions:
        raise ValueError("a question and at least one sub-question are needed before approval")
    return QuestionSet(question=qs.question, goals=list(qs.goals), sub_questions=list(qs.sub_questions),
                       status="approved", edited_by_owner=qs.edited_by_owner, note=qs.note,
                       model_calls=qs.model_calls, approved_at=clock())
