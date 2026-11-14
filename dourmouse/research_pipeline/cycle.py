"""The answer cycle and the presentation (PL-10 T1, steps 6 and 7).

One round goes over every approved sub-question:

1. Retrieve: the quoted findings of the paper cards that bear on it, by library
   search over this job's findings plus the cards the question set named.
2. Search again where the evidence is thin (fewer than two papers): follow the
   citation graph of the best card for it backwards (its references) and
   forwards (papers citing it) on Semantic Scholar, or, failing that, search
   the three indexes with the sub-question; read and card the best new papers.
3. Answer in two or three sentences, each citing the evidence it rests on. The
   platform checks every citation: the cited quote must share content words
   with the sentence, every number in the sentence must be in a cited quote,
   and the polarity must agree ("reduces" is not backed by "does not reduce").
   A sentence left with no valid citation is removed and the removal recorded.
   A sub-question whose evidence did not change since its last answer is not
   asked again.
4. Record disagreements: each pair of cited findings from different papers is
   judged once with the pipeline's existing contradiction prompt.
5. Numbers (step 7): values of one quantity reported by two or more cited
   findings, each checked to appear as written in its quote, go through
   ``hypotheses.describe_samples`` (n, mean, sd, 95% CI). It is an unweighted
   summary of reported values, not a meta-analysis, and says so.

Stop rule: the cycle stops when a round adds no new claim (a claim is one
finding backing one sentence of one sub-question), or when the job's budget
(rounds, model calls, minutes) is spent, and the report says which.

Confidence is a word chosen by rule, never by the model: high (three or more
papers, all graded B or better, no disagreement), medium (two papers, or one
graded A), low (one paper, or sources disagree), none (nothing backed it).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from itertools import combinations
from typing import Any

from .cards import PaperCard, make_card, regrade, store_card
from .citations import DEFAULT_STYLE, reference_list
from .papers import Paper, PaperClient, PaperSourceError, merge_papers, query_terms, rank_papers
from .questions import QuestionSet
from .reading import PaperText, normalize_for_match, read_paper, store_reading

Complete = Callable[[str], str]

EVIDENCE_PER_SUBQ = 8
#: Content words a finding must share with a sub-question to be offered as evidence.
MIN_OVERLAP = 2
MAX_SENTENCES = 3
MAX_PAIRS_PER_SUBQ = 3
THIN_PAPERS = 2


@dataclass
class Budget:
    initial_papers: int = 8
    max_papers: int = 14
    max_rounds: int = 3
    max_model_calls: int = 90
    max_minutes: float = 90.0
    followup_papers: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Budget:
        b = cls()
        for k, v in (d or {}).items():
            if hasattr(b, k) and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                setattr(b, k, type(getattr(b, k))(v))
        return b


@dataclass
class Spend:
    model_calls: int = 0
    papers_read: int = 0
    started_at: float = 0.0


@dataclass(frozen=True)
class Evidence:
    ref: str
    finding_id: str
    card_id: str
    paper_key: str
    text: str
    quote: str
    location: str
    grade: int
    letter: str


@dataclass
class Sentence:
    text: str
    cites: list[str]  # finding ids


@dataclass
class Answer:
    sub_question: str
    sentences: list[Sentence] = field(default_factory=list)
    confidence: str = "none"
    confidence_reason: str = ""
    dropped: list[str] = field(default_factory=list)
    numbers: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    round: int = 0


@dataclass
class Disagreement:
    sub_index: int
    a: str  # finding id
    b: str
    note: str


@dataclass
class CycleState:
    round: int = 0
    answers: dict[int, Answer] = field(default_factory=dict)
    #: [sub_index, finding_id, round first cited]
    claims: list[list[Any]] = field(default_factory=list)
    judged_pairs: list[str] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    stop_reason: str = ""
    log: list[str] = field(default_factory=list)
    tried_papers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["answers"] = {str(k): v for k, v in d["answers"].items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> CycleState:
        d = d or {}
        s = cls(round=int(d.get("round") or 0), claims=[list(c) for c in d.get("claims") or []],
                judged_pairs=list(d.get("judged_pairs") or []), stop_reason=d.get("stop_reason", ""),
                log=list(d.get("log") or []), tried_papers=list(d.get("tried_papers") or []))
        for k, a in (d.get("answers") or {}).items():
            s.answers[int(k)] = Answer(
                sub_question=a["sub_question"], sentences=[Sentence(x["text"], list(x["cites"])) for x in a.get("sentences") or []],
                confidence=a.get("confidence", "none"), confidence_reason=a.get("confidence_reason", ""),
                dropped=list(a.get("dropped") or []), numbers=list(a.get("numbers") or []),
                evidence_ids=list(a.get("evidence_ids") or []), round=int(a.get("round") or 0))
        s.disagreements = [Disagreement(**x) for x in d.get("disagreements") or []]
        return s


@dataclass
class CycleContext:
    topic: str
    job_id: str
    questions: QuestionSet
    cards: dict[str, PaperCard]
    library: Any
    client: PaperClient
    complete: Complete
    budget: Budget
    spend: Spend
    now_year: int
    read: Callable[[Paper], PaperText] = read_paper
    clock: Callable[[], float] = time.time
    #: Called after every card is made, so a job can save as it goes.
    on_card: Callable[[PaperCard], None] | None = None

    def ask(self, prompt: str) -> str:
        self.spend.model_calls += 1
        return self.complete(prompt) or ""

    def exhausted(self) -> str:
        if self.spend.model_calls >= self.budget.max_model_calls:
            return f"budget: {self.spend.model_calls} of {self.budget.max_model_calls} model calls spent"
        minutes = (self.clock() - self.spend.started_at) / 60.0 if self.spend.started_at else 0.0
        if minutes >= self.budget.max_minutes:
            return f"budget: {minutes:.0f} of {self.budget.max_minutes:.0f} minutes spent"
        return ""


# ---------------------------------------------------------------- retrieval


def _finding_index(cards: dict[str, PaperCard]) -> dict[str, tuple[PaperCard, Any]]:
    return {f.id: (c, f) for c in cards.values() for f in c.findings}


def evidence_for(ctx: CycleContext, sub_index: int) -> list[Evidence]:
    """Findings bearing on one sub-question, best first, at most EVIDENCE_PER_SUBQ."""
    from .answer_critic import _tokens

    subq = ctx.questions.sub_questions[sub_index]
    index = _finding_index(ctx.cards)
    words = _tokens(subq.text)[0]

    def overlap(fid: str) -> int:
        f = index[fid][1]
        return len(words & _tokens(f.text + " " + f.quote)[0])

    picked: list[str] = []
    # The keyword search ORs the words, so one shared word ("recall") is a hit;
    # a finding must share MIN_OVERLAP content words to count as evidence, or the
    # thin-evidence check would be fooled by findings about something else.
    for hit in ctx.library.search(subq.text, filters={"meta": {"job_id": ctx.job_id, "kind": "finding"}},
                                  limit=EVIDENCE_PER_SUBQ * 3):
        fid = hit["meta"].get("finding_id")
        if fid in index and fid not in picked and overlap(fid) >= MIN_OVERLAP:
            picked.append(fid)
    # Findings of the cards the question set named for this sub-question need one shared word.
    named = [f.id for cid in subq.cards if cid in ctx.cards for f in ctx.cards[cid].findings]
    for fid in sorted(named, key=lambda x: -overlap(x)):
        if fid not in picked and overlap(fid) >= 1:
            picked.append(fid)
    out: list[Evidence] = []
    for n, fid in enumerate(picked[:EVIDENCE_PER_SUBQ], 1):
        card, f = index[fid]
        out.append(Evidence(f"E{n}", fid, card.card_id, card.paper_key, f.text, f.quote, f.location,
                            card.grade.score, card.grade.letter))
    return out


def is_thin(evidence: list[Evidence]) -> bool:
    return len({e.paper_key for e in evidence}) < THIN_PAPERS


def follow_up(ctx: CycleContext, state: CycleState, sub_index: int, evidence: list[Evidence]) -> list[PaperCard]:
    """Read and card up to ``followup_papers`` new papers for a thin sub-question."""
    room = min(ctx.budget.followup_papers, ctx.budget.max_papers - ctx.spend.papers_read)
    if room <= 0:
        state.log.append(f"round {state.round}, sub-question {sub_index + 1}: thin, but the paper budget is spent")
        return []
    subq = ctx.questions.sub_questions[sub_index]
    seed_ids = [e.card_id for e in evidence] + list(subq.cards)
    seeds = sorted({cid: ctx.cards[cid] for cid in seed_ids if cid in ctx.cards}.values(), key=lambda c: -c.grade.score)
    candidates: list[Paper] = []
    how = ""
    if seeds:
        for direction in ("references", "citations"):
            try:
                candidates += ctx.client.s2_neighbours(seeds[0].paper, direction=direction, limit=20)
            except PaperSourceError as exc:
                state.log.append(f"round {state.round}, sub-question {sub_index + 1}: citation graph: {exc}")
        how = f"citation graph of \"{seeds[0].paper.title}\""
    if not candidates:
        result = ctx.client.search(subq.text, per_source=8)
        candidates = result.papers
        how = "search: " + result.note()
    tried = set(state.tried_papers) | {c.paper_key for c in ctx.cards.values()}
    fresh = [p for p in merge_papers(candidates) if p.key not in tried]
    ranked = rank_papers(fresh, query_terms(subq.text, ctx.topic), now_year=ctx.now_year)
    made: list[PaperCard] = []
    for paper, score in ranked[:room]:
        if score.relevance <= 0 or ctx.exhausted():
            break
        state.tried_papers.append(paper.key)
        made.append(read_and_card(ctx, paper))
    state.log.append(f"round {state.round}, sub-question {sub_index + 1}: thin evidence; {how}; "
                     f"{len(made)} new paper(s) read")
    return made


def read_and_card(ctx: CycleContext, paper: Paper) -> PaperCard:
    reading = ctx.read(paper)
    store_reading(reading, paper, ctx.library, job_id=ctx.job_id)
    card, calls = make_card(paper, reading, ctx.topic, ctx.complete, now_year=ctx.now_year, clock=ctx.clock)
    ctx.spend.model_calls += calls
    ctx.spend.papers_read += 1
    card = store_card(card, ctx.library, job_id=ctx.job_id)
    ctx.cards[card.card_id] = card
    if ctx.on_card is not None:
        ctx.on_card(card)
    return card


# ---------------------------------------------------------------- answering


_ANSWER_PROMPT = """You are answering one sub-question of a student's research, using ONLY the evidence
below. Each evidence item is a finding quoted from a research paper.

RESEARCH QUESTION: {question}
SUB-QUESTION: {sub_question}

EVIDENCE:
{evidence}

Write two or three sentences that answer the sub-question. After each sentence, list the
evidence items it rests on. Say only what the cited evidence says; copy every number exactly
as it is written in the quote. If the evidence does not answer the sub-question, write one
sentence saying so, with no evidence listed.
Also list numbers that measure the SAME quantity in two or more evidence items, each copied
exactly as written in its quote.

Reply with one JSON object and nothing else:
{{"sentences": [{{"text": "...", "cites": ["E1", "E3"]}}],
 "numbers": [{{"quantity": "what is measured", "unit": "...", "values": [{{"evidence": "E1", "as_written": "64%"}}]}}]}}"""

_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _evidence_block(ctx: CycleContext, evidence: list[Evidence]) -> str:
    lines = []
    for e in evidence:
        p = ctx.cards[e.card_id].paper
        who = (p.authors[0].split()[-1] + (" et al." if len(p.authors) > 2 else "")) if p.authors else p.title[:40]
        lines.append(f"[{e.ref}] ({who} {p.year or 'n.d.'}, grade {e.letter}) Finding: {e.text}\n"
                     f"      Quote: \"{e.quote}\"")
    return "\n".join(lines)


def check_sentence(text: str, cited: list[Evidence]) -> tuple[list[str], str]:
    """(finding ids that back ``text``, why it is unsupported or "")."""
    from .answer_critic import _coverage, _negated, _tokens

    words, numbers = _tokens(text)
    negated = _negated(text)
    backing: list[Evidence] = []
    for e in cited:
        share, matched, _agrees = _coverage(words, frozenset(), e.quote, negated)
        if (matched >= 2 or share >= 0.25) and _negated(e.quote) == negated:
            backing.append(e)
    if not backing:
        return [], "no cited quote backs it" if cited else "it cites nothing"
    quoted_numbers: set[str] = set()
    for e in backing:
        quoted_numbers |= _tokens(e.quote)[1]
    missing = sorted(numbers - quoted_numbers)
    if missing:
        return [], f"its number(s) {', '.join(missing)} are not in the cited quotes"
    return [e.finding_id for e in backing], ""


def _verified_numbers(raw: Any, by_ref: dict[str, Evidence]) -> list[dict[str, Any]]:
    """Quantities with two or more values, each found as written in its own quote."""
    from .hypotheses import describe_samples

    out: list[dict[str, Any]] = []
    for q in list(raw or [])[:5]:
        if not isinstance(q, dict):
            continue
        values: list[float] = []
        shown: list[dict[str, Any]] = []
        used: set[str] = set()
        for v in list(q.get("values") or [])[:20]:
            if not isinstance(v, dict):
                continue
            e = by_ref.get(str(v.get("evidence") or ""))
            written = str(v.get("as_written") or "").strip()
            m = _NUMBER.search(written)
            if e is None or not m or e.finding_id in used:
                continue
            if normalize_for_match(written) not in normalize_for_match(e.quote):
                continue
            try:
                number = float(m.group(0).replace(",", ""))
            except ValueError:
                continue
            used.add(e.finding_id)
            values.append(number)
            shown.append({"value": number, "as_written": written, "finding_id": e.finding_id})
        if len(values) >= 2:
            stats = describe_samples(values)
            for k in ("t", "p_two_sided", "null_mean"):
                stats.pop(k, None)  # no null hypothesis here: describing reported values only
            stats["method"] = ("unweighted summary of values reported by different papers, not a meta-analysis "
                               "(studies differ in size and design)")
            out.append({"quantity": str(q.get("quantity") or "")[:120], "unit": str(q.get("unit") or "")[:40],
                        "values": shown, "statistics": stats})
    return out


def confidence(cited: list[Evidence], disputed: bool) -> tuple[str, str]:
    papers = {e.paper_key for e in cited}
    if not papers:
        return "none", "no sentence could be backed by the evidence"
    grades = [e.grade for e in cited]
    if disputed:
        return "low", "the sources disagree on this (see where sources disagree)"
    if len(papers) >= 3 and min(grades) >= 65:
        return "high", f"{len(papers)} papers, all graded B or better"
    if len(papers) >= 2:
        return "medium", f"{len(papers)} papers"
    if max(grades) >= 80:
        return "medium", "one paper, graded A"
    return "low", "rests on one paper"


def answer_sub_question(ctx: CycleContext, state: CycleState, sub_index: int, evidence: list[Evidence]) -> Answer:
    from .answer_critic import _HEDGE
    from .hypotheses import _json

    subq = ctx.questions.sub_questions[sub_index]
    answer = Answer(sub_question=subq.text, evidence_ids=[e.finding_id for e in evidence], round=state.round)
    if not evidence:
        answer.confidence, answer.confidence_reason = "none", "no paper card has a finding on this sub-question"
        return answer
    prompt = _ANSWER_PROMPT.format(question=ctx.questions.question, sub_question=subq.text,
                                   evidence=_evidence_block(ctx, evidence))
    reply = _json(ctx.ask(prompt))
    if reply is None and not ctx.exhausted():
        reply = _json(ctx.ask(prompt + "\n\nReply with the JSON object only."))
    reply = reply or {}
    by_ref = {e.ref: e for e in evidence}
    for raw in list(reply.get("sentences") or []):
        if len(answer.sentences) >= MAX_SENTENCES:
            break
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get("text") or "").split())
        if not text:
            continue
        cited = [by_ref[r] for r in dict.fromkeys(str(x) for x in raw.get("cites") or []) if r in by_ref]
        if not cited and _HEDGE.search(text):
            answer.sentences.append(Sentence(text, []))  # "the evidence does not settle this": not a claim
            continue
        ids, why = check_sentence(text, cited)
        if ids:
            answer.sentences.append(Sentence(text, ids))
        else:
            answer.dropped.append(f"removed \"{text[:160]}\": {why}")
    answer.numbers = _verified_numbers(reply.get("numbers"), by_ref)
    return answer


# ---------------------------------------------------------------- disagreements


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def judge_disagreements(ctx: CycleContext, state: CycleState, sub_index: int, answer: Answer) -> None:
    from .stages import _CONTRADICTION_PROMPT, _parse_contradiction_reply

    index = _finding_index(ctx.cards)
    cited = list(dict.fromkeys(fid for s in answer.sentences for fid in s.cites if fid in index))
    pairs = [(a, b) for a, b in combinations(cited, 2)
             if index[a][0].paper_key != index[b][0].paper_key and _pair_key(a, b) not in state.judged_pairs]
    for a, b in pairs[:MAX_PAIRS_PER_SUBQ]:
        if ctx.exhausted():
            return
        fa, fb = index[a][1], index[b][1]
        reply = ctx.ask(_CONTRADICTION_PROMPT.format(
            sub_question=answer.sub_question, claim_a=f"{fa.text} (quote: \"{fa.quote}\")",
            claim_b=f"{fb.text} (quote: \"{fb.quote}\")"))
        state.judged_pairs.append(_pair_key(a, b))
        parsed = _parse_contradiction_reply(reply)
        if parsed and parsed[0] == "yes":
            state.disagreements.append(Disagreement(sub_index, a, b, parsed[1][:400]))


def _regrade_all(ctx: CycleContext, state: CycleState) -> None:
    index = _finding_index(ctx.cards)
    agrees: dict[str, set[str]] = {}
    disputes: dict[str, set[str]] = {}
    for ans in state.answers.values():
        for s in ans.sentences:
            keys = {index[f][0].card_id for f in s.cites if f in index}
            for k in keys:
                agrees.setdefault(k, set()).update(keys - {k})
    for d in state.disagreements:
        if d.a in index and d.b in index:
            ca, cb = index[d.a][0].card_id, index[d.b][0].card_id
            disputes.setdefault(ca, set()).add(cb)
            disputes.setdefault(cb, set()).add(ca)
    for cid, card in list(ctx.cards.items()):
        new = regrade(card, agrees=len(agrees.get(cid, ())), disputes=len(disputes.get(cid, ())), now_year=ctx.now_year)
        if new.grade != card.grade:
            ctx.cards[cid] = new
            if ctx.on_card is not None:
                ctx.on_card(new)


# ---------------------------------------------------------------- one round


def run_round(ctx: CycleContext, state: CycleState) -> tuple[int, str]:
    """One pass over every sub-question. Returns (new claims, stop reason or "")."""
    if not ctx.spend.started_at:
        ctx.spend.started_at = ctx.clock()
    state.round += 1
    r = state.round
    known = {(int(c[0]), str(c[1])) for c in state.claims}
    new_claims = 0
    for i, _subq in enumerate(ctx.questions.sub_questions):
        spent = ctx.exhausted()
        if spent:
            state.stop_reason = spent
            state.log.append(f"round {r}: stopped at sub-question {i + 1}: {spent}")
            return new_claims, spent
        evidence = evidence_for(ctx, i)
        if is_thin(evidence) and follow_up(ctx, state, i, evidence):
            evidence = evidence_for(ctx, i)
        prev = state.answers.get(i)
        if prev is not None and set(prev.evidence_ids) == {e.finding_id for e in evidence}:
            continue  # nothing new to say about this one
        answer = answer_sub_question(ctx, state, i, evidence)
        state.answers[i] = answer
        for s in answer.sentences:
            for fid in s.cites:
                if (i, fid) not in known:
                    known.add((i, fid))
                    state.claims.append([i, fid, r])
                    new_claims += 1
        judge_disagreements(ctx, state, i, answer)
        index = {e.finding_id: e for e in evidence}
        cited = [index[f] for s in answer.sentences for f in s.cites if f in index]
        disputed = any(d.sub_index == i for d in state.disagreements)
        answer.confidence, answer.confidence_reason = confidence(cited, disputed)
    _regrade_all(ctx, state)
    state.log.append(f"round {r}: {new_claims} new claim(s)")
    if new_claims == 0:
        state.stop_reason = f"converged: round {r} added no new claim"
    elif r >= ctx.budget.max_rounds:
        state.stop_reason = f"budget: {r} of {ctx.budget.max_rounds} rounds used"
    else:
        state.stop_reason = ctx.exhausted()
    return new_claims, state.stop_reason


# ---------------------------------------------------------------- the report


def build_report(questions: QuestionSet, cards: dict[str, PaperCard], state: CycleState, *,
                 style: str = DEFAULT_STYLE, spend: Spend | None = None) -> dict[str, Any]:
    index = _finding_index(cards)
    cited_keys: list[str] = []
    for ans in state.answers.values():
        for s in ans.sentences:
            for fid in s.cites:
                if fid in index and index[fid][0].paper_key not in cited_keys:
                    cited_keys.append(index[fid][0].paper_key)
    for d in state.disagreements:
        for fid in (d.a, d.b):
            if fid in index and index[fid][0].paper_key not in cited_keys:
                cited_keys.append(index[fid][0].paper_key)
    by_key = {c.paper_key: c for c in cards.values()}
    refs, cites = reference_list([by_key[k].paper for k in cited_keys], style)

    def cite(fids: list[str]) -> str:
        keys = list(dict.fromkeys(index[f][0].paper_key for f in fids if f in index))
        return "(" + "; ".join(cites[k] for k in keys) + ")" if keys else ""

    answers = []
    for i, subq in enumerate(questions.sub_questions):
        ans = state.answers.get(i) or Answer(sub_question=subq.text, confidence_reason="not reached before the cycle stopped")
        answers.append({
            "sub_question": subq.text,
            "sentences": [{"text": s.text, "citation": cite(s.cites),
                           "evidence": [{"finding_id": f, "quote": index[f][1].quote, "location": index[f][1].location,
                                         "paper_key": index[f][0].paper_key, "cite": cites.get(index[f][0].paper_key, "")}
                                        for f in s.cites if f in index]} for s in ans.sentences],
            "confidence": ans.confidence, "confidence_reason": ans.confidence_reason,
            "numbers": ans.numbers, "removed": ans.dropped,
        })
    disagreements = []
    for d in state.disagreements:
        if d.a in index and d.b in index:
            disagreements.append({
                "sub_question": questions.sub_questions[d.sub_index].text if d.sub_index < len(questions.sub_questions) else "",
                "a": {"text": index[d.a][1].text, "quote": index[d.a][1].quote, "cite": cites.get(index[d.a][0].paper_key, "")},
                "b": {"text": index[d.b][1].text, "quote": index[d.b][1].quote, "cite": cites.get(index[d.b][0].paper_key, "")},
                "note": d.note,
            })
    read = list(cards.values())
    return {
        "question": questions.question, "goals": list(questions.goals), "style": style,
        "answers": answers, "disagreements": disagreements, "references": refs,
        "stop_reason": state.stop_reason or "not finished", "rounds": state.round,
        "papers_read": len(read), "abstract_only": sum(1 for c in read if c.read_from == "abstract"),
        "model_calls": spend.model_calls if spend else None, "log": list(state.log),
    }


def render_markdown(report: dict[str, Any]) -> str:
    style = "APA" if report.get("style") == "apa" else "MLA"
    out = [f"# {report['question']}", ""]
    if report.get("goals"):
        out += ["Goals: " + "; ".join(report["goals"]), ""]
    for n, a in enumerate(report["answers"], 1):
        out += [f"## {n}. {a['sub_question']}", ""]
        if a["sentences"]:
            out.append(" ".join(f"{s['text'].rstrip('.')} {s['citation']}.".replace(" .", ".")
                                if s["citation"] else s["text"] for s in a["sentences"]))
        else:
            out.append("Not answered: the evidence found does not address this sub-question.")
        out += ["", f"Confidence: {a['confidence']} ({a['confidence_reason']})."]
        for q in a.get("numbers") or []:
            st = q["statistics"]
            if "error" in st:
                continue
            vals = ", ".join(v["as_written"] for v in q["values"])
            out.append(f"Numbers, {q['quantity']} ({q['unit']}): reported {vals}; n={st['n']}, mean {st['mean']:.3g}, "
                       f"95% CI {st['ci95'][0]:.3g} to {st['ci95'][1]:.3g}. {st['method'].capitalize()}.")
        out.append("")
    out += ["## Where sources disagree", ""]
    if report["disagreements"]:
        for d in report["disagreements"]:
            out.append(f"- {d['sub_question']}: \"{d['a']['quote']}\" ({d['a']['cite']}) versus "
                       f"\"{d['b']['quote']}\" ({d['b']['cite']}). {d['note']}")
    else:
        out.append("No disagreement was found among the cited findings.")
    out += ["", f"## References ({style})", ""] + [f"- {r}" for r in report["references"]]
    out += ["", "## Evidence for each claim", ""]
    for n, a in enumerate(report["answers"], 1):
        for s in a["sentences"]:
            for e in s["evidence"]:
                out.append(f"- {n}. {e['cite']}, {e['location'] or 'location not found'}: \"{e['quote']}\"")
    out += ["", "## How this was made", "",
            f"Stopped: {report['stop_reason']}. Rounds: {report['rounds']}. Papers read: {report['papers_read']} "
            f"({report['abstract_only']} from the abstract only)."
            + (f" Model calls: {report['model_calls']}." if report.get("model_calls") is not None else ""),
            "Every quote above was checked to appear in its paper's text; every sentence's citations were checked "
            "against those quotes."]
    return "\n".join(out).rstrip() + "\n"
