"""2026-09-14, real live-caught gap: "There's no 'Dourmouse preview' pane
that can display a PDF inline" -- open_path's only real option was
shelling out to macOS `open` (Preview.app), and study.html's own
preview pane could only ever show extracted TEXT, losing every real
image/diagram this folder's own content (mostly scanned-image PDFs)
actually has. This tests the real new routes that fix that: an
open_path-trust-level general path (/api/files/*) and a
study-folder-sandboxed one (/api/study/*), both backed by the SAME
already-proven real PDFium renderer (dourmouse/pdf_reader.py) already
wired into ui/workspace.html's Vision OS PDF READER panel.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from dourmouse.dispatch import DispatchRegistry, Subagent
from dourmouse.webui import _sandboxed_preview_path, run_server

pypdfium2 = pytest.importorskip("pypdfium2")

from dourmouse.tests.test_pdf_reader import _write_minimal_pdf  # noqa: E402

# A real, minimal, valid 1x1 red PNG (67 bytes) -- hand-built once and
# checked into this file as a literal, same "no PDF-writing library
# dependency" reasoning test_pdf_reader.py's own fixture already uses.
_MINIMAL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da6360f8cfc0f01f0005050100afaf"
    "e8ba0000000049454e44ae426082"
)


def _registry() -> DispatchRegistry:
    reg = DispatchRegistry()
    reg.register_subagent(Subagent(name="echo_agent", domain="test", description="d", tools=()))
    return reg


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(port: int, path: str) -> tuple[int, bytes, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    headers = dict(resp.getheaders())
    conn.close()
    return resp.status, body, headers


class TestSandboxedPreviewPath:
    """The real trust boundary for /api/files/* -- same as open_path
    itself (any absolute path already nameable in a real tool call), a
    real extension allowlist, and a real existence check. Never raises."""

    def test_none_for_relative_path(self):
        assert _sandboxed_preview_path("relative/file.pdf") is None

    def test_none_for_missing_file(self, tmp_path):
        assert _sandboxed_preview_path(str(tmp_path / "nope.pdf")) is None

    def test_none_for_unsupported_extension(self, tmp_path):
        p = tmp_path / "notes.txt"
        p.write_text("hi")
        assert _sandboxed_preview_path(str(p)) is None

    def test_real_pdf_path_resolves(self, tmp_path):
        p = tmp_path / "doc.pdf"
        _write_minimal_pdf(p)
        out = _sandboxed_preview_path(str(p))
        assert out == p.resolve()

    def test_real_image_path_resolves(self, tmp_path):
        p = tmp_path / "pic.png"
        p.write_bytes(_MINIMAL_PNG)
        out = _sandboxed_preview_path(str(p))
        assert out == p.resolve()

    def test_empty_string_is_none(self):
        assert _sandboxed_preview_path("") is None


class TestFilesPreviewRoutes:
    """/api/files/pdf-info, /api/files/pdf-page.png, /api/files/image --
    the general, arbitrary-absolute-path preview routes."""

    def test_pdf_info_real_page_count(self, server, tmp_path):
        srv, port = server
        p = tmp_path / "doc.pdf"
        _write_minimal_pdf(p, "Hello PDF")
        status, body, _ = _get(port, "/api/files/pdf-info?path=" + str(p))
        data = json.loads(body)
        assert status == 200
        assert data["ok"] is True
        assert data["page_count"] == 1

    def test_pdf_info_rejects_non_pdf(self, server, tmp_path):
        srv, port = server
        p = tmp_path / "pic.png"
        p.write_bytes(_MINIMAL_PNG)
        status, body, _ = _get(port, "/api/files/pdf-info?path=" + str(p))
        assert status == 400

    def test_pdf_page_png_renders_a_real_image(self, server, tmp_path):
        srv, port = server
        p = tmp_path / "doc.pdf"
        _write_minimal_pdf(p, "Hello PDF")
        status, body, headers = _get(port, "/api/files/pdf-page.png?path=" + str(p) + "&page=0")
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert body.startswith(b"\x89PNG")

    def test_image_serves_real_bytes(self, server, tmp_path):
        srv, port = server
        p = tmp_path / "pic.png"
        p.write_bytes(_MINIMAL_PNG)
        status, body, headers = _get(port, "/api/files/image?path=" + str(p))
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert body == _MINIMAL_PNG

    def test_image_rejects_missing_file(self, server, tmp_path):
        srv, port = server
        status, body, _ = _get(port, "/api/files/image?path=" + str(tmp_path / "nope.png"))
        assert status == 400

    def test_image_rejects_relative_path(self, server):
        srv, port = server
        status, body, _ = _get(port, "/api/files/image?path=relative.png")
        assert status == 400


class TestStudyPreviewRoutes:
    """/api/study/pdf-info, /api/study/pdf-page.png, /api/study/image --
    sandboxed to the study folder via study_agent._resolve_within_root,
    the SAME real sandbox list_study_files/read_study_file already use
    and are already tested against."""

    @pytest.fixture(autouse=True)
    def _study_dir(self, monkeypatch, tmp_path):
        study_dir = tmp_path / "study"
        study_dir.mkdir()
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(study_dir))
        return study_dir

    def test_pdf_info_real_page_count(self, server, _study_dir):
        srv, port = server
        _write_minimal_pdf(_study_dir / "textbook.pdf", "Hello PDF")
        status, body, _ = _get(port, "/api/study/pdf-info?path=textbook.pdf")
        data = json.loads(body)
        assert status == 200
        assert data["ok"] is True
        assert data["page_count"] == 1

    def test_pdf_page_png_renders_a_real_image(self, server, _study_dir):
        srv, port = server
        _write_minimal_pdf(_study_dir / "textbook.pdf", "Hello PDF")
        status, body, headers = _get(port, "/api/study/pdf-page.png?path=textbook.pdf&page=0")
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert body.startswith(b"\x89PNG")

    def test_image_serves_real_bytes(self, server, _study_dir):
        srv, port = server
        (_study_dir / "diagram.png").write_bytes(_MINIMAL_PNG)
        status, body, headers = _get(port, "/api/study/image?path=diagram.png")
        assert status == 200
        assert body == _MINIMAL_PNG

    def test_refuses_escaping_the_study_root(self, server, _study_dir):
        """Same real containment check as list_study_files/read_study_file
        -- a path that resolves outside the sandboxed folder is refused,
        never silently clamped (Rule 2.2)."""
        srv, port = server
        status, body, _ = _get(port, "/api/study/pdf-info?path=../../etc/passwd")
        assert status == 400

    def test_refuses_a_real_file_that_is_not_a_pdf(self, server, _study_dir):
        srv, port = server
        (_study_dir / "notes.txt").write_text("hi")
        status, body, _ = _get(port, "/api/study/pdf-info?path=notes.txt")
        assert status == 400


class TestFilePreviewPage:
    def test_served_at_both_real_routes(self, server):
        srv, port = server
        for route in ("/file_preview", "/file_preview.html"):
            status, body, headers = _get(port, route)
            assert status == 200
            assert b"<title>Preview</title>" in body
