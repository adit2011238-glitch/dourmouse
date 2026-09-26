"""A critic over synthesized research answers (R9, founding spec item 58).

Flags the sentences of an answer that nothing in the stored research backs,
and the ones that are backed but carry no citation. Same principle as
``execution_policy`` (R7): the model proposes, the platform verifies.

    1. The answer is split into sentences (abbreviations, decimals, bullets,
       markdown and numbered lists handled; citation markers stay attached
       to the sentence they follow).
    2. Each sentence is checked, with no model, against the stored claims and
       passages of THIS question by normalised token overlap. A sentence whose
       content words (and every number in it) are found in one claim or
       passage is SUPPORTED, and the ids of what backs it are returned.
    3. Only sentences that overlap partly (the ambiguous band) may go to a
       model, and it must QUOTE the passage it relies on. The platform then
       checks that the quote appears verbatim in a stored passage of this
       question and bears on the sentence; a quote that fails is a demotion to
       UNSUPPORTED. The model can never create support the record lacks, and
       nothing here rewrites the answer or invents a citation.

Verdicts: SUPPORTED, NO_CITATION (backed, but the answer's format calls for a
citation marker or URL and this sentence has none), CITED_UNVERIFIED (it cites
something, but nothing stored backs it), UNSUPPORTED (a factual assertion
nothing backs), NOT_A_CLAIM (question, hedge, transition, heading).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from dourmouse.research_graph.store import GraphStore
from dourmouse.research_graph.store import default_db as _graph_db
from dourmouse.research_graph.sync import _h
from dourmouse.research_pipeline.core import ResearchRecord

Complete = Callable[[str], str]

SUPPORTED = "SUPPORTED"
NO_CITATION = "NO_CITATION"
CITED_UNVERIFIED = "CITED_UNVERIFIED"
UNSUPPORTED = "UNSUPPORTED"
NOT_A_CLAIM = "NOT_A_CLAIM"
VERDICTS = (SUPPORTED, NO_CITATION, CITED_UNVERIFIED, UNSUPPORTED, NOT_A_CLAIM)

#: Share of a sentence's content words one stored claim or passage must hold
#: for the deterministic core to call it supported; below the second bound
#: nothing is close enough to be worth a model's time.
_SUPPORT_MIN = 0.7
_AMBIGUOUS_MIN = 0.4
_MIN_MATCHED = 2
_MODEL_BATCH = 10
_MAX_CANDIDATES = 3
_CANDIDATE_CHARS = 1500
_MIN_QUOTE_WORDS = 3

def _word_set(text: str) -> frozenset[str]:
    return frozenset(text.split())


_STOPWORDS = _word_set(
    "a an and are as at be been being but by can could did do does for from had has have he her his how i if in into "
    "is it its may might more most must no not of on or our over she should so some such than that the their them "
    "then there these they this those to too under up us was we were what when where which while who whom why will "
    "with would you your also about after all any because before between both each few other only own same very "
    "just than then thus via per"
)
_ABBREVIATIONS = _word_set(
    "dr mr mrs ms prof sr jr st vs e.g i.e u.s u.k cf fig figs eq eqs approx inc ltd co corp al no vol pp ca "
    "jan feb mar apr jun jul aug sep sept oct nov dec"
)
_HEDGE = re.compile(
    r"^(?:it (?:is|remains|seems) (?:unclear|unknown|uncertain|not clear)|(?:the )?(?:evidence|sources?|claims?) "
    r"(?:is|are|do|does|remain)[a-z ]*(?:unsettled|inconclusive|mixed|not (?:fully )?(?:answer|settle|address)|"
    r"disagree|conflict)|i (?:cannot|can't|could not|don't|do not) |(?:this|the answer) (?:may|might|could) "
    r"(?:be incomplete|change)|more (?:research|evidence|data) (?:is|would be) (?:needed|required))",
    re.I,
)
_TRANSITION = re.compile(
    r"^(?:however|moreover|furthermore|in summary|in short|in conclusion|to summari[sz]e|overall|finally|"
    r"first(?:ly)?|second(?:ly)?|third(?:ly)?|next|additionally|in addition|on the other hand|that said|"
    r"note that|here is|here are|here's|below is|the following)\b[\s,:;-]*",
    re.I,
)

_URL = re.compile(r"https?://[^\s)\]>\"']+")
_MARKER = re.compile(
    r"\[\d+(?:\s*[,\-–]\s*\d+)*\]"
    r"|\[(?:sources?|src)\s*:[^\]]*\]"
    r"|\((?:sources?|src)\s*:[^)]*\)"
    r"|\([A-Z][A-Za-z\-]+(?: et al\.)?,? (?:19|20)\d{2}[a-z]?\)",
    re.I,
)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_LIST_PREFIX = re.compile(r"^\s*(?:>\s*)*(?:[-*+•]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_BOLD_LINE = re.compile(r"^\s*(?:\*\*|__)[^*_]+(?:\*\*|__)\s*:?\s*$")
_CANDIDATE_END = re.compile(r"[.!?]+[\"')\]]*\s+")
_WORD = re.compile(r"\d+(?:[.,]\d+)*|[a-z]+")


@dataclass(frozen=True)
class Evidence:
    """One stored thing a sentence may be checked against: a claim (with the
    passage it was drawn from) or a bare passage."""

    ids: tuple[str, ...]
    text: str
    passage_id: str | None = None


@dataclass(frozen=True)
class SentenceVerdict:
    index: int
    text: str
    verdict: str
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""
    decided_by: str = "deterministic"  # "deterministic" | "model" | "model_demoted"
    quote: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "text": self.text, "verdict": self.verdict,
                "evidence_ids": list(self.evidence_ids), "reason": self.reason,
                "decided_by": self.decided_by, "quote": self.quote}


@dataclass(frozen=True)
class CritiqueResult:
    sentences: tuple[SentenceVerdict, ...]
    counts: dict[str, int]
    support_ratio: float | None  # backed / factual sentences; None when the answer makes no factual sentence
    missing_citations: tuple[str, ...]
    summary: str
    model_error: str = ""
    citations_expected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"sentences": [s.to_dict() for s in self.sentences], "counts": dict(self.counts),
                "support_ratio": self.support_ratio, "missing_citations": list(self.missing_citations),
                "summary": self.summary, "model_error": self.model_error,
                "citations_expected": self.citations_expected}


# -- sentence splitting ---------------------------------------------------- #


def _is_abbreviation(before: str) -> bool:
    """`before` is the text up to (not including) a candidate boundary's
    period. A boundary after an abbreviation or a single initial is not one."""
    words = before.split()
    if not words:
        return False
    last = words[-1].strip("([\"'").lower()
    return last in _ABBREVIATIONS or (len(last) == 1 and last.isalpha())


def _split_line(line: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for m in _CANDIDATE_END.finditer(line):
        rest = line[m.end():]
        if not rest or not (rest[0].isupper() or rest[0].isdigit() or rest[0] in "\"'([“"):
            continue
        punct = m.group(0).strip()
        if punct.startswith(".") and _is_abbreviation(line[start:m.start()]):
            continue
        parts.append(line[start:m.end()].strip())
        start = m.end()
    tail = line[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _clean_markdown(line: str) -> str:
    line = _LINK.sub(r"\1 (\2)", line)
    line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)
    line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", line)
    return line.replace("`", "")


def _strip_citations(text: str) -> str:
    return _URL.sub(" ", _MARKER.sub(" ", text))


def has_citation(text: str) -> bool:
    return bool(_URL.search(text) or _MARKER.search(text))


def split_sentences(answer: str) -> list[tuple[str, str]]:
    """(sentence, structure) pairs, structure being "heading", "lead_in" or
    "sentence". Lists, headings and code fences are handled per line; a
    fragment that is only a citation marker joins the sentence before it."""
    out: list[tuple[str, str]] = []
    in_fence = False
    for raw in answer.splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not raw.strip():
            continue
        if _HEADING.match(raw) or _BOLD_LINE.match(raw):
            heading = _clean_markdown(_HEADING.sub("", raw)).strip()
            if heading:
                out.append((heading, "heading"))
            continue
        line = _clean_markdown(_LIST_PREFIX.sub("", raw)).strip()
        if not line:
            continue
        for part in _split_line(line):
            if out and out[-1][1] == "sentence" and not _WORD.search(_strip_citations(part).lower()):
                out[-1] = (out[-1][0] + " " + part, "sentence")  # a lone "[2]" after the full stop
                continue
            out.append((part, "lead_in" if part.endswith(":") else "sentence"))
    return out


# -- deterministic matching ------------------------------------------------ #


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """(content words, numbers) of `text`, normalised. Numbers are kept out
    of the word set so that a wrong figure can never be outvoted by matching
    words: every number in a sentence must be in what backs it."""
    words: set[str] = set()
    numbers: set[str] = set()
    for w in _WORD.findall(_strip_citations(text).lower()):
        if w[0].isdigit():
            numbers.add(w.replace(",", ""))
        elif w not in _STOPWORDS and len(w) > 1:
            words.add(_stem(w))
    return frozenset(words), frozenset(numbers)


def _coverage(sent_words: frozenset[str], sent_numbers: frozenset[str], ev_text: str) -> tuple[float, int, bool]:
    ev_words, ev_numbers = _tokens(ev_text)
    matched = len(sent_words & ev_words)
    return (matched / len(sent_words) if sent_words else 0.0), matched, sent_numbers <= ev_numbers


def _units(evidence: list[Evidence], passages: dict[str, str]) -> list[Evidence]:
    """What a sentence is compared against: each claim, and each sentence (and
    adjacent pair) of each passage, so overlap is local and a long passage
    cannot back a sentence by containing its common words somewhere."""
    units = list(evidence)
    for pid, text in passages.items():
        parts = [p for p, kind in split_sentences(text) if kind == "sentence"]
        for i, p in enumerate(parts):
            units.append(Evidence((pid,), p, pid))
            if i + 1 < len(parts):
                units.append(Evidence((pid,), p + " " + parts[i + 1], pid))
    return units


def _best(sent_words: frozenset[str], sent_numbers: frozenset[str],
          units: list[Evidence]) -> list[tuple[float, int, bool, Evidence]]:
    scored = [(*_coverage(sent_words, sent_numbers, u.text), u) for u in units]
    scored.sort(key=lambda r: (r[2], r[0], r[1]), reverse=True)
    return scored


def _ids_of(u: Evidence) -> tuple[str, ...]:
    return tuple(dict.fromkeys(u.ids + ((u.passage_id,) if u.passage_id else ())))


# -- evidence from the stored question ------------------------------------- #


def evidence_from_record(record: ResearchRecord) -> tuple[list[Evidence], dict[str, str]]:
    """Active claims (id = the graph id sync gives them) and their passages."""
    from dourmouse.research_graph.sync import claim_fingerprint

    evidence: list[Evidence] = []
    passages: dict[str, str] = {}
    for c in record.active_claims():
        doc_id = "doc-" + _h(c.document_hash)
        pas_id = "pas-" + _h(doc_id, c.passage, c.location)
        passages[pas_id] = c.passage
        evidence.append(Evidence(("clm-" + _h(claim_fingerprint(c)),), c.claim, pas_id))
    return evidence, passages


def evidence_from_graph(store: GraphStore, question_id: str) -> tuple[list[Evidence], dict[str, str]]:
    from dourmouse.research_pipeline.hypotheses import _question_claims

    evidence: list[Evidence] = []
    passages: dict[str, str] = {}
    for c in _question_claims(store, question_id):
        if c.body.get("status") == "REJECTED":
            continue
        pas_ids: list[str] = []
        for ev in store.related(c.ref, "supported_by"):
            for pas in store.related(ev.ref, "extracted_from"):
                passages[pas.id] = str(pas.body.get("text") or "")
                pas_ids.append(pas.id)
        evidence.append(Evidence((c.id,), str(c.body.get("text") or ""), pas_ids[0] if pas_ids else None))
    return evidence, passages


# -- the model's part ------------------------------------------------------ #

_ADJUDICATE = (
    "You are checking sentences of a research answer against the stored source passages listed for each. For "
    "each numbered sentence decide whether one of ITS passages states or directly entails it. If yes, give "
    "the exact words of the passage you rely on, copied character for character (a quote, never a paraphrase). "
    "If no passage backs it, set supported to false. Answer with ONLY JSON: "
    '{{"verdicts": [{{"n": 1, "supported": true, "quote": "..."}}]}}\n\n{blocks}'
)


def _norm_ws(text: str) -> str:
    return " ".join(text.split())


def _verify_quote(quote: str, passages: dict[str, str], sent_words: frozenset[str],
                  sent_numbers: frozenset[str]) -> tuple[str | None, str]:
    """(passage id, "") when the quote is verbatim in a stored passage of this
    question and bears on the sentence; (None, why not) otherwise."""
    q = _norm_ws(quote)
    if len(q.split()) < _MIN_QUOTE_WORDS:
        return None, "the quote is too short to check"
    for pid, text in passages.items():
        if q in _norm_ws(text):
            q_words, q_numbers = _tokens(q)
            if not sent_numbers <= q_numbers:
                return None, "the quoted passage does not contain the numbers in the sentence"
            if len(sent_words & q_words) < min(_MIN_MATCHED, len(sent_words)):
                return None, "the quoted passage does not bear on the sentence"
            return pid, ""
    return None, "the quote does not appear in any stored passage of this question"


def _adjudicate(batch: list[tuple[int, str, list[str]]], llm: Complete) -> dict[int, tuple[bool, str]]:
    blocks = []
    for n, sentence, cands in batch:
        body = "\n".join(f"  passage {j}: {c}" for j, c in enumerate(cands, 1)) or "  (no passage)"
        blocks.append(f"SENTENCE {n}: {sentence}\n{body}")
    from dourmouse.research_pipeline.hypotheses import _json

    parsed = _json(llm(_ADJUDICATE.format(blocks="\n\n".join(blocks)))) or {}
    verdicts: dict[int, tuple[bool, str]] = {}
    for v in parsed.get("verdicts") or []:
        if isinstance(v, dict) and isinstance(v.get("n"), int) and not isinstance(v.get("n"), bool):
            verdicts[v["n"]] = (v.get("supported") is True, str(v.get("quote") or ""))
    return verdicts


# -- the critic ------------------------------------------------------------ #


@dataclass
class _Pending:
    index: int
    text: str
    words: frozenset[str]
    numbers: frozenset[str]
    ranked: list[tuple[float, int, bool, Evidence]] = field(default_factory=list)


def critique_answer(answer_text: str, question_or_record: ResearchRecord | str, llm: Complete | None = None,
                    *, store: GraphStore | None = None, require_citations: bool | None = None) -> CritiqueResult:
    """Critique `answer_text` against the stored research of one question.

    `question_or_record` is a ResearchRecord, or the id of a research question
    in the research graph (a KeyError for an unknown id). `llm` is a plain
    prompt-to-text callable used only for sentences that overlap partly; with
    none, those stay UNSUPPORTED. `require_citations` says whether the answer's
    format calls for a citation marker or URL on each backed sentence; left as
    None it is inferred (true when any sentence of the answer carries one)."""
    if isinstance(question_or_record, ResearchRecord):
        evidence, passages = evidence_from_record(question_or_record)
    else:
        evidence, passages = evidence_from_graph(store or GraphStore(_graph_db()), str(question_or_record))
    units = _units(evidence, passages)

    split = split_sentences(answer_text or "")
    expected = require_citations if require_citations is not None else any(
        has_citation(s) for s, kind in split if kind == "sentence")

    base: dict[int, SentenceVerdict] = {}
    pending: list[_Pending] = []
    facts: dict[int, tuple[str, frozenset[str], frozenset[str]]] = {}
    for i, (text, kind) in enumerate(split, 1):
        why = _not_a_claim(text, kind)
        if why:
            base[i] = SentenceVerdict(i, text, NOT_A_CLAIM, reason=why)
            continue
        words, numbers = _tokens(text)
        facts[i] = (text, words, numbers)
        ranked = _best(words, numbers, units)
        top = ranked[0] if ranked else None
        if top and top[2] and top[0] >= _SUPPORT_MIN and top[1] >= min(_MIN_MATCHED, len(words)):
            base[i] = SentenceVerdict(i, text, SUPPORTED, _ids_of(top[3]),
                                      f"{top[1]} of {len(words)} content words found in one stored source")
        elif top and top[0] >= _AMBIGUOUS_MIN and top[1] >= _MIN_MATCHED:
            pending.append(_Pending(i, text, words, numbers, ranked))
        else:
            base[i] = SentenceVerdict(i, text, UNSUPPORTED, reason="nothing stored for this question backs it")

    model_error = ""
    decided: dict[int, SentenceVerdict] = {}
    if pending and llm is not None:
        for k in range(0, len(pending), _MODEL_BATCH):
            chunk = pending[k:k + _MODEL_BATCH]
            batch = [(p.index, p.text, _candidates(p, passages)) for p in chunk]
            try:
                verdicts = _adjudicate(batch, llm)
            except Exception as exc:  # noqa: BLE001 - a model failure is reported, never allowed to void the deterministic verdicts
                model_error = f"{type(exc).__name__}: {exc}"
                break
            for p in chunk:
                said = verdicts.get(p.index)
                if said is None:
                    decided[p.index] = SentenceVerdict(p.index, p.text, UNSUPPORTED,
                                                       reason="the model gave no verdict for this sentence",
                                                       decided_by="model")
                elif not said[0]:
                    decided[p.index] = SentenceVerdict(p.index, p.text, UNSUPPORTED,
                                                       reason="the model found no passage that backs it",
                                                       decided_by="model")
                else:
                    pid, why = _verify_quote(said[1], passages, p.words, p.numbers)
                    if pid is None:
                        decided[p.index] = SentenceVerdict(p.index, p.text, UNSUPPORTED,
                                                           reason=f"model claim demoted: {why}",
                                                           decided_by="model_demoted", quote=said[1])
                    else:
                        decided[p.index] = SentenceVerdict(p.index, p.text, SUPPORTED, (pid,),
                                                           "the model's quote was verified in a stored passage",
                                                           decided_by="model", quote=_norm_ws(said[1]))
    for p in pending:
        if p.index not in decided:
            partial = p.ranked[0][0]
            reason = ("only partly matched a stored source and no model was available to decide"
                      if llm is None else "only partly matched a stored source and the model could not be used")
            decided[p.index] = SentenceVerdict(p.index, p.text, UNSUPPORTED, reason=f"{reason} ({partial:.0%})")

    final: list[SentenceVerdict] = []
    for i in range(1, len(split) + 1):
        v = base.get(i) or decided[i]
        final.append(_apply_citation_rule(v, expected))
    return _summarise(tuple(final), expected, model_error)


def _not_a_claim(text: str, kind: str) -> str:
    if kind == "heading":
        return "heading"
    if kind == "lead_in":
        return "lead-in to what follows"
    if text.rstrip().endswith("?"):
        return "question"
    body = _TRANSITION.sub("", text.strip(), count=1)
    if _HEDGE.match(body):
        return "hedge, not a factual assertion"
    words, numbers = _tokens(body)
    if len(words) < 2 and not numbers:
        return "too short to assert anything (a transition)"
    return ""


def _candidates(p: _Pending, passages: dict[str, str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for _cov, _matched, _nums, unit in p.ranked:
        pid = unit.passage_id or next((i for i in unit.ids if i in passages), None)
        if pid and pid not in seen:
            seen.add(pid)
            out.append(passages[pid][:_CANDIDATE_CHARS])
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


def _apply_citation_rule(v: SentenceVerdict, expected: bool) -> SentenceVerdict:
    cited = has_citation(v.text)
    if v.verdict == SUPPORTED and expected and not cited:
        return replace(v, verdict=NO_CITATION, reason=v.reason + "; it carries no citation marker or URL")
    if v.verdict == UNSUPPORTED and cited and v.decided_by != "model_demoted":
        return replace(v, verdict=CITED_UNVERIFIED,
                       reason="it cites a source, but nothing stored for this question backs it")
    return v


def _summarise(sentences: tuple[SentenceVerdict, ...], expected: bool, model_error: str) -> CritiqueResult:
    counts = dict.fromkeys(VERDICTS, 0)
    for s in sentences:
        counts[s.verdict] += 1
    factual = len(sentences) - counts[NOT_A_CLAIM]
    ratio = (counts[SUPPORTED] + counts[NO_CITATION]) / factual if factual else None
    missing = tuple(s.text for s in sentences if s.verdict == NO_CITATION)
    if not sentences:
        summary = "The answer is empty: there is nothing to check."
    elif not factual:
        summary = f"None of the {len(sentences)} sentence(s) makes a factual claim, so there is nothing to verify."
    else:
        parts = [f"{factual} factual sentence(s) checked against the stored claims and passages: "
                 f"{counts[SUPPORTED] + counts[NO_CITATION]} backed ({ratio:.0%})"]
        if counts[UNSUPPORTED]:
            parts.append(f"{counts[UNSUPPORTED]} unsupported")
        if counts[CITED_UNVERIFIED]:
            parts.append(f"{counts[CITED_UNVERIFIED]} cite a source that nothing stored backs")
        if counts[NO_CITATION]:
            parts.append(f"{counts[NO_CITATION]} backed but missing a citation")
        summary = ", ".join(parts) + "."
    if model_error:
        summary += f" The model check failed ({model_error}); partly matching sentences were left unsupported."
    return CritiqueResult(sentences, counts, ratio, missing, summary, model_error, expected)
