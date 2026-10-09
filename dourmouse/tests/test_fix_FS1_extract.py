"""FS1 fixes in dourmouse/extract.py: P5-15 (Subtotal picked as total) and
P5-16 (encrypted PDF raised instead of reporting PDF READ FAILED)."""

from __future__ import annotations

import pypdf

from dourmouse import extract
from dourmouse.tests.test_extract import make_pdf


def _pdf(tmp_path, lines, name="r.pdf"):
    p = tmp_path / name
    p.write_bytes(make_pdf(lines))
    return p


def test_total_is_not_the_subtotal(tmp_path):
    p = _pdf(tmp_path, ["Corner Shop", "Subtotal $10.00", "Tax $1.00", "Total $11.00"])
    out = extract.extract_receipt(p)
    assert "- total: $11.00" in out


def test_sub_total_spelled_apart_is_not_the_total(tmp_path):
    p = _pdf(tmp_path, ["Corner Shop", "Sub-total $10.00", "Sub total $10.00", "VAT $2.00", "TOTAL $12.00"])
    out = extract.extract_receipt(p)
    assert "- total: $12.00" in out
    # H-FS1-1: sub-totals and VAT are not line items either
    assert "- line items (0):" in out


def test_grand_total_wins_over_an_earlier_total(tmp_path):
    p = _pdf(tmp_path, ["Shop", "Total items $9.00", "Grand Total $9.90"])
    assert "- total: $9.90" in extract.extract_receipt(p)


def test_encrypted_pdf_reports_read_failed(tmp_path):
    plain = _pdf(tmp_path, ["Invoice", "Total $5.00"], "plain.pdf")
    writer = pypdf.PdfWriter(clone_from=str(plain))
    writer.encrypt(user_password="secret", owner_password="owner")
    enc = tmp_path / "enc.pdf"
    with enc.open("wb") as f:
        writer.write(f)
    out = extract.extract_pdf_text(enc)
    assert out.startswith("PDF READ FAILED")
    assert "password" in out.lower() or "encrypt" in out.lower()
    assert extract.extract_receipt(enc).startswith("PDF READ FAILED")
