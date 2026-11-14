"""PL-10 T1 step 4: the context brief, every item quoted from a named source."""

from __future__ import annotations

import json

import pytest

from dourmouse.research_pipeline import brief as B
from dourmouse.research_pipeline.library_port import InterimLibrary
from dourmouse.research_pipeline.papers import OPENALEX, Paper, PaperSourceError, SearchResult
from dourmouse.research_pipeline.reading import PaperText, Section

WIKI = ("The spacing effect demonstrates that learning is more effective when study sessions are spaced out. "
        "This effect was first identified by Hermann Ebbinghaus in 1885 in his studies of memory. "
        "Researchers still debate whether the effect comes from study-phase retrieval or encoding variability.")
REVIEW = ("Distributed practice produced better long-term retention than massed practice across 254 studies. "
          "Two accounts compete: deficient processing theory and the encoding variability account.")


class FakeClient:
    def __init__(self, wiki=("Spacing effect", "https://en.wikipedia.org/wiki/Spacing_effect", WIKI),
                 reviews=None, wiki_error=None):
        self.wiki, self.wiki_error = wiki, wiki_error
        self.reviews = reviews if reviews is not None else SearchResult(
            papers=[Paper(title="Distributed practice in verbal recall tasks", year=2006, doi="10.1/rev",
                          abstract=REVIEW, is_review=True)], counts={OPENALEX: 1})

    def wikipedia_article(self, topic):
        if self.wiki_error:
            raise self.wiki_error
        return self.wiki

    def top_reviews(self, topic, n=2):
        return self.reviews


def _read(paper):
    return PaperText(paper.key, "abstract", paper.url, text=paper.abstract,
                     sections=(Section("Abstract", 0, len(paper.abstract)),))


REPLY = {
    "key_terms": [
        {"term": "Spacing effect", "definition": "Learning is better when sessions are spaced.", "source": "W",
         "quote": "learning is more effective when study sessions are spaced out"},
        {"term": "Made up", "definition": "Not in any source.", "source": "W", "quote": "this sentence is not in the article"},
    ],
    "schools": [{"name": "Encoding variability", "summary": "Spacing varies the context of encoding.", "source": "R1",
                 "quote": "deficient processing theory and the encoding variability account"}],
    "timeline": [{"year": "1885", "event": "Ebbinghaus identifies the effect.", "source": "W",
                  "quote": "first identified by Hermann Ebbinghaus in 1885"}],
    "debates": [{"debate": "Which mechanism explains spacing.", "source": "R2",
                 "quote": "whether the effect comes from study-phase retrieval"}],
    "search_terms": ["spacing effect retention", "distributed practice meta-analysis"],
}


def _complete(reply):
    calls = []

    def complete(prompt):
        calls.append(prompt)
        return json.dumps(reply) if isinstance(reply, dict) else reply

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def test_brief_keeps_only_items_quoted_from_their_named_source():
    complete = _complete(REPLY)
    brief, readings, reviews = B.build_brief("spacing effect", FakeClient(), complete, read=_read)
    kinds = [(i.kind, i.label) for i in brief.items]
    assert kinds == [("term", "Spacing effect"), ("school", "Encoding variability"), ("event", "1885")]
    assert any("key_terms 2: quote not found in source W" in r for r in brief.rejected)
    assert any("debates 1: names a source (R2) that was not given" in r for r in brief.rejected)
    assert brief.search_terms[0] == "spacing effect" and "distributed practice meta-analysis" in brief.search_terms
    assert set(readings) == {"W", "R1"} and len(reviews) == 1
    assert brief.model_calls == 1
    assert "SOURCE R1: review paper" in complete.calls[0] and "abstract only" in complete.calls[0]


def test_markdown_lists_sources_and_owner_corrections_override():
    brief, _, _ = B.build_brief("spacing effect", FakeClient(), _complete(REPLY), read=_read)
    brief.owner_notes = "Focus on school-age learners, not adults."
    md = brief.as_markdown()
    assert "## Key terms" in md and "**1885**: Ebbinghaus identifies the effect. [W]" in md
    assert "[W] Wikipedia, \"Spacing effect\"" in md and "read: abstract only" in md
    assert "## Owner's corrections" in md
    assert brief.context_for_prompt().startswith("OWNER'S CORRECTIONS (these override the brief):")
    assert B.Brief.from_dict(json.loads(json.dumps(brief.to_dict()))).as_markdown() == md


def test_gaps_are_recorded_when_a_service_fails():
    client = FakeClient(wiki_error=PaperSourceError("wikipedia", "HTTP 503"),
                        reviews=SearchResult(errors={OPENALEX: "rate limited (HTTP 429)"}))
    with pytest.raises(ValueError, match="nothing to write a context brief from"):
        B.build_brief("x", client, _complete(REPLY), read=_read)
    client = FakeClient(reviews=SearchResult(errors={OPENALEX: "rate limited (HTTP 429)"}))
    brief, _, _ = B.build_brief("spacing effect", client, _complete(REPLY), read=_read)
    assert any("reviews (openalex): rate limited" in g for g in brief.gaps)
    assert "Not available:" in brief.as_markdown()


def test_store_brief_puts_the_brief_and_the_article_in_the_library(tmp_path):
    brief, readings, _ = B.build_brief("spacing effect", FakeClient(), _complete(REPLY), read=_read)
    lib = InterimLibrary(tmp_path / "lib.db")
    ids = B.store_brief(brief, lib, job_id="g1", readings=readings)
    assert len(ids) >= 2
    kinds = {lib.cite(i)["meta"]["kind"] for i in ids}
    assert kinds == {"context_brief", "encyclopedia"}
