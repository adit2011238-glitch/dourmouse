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
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - encrypted/corrupt PDFs, honest
        return f"PDF READ FAILED: {type(exc).__name__}: {exc}"
    pages = []
    for i, page in enumerate(reader.pages, 1):
        try:
            pages.append(f"--- page {i} ---\n" + (page.extract_text() or ""))
        except Exception as exc:  # noqa: BLE001
            pages.append(f"--- page {i} ---\n[page text unavailable: {exc}]")
    if not any(p.strip() for p in pages):
        return "PDF READ: no extractable text (scanned image PDFs need OCR, which is not included)."
    return "\n".join(pages)


_CURRENCY = r"\$\s?[\d,]+\.?\d*|£\s?[\d,]+\.?\d*|€\s?[\d,]+\.?\d*"

_DATE_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|"
    r"\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"\s+\d{2,4})\b",
    re.I,
)


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
    for label in ("total", "amount due", "grand total", "balance due", "amount:"):
        for m in re.finditer(label + r"[^\n]*" + _CURRENCY, text, re.I):
            amt = re.search(_CURRENCY, m.group(0))
            if amt:
                total = amt.group(0)
                break
        if total:
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
        if m and not re.search(r"^(total|amount|balance|subtotal|tax|grand)", ln, re.I):
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
