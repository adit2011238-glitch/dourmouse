"""The context brief (PL-10 T1, step 4): the owner's method starts with
understanding the general context of the topic before any question is set.

Sources: an encyclopedia pass (the best English Wikipedia article for the
topic, as plain text) and the two most cited review papers on it (OpenAlex
works of type review, sorted by citations), each read from its open-access PDF
or, failing that, its abstract.

The model writes a one-page brief: key terms, main schools of thought, a
timeline and the open debates. Every item names its source (W for the
encyclopedia, R1 or R2 for a review) and quotes it; an item whose quote is not
in that source's text is dropped and counted. The brief also proposes a few
short search queries for paper search; those are queries, not claims, so they
are not checked. The owner can correct the brief (``owner_notes``) before the
paper stage starts; the corrections travel with the brief to every later
stage, marked as the owner's.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .papers import Paper, PaperClient, PaperSourceError
from .reading import PaperText, Section, quote_in_text, read_paper

Complete = Callable[[str], str]

WIKI_PROMPT_CHARS = 24_000
REVIEW_PROMPT_CHARS = 14_000
LIMITS = {"term": 8, "school": 4, "event": 8, "debate": 4}
MAX_SEARCH_TERMS = 6


@dataclass(frozen=True)
class BriefItem:
    kind: str  # "term" | "school" | "event" | "debate"
    label: str  # the term, the school's name, the year, or "" for a debate
    text: str
    source: str  # "W", "R1", "R2"
    quote: str


@dataclass
class Brief:
    topic: str
    items: list[BriefItem] = field(default_factory=list)
    #: {"W": {"title", "url", "kind": "encyclopedia"}, "R1": {... paper dict, read_from, note}}
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    search_terms: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    #: What could not be fetched (a service down, no article, no reviews).
    gaps: list[str] = field(default_factory=list)
    owner_notes: str = ""
    model_calls: int = 0
    made_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Brief:
        b = cls(**{k: v for k, v in d.items() if k != "items"})
        b.items = [BriefItem(**i) for i in d.get("items") or []]
        return b

    def as_markdown(self) -> str:
        out = [f"# Context brief: {self.topic}", ""]
        titles = {"term": "Key terms", "school": "Main schools of thought", "event": "Timeline",
                  "debate": "Open debates"}
        for kind, title in titles.items():
            items = [i for i in self.items if i.kind == kind]
            if not items:
                continue
            out += [f"## {title}", ""]
            ordered = sorted(items, key=lambda x: x.label) if kind == "event" else items
            for i in ordered:
                head = f"**{i.label}**: " if i.label else ""
                out.append(f"- {head}{i.text} [{i.source}]")
            out.append("")
        if self.owner_notes.strip():
            out += ["## Owner's corrections", "", self.owner_notes.strip(), ""]
        out += ["## Sources", ""]
        for key, s in self.sources.items():
            if s.get("kind") == "encyclopedia":
                out.append(f"- [{key}] Wikipedia, \"{s.get('title')}\", {s.get('url')}")
            else:
                read = "full text" if s.get("read_from") == "pdf" else "abstract only"
                out.append(f"- [{key}] {s.get('title')} ({s.get('year') or 'n.d.'}), review, read: {read}, {s.get('url')}")
        if self.gaps:
            out += ["", "Not available: " + "; ".join(self.gaps)]
        return "\n".join(out).rstrip() + "\n"

    def context_for_prompt(self) -> str:
        """What later stages are told about the context, owner corrections first."""
        text = self.as_markdown()
        if self.owner_notes.strip():
            text = "OWNER'S CORRECTIONS (these override the brief):\n" + self.owner_notes.strip() + "\n\n" + text
        return text


_BRIEF_PROMPT = """You are writing a one-page context brief on a research topic for a student, from the
sources below ONLY. Every item must name its source (W, R1 or R2) and QUOTE it exactly
(a character-for-character copy of at least six words from that source). If a source
does not support an item, leave the item out. Never use outside knowledge.

TOPIC: {topic}

{sources}

Reply with one JSON object and nothing else:
{{"key_terms": [{{"term": "...", "definition": "one sentence", "source": "W", "quote": "..."}}],
 "schools": [{{"name": "...", "summary": "one sentence", "source": "R1", "quote": "..."}}],
 "timeline": [{{"year": "1885", "event": "one sentence", "source": "W", "quote": "..."}}],
 "debates": [{{"debate": "one sentence on what is still disputed", "source": "R2", "quote": "..."}}],
 "search_terms": ["three to six short search queries (2 to 5 words each) for finding research papers"]}}
At most {term} key terms, {school} schools, {event} timeline entries and {debate} debates."""


def _review_block(key: str, paper: Paper, reading: PaperText) -> str:
    how = "full text" if reading.read_from == "pdf" else "abstract only"
    return (f"SOURCE {key}: review paper \"{paper.title}\" ({paper.year or 'n.d.'}), {how}:\n"
            f"{reading.body()[:REVIEW_PROMPT_CHARS]}")


def build_brief(topic: str, client: PaperClient, complete: Complete, *, read: Callable[[Paper], PaperText] = read_paper,
                clock: Callable[[], float] = time.time) -> tuple[Brief, dict[str, PaperText], list[Paper]]:
    """(brief, readings by source key, the review papers). Raises ValueError
    only when there is nothing at all to write from."""
    from .hypotheses import _json

    brief = Brief(topic=topic, made_at=clock())
    texts: dict[str, str] = {}
    readings: dict[str, PaperText] = {}
    blocks: list[str] = []
    try:
        wiki = client.wikipedia_article(topic)
    except PaperSourceError as exc:
        wiki = None
        brief.gaps.append(f"encyclopedia: {exc}")
    if wiki:
        title, url, text = wiki
        brief.sources["W"] = {"kind": "encyclopedia", "title": title, "url": url}
        texts["W"] = text
        readings["W"] = PaperText(f"wiki:{title}", "encyclopedia", url, text=text,
                                  sections=(Section("Article", 0, len(text)),))
        blocks.append(f"SOURCE W: Wikipedia article \"{title}\":\n{text[:WIKI_PROMPT_CHARS]}")
    elif not brief.gaps:
        brief.gaps.append("encyclopedia: no Wikipedia article matched the topic")
    reviews = client.top_reviews(topic, n=2)
    for svc, err in reviews.errors.items():
        brief.gaps.append(f"reviews ({svc}): {err}")
    if not reviews.papers and not reviews.errors:
        brief.gaps.append("reviews: no review paper matched the topic in OpenAlex")
    review_papers: list[Paper] = []
    for i, paper in enumerate(reviews.papers[:2], 1):
        key = f"R{i}"
        reading = read(paper)
        if reading.read_from == "none":
            brief.gaps.append(f"{key}: {paper.title}: {reading.note}")
            continue
        review_papers.append(paper)
        readings[key] = reading
        texts[key] = reading.checkable_text()
        brief.sources[key] = {**paper.to_dict(), "kind": "review", "read_from": reading.read_from,
                              "note": reading.note}
        blocks.append(_review_block(key, paper, reading))
    if not blocks:
        raise ValueError("nothing to write a context brief from: " + "; ".join(brief.gaps))
    prompt = _BRIEF_PROMPT.format(topic=topic, sources="\n\n".join(blocks), **LIMITS)
    reply: dict[str, Any] | None = None
    for attempt in range(2):
        brief.model_calls += 1
        reply = _json(complete(prompt if attempt == 0 else prompt + "\n\nReply with the JSON object only.") or "")
        if reply is not None:
            break
    reply = reply or {}
    fields = (("key_terms", "term", "term", "definition"), ("schools", "school", "name", "summary"),
              ("timeline", "event", "year", "event"), ("debates", "debate", "", "debate"))
    for list_key, kind, label_key, text_key in fields:
        for n, raw in enumerate(list(reply.get(list_key) or [])[:LIMITS[kind]], 1):
            if not isinstance(raw, dict):
                continue
            text = str(raw.get(text_key) or "").strip()
            label = str(raw.get(label_key) or "").strip() if label_key else ""
            source = str(raw.get("source") or "").strip().upper()
            quote = str(raw.get("quote") or "").strip()
            where = f"{list_key} {n}"
            if not text:
                continue
            if source not in texts:
                brief.rejected.append(f"{where}: names a source ({source or 'none'}) that was not given")
                continue
            if len(quote.split()) < 6 or not quote_in_text(quote, texts[source]):
                brief.rejected.append(f"{where}: quote not found in source {source}")
                continue
            brief.items.append(BriefItem(kind, label, text, source, quote))
    brief.search_terms = [" ".join(str(t).split())[:80] for t in list(reply.get("search_terms") or [])
                          if str(t).strip()][:MAX_SEARCH_TERMS]
    if topic not in brief.search_terms:
        brief.search_terms.insert(0, topic)
    return brief, readings, review_papers


def store_brief(brief: Brief, library: Any, *, job_id: str, readings: dict[str, PaperText] | None = None) -> list[str]:
    """The brief, and the encyclopedia text it quotes, into the library."""
    ids = library.add(f"brief:{job_id}", brief.as_markdown(), {"kind": "context_brief", "job_id": job_id})
    w, wiki = brief.sources.get("W"), (readings or {}).get("W")
    if w and wiki is not None and wiki.text:
        ids += library.add(w["url"], wiki.text, {"kind": "encyclopedia", "job_id": job_id, "title": w["title"]})
    return ids
