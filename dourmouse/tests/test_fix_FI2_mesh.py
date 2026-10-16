"""FI2: research mesh findings (P5-43, P3-49, P4-51, P3-48)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from dourmouse.research_mesh.brain import BrainAnswer, MockBrain
from dourmouse.research_mesh.core import AgentRecord, Status
from dourmouse.research_mesh.exams import build_iterations, grade_iteration
from dourmouse.research_mesh.pipeline import QualificationPipeline
from dourmouse.research_mesh.store import AgentStore
from dourmouse.research_mesh.study import StudyEngine, load_corpus


def _pdf_with_text(text: str) -> bytes:
    """A minimal but valid one-page PDF whose page shows `text`."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def _corpus(tmp_path: Path, papers: dict[str, tuple[bytes | str, bytes | str | None]]):
    folder = tmp_path / "Mathematics" / "Algebra"
    (folder / "tests").mkdir(parents=True)
    (folder / "keys").mkdir()
    for name, (paper, key) in papers.items():
        p = folder / "tests" / name
        p.write_bytes(paper if isinstance(paper, bytes) else paper.encode())
        if key is not None:
            k = folder / "keys" / name
            k.write_bytes(key if isinstance(key, bytes) else key.encode())
    return load_corpus(tmp_path, "Mathematics", "Algebra")


# ---------------------------------------------------------------- P5-43

@pytest.fixture
def corpus(tmp_path):
    return _corpus(tmp_path, {
        "qual_2023.pdf": ("exam one", "homomorphisms kernels quotient groups"),
        "p1": ("exam short id", None),
    })


@pytest.mark.parametrize("fake", ["pdf", "http", "2019", "a", "qual", "p1 xyz paper-12 p1", ".pdf"])
def test_short_or_containing_citations_are_fabricated(corpus, fake):
    it = build_iterations(corpus)[0]
    g = grade_iteration(corpus, it, BrainAnswer(
        text="homomorphisms kernels quotient groups", citations=(fake,)))
    assert g.fabricated_citations == 1
    assert not g.citations_verified


@pytest.mark.parametrize("real", ["qual_2023.pdf", "QUAL_2023.PDF", " qual_2023.pdf ",
                                  "tests/qual_2023.pdf", "'qual_2023.pdf'"])
def test_real_citations_still_verify(corpus, real):
    it = build_iterations(corpus)[0]
    g = grade_iteration(corpus, it, BrainAnswer(
        text="homomorphisms kernels quotient groups", citations=(real,)))
    assert g.citations_verified and g.fabricated_citations == 0


def test_url_citation_requires_equal_url(tmp_path):
    from dourmouse.research_mesh.study import FieldCorpus, Paper
    p = Paper(id="x.pdf", path=tmp_path / "x.pdf", url="https://example.org/exams/x.pdf",
              has_key=False, key_path=None)
    c = FieldCorpus(domain="d", field="f", folder=tmp_path, papers=(p,))
    it = build_iterations(c)[0]
    ok = grade_iteration(c, it, BrainAnswer("t" * 200, ("https://example.org/exams/x.pdf/",)))
    assert ok.citations_verified
    bad = grade_iteration(c, it, BrainAnswer("t" * 200, ("https://example.org",)))
    assert not bad.citations_verified


# ---------------------------------------------------------------- P3-49

def test_real_pdf_key_is_text_extracted_before_grading(tmp_path):
    key_words = "homomorphisms kernels quotient groups isomorphism theorem"
    c = _corpus(tmp_path, {"qual_2023.pdf": ("exam", _pdf_with_text(key_words))})
    it = build_iterations(c)[0]
    g = grade_iteration(c, it, BrainAnswer(text=key_words, citations=(it.paper.id,)))
    assert g.score >= 0.9, g.feedback
    assert g.passed
    assert "no official key" not in g.feedback


def test_unextractable_key_is_flagged_not_graded_as_binary(tmp_path):
    c = _corpus(tmp_path, {"qual_2023.pdf": ("exam", b"%PDF-1.4\n\x00\x01\x02 garbage not a pdf")})
    it = build_iterations(c)[0]
    g = grade_iteration(c, it, BrainAnswer(text="x" * 200, citations=(it.paper.id,)))
    assert "could not be read" in g.feedback
    # honest label, not a fake key score of ~0
    assert "key-graded" not in g.feedback


def test_practice_scores_use_extracted_key_text(tmp_path):
    key_words = "homomorphisms kernels quotient groups isomorphism theorem"
    c = _corpus(tmp_path, {"qual_2023.pdf": (_pdf_with_text("exam"), _pdf_with_text(key_words))})
    brain = MockBrain()
    brain._facts["qual_2023.pdf"] = key_words
    eng = StudyEngine(c, brain)
    eng._practice_all()
    assert eng._practice_scores == [1.0]
    rec = AgentRecord(domain="Mathematics", field="Algebra", status=Status.STUDYING,
                      passed_iterations=("qual_2023.pdf",))
    eng2 = StudyEngine(c, brain)
    eng2._practice(rec, {"qual_2023.pdf"})
    assert eng2._practice_scores == [1.0]


def test_practice_skips_unextractable_key(tmp_path):
    c = _corpus(tmp_path, {"qual_2023.pdf": ("exam", b"%PDF-1.4\n\x00\x01 garbage")})
    brain = MockBrain()
    brain._facts["qual_2023.pdf"] = "anything"
    eng = StudyEngine(c, brain)
    eng._practice_all()
    assert eng._practice_scores == []


# ---------------------------------------------------------------- P4-51

def _resumable(tmp_path):
    c = _corpus(tmp_path, {
        "qual_2023.pdf": ("PhD exam 2023 groups.", "groups homomorphisms kernels"),
        "qual_2024.pdf": ("PhD exam 2024 rings.", "rings ideals quotient"),
    })
    return c


def test_resumed_pipeline_restores_what_the_brain_studied(tmp_path):
    c = _resumable(tmp_path)
    store = AgentStore(tmp_path / "q.db")
    rec = AgentRecord(domain="Mathematics", field="Algebra", status=Status.READY,
                      passed_iterations=("qual_2023.pdf",))
    brain = MockBrain()  # fresh brain: knows nothing
    pipe = QualificationPipeline(store, brain, c)
    pipe.step(rec)
    assert "qual_2023.pdf" in brain._facts          # passed paper restored
    assert "qual_2024.pdf" not in brain._facts      # held-out stays held out


def test_resume_keeps_remediated_paper_but_not_future_keys(tmp_path):
    c = _resumable(tmp_path)
    store = AgentStore(tmp_path / "q.db")
    rec = AgentRecord(domain="Mathematics", field="Algebra", status=Status.TESTING,
                      current_iteration="qual_2023.pdf", attempts_on_iteration=1)
    brain = MockBrain()
    QualificationPipeline(store, brain, c).step(rec)
    assert "qual_2023.pdf" in brain._facts  # remediated earlier: allowed again
    assert "qual_2024.pdf" not in brain._facts


def test_unlearned_record_is_not_double_studied(tmp_path):
    c = _resumable(tmp_path)
    store = AgentStore(tmp_path / "q.db")
    brain = MockBrain()
    calls = []
    orig = brain.study
    brain.study = lambda s, cc: (calls.append(1), orig(s, cc))[1]
    pipe = QualificationPipeline(store, brain, c)
    pipe.step(AgentRecord(domain="Mathematics", field="Algebra"))
    assert len(calls) == 3  # curriculum, ingestion, gap closure; no extra restore


def test_qualify_tool_restores_brain_on_second_call(tmp_path, monkeypatch):
    from dourmouse import research_mesh_tools as tools
    _resumable(tmp_path)
    store = AgentStore(tmp_path / "q.db")
    store.save(AgentRecord(domain="Mathematics", field="Algebra", status=Status.READY,
                           passed_iterations=("qual_2023.pdf",)), 0.0)
    seen: list[set[str]] = []

    class Spy(MockBrain):
        def answer(self, question):
            seen.append(set(self._facts))
            return super().answer(question)

    monkeypatch.setattr(tools, "PAPERS_ROOT", tmp_path)
    monkeypatch.setattr(tools, "default_db", lambda: tmp_path / "q.db")
    monkeypatch.setattr(tools, "RealBrain", Spy)
    tools._research_mesh_qualify({"domain": "Mathematics", "field": "Algebra", "max_steps": 2})
    assert seen and "qual_2023.pdf" in seen[0]


# ---------------------------------------------------------------- P3-48

def test_testing_state_with_missing_paper_resets_instead_of_stopiteration(tmp_path, caplog):
    c = _resumable(tmp_path)
    store = AgentStore(tmp_path / "q.db")
    rec = AgentRecord(domain="Mathematics", field="Algebra", status=Status.TESTING,
                      current_iteration="removed_paper_2019.pdf")
    pipe = QualificationPipeline(store, MockBrain(), c)
    with caplog.at_level(logging.WARNING):
        status = pipe.step(rec)
    assert status is Status.READY
    assert rec.current_iteration is None and rec.attempts_on_iteration == 0
    assert any("removed_paper_2019.pdf" in r.getMessage() for r in caplog.records)
    # and it persisted, so a resume does not repeat the crash
    assert store.load("Mathematics", "Algebra").status is Status.READY


def test_testing_state_with_already_passed_paper_resets(tmp_path):
    c = _resumable(tmp_path)
    store = AgentStore(tmp_path / "q.db")
    rec = AgentRecord(domain="Mathematics", field="Algebra", status=Status.TESTING,
                      current_iteration="qual_2023.pdf",
                      passed_iterations=("qual_2023.pdf",))
    status = QualificationPipeline(store, MockBrain(), c).step(rec)
    assert status is Status.READY
