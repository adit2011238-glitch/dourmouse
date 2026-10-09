"""FS2 P3-51: deeply nested HTML must not raise RecursionError."""

from __future__ import annotations

from dourmouse.research_pipeline import acquire
from dourmouse.research_pipeline.extract_html import extract_main


def _deep(n: int, inner: str = "hello world " * 50) -> str:
    return "<html><head><title>T</title></head><body>" + "<div>" * n + inner


def test_unclosed_divs_do_not_raise_and_keep_the_text():
    doc = extract_main(_deep(1500))
    assert "hello world" in doc.text and doc.title == "T"


def test_very_deep_nesting_is_linear_enough():
    doc = extract_main(_deep(60000))
    assert "hello world" in doc.text


def test_deep_nesting_of_blocks_and_headings_keeps_structure():
    html = "<body>" + "<div>" * 1200 + "<h2>Top</h2><p>" + "alpha beta, gamma. " * 20 + "</p><h3>Sub</h3><p>" + "delta epsilon, zeta. " * 20
    doc = extract_main(html)
    kinds = [(b.kind, b.heading_path) for b in doc.blocks]
    assert ("heading", ("Top",)) in kinds
    assert any(k == "paragraph" and p == ("Top", "Sub") for k, p in kinds)


def test_html_to_text_and_decode_survive_deep_pages():
    html = _deep(3000)
    assert "hello world" in acquire.html_to_text(html)
    text, _structure = acquire._decode(html.encode(), "html", "utf-8", acquire.Path("/nonexistent"))
    assert "hello world" in text


def test_normal_page_unchanged():
    doc = extract_main("<html><body><nav>menu</nav><article><h1>Hi</h1><p>" + "word, " * 60 + "</p></article></body></html>")
    assert doc.blocks[0].kind == "heading" and "menu" not in doc.text
