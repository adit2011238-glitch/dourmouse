"""extract.py — PDF text + receipt/invoice extraction (v5.x).

Small-business paperwork: turn an uploaded PDF (receipt, invoice) into
usable structured text. Uses ``pypdf`` (pure-Python, optional extra in
``requirements-extract.txt``) — when it is not installed the tools report
NOT CONFIGURED honestly (Rule 2.2), exactly like the calendar/voice
extras. Extraction itself is deterministic regex — the model never
invents fields it could not read.

Receipt parsing is honest best-effort: it reports the fields it found AND
the fields it could not parse, never a fabricated total.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _pypdf() -> Any:
    try:
        import pypdf
    except ImportError as exc:
        raise RuntimeError(
            "NOT CONFIGURED: PDF extraction needs the optional 'pypdf' "
            "package. Install it with: pip install -r requirements-extract.txt "
            "Nothing was extracted."
        ) from exc
    return pypdf


def extract_pdf_text(path: str | Path) -> str:
    """Extract text from a PDF (all pages, joined with page markers)."""
    target = Path(path)
    if not target.is_file():
        return f"ERROR: no such file: {target}"
    try:
        reader = _pypdf().PdfReader(str(target))
        # Finding P5-16: an encrypted PDF constructs fine and only raises
        # FileNotDecryptedError once its pages are touched, which was
        # outside this try. Many "encrypted" PDFs only carry an owner
        # password and open with an empty user password, so that is tried.
        if reader.is_encrypted and not reader.decrypt(""):
            return (
                "PDF READ FAILED: the PDF is password-protected (encrypted), "
                "so nothing was extracted. Remove the password and retry."
            )
        page_list = list(reader.pages)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - encrypted/corrupt PDFs, honest
        return f"PDF READ FAILED: {type(exc).__name__}: {exc}"
    pages = []
    # Real bug found live-testing this session, against a real 51MB scanned
    # textbook with zero text layer: the "no extractable text" check below
    # used to test the FORMATTED per-page string (page marker + text), which
    # is never empty even when the actual extracted text is -- "--- page 1
    # ---" alone survives .strip(). A 100%-scanned PDF silently "succeeded"
    # with blank pages instead of honestly saying so. real_text tracks just
    # the extracted content, separately from the markers used for display.
    real_text: list[str] = []
    for i, page in enumerate(page_list, 1):
        try:
            extracted = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            extracted = f"[page text unavailable: {exc}]"
        else:
            real_text.append(extracted)
        pages.append(f"--- page {i} ---\n" + extracted)
    if not any(t.strip() for t in real_text):
        return "PDF READ: no extractable text (scanned image PDFs need OCR, which is not included)."
    return "\n".join(pages)


_CURRENCY = r"\$\s?[\d,]+\.?\d*|£\s?[\d,]+\.?\d*|€\s?[\d,]+\.?\d*"

_DATE_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|"
    r"\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"\s+\d{2,4})\b",
    re.I,
)


#: Total-line labels, most specific first. Word-bounded, and "total" is not
#: matched when it is part of "subtotal", "sub-total" or "sub total".
_TOTAL_LABELS = (
    r"\bgrand\s+total\b",
    # "Total" is the receipt's total unless the next word says it is a different figure
    # (Total savings, Total VAT, Total items, Total before tax): those follow the real total.
    r"\b(?<!sub-)(?<!sub )total\b(?!\s+(?:savings?|saved|items?|qty|quantity|points?|discounts?|rewards?|weight"
    r"|vat|tax|taxes|before|excl|excluding|ex|net)\b)",
    r"\bamount\s+due\b",
    r"\bbalance\s+due\b",
    r"\bamount:",
)

#: Lines that are totals, taxes or sub-totals, not line items.
_NOT_A_LINE_ITEM = re.compile(r"^(total|amount|balance|sub[- ]?total|tax|vat|grand)", re.I)


def extract_receipt(path: str | Path) -> str:
    """Parse a receipt/invoice PDF into structured fields (best-effort).

    Reports found AND missing fields honestly — a missing total is
    reported as such, never estimated (Rule 2.2).
    """
    text = extract_pdf_text(path)
    if text.startswith("PDF READ FAILED") or text.startswith("ERROR") or text.startswith("PDF READ:"):
        return text
    first_lines = [
        ln.strip() for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("--- page")
    ]
    vendor = first_lines[0][:80] if first_lines else "(no text)"

    date = _DATE_RE.search(text)
    total = None
    # Finding P5-15: "total" used to match inside "Subtotal", and the first
    # hit in text order won, so the subtotal (printed before the real total)
    # was reported. Labels are now whole words, a sub-total in any spelling
    # is skipped, the most specific label is tried first, and the LAST
    # matching line wins (the final total comes after the running ones).
    for label in _TOTAL_LABELS:
        hits = [
            amt.group(0)
            for m in re.finditer(label + r"[^\n]*?(?:" + _CURRENCY + ")", text, re.I)
            if (amt := re.search(_CURRENCY, m.group(0)))
        ]
        if hits:
            total = hits[-1]
            break
    if total is None:
        # Last line carrying a currency amount often IS the total.
        amts = re.findall(_CURRENCY, text)
        if amts:
            total = amts[-1]

    lines: list[str] = ["RECEIPT EXTRACTION:"]
    lines.append(f"- vendor: {vendor}")
    lines.append(f"- date: {date.group(1) if date else 'NOT FOUND'}")
    lines.append(f"- total: {total if total else 'NOT FOUND'}")
    # line items: non-empty lines ending in a currency amount
    items = []
    for ln in first_lines:
        m = re.search(_CURRENCY + r"\s*$", ln)
        if m and not _NOT_A_LINE_ITEM.search(ln):
            items.append(ln[:120])
    lines.append(f"- line items ({len(items)}):")
    lines.extend("    " + it for it in items[:20])
    if not items:
        lines.append("    (none detected)")
    lines.append("")
    lines.append("HONESTY NOTE: fields are regex-extracted from the PDF text;")
    lines.append("if a field says NOT FOUND the parser could not locate it,")
    lines.append("not that the receipt lacks it. Verify totals before acting.")
    return "\n".join(lines)
