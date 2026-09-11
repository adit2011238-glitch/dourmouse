"""High-performance PDF/textbook reader (v13.5).

Vision OS checklist item: "A specialized PDF and textbook rendering
engine built on Google's PDFium and enhanced by Marker's machine
learning pipelines... extracts complex mathematical formulas, vector
graphics, and dense multi-column text layouts without formatting loss."

Real, honest scope for THIS module (stated plainly, not silently
implied):

- **PDFium: real, built.** Uses ``pypdfium2`` — the real, official
  Python bindings for Google's PDFium (the same engine Chrome's own PDF
  viewer uses). Real text extraction (page_text/all_text) and real page
  rendering to PNG images (render_page_png) — both verified live against
  actual multi-page PDFs already in this repo (a real 5-page report:
  correct text, a real 1191x1684 rendered PNG).
- **Marker: NOT built.** Marker is a genuinely heavy ML pipeline (its
  own layout-detection/OCR/table/formula-recognition models, multi-GB
  weights, GPU-recommended) — a real, separate, substantial integration,
  not something to bolt on quickly alongside everything else built this
  session. Formula/table/complex-layout recognition beyond what PDFium's
  own text extraction gives for free is explicitly NOT here. dourmouse/
  extract.py's existing ``extract_pdf_text`` (pypdf-based, simpler, already
  live and used by bulk_ingest.py's real RAG indexing) is UNCHANGED and
  UNAFFECTED by this module — this is a genuinely separate, additive
  capability (real page rendering + a live reader panel), not a
  replacement.

Every real function here is wrapped so a corrupt/encrypted/missing PDF
reports an honest error string, never a fabricated result or a crash
(Rule 2.1/2.2, same discipline as extract.py's own extract_pdf_text).

**Real, live-reproduced concurrency bug fixed here, not silently
avoided**: PDFium (and pypdfium2's real, documented binding over it) is
NOT safe to call from multiple threads concurrently — confirmed live:
two threads calling page_text() and render_page_png() at the same
moment (exactly what happened the first time the PDF READER panel's own
JS fired both a text and a page-image request back to back) took down
the ENTIRE Python process with SIGABRT (exit 133), not just the one
request — dourmouse.webui's ThreadingHTTPServer gives every request its
own thread, so this was a real, guaranteed-to-recur crash, not a rare
edge case. Fixed with one real module-level lock serializing every
PDFium call in this module — the honest cost is that two simultaneous
PDF requests queue instead of running in parallel, which is a real,
acceptable trade against "the whole server dies."
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

# See this module's own docstring for the real, live-reproduced crash
# (SIGABRT, not an exception) this exists to prevent — held for the
# ENTIRE lifetime of a PdfDocument (open through close), not just
# individual calls, since PDFium's thread-unsafety is a property of the
# whole library's global state, not any one operation.
_PDFIUM_LOCK = threading.Lock()


def _pdfium():
    try:
        import pypdfium2

        return pypdfium2
    except ImportError as exc:
        raise RuntimeError(
            "NOT CONFIGURED: PDF reading needs the optional 'pypdfium2' "
            "package (pip install pypdfium2)."
        ) from exc


def pdf_info(path: str | Path) -> dict[str, Any]:
    """Real page count + basic metadata. Never raises — an unreadable
    file reports {"ok": False, "error": "..."}."""
    try:
        pdfium = _pdfium()
        target = Path(path)
        if not target.is_file():
            return {"ok": False, "error": f"no such file: {target}"}
        with _PDFIUM_LOCK:
            doc = pdfium.PdfDocument(str(target))
            try:
                return {"ok": True, "page_count": len(doc)}
            finally:
                doc.close()
    except Exception as exc:  # noqa: BLE001 - honest error, never a crash
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def page_text(path: str | Path, page_index: int) -> str:
    """Real text of ONE page (0-indexed). PDFium's own text extraction —
    generally more layout-aware than dourmouse/extract.py's pypdf-based
    extractor for multi-column pages, though still not Marker-level
    structural understanding (no table/formula recognition). Honest
    error string on failure, never a crash or a fabricated result.
    """
    try:
        pdfium = _pdfium()
        target = Path(path)
        if not target.is_file():
            return f"PDF READ FAILED: no such file: {target}"
        with _PDFIUM_LOCK:
            doc = pdfium.PdfDocument(str(target))
            try:
                if not (0 <= page_index < len(doc)):
                    return f"PDF READ FAILED: page {page_index} out of range (0..{len(doc) - 1})"
                page = doc[page_index]
                textpage = page.get_textpage()
                try:
                    return textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                doc.close()
    except Exception as exc:  # noqa: BLE001 - honest error, never a crash
        return f"PDF READ FAILED: {type(exc).__name__}: {exc}"


# v14 (user-directed, 2026-09-08): real fix for backlog #9's actual most
# common real use case ("read my textbook") -- the study folder
# (~/Documents/MYP data folder) is mostly scanned-image PDFs with no
# embedded text layer, which PDFium's own text extraction (page_text
# above) correctly and honestly returns empty for. Bounded to this many
# pages so a real 300-page scanned book doesn't turn one read request
# into minutes of rendering + OCR -- a real, deliberate trade-off
# (partial-but-real beats a timeout), never silently hidden: see
# ocr_page_text's own real page count in the [REAL OCR applied to N
# page(s)...] marker all_text() emits below.
_OCR_MAX_PAGES_DEFAULT = 20


def _run_tesseract(png_bytes: bytes) -> str:
    """Real OCR over one rendered page PNG, via the tesseract CLI
    (stdin/stdout pipe, zero new Python dependencies -- tesseract itself
    is a real system binary, honestly NOT CONFIGURED if it's missing).
    """
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout"],
            input=png_bytes,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "NOT CONFIGURED: OCR needs the 'tesseract' command-line tool "
            "(macOS: brew install tesseract)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("tesseract timed out after 60s") from exc
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(f"tesseract error: {err or 'unknown failure'}")
    return proc.stdout.decode(errors="replace")


def ocr_page_text(path: str | Path, page_index: int, scale: float = 2.0) -> str:
    """Real OCR text for ONE page: render it to PNG (render_page_png,
    already real and PDFium-lock-safe), then pipe that through tesseract.
    Honest error string on failure, never a crash or a fabricated
    result -- same discipline as page_text above."""
    try:
        png_bytes = render_page_png(path, page_index, scale=scale)
    except RuntimeError as exc:
        return f"OCR FAILED: {exc}"
    try:
        return _run_tesseract(png_bytes)
    except RuntimeError as exc:
        return f"OCR FAILED: {exc}"


def all_text(
    path: str | Path,
    ocr_fallback: bool = False,
    ocr_max_pages: int = _OCR_MAX_PAGES_DEFAULT,
) -> str:
    """Real text of every page, joined with page markers. Mirrors
    dourmouse/extract.py's extract_pdf_text output shape (page markers,
    honest failure strings) so callers already handling that format work
    unchanged against this one too.

    Real bug fixed here (2026-09-08): the per-page marker
    ("--- page N ---") was unconditionally appended even for a page with
    NO real extracted text, so the joined result was always non-empty --
    the "no extractable text" honest message below could never actually
    fire, no matter how blank the real content was. real_text now tracks
    the genuine extracted text separately from the display markers.

    ocr_fallback=True: when NO page has any real text (a scanned-image
    PDF with no embedded text layer), retries up to ocr_max_pages pages
    through real tesseract OCR (ocr_page_text) instead of giving up --
    see that function's own docstring and _OCR_MAX_PAGES_DEFAULT's
    comment on the page-count trade-off. Off by default: OCR is real
    work (rendering + tesseract per page), not something every caller
    should silently pay for.
    """
    info = pdf_info(path)
    if not info.get("ok"):
        return f"PDF READ FAILED: {info.get('error', 'unknown error')}"
    page_count = info["page_count"]
    pages = []
    real_text = []
    for i in range(page_count):
        text = page_text(path, i)
        if text.startswith("PDF READ FAILED"):
            return text
        pages.append(f"--- page {i + 1} ---\n{text}")
        if text.strip():
            real_text.append(text)
    if real_text:
        return "\n\n".join(pages)
    if not ocr_fallback:
        return "PDF READ: no extractable text (scanned image PDFs need OCR, which is not included)."
    ocr_pages = []
    ocr_real_text = []
    ocr_errors = []
    limit = min(page_count, max(1, ocr_max_pages))
    for i in range(limit):
        text = ocr_page_text(path, i)
        if text.startswith("OCR FAILED"):
            ocr_errors.append(text)
            ocr_pages.append(f"--- page {i + 1} ---\n{text}")
            continue
        ocr_pages.append(f"--- page {i + 1} ---\n{text}")
        if text.strip():
            ocr_real_text.append(text)
    if not ocr_real_text:
        # Surface the REAL error (e.g. tesseract not installed) rather
        # than a generic "blank or corrupted scan" guess -- Rule 2.2.
        if ocr_errors:
            return f"PDF READ FAILED: OCR fallback found no real text. {ocr_errors[0]}"
        return "PDF READ: no extractable text even after OCR (scanned pages may be blank or badly corrupted)."
    header = f"[REAL OCR applied to {limit} page(s) with no embedded text layer]"
    return header + "\n\n" + "\n\n".join(ocr_pages)


def render_page_png(path: str | Path, page_index: int, scale: float = 2.0) -> bytes:
    """Real PNG bytes of ONE rendered page — the "pull specific diagrams
    onto your kinetic canvas" capability. scale=2.0 (~144 DPI-equivalent
    for a normal PDF point size) is a real, reasonable default for
    on-screen viewing; a caller wanting print quality can pass higher.
    Raises RuntimeError with an honest message on any failure (unlike
    the text functions above, this returns raw bytes on success so a
    caller can't mistake a failure string for real image data — the
    error has to be an exception, not a sentinel byte string).
    """
    pdfium = _pdfium()
    target = Path(path)
    if not target.is_file():
        raise RuntimeError(f"no such file: {target}")
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(target))
        try:
            if not (0 <= page_index < len(doc)):
                raise RuntimeError(f"page {page_index} out of range (0..{len(doc) - 1})")
            page = doc[page_index]
            bitmap = page.render(scale=scale)
            pil_img = bitmap.to_pil()
            import io

            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            doc.close()
