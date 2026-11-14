"""PL-10 T1 benchmark: the fixed question set and the two measurements."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from dourmouse.research_pipeline import benchmark as BM
from dourmouse.research_pipeline.cards import Finding, Grade, PaperCard
from dourmouse.research_pipeline.library_port import InterimLibrary
from dourmouse.research_pipeline.papers import OPENALEX, Paper, SearchResult
from dourmouse.research_pipeline.reading import PaperText, Section


def test_ten_fixed_questions_each_with_references_and_sub_questions():
    items = BM.load_items()
    assert len(items) == 10 and len({i["id"] for i in items}) == 10
    for i in items:
        assert i["question"].endswith("?") and 3 <= len(i["sub_questions"]) <= 6
        assert len(i["references"]) >= 3
        for r in i["references"]:
            assert r.get("title") and (r.get("doi") or r.get("arxiv"))
            assert BM._ref_ids(r)  # normalizes to a real DOI or arXiv id
    blob = json.dumps(items)
    assert "—" not in blob


def test_source_recall_counts_found_and_read_by_doi_or_arxiv():
    refs = [{"doi": "10.1/A", "title": "a"}, {"arxiv": "2001.00001", "title": "b"}, {"doi": "10.1/c", "title": "c"}]
    found = [Paper(title="a", doi="10.1/a"), Paper(title="b", arxiv_id="2001.00001"), Paper(title="z", doi="10.1/z")]
    read = [Paper(title="a", doi="10.1/a")]
    r = BM.source_recall(refs, found, read)
    assert (r["found"], r["read"], r["missed"]) == (2, 1, ["c"])
    assert round(r["found_share"], 3) == 0.667


def _card(read_from, **kw):
    p = Paper(title="t", doi="10.1/t", abstract="Spaced practice improved scores on a delayed classroom test.")
    g = Grade(70, "B", "journal", 1, 1.0, 2020, read_from)
    return PaperCard("card-t", p, read_from, "", "", "", "", (), (), "", g, 0, **kw)


def test_precision_rechecks_quotes_against_the_stored_source():
    report = {"answers": [{"sentences": [{"evidence": [
        {"paper_key": "doi:10.1/t", "quote": "improved scores on a delayed classroom test"},
        {"paper_key": "doi:10.1/t", "quote": "improved scores on every test ever"},
    ]}]}]}
    r = BM.citation_precision(report, {"card-t": _card("abstract")})
    assert (r["claims"], r["verified"], r["precision"]) == (2, 1, 0.5)
    pdf = _card("pdf", read_url="https://arxiv.org/pdf/1", raw_sha256="aa")
    doc = SimpleNamespace(raw_sha256="aa", text="--- page 1 ---\nWe found it improved scores on a delayed classroom test here.")
    assert BM.citation_precision(report, {"card-t": pdf}, load_cached=lambda url: doc)["verified"] == 1
    changed = SimpleNamespace(raw_sha256="bb", text=doc.text)
    assert BM.citation_precision(report, {"card-t": pdf}, load_cached=lambda url: changed)["verified"] == 0
    assert BM.citation_precision(report, {"card-t": pdf}, load_cached=lambda url: None)["verified"] == 0


def test_run_item_end_to_end_with_fakes(tmp_path):
    item = BM.load_items()[0]
    p1 = Paper(title="Distributed practice in verbal recall tasks: A review and quantitative synthesis",
               authors=("Nicholas J. Cepeda",), year=2006, doi="10.1037/0033-2909.132.3.354", venue="Psych Bull",
               venue_type="journal", cited_by=3000, is_review=True,
               abstract="Spaced study produced better long-term retention than massed study in 254 studies.")
    p2 = Paper(title="Spacing effects in learning", authors=("Nicholas J. Cepeda", "Ed Vul"), year=2008,
               doi="10.1111/j.1467-9280.2008.02209.x", venue="Psych Sci", venue_type="journal", cited_by=900,
               abstract="The best gap between study sessions increased as the retention interval increased.")

    class Client:
        requests_sent = {OPENALEX: 3}

        def wikipedia_article(self, topic):
            return None

        def top_reviews(self, topic, n=2):
            return SearchResult(papers=[p1], counts={OPENALEX: 1})

        def search(self, query, per_source=10):
            return SearchResult(papers=[p1, p2], counts={OPENALEX: 2})

        def s2_neighbours(self, paper, direction="references", limit=20):
            return []

    def read(p):
        return PaperText(p.key, "abstract", p.url, text=p.abstract, sections=(Section("Abstract", 0, len(p.abstract)),))

    def complete(prompt):
        if "one-page context brief" in prompt:
            return json.dumps({"search_terms": ["spacing effect"]})
        if "make a short, factual card" in prompt:
            ab = re.search(r"TEXT:\n(.*)\n\nReply", prompt, re.S).group(1).split("]\n", 1)[-1].strip()
            return json.dumps({"findings": [{"text": ab, "quote": " ".join(ab.split()[:10])}]})
        if "SUB-QUESTION:" in prompt:
            refs = re.findall(r"\[(E\d+)\][^\n]*\n\s+Quote: \"([^\"]+)\"", prompt)
            return json.dumps({"sentences": [{"text": q + ".", "cites": [r]} for r, q in refs[:1]]})
        return "CONTRADICTION: no\nNOTE: fine"

    res = BM.run_item(item, client=Client(), complete=complete, library=InterimLibrary(tmp_path / "l.db"),
                      now_year=2026, read=read)
    assert res["papers_read"] == 2 and res["abstract_only"] == 2
    assert res["recall"]["found"] == 2 and res["recall"]["references"] == 3
    assert res["precision"]["claims"] >= 1 and res["precision"]["precision"] == 1.0
    assert res["stop_reason"].startswith(("converged", "budget"))
    assert res["owner_grade"] is None
    md = BM.summary_markdown([res])
    assert "| spacing |" in md and "Owner grades: not given yet." in md
