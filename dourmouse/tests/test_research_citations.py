"""APA 7 (default) and MLA 9 references and in-text citations."""

from __future__ import annotations

import pytest

from dourmouse.research_pipeline import citations as C
from dourmouse.research_pipeline.papers import Paper

JOURNAL = Paper(title="Spacing effects in learning: A temporal ridgeline of optimal retention",
                authors=("Nicholas J. Cepeda", "Edward Vul", "Doug Rohrer", "John T. Wixted", "Harold Pashler"),
                year=2008, venue="Psychological Science", venue_type="journal", volume="19", issue="11",
                pages="1095-1102", doi="10.1111/j.1467-9280.2008.02209.x")
PREPRINT = Paper(title="Attention Is All You Need", authors=("Ashish Vaswani", "Noam Shazeer"), year=2017,
                 arxiv_id="1706.03762", venue="arXiv", venue_type="repository")


@pytest.mark.parametrize("raw,given,surname,suffix", [
    ("Nicholas J. Cepeda", "Nicholas J.", "Cepeda", ""),
    ("Ludwig van Beethoven", "Ludwig", "van Beethoven", ""),
    ("Martin Luther King Jr.", "Martin Luther", "King", "Jr."),
    ("Cepeda, Nicholas", "Nicholas", "Cepeda", ""),
    ("Plato", "", "Plato", ""),
])
def test_parse_name(raw, given, surname, suffix):
    assert C.parse_name(raw) == C.Name(given, surname, suffix)


def test_initials():
    assert C.initials("Nicholas J.") == "N. J."
    assert C.initials("Jean-Paul") == "J.-P."


def test_apa_journal_reference_and_in_text():
    assert C.reference(JOURNAL) == (
        "Cepeda, N. J., Vul, E., Rohrer, D., Wixted, J. T., & Pashler, H. (2008). Spacing effects in learning: "
        "A temporal ridgeline of optimal retention. *Psychological Science*, *19*(11), 1095-1102. "
        "https://doi.org/10.1111/j.1467-9280.2008.02209.x")
    assert C.in_text(JOURNAL) == "Cepeda et al., 2008"
    assert C.in_text(PREPRINT) == "Vaswani & Shazeer, 2017"


def test_apa_preprint_reference():
    assert C.reference(PREPRINT) == ("Vaswani, A., & Shazeer, N. (2017). *Attention Is All You Need* [Preprint]. "
                                     "arXiv. https://doi.org/10.48550/arXiv.1706.03762")


def test_mla_references_and_in_text():
    assert C.reference(JOURNAL, "mla") == (
        "Cepeda, Nicholas J., et al. \"Spacing effects in learning: A temporal ridgeline of optimal retention.\" "
        "*Psychological Science*, vol. 19, no. 11, 2008, pp. 1095-1102, doi.org/10.1111/j.1467-9280.2008.02209.x.")
    assert C.reference(PREPRINT, "mla") == ("Vaswani, Ashish, and Noam Shazeer. \"Attention Is All You Need.\" "
                                            "*arXiv*, 2017, doi.org/10.48550/arXiv.1706.03762.")
    assert C.in_text(JOURNAL, "mla") == "Cepeda et al."
    assert C.in_text(PREPRINT, "mla") == "Vaswani and Shazeer"


def test_missing_fields_are_left_out_not_invented():
    bare = Paper(title="A study", year=None, doi="10.1/x")
    assert C.reference(bare) == "A study. (n.d.). https://doi.org/10.1/x"
    assert C.in_text(bare) == "\"A study\", n.d."


def test_reference_list_sorts_dedupes_and_disambiguates_same_author_year():
    a = Paper(title="Beta study", authors=("Doug Rohrer",), year=2015, doi="10.1/b", venue="J", venue_type="journal")
    b = Paper(title="Alpha study", authors=("Doug Rohrer",), year=2015, doi="10.1/a", venue="J", venue_type="journal")
    entries, cites = C.reference_list([JOURNAL, a, b, a])
    assert len(entries) == 3 and entries[0].startswith("Cepeda")
    assert cites["doi:10.1/a"] == "Rohrer, 2015a" and cites["doi:10.1/b"] == "Rohrer, 2015b"
    assert any("(2015a). Alpha study." in e for e in entries)
    _, mla = C.reference_list([a, b], "mla")
    assert mla["doi:10.1/a"] == "Rohrer, \"Alpha study\""
    with pytest.raises(ValueError):
        C.reference_list([a], "chicago")
