"""Paper cards and source grades (PL-10 T1, step 3).

One card per paper read: its question, method, data or sample, main findings,
limitations, how it bears on the topic, and a grade. This is the "source
validation" stage the RESEARCH screen listed as missing.

The model proposes; the platform verifies. Every field the model fills from
the paper (question, method, data, each finding, each limitation) must come
with a quote, and the quote must be in the paper's text (``reading.
quote_in_text``: character for character after one shared normalisation).
A field whose quote fails is not kept, and the card counts what was dropped
and why. Limitations come only from the paper's own words, plus facts the
platform knows (read from the abstract only, preprint, truncated PDF): no
model-invented weaknesses. ``bearing`` is labelled an assessment.

The grade is arithmetic over facts, never a model's opinion: where the work
appeared, citations per year, age, whether the full text was read, whether any
finding could be quoted, and (after the answer cycle compares cards) how many
other papers agree with or dispute it. Each point comes with its reason.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .papers import Paper, citations_per_year
from .reading import PROMPT_CHARS, PaperText, quote_in_text

Complete = Callable[[str], str]

MAX_FINDINGS = 5
MAX_LIMITATIONS = 3


@dataclass(frozen=True)
class Finding:
    id: str
    text: str
    quote: str
    location: str


@dataclass(frozen=True)
class Grade:
    score: int
    letter: str
    status: str
    citations: int | None
    citations_per_year: float | None
    year: int | None
    read_from: str
    agrees: int = 0
    disputes: int = 0
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperCard:
    card_id: str
    paper: Paper
    read_from: str
    reading_note: str
    question: str
    method: str
    data: str
    findings: tuple[Finding, ...]
    limitations: tuple[str, ...]
    bearing: str
    grade: Grade
    quotes_checked: int
    #: Why each dropped field was dropped ("method: quote not found in the paper").
    rejected: tuple[str, ...] = ()
    made_at: float = field(default=0.0, compare=False)
    #: Library passage ids of each finding's quote, by finding id.
    passage_ids: dict[str, str] = field(default_factory=dict, compare=False)
    #: The PDF read (or the paper URL for an abstract) and the SHA-256 of the
    #: stored raw bytes, so a quote can be re-verified against the original later.
    read_url: str = ""
    raw_sha256: str = ""

    @property
    def paper_key(self) -> str:
        return self.paper.key

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["paper"] = self.paper.to_dict()
        d["paper_key"] = self.paper_key
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PaperCard:
        g = dict(d["grade"])
        g["reasons"] = tuple(g.get("reasons") or ())
        return cls(
            card_id=d["card_id"], paper=Paper.from_dict(d["paper"]), read_from=d["read_from"],
            reading_note=d.get("reading_note", ""), question=d.get("question", ""), method=d.get("method", ""),
            data=d.get("data", ""),
            findings=tuple(Finding(**f) for f in d.get("findings") or ()),
            limitations=tuple(d.get("limitations") or ()), bearing=d.get("bearing", ""),
            grade=Grade(**g), quotes_checked=int(d.get("quotes_checked") or 0),
            rejected=tuple(d.get("rejected") or ()), made_at=float(d.get("made_at") or 0.0),
            passage_ids=dict(d.get("passage_ids") or {}),
            read_url=d.get("read_url", ""), raw_sha256=d.get("raw_sha256", ""),
        )

    def as_text(self) -> str:
        """The card as plain text (what the library indexes)."""
        lines = [f"Paper card: {self.paper.title} ({self.paper.year or 'n.d.'})"]
        for label, value in (("Question", self.question), ("Method", self.method), ("Data", self.data)):
            if value:
                lines.append(f"{label}: {value}")
        lines += [f"Finding: {f.text} Quote: \"{f.quote}\"" for f in self.findings]
        lines += [f"Limitation: {x}" for x in self.limitations]
        if self.bearing:
            lines.append(f"Bearing (assessment): {self.bearing}")
        lines.append(f"Grade: {self.grade.letter} ({self.grade.score}/100)")
        return "\n".join(lines)


def card_id_for(paper_key: str) -> str:
    return "card-" + hashlib.sha256(paper_key.encode("utf-8")).hexdigest()[:12]


def finding_id(paper_key: str, quote: str) -> str:
    return "f-" + hashlib.sha256(f"{paper_key}\x00{quote}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------- grading


def letter_for(score: int) -> str:
    return "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D"


def grade_paper(paper: Paper, *, read_from: str, findings: int, now_year: int, truncated: bool = False,
                agrees: int = 0, disputes: int = 0) -> Grade:
    """Base 40, then the points below, clamped to 0..100. A 80+, B 65+, C 50+, D under 50."""
    score = 40
    reasons: list[str] = []
    status = paper.status
    venue = paper.venue or "an unnamed venue"
    if status == "journal":
        score += 20
        reasons.append(f"+20 published in {venue}, a journal (peer review assumed from the venue)")
    elif status == "conference":
        score += 15
        reasons.append(f"+15 published in {venue}, a conference (peer review assumed from the venue)")
    elif status == "preprint":
        reasons.append("+0 preprint: not peer reviewed")
    else:
        score += 5
        reasons.append("+5 venue unknown")
    cpy = citations_per_year(paper, now_year)
    if cpy is None:
        reasons.append("+0 citation count unknown")
    else:
        pts = min(20, int(round(6 * math.log2(1 + cpy))))
        score += pts
        reasons.append(f"+{pts} cited {paper.cited_by} times, about {cpy:.1f} a year")
    if paper.year is None:
        reasons.append("+0 year unknown")
    else:
        age = max(0, now_year - paper.year)
        if age <= 5:
            score += 10
            reasons.append(f"+10 published {paper.year} (recent)")
        elif age <= 15:
            score += 5
            reasons.append(f"+5 published {paper.year}")
        else:
            reasons.append(f"+0 published {paper.year}: check whether later work supersedes it")
    if read_from == "pdf":
        score += 10
        reasons.append("+10 read in full from the open-access PDF" + (" (cut at the reading limit)" if truncated else ""))
    elif read_from == "abstract":
        score -= 10
        reasons.append("-10 read from the abstract only: no open-access PDF could be read")
    else:
        score -= 30
        reasons.append("-30 nothing could be read: no PDF and no abstract")
    if findings == 0:
        score -= 15
        reasons.append("-15 no finding could be quoted from the text")
    if agrees:
        pts = min(9, 3 * agrees)
        score += pts
        reasons.append(f"+{pts} agrees with {agrees} other paper(s) on a sub-question")
    if disputes:
        pts = min(15, 5 * disputes)
        score -= pts
        reasons.append(f"-{pts} disputed by {disputes} other paper(s) on a sub-question")
    score = max(0, min(100, score))
    return Grade(score=score, letter=letter_for(score), status=status, citations=paper.cited_by,
                 citations_per_year=round(cpy, 2) if cpy is not None else None, year=paper.year,
                 read_from=read_from, agrees=agrees, disputes=disputes, reasons=tuple(reasons))


def regrade(card: PaperCard, *, agrees: int, disputes: int, now_year: int) -> PaperCard:
    """The card with its agreement counts updated after the cycle compared cards."""
    g = grade_paper(card.paper, read_from=card.read_from, findings=len(card.findings), now_year=now_year,
                    truncated="cut at the reading limit" in " ".join(card.grade.reasons),
                    agrees=agrees, disputes=disputes)
    return replace(card, grade=g)


# ---------------------------------------------------------------- the card


_CARD_PROMPT = """You are reading one research paper to make a short, factual card about it.
Use ONLY the paper text below. Every field you fill must come with a QUOTE: an exact,
character-for-character copy of a sentence or phrase from the text (at least six words).
If the text does not state something, leave that field out. Never guess.

TOPIC THE READER IS RESEARCHING: {topic}

PAPER: {title} ({year}). {venue}
READ FROM: {read_from}

TEXT:
{text}

Reply with one JSON object and nothing else:
{{"question": {{"text": "the paper's own research question or aim, one sentence", "quote": "..."}},
 "method": {{"text": "how the study was done, one sentence", "quote": "..."}},
 "data": {{"text": "the data or sample (who or what, how many)", "quote": "..."}},
 "findings": [{{"text": "one main finding, one sentence, with its numbers", "quote": "..."}}],
 "limitations": [{{"text": "a limitation the paper itself states", "quote": "..."}}],
 "bearing": "one or two sentences: how this paper bears on the topic above"}}
At most {max_findings} findings and {max_limitations} limitations."""


def _field(obj: Any) -> tuple[str, str]:
    if isinstance(obj, dict):
        return str(obj.get("text") or "").strip(), str(obj.get("quote") or "").strip()
    return "", ""


def make_card(paper: Paper, reading: PaperText, topic: str, complete: Complete, *, now_year: int,
              clock: Callable[[], float] = time.time) -> tuple[PaperCard, int]:
    """(card, model calls used). A reading with nothing in it gets a card with
    no model call; a reply that is not JSON is asked for once more."""
    from .hypotheses import _json
    from .stages import _weak_passage

    calls = 0
    reply: dict[str, Any] | None = None
    text = reading.body()[:PROMPT_CHARS]
    if reading.read_from != "none" and text.strip():
        prompt = _CARD_PROMPT.format(
            topic=topic, title=paper.title, year=paper.year or "n.d.", venue=paper.venue or "venue unknown",
            read_from="the full PDF" if reading.read_from == "pdf" else "the abstract only",
            text=text, max_findings=MAX_FINDINGS, max_limitations=MAX_LIMITATIONS,
        )
        for attempt in range(2):
            calls += 1
            raw = complete(prompt if attempt == 0 else prompt + "\n\nReply with the JSON object only.")
            reply = _json(raw or "")
            if reply is not None:
                break
    reply = reply or {}
    checkable = reading.checkable_text()
    rejected: list[str] = []
    checked = 0

    def verified(label: str, obj: Any) -> tuple[str, str]:
        nonlocal checked
        value, quote = _field(obj)
        if not value:
            return "", ""
        checked += 1
        if not quote:
            rejected.append(f"{label}: no quote given")
            return "", ""
        if not quote_in_text(quote, checkable):
            rejected.append(f"{label}: quote not found in the paper text")
            return "", ""
        weak = _weak_passage(value, quote)
        if weak:
            rejected.append(f"{label}: {weak}")
            return "", ""
        return value, quote

    question, _ = verified("question", reply.get("question"))
    method, _ = verified("method", reply.get("method"))
    data, _ = verified("data", reply.get("data"))
    findings: list[Finding] = []
    for i, item in enumerate(list(reply.get("findings") or [])[:MAX_FINDINGS], 1):
        value, quote = verified(f"finding {i}", item)
        if value:
            fid = finding_id(paper.key, quote)
            if all(f.id != fid for f in findings):
                findings.append(Finding(fid, value, quote, reading.locate(quote)))
    limitations: list[str] = []
    for i, item in enumerate(list(reply.get("limitations") or [])[:MAX_LIMITATIONS], 1):
        value, quote = verified(f"limitation {i}", item)
        if value:
            limitations.append(f"{value} (the paper: \"{quote}\")")
    if reading.read_from == "abstract":
        limitations.append("Read from the abstract only: methods and results were not checked against the full text.")
    elif reading.read_from == "none":
        limitations.append("Nothing could be read: no open-access PDF and no abstract.")
    if paper.status == "preprint":
        limitations.append("Preprint: not peer reviewed.")
    if reading.truncated:
        limitations.append("Only the first part of a very long PDF was read.")
    bearing = str(reply.get("bearing") or "").strip()[:600]
    grade = grade_paper(paper, read_from=reading.read_from, findings=len(findings), now_year=now_year,
                        truncated=reading.truncated)
    card = PaperCard(
        card_id=card_id_for(paper.key), paper=paper, read_from=reading.read_from, reading_note=reading.note,
        question=question, method=method, data=data, findings=tuple(findings), limitations=tuple(limitations),
        bearing=bearing, grade=grade, quotes_checked=checked, rejected=tuple(rejected), made_at=clock(),
        read_url=reading.source_url, raw_sha256=reading.raw_sha256,
    )
    return card, calls


def store_card(card: PaperCard, library: Any, *, job_id: str = "") -> PaperCard:
    """The card text and each finding's quote into the library; the card comes
    back with the quote passage ids, so an answer can cite one exact passage."""
    source = card.paper.url or card.paper_key
    base = {"paper_key": card.paper_key, "card_id": card.card_id, "job_id": job_id, "title": card.paper.title}
    library.add(source, card.as_text(), {**base, "kind": "paper_card"})
    ids: dict[str, str] = {}
    for f in card.findings:
        got = library.add(source, f.quote, {**base, "kind": "finding", "finding_id": f.id, "location": f.location})
        if got:
            ids[f.id] = got[0]
    return replace(card, passage_ids=ids)
