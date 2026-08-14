"""PDF + receipt extraction tests (v5.x) — extract.py + system tools.

Builds a real minimal PDF with a correct xref table (pypdf is a test
dependency via requirements-extract.txt), plus the honest NOT CONFIGURED
path when pypdf is absent.
"""

from __future__ import annotations

import sys

import pytest

from dourmouse import extract
from dourmouse.system_access import build_system_subagent


def make_pdf(lines: list[str]) -> bytes:
    """A minimal single-page PDF whose text is ``lines`` (real xref)."""
    content = "\n".join(
        f"BT /F1 11 Tf 72 {720 - 20 * i} Td ({ln}) Tj ET"
        for i, ln in enumerate(lines)
    ).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


def _receipt_pdf(tmp_path) -> str:
    p = tmp_path / "receipt.pdf"
    p.write_bytes(make_pdf([
        "ACME Coffee - Monthly Statement",
        "Date: 2026-08-12",
        "Invoice #1024",
        "Latte - $4.50",
        "Croissant - $3.00",
        "TOTAL $7.50",
    ]))
    return str(p)


class TestExtractPdfText:
    def test_extracts_real_pdf(self, tmp_path):
        p = _receipt_pdf(tmp_path)
        out = extract.extract_pdf_text(p)
        assert "ACME Coffee" in out
        assert "TOTAL $7.50" in out

    def test_missing_file_honest(self):
        out = extract.extract_pdf_text("/nonexistent/nope.pdf")
        assert "ERROR" in out and "no such file" in out

    def test_missing_pypdf_reports_not_configured(self, tmp_path, monkeypatch):
        p = tmp_path / "x.pdf"
        p.write_bytes(make_pdf(["hi"]))
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "pypdf":
                raise ImportError("no pypdf")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            extract.extract_pdf_text(str(p))
        # and the roster handler surfaces it honestly
        from dourmouse.system_access import _extract_pdf_tool

        out = _extract_pdf_tool({"path": str(p)})
        assert "NOT CONFIGURED" in out and "pypdf" in out


class TestExtractReceipt:
    def test_parses_fields(self, tmp_path):
        out = extract.extract_receipt(_receipt_pdf(tmp_path))
        assert "vendor: ACME Coffee" in out
        assert "date: 2026-08-12" in out
        assert "total: $7.50" in out
        assert "Latte - $4.50" in out and "Croissant - $3.00" in out
        assert "HONESTY NOTE" in out

    def test_missing_fields_reported_not_invented(self, tmp_path):
        p = tmp_path / "nofields.pdf"
        p.write_bytes(make_pdf(["Some random statement"]))
        out = extract.extract_receipt(str(p))
        assert "date: NOT FOUND" in out
        assert "total: NOT FOUND" in out


class TestSystemTools:
    def test_extract_tools_registered(self):
        sub = build_system_subagent()
        names = {t.name for t in sub.tools}
        assert {"extract_pdf", "extract_receipt"} <= names

    def test_handler_honest_missing_path(self):
        sub = build_system_subagent()
        tool = next(t for t in sub.tools if t.name == "extract_pdf")
        out = tool.handler({"path": ""})
        assert "ERROR" in out and "path" in out
