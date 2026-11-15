"""Finding 206: passage chunking keeps page numbers and headings and overlaps cut paragraphs."""

from __future__ import annotations

import pytest

from dourmouse.library.chunking import chunk_text, page_marker


def words(n, prefix="w"):
    return " ".join(f"{prefix}{i}" for i in range(n))


def test_short_paragraphs_join_into_one_passage_under_the_limit():
    out = chunk_text("alpha beta\n\ngamma delta")
    assert len(out) == 1 and out[0].text == "alpha beta gamma delta"


def test_passage_never_exceeds_target_words_and_paragraphs_stay_whole():
    text = "\n\n".join(words(150, f"p{i}x") for i in range(5))
    out = chunk_text(text, target_words=400)
    assert all(len(c.text.split()) <= 400 for c in out)
    assert [len(c.text.split()) for c in out] == [300, 300, 150]


def test_a_long_paragraph_is_cut_with_overlap_so_the_seam_is_in_both():
    out = chunk_text(words(1000), target_words=400, overlap_words=40)
    assert [len(c.text.split()) for c in out] == [400, 400, 280]
    assert out[0].text.split()[-40:] == out[1].text.split()[:40]


def test_headings_are_kept_with_the_passages_under_them():
    text = "# Methods\n\nWe sampled soil.\n\n# Results\n\nGrowth doubled."
    out = chunk_text(text, target_words=20)
    assert out[0].heading == "Results" or out[0].heading == "Methods"
    big = chunk_text("# Methods\n\n" + words(50) + "\n\n# Results\n\n" + words(50, "r"), target_words=60)
    assert [c.heading for c in big] == ["Methods", "Results"]


def test_page_markers_set_the_page_and_are_not_part_of_the_text():
    text = f"{page_marker(1)}\n\nfirst page words\n\n{page_marker(2)}\n\nsecond page words"
    out = chunk_text(text, target_words=20)
    assert "[[page" not in " ".join(c.text for c in out)
    big = chunk_text(f"{page_marker(3)}\n\n{words(30)}\n\n{page_marker(4)}\n\n{words(30, 'q')}", target_words=40)
    assert [c.page for c in big] == [3, 4]


def test_form_feeds_count_pages():
    big = chunk_text(f"{words(30)}\f{words(30, 'q')}", target_words=40)
    assert [c.page for c in big] == [None, 2] or [c.page for c in big] == [1, 2]


def test_empty_and_tiny_targets():
    assert chunk_text("") == [] and chunk_text("  \n\n ") == []
    with pytest.raises(ValueError):
        chunk_text("x", target_words=5)
