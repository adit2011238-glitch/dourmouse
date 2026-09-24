"""Finding #101 (MS-4): Downloads watch and file assessment, on real files."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import zipfile

import pytest

from dourmouse.security import downloads as dl
from dourmouse.security.sentry import SentryStore

on_mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS xattr/codesign/spctl")


class TestPureRules:
    @pytest.mark.parametrize(("name", "head", "kind"), [
        ("a.bin", b"\xcf\xfa\xed\xfe" + b"\0" * 12, "macho"),
        ("a", b"\xca\xfe\xba\xbe" + b"\0" * 12, "macho"),
        ("A.class", b"\xca\xfe\xba\xbe" + b"\0" * 12, "document"),
        ("x.pkg", b"xar!" + b"\0" * 12, "pkg"),
        ("run", b"#!/bin/sh\n", "script"),
        ("go.command", b"echo hi", "script"),
        ("x.jar", b"PK\x03\x04", "jar"),
        ("x.docm", b"PK\x03\x04", "office_macro"),
        ("x.zip", b"PK\x03\x04", "zip"),
        ("x.pdf", b"%PDF-1.7", "pdf"),
    ])
    def test_kind_comes_from_the_bytes(self, name, head, kind):
        assert dl.classify_bytes(name, head, b"") == kind

    def test_a_dmg_is_recognised_by_its_trailer(self):
        assert dl.classify_bytes("renamed.bin", b"\0" * 16, b"koly" + b"\0" * 508) == "dmg"

    @pytest.mark.parametrize(("name", "tricky"), [
        ("invoice.pdf.app", True), ("photo.jpg.command", True), ("report   .pdf", False), ("photo      .app", True),
        ("notes.txt", False), ("archive.tar.gz", False), ("setup.pkg", False),
    ])
    def test_double_extension(self, name, tricky):
        assert dl.double_extension(name) is tricky

    def test_quarantine_parsing(self):
        q = dl.parse_quarantine("0081;6ab4d349;Chrome;2D45A21B-1E8C-4C51-A556-A17BF5AEE14B")
        assert q["agent"] == "Chrome" and q["user_approved"] is False and q["downloaded_at"] == 0x6AB4D349
        assert dl.parse_quarantine("00c1;6ab4d349;Safari;x")["user_approved"] is True
        assert dl.parse_quarantine(None) is None


def _quarantine(path, agent="Chrome"):
    subprocess.run(["xattr", "-w", "com.apple.quarantine", f"0081;{int(time.time()):x};{agent};TEST", str(path)], check=True)


@on_mac
class TestRealFiles:
    def test_an_apple_signed_binary_is_not_high(self, tmp_path):
        p = tmp_path / "echo"
        shutil.copy("/bin/echo", p)
        _quarantine(p)
        a = dl.assess(p)
        assert a.kind == "macho" and a.signature["kind"] == "apple" and a.risk == "med"  # executable, signed

    def test_an_unsigned_binary_is_high(self, tmp_path):
        p = tmp_path / "tool"
        shutil.copy("/bin/echo", p)
        subprocess.run(["codesign", "--remove-signature", str(p)], check=True, capture_output=True)
        _quarantine(p)
        a = dl.assess(p)
        assert a.risk == "high" and a.signature["kind"] == "unsigned"
        assert any("unsigned" in r for r in a.reasons)

    def test_a_missing_quarantine_flag_on_an_executable_is_named(self, tmp_path):
        p = tmp_path / "setup.sh"
        p.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        a = dl.assess(p)
        assert a.kind == "script" and a.risk == "med"
        assert any("quarantine flag is missing" in r for r in a.reasons)

    def test_a_double_extension_decoy_is_high(self, tmp_path):
        p = tmp_path / "invoice.pdf.command"
        p.write_text("#!/bin/sh\necho gotcha\n", encoding="utf-8")
        _quarantine(p)
        a = dl.assess(p)
        assert a.risk == "high" and any("double extension" in r for r in a.reasons)

    def test_an_app_hidden_in_a_zip_is_flagged(self, tmp_path):
        p = tmp_path / "photos.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("Photos.app/Contents/MacOS/Photos", b"\xcf\xfa\xed\xfe")
        a = dl.assess(p)
        assert a.kind == "zip" and a.risk == "med" and a.archive["apps"] == ["Photos.app"]

    def test_a_pdf_is_info_and_the_verdict_says_nothing_was_scanned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dl.shutil, "which", lambda tool: None)
        p = tmp_path / "paper.pdf"
        p.write_bytes(b"%PDF-1.7\n%%EOF\n")
        _quarantine(p, agent="Safari")
        a = dl.assess(p)
        assert a.kind == "pdf" and a.risk == "info" and a.quarantine["agent"] == "Safari"
        assert a.scan["scanned"] is False and "not checked for known malware" in a.confidence

    def test_where_from_is_read(self, tmp_path):
        import plistlib

        p = tmp_path / "f.pdf"
        p.write_bytes(b"%PDF-1.7")
        hexdata = plistlib.dumps(["https://example.com/f.pdf", "https://example.com/"], fmt=plistlib.FMT_BINARY).hex()
        subprocess.run(["xattr", "-wx", "com.apple.metadata:kMDItemWhereFroms", hexdata, str(p)], check=True)
        assert dl.where_from(p) == ["https://example.com/f.pdf", "https://example.com/"]


class TestWatcher:
    def test_reports_only_new_settled_files_and_skips_partial_downloads(self, tmp_path):
        (tmp_path / "old.pdf").write_bytes(b"%PDF")
        w = dl.DownloadsWatcher(tmp_path)
        assert w.poll_once() == []  # primes: existing files are not "new"
        (tmp_path / "new.pdf").write_bytes(b"%PDF-1")
        (tmp_path / "big.iso.crdownload").write_bytes(b"x")
        assert w.poll_once() == []  # first sight: wait for the size to settle
        assert [p.name for p in w.poll_once()] == ["new.pdf"]
        assert w.poll_once() == []


@on_mac
def test_a_high_risk_download_becomes_a_finding(tmp_path):
    p = tmp_path / "invoice.pdf.command"
    p.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
    store = SentryStore(tmp_path / "s.db")
    a = dl.handle_new_file(p, store, now=1000.0, write_alerts=False)
    assert a.risk == "high"
    assert store.recent_downloads()[0]["name"] == "invoice.pdf.command"
    kinds = [r["kind"] for r in store.snapshot()]
    assert "risky_download" in kinds
