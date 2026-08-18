"""Shared fixtures: a fake field corpus built from plain-text 'PDFs'.

The corpus files are plain text with .pdf extensions: load_corpus() only globs
names and MockBrain._file_text() reads non-%PDF bytes as text, so the pipeline
mechanics (study, held-out, grading, remediation) are tested without needing
real binary PDFs. The real corpus path is exercised separately by the pilot.
"""

from __future__ import annotations

import pytest

from ..study import load_corpus

# Two keyed papers, oldest first (chronological ordering via year in filename).
PAPERS = {
    "qual_2023.pdf": (
        "PhD qualifying exam 2023. This exam tests group theory homomorphisms "
        "kernels and quotient groups.",
        "homomorphisms kernels quotient groups",
    ),
    "qual_2024.pdf": (
        "PhD qualifying exam 2024. This exam tests ring theory ideals and "
        "maximal ideals quotient rings.",
        "ideals maximal ideals quotient rings",
    ),
}


@pytest.fixture
def corpus(tmp_path):
    """A materialized field folder: tests/ + keys/ with matching basenames."""
    folder = tmp_path / "Mathematics" / "Algebra"
    tests = folder / "tests"
    keys = folder / "keys"
    tests.mkdir(parents=True)
    keys.mkdir()
    for name, (paper_text, key_text) in PAPERS.items():
        (tests / name).write_text(paper_text)
        (keys / name).write_text(key_text)
    return load_corpus(tmp_path, "Mathematics", "Algebra")


@pytest.fixture
def no_key_corpus(tmp_path):
    """One paper with no published key (citation-gate grading path)."""
    tests = tmp_path / "Mathematics" / "Algebra" / "tests"
    tests.mkdir(parents=True)
    (tests / "qual_2025.pdf").write_text(
        "PhD qualifying exam 2025. Free response on module theory.")
    return load_corpus(tmp_path, "Mathematics", "Algebra")
