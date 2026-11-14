"""PL-10 T1 step 2: reading open-access PDFs, and the interim library port."""

from __future__ import annotations

import urllib.error
from types import SimpleNamespace

import pytest

from dourmouse.research_pipeline import reading as R
from dourmouse.research_pipeline.library_port import InterimLibrary, split_passages
from dourmouse.research_pipeline.papers import Paper

PDF_TEXT = (
    "--- page 1 ---\n"
    "Spacing effects in learning\n"
    "Abstract. Spaced study produced better retention than massed study.\n"
    "1 Introduction\n"
    "Distributed practice has a long history in experi-\nmental psychology and long-\nterm memory research.\n"
    "--- page 2 ---\n"
    "2 Method\n"
    "Participants (N = 1354) studied 32 obscure facts.\n"
    "3 Results\n"
    "The optimal gap increased as the test delay increased, with ﬁnal recall at 64% versus 37%.\n"
    "--- page 3 ---\n"
    "4 Discussion\n"
    "These “spacing” results matter for classrooms.\n"
    "References\n"
    "Ebbinghaus, H. (1885). Memory: A contribution to experimental psychology.\n"
) + "Filler sentence about nothing in particular. " * 20


def _doc(text=PDF_TEXT, kind="pdf", **kw):
    return SimpleNamespace(text=text, kind=kind, final_url=kw.get("final_url", "https://arxiv.org/pdf/0801.00001"),
                           raw_sha256="ab" * 32, truncated=kw.get("truncated", False))


PAPER = Paper(title="Spacing effects in learning", authors=("Nicholas J. Cepeda",), year=2008,
              arxiv_id="0801.00001", abstract="Spaced study produced better retention than massed study.",
              oa_pdf_urls=("https://arxiv.org/pdf/0801.00001", "https://example.org/b.pdf"))


def test_reads_the_first_pdf_and_finds_sections_and_pages():
    r = R.read_paper(PAPER, fetch=lambda url: _doc())
    assert r.read_from == "pdf" and r.source_url == "https://arxiv.org/pdf/0801.00001"
    names = [s.name for s in r.sections]
    assert names[:5] == ["Front matter", "Abstract", "Introduction", "Method", "Results"]
    assert "References" in names
    assert [n for n, _ in r.pages] == [1, 2, 3]
    assert "--- page" not in r.text


def test_references_are_kept_out_of_the_body_and_out_of_quote_checks():
    r = R.read_paper(PAPER, fetch=lambda url: _doc())
    assert "Ebbinghaus" not in r.body()
    assert not R.quote_in_text("Memory: A contribution to experimental psychology", r.checkable_text())
    assert R.quote_in_text("Participants (N = 1354) studied 32 obscure facts.", r.checkable_text())


def test_quote_check_handles_ligatures_hyphens_and_curly_quotes_but_stays_exact():
    t = R.read_paper(PAPER, fetch=lambda url: _doc()).checkable_text()
    assert R.quote_in_text("with final recall at 64% versus 37%", t)  # fi ligature in the PDF
    assert R.quote_in_text("a long history in experimental psychology", t)  # experi-\nmental
    assert R.quote_in_text("long-term memory research", t)  # long-\nterm
    assert R.quote_in_text('These "spacing" results matter', t)  # curly quotes in the PDF
    assert not R.quote_in_text("with final recall at 65% versus 37%", t)
    assert not R.quote_in_text("", t)


def test_locate_names_the_section_and_pdf_page():
    r = R.read_paper(PAPER, fetch=lambda url: _doc())
    assert r.locate("The optimal gap increased as the test delay increased") == "Results, PDF page 2"
    assert r.locate("not in the paper at all") == ""


def test_a_landing_page_robots_refusal_and_network_error_fall_back_to_the_abstract():
    from dourmouse.research_pipeline.politeness import RobotsDisallowed

    seen: list[str] = []

    def fetch(url):
        seen.append(url)
        if url.endswith("0801.00001"):
            raise RobotsDisallowed("no")
        return _doc(kind="html")

    r = R.read_paper(PAPER, fetch=fetch)
    assert r.read_from == "abstract" and r.text == PAPER.abstract
    assert "robots.txt" in r.note and "html page, not a PDF" in r.note
    assert r.locate("better retention than massed study") == "Abstract"

    def offline(url):
        raise urllib.error.URLError("offline")

    assert "URLError" in R.read_paper(PAPER, fetch=offline).note


def test_a_pdf_without_a_text_layer_is_not_used():
    r = R.read_paper(PAPER, fetch=lambda url: _doc(text="--- page 1 ---\n \n"))
    assert r.read_from == "abstract" and "no text layer" in r.note


def test_no_pdf_and_no_abstract_is_said_plainly():
    r = R.read_paper(Paper(title="Paywalled", doi="10.1/x"))
    assert r.read_from == "none" and "no abstract" in r.note


def test_a_truncated_pdf_is_marked():
    r = R.read_paper(PAPER, fetch=lambda url: _doc(truncated=True))
    assert r.truncated and "only the start" in r.note


@pytest.mark.parametrize("line,name", [
    ("1 Introduction", "Introduction"), ("2. Methods", "Methods"), ("III. RESULTS", "Results"),
    ("Materials and Methods", "Materials And Methods"), ("General Discussion", "General Discussion"),
    ("Appendix A", "Appendix"),
])
def test_heading_forms(line, name):
    secs = R.find_sections("Title line\n" + line + "\nbody text here\n" * 30)
    assert name in [s.name for s in secs]


def test_a_sentence_mentioning_results_is_not_a_heading():
    secs = R.find_sections("The results of this study were large and clear for everyone.\n" * 10)
    assert [s.name for s in secs] == ["Body"]


# ---------------------------------------------------------------- library port


def test_split_passages_packs_paragraphs_and_cuts_long_ones():
    text = "\n\n".join(["Short paragraph one."] * 3 + ["Sentence number one. " * 120])
    parts = split_passages(text, size=300)
    assert all(len(p) <= 300 for p in parts)
    assert "".join(parts).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_library_add_search_cite_and_idempotent_ids(tmp_path):
    lib = InterimLibrary(tmp_path / "lib.db")
    ids = lib.add("https://doi.org/10.1/x", "Spaced practice improves retention.\n\nMassed practice does not.",
                  {"paper_key": "doi:10.1/x", "job_id": "g1"})
    again = lib.add("https://doi.org/10.1/x", "Spaced practice improves retention.\n\nMassed practice does not.",
                    {"paper_key": "doi:10.1/x", "job_id": "g1"})
    assert ids == again and lib.count() == len(ids)
    lib.add("https://doi.org/10.1/y", "Sleep consolidates memory.", {"paper_key": "doi:10.1/y", "job_id": "g2"})
    hits = lib.search("spaced retention", filters={"meta": {"job_id": "g1"}})
    assert hits and hits[0]["source"] == "https://doi.org/10.1/x"
    assert lib.search("sleep", filters={"source": ["https://doi.org/10.1/x"]}) == []
    c = lib.cite(hits[0]["passage_id"])
    assert "Spaced practice improves retention." in c["text"] and c["meta"]["paper_key"] == "doi:10.1/x"
    with pytest.raises(KeyError):
        lib.cite("p-missing")
    with pytest.raises(ValueError):
        lib.search("x", filters={"meta": {"bad key; drop": 1}})
    assert lib.search("the of and") == []


def test_store_reading_puts_sections_but_not_references_in_the_library(tmp_path):
    lib = InterimLibrary(tmp_path / "lib.db")
    r = R.read_paper(PAPER, fetch=lambda url: _doc())
    ids = R.store_reading(r, PAPER, lib, job_id="g1")
    assert ids
    texts = " ".join(lib.cite(i)["text"] for i in ids)
    assert "Ebbinghaus" not in texts and "1354" in texts
    sections = {lib.cite(i)["meta"]["section"] for i in ids}
    assert "Results" in sections and "References" not in sections
