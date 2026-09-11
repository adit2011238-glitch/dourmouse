"""dourmouse/pdf_reader.py — Vision OS checklist item, real PDFium-backed
PDF reading (text extraction + page rendering). See that module's own
docstring for what's real (PDFium via pypdfium2, live-verified against
an actual multi-page PDF this session) vs. explicitly not built (Marker's
ML-based formula/table/layout recognition — flagged, not silently
skipped).

Real, hermetic fixture: a hand-built minimal valid PDF (~580 bytes,
single page, one text run) constructed directly in this file rather than
depending on a real PDF checked into a git submodule that might not be
present in every checkout (this repo's own atlas-strategy-lab/ has real
sample PDFs, live-verified once against pdf_reader.py while building it,
but a test file must not depend on submodule content being checked out).
Verified live before writing these tests: pypdfium2 opens this exact
fixture and extracts "Hello PDF" correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pypdfium2 = pytest.importorskip("pypdfium2")

from dourmouse import pdf_reader


def _write_minimal_pdf(path: Path, text: str = "Hello PDF") -> None:
    """A real, minimal, valid single-page PDF with one text run — hand-
    built (not via a PDF-writing library, none is a dependency of this
    project) using the well-known minimal-PDF object structure: catalog
    -> pages -> one page -> a content stream drawing `text`, plus a real
    xref table with correct byte offsets."""
    content = f"BT /F1 18 Tf 10 50 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R >> >> "
        b"/MediaBox [0 0 200 100] /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(pdf)
    pdf += f"xref\n0 {len(objs) + 1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    path.write_bytes(pdf)


@pytest.fixture
def real_pdf(tmp_path) -> Path:
    p = tmp_path / "test.pdf"
    _write_minimal_pdf(p, "Hello PDF")
    return p


class TestPdfInfo:
    def test_real_page_count(self, real_pdf):
        info = pdf_reader.pdf_info(real_pdf)
        assert info == {"ok": True, "page_count": 1}

    def test_missing_file_is_honest(self, tmp_path):
        info = pdf_reader.pdf_info(tmp_path / "does-not-exist.pdf")
        assert info["ok"] is False
        assert "no such file" in info["error"]

    def test_corrupt_file_is_honest_not_a_crash(self, tmp_path):
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"this is not a real PDF at all")
        info = pdf_reader.pdf_info(bad)
        assert info["ok"] is False
        assert info["error"]  # some real error message, not fabricated success


class TestPageText:
    def test_real_text_extraction(self, real_pdf):
        text = pdf_reader.page_text(real_pdf, 0)
        assert text == "Hello PDF"

    def test_out_of_range_page_is_honest(self, real_pdf):
        text = pdf_reader.page_text(real_pdf, 5)
        assert text.startswith("PDF READ FAILED")
        assert "out of range" in text

    def test_missing_file_is_honest(self, tmp_path):
        text = pdf_reader.page_text(tmp_path / "nope.pdf", 0)
        assert text.startswith("PDF READ FAILED")


class TestAllText:
    def test_real_full_document_text_with_page_markers(self, real_pdf):
        text = pdf_reader.all_text(real_pdf)
        assert "--- page 1 ---" in text
        assert "Hello PDF" in text

    def test_missing_file_propagates_the_honest_error(self, tmp_path):
        text = pdf_reader.all_text(tmp_path / "nope.pdf")
        assert text.startswith("PDF READ FAILED")


class TestConcurrentThreadSafety:
    """Real, live-reproduced bug (see pdf_reader.py's own module
    docstring): PDFium is NOT safe to call from multiple threads at
    once — two threads calling page_text()/render_page_png()
    concurrently crashed the ENTIRE Python process with SIGABRT (exit
    133), not a catchable Python exception. dourmouse.webui's
    ThreadingHTTPServer gives every request its own thread, so this was
    guaranteed to recur, not a rare edge case — confirmed live: the PDF
    READER panel's own JS fires a text request and a page-image request
    back to back, and the very first real use of it took the whole
    server down.

    Run as a REAL SUBPROCESS (not in-process threads) on purpose: if the
    _PDFIUM_LOCK fix ever regresses, the failure mode is a genuine
    SIGABRT, not a Python exception pytest can catch cleanly — a
    subprocess crashing shows up as a normal, readable test FAILURE
    (returncode != 0) instead of taking the whole pytest run down with
    it.
    """

    def test_concurrent_text_and_render_calls_never_crash_the_process(self, real_pdf):
        script = f'''
import threading
from dourmouse import pdf_reader

path = {str(real_pdf)!r}

def worker(kind):
    if kind == "text":
        r = pdf_reader.page_text(path, 0)
        assert r == "Hello PDF", r
    else:
        r = pdf_reader.render_page_png(path, 0)
        assert r.startswith(b"\\x89PNG"), "not a real PNG"

threads = [threading.Thread(target=worker, args=(k,)) for k in ["text", "png"] * 5]
for t in threads: t.start()
for t in threads: t.join()
print("ALL_OK")
'''
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"subprocess crashed (returncode={result.returncode}, likely SIGABRT if negative) — "
            f"stdout={result.stdout!r} stderr={result.stderr[-2000:]!r}"
        )
        assert "ALL_OK" in result.stdout


class TestOcrPageText:
    """v14 (user-directed, 2026-09-08): real, unmocked tesseract — this
    machine has it installed (see pdf_reader._run_tesseract's own NOT
    CONFIGURED path for what happens when it isn't). Uses the same
    real_pdf fixture: it has a real embedded text layer, but OCR reads
    the RENDERED PIXELS regardless of that layer's presence, so this
    proves the real render -> tesseract pipeline actually recognizes
    real text, not just that it runs without crashing."""

    def test_real_ocr_recognizes_real_text(self, real_pdf):
        pytest.importorskip("shutil").which("tesseract") or pytest.skip("tesseract not installed")
        text = pdf_reader.ocr_page_text(real_pdf, 0)
        assert not text.startswith("OCR FAILED")
        assert "Hello PDF" in text or "Hello" in text

    def test_missing_file_is_honest(self, tmp_path):
        text = pdf_reader.ocr_page_text(tmp_path / "nope.pdf", 0)
        assert text.startswith("OCR FAILED")

    def test_out_of_range_page_is_honest(self, real_pdf):
        text = pdf_reader.ocr_page_text(real_pdf, 99)
        assert text.startswith("OCR FAILED")
        assert "out of range" in text

    def test_missing_tesseract_reports_not_configured(self, real_pdf, monkeypatch):
        def fake_run(*a, **k):
            raise FileNotFoundError("no such file: tesseract")

        monkeypatch.setattr(pdf_reader.subprocess, "run", fake_run)
        text = pdf_reader.ocr_page_text(real_pdf, 0)
        assert text.startswith("OCR FAILED")
        assert "NOT CONFIGURED" in text

    def test_tesseract_timeout_is_honest(self, real_pdf, monkeypatch):
        import subprocess as _subprocess

        def fake_run(*a, **k):
            raise _subprocess.TimeoutExpired(cmd="tesseract", timeout=60)

        monkeypatch.setattr(pdf_reader.subprocess, "run", fake_run)
        text = pdf_reader.ocr_page_text(real_pdf, 0)
        assert text.startswith("OCR FAILED")
        assert "timed out" in text


class TestAllTextOcrFallback:
    """v14 (user-directed, 2026-09-08): real gap found this session —
    the study folder's own real content (~/Documents/MYP data folder)
    is mostly scanned-image textbooks with no embedded text layer, so
    "read my textbook" always hit the honest-but-useless "needs OCR,
    which is not included" message. Monkeypatches ocr_page_text
    directly (never touches real tesseract here) to exercise the
    all_text() branching deterministically."""

    def _blank_pdf(self, tmp_path) -> Path:
        # A real, valid PDF with an EMPTY content stream — a genuinely
        # blank page, exactly what a scanned-image PDF with no text
        # layer looks like to PDFium's own text extraction.
        p = tmp_path / "blank.pdf"
        _write_minimal_pdf(p, "")
        return p

    def test_ocr_fallback_off_by_default_reports_the_old_honest_message(self, tmp_path):
        blank = self._blank_pdf(tmp_path)
        text = pdf_reader.all_text(blank)
        assert "need OCR" in text

    def test_ocr_fallback_on_uses_real_ocr_when_no_text_layer_exists(self, tmp_path, monkeypatch):
        blank = self._blank_pdf(tmp_path)
        monkeypatch.setattr(pdf_reader, "ocr_page_text", lambda path, i, scale=2.0: "OCR recovered text")
        text = pdf_reader.all_text(blank, ocr_fallback=True)
        assert "REAL OCR applied" in text
        assert "OCR recovered text" in text

    def test_ocr_fallback_never_runs_when_a_real_text_layer_exists(self, real_pdf, monkeypatch):
        """A page WITH real extractable text must never even attempt
        OCR — real_pdf's own text ("Hello PDF") should short-circuit
        before ocr_page_text is ever called."""
        called = []
        monkeypatch.setattr(
            pdf_reader, "ocr_page_text", lambda path, i, scale=2.0: called.append(i) or "x"
        )
        text = pdf_reader.all_text(real_pdf, ocr_fallback=True)
        assert called == []
        assert "Hello PDF" in text

    def test_ocr_fallback_surfaces_the_real_error_when_ocr_itself_fails(self, tmp_path, monkeypatch):
        blank = self._blank_pdf(tmp_path)
        monkeypatch.setattr(
            pdf_reader, "ocr_page_text",
            lambda path, i, scale=2.0: "OCR FAILED: NOT CONFIGURED: tesseract missing",
        )
        text = pdf_reader.all_text(blank, ocr_fallback=True)
        assert text.startswith("PDF READ FAILED")
        assert "NOT CONFIGURED" in text


class TestRenderPagePng:
    def test_real_png_bytes_are_produced(self, real_pdf):
        png = pdf_reader.render_page_png(real_pdf, 0)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")  # the real PNG magic bytes
        assert len(png) > 100  # a real image, not an empty/stub file

    def test_higher_scale_produces_more_bytes(self, real_pdf):
        # Not a pixel-perfect check -- just proves `scale` is real and
        # actually affects the render, not a silently-ignored parameter.
        small = pdf_reader.render_page_png(real_pdf, 0, scale=1.0)
        large = pdf_reader.render_page_png(real_pdf, 0, scale=4.0)
        assert len(large) > len(small)

    def test_missing_file_raises_a_real_error(self, tmp_path):
        with pytest.raises(RuntimeError, match="no such file"):
            pdf_reader.render_page_png(tmp_path / "nope.pdf", 0)

    def test_out_of_range_page_raises_a_real_error(self, real_pdf):
        with pytest.raises(RuntimeError, match="out of range"):
            pdf_reader.render_page_png(real_pdf, 99)
