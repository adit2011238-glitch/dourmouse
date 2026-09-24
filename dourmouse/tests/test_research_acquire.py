"""Finding #089 (R0-6 raw cache, R0-4 decoding, R0-5 provenance).

A real local HTTP server serves the documents; only 127.0.0.1 is treated as
public (the same narrow relaxation test_net_guard.py uses), so every fetch
runs the real guarded opener end to end."""

from __future__ import annotations

import hashlib
import http.server
import ipaddress
import threading

import pytest

from dourmouse import net_guard
from dourmouse.research_pipeline import acquire
from dourmouse.research_pipeline.acquire import (
    DocumentCache,
    UnsupportedContent,
    cut_at_word,
    detect_charset,
    fetch_document,
    html_to_text,
    load_cached,
)


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    hits: dict = {}

    def do_GET(self):  # noqa: N802
        self.hits[self.path] = self.hits.get(self.path, 0) + 1
        status, headers, body = self.routes.get(self.path, (404, {}, b"missing"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def site(monkeypatch, tmp_path):
    real = net_guard.is_public_address
    monkeypatch.setattr(
        net_guard, "is_public_address",
        lambda a: a == ipaddress.ip_address("127.0.0.1") or real(a),
    )
    routes: dict = {}
    hits: dict = {}
    handler = type("H", (_Handler,), {"routes": routes, "hits": hits})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", routes, hits, DocumentCache(tmp_path / "raw")
    finally:
        srv.shutdown()
        srv.server_close()


class TestRawCache:
    def test_raw_bytes_are_stored_under_their_own_hash(self, site):
        base, routes, _, cache = site
        body = b"<html><body><p>Hello</p></body></html>"
        routes["/a"] = (200, {"Content-Type": "text/html; charset=utf-8"}, body)
        doc = fetch_document(base + "/a", cache=cache)
        assert doc.raw_sha256 == hashlib.sha256(body).hexdigest()
        assert cache.read_raw(doc.raw_sha256) == body
        assert doc.text == "Hello"

    def test_a_repeated_source_is_served_from_the_cache_not_refetched(self, site):
        base, routes, hits, cache = site
        routes["/a"] = (200, {"Content-Type": "text/plain"}, b"cached body")
        fetch_document(base + "/a", cache=cache)
        again = fetch_document(base + "/a", cache=cache)
        assert hits["/a"] == 1
        assert again.text == "cached body"

    def test_text_is_rederived_from_the_stored_bytes(self, site):
        base, routes, _, cache = site
        routes["/a"] = (200, {"Content-Type": "text/html"}, b"<p>caf&eacute; &mdash; ok</p>")
        first = fetch_document(base + "/a", cache=cache)
        reloaded = load_cached(base + "/a", cache)
        assert reloaded is not None
        assert reloaded.text == first.text == "café — ok"
        assert reloaded.meta() == first.meta()

    def test_a_corrupted_blob_is_reported_not_served(self, site):
        base, routes, _, cache = site
        routes["/a"] = (200, {"Content-Type": "text/plain"}, b"original")
        doc = fetch_document(base + "/a", cache=cache)
        cache.raw_path(doc.raw_sha256).write_bytes(b"tampered")
        with pytest.raises(ValueError, match="corrupt"):
            load_cached(base + "/a", cache)


class TestDecoding:
    def test_header_charset_is_honoured(self, site):
        base, routes, _, cache = site
        routes["/l1"] = (200, {"Content-Type": "text/plain; charset=iso-8859-1"}, "Grüße".encode("latin-1"))
        doc = fetch_document(base + "/l1", cache=cache)
        assert doc.text == "Grüße"
        assert (doc.charset, doc.charset_source) == ("iso8859-1", "header")

    def test_meta_charset_is_honoured_when_the_header_has_none(self, site):
        base, routes, _, cache = site
        body = '<html><head><meta charset="shift_jis"></head><body>日本</body></html>'.encode("shift_jis")
        routes["/sj"] = (200, {"Content-Type": "text/html"}, body)
        doc = fetch_document(base + "/sj", cache=cache)
        assert "日本" in doc.text
        assert doc.charset_source == "meta"

    def test_a_bom_wins_over_the_default(self):
        assert detect_charset(b"\xef\xbb\xbfhello", None) == ("utf-8-sig", "bom")

    def test_an_unknown_header_charset_is_not_trusted(self):
        assert detect_charset(b"plain", "x-made-up") == ("utf-8", "default")

    def test_all_entities_are_decoded_not_just_four(self):
        assert html_to_text("<p>&eacute;&nbsp;&#8212;&hellip;&amp;</p>") == "é —…&"

    def test_scripts_and_styles_never_become_text(self):
        assert html_to_text("<style>a{}</style><script>x()</script><p>kept</p>") == "kept"

    def test_a_big_head_no_longer_eats_the_body(self, site):
        """The old read took max_chars*2+4096 bytes before parsing."""
        base, routes, _, cache = site
        head = "<head><style>" + ("x" * 50_000) + "</style></head>"
        routes["/big"] = (200, {"Content-Type": "text/html"}, f"<html>{head}<body><p>the body</p></body></html>".encode())
        assert "the body" in fetch_document(base + "/big", cache=cache).text


class TestContentType:
    def test_an_image_is_refused_not_stripped_into_garbage(self, site):
        base, routes, _, cache = site
        routes["/img"] = (200, {"Content-Type": "image/png"}, b"\x89PNG\r\n\x1a\n...")
        with pytest.raises(UnsupportedContent, match="image/png"):
            fetch_document(base + "/img", cache=cache)

    def test_a_pdf_is_read_as_a_pdf(self, site):
        pypdf = pytest.importorskip("pypdf")
        import io

        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buf = io.BytesIO()
        writer.write(buf)
        base, routes, _, cache = site
        routes["/doc.pdf"] = (200, {"Content-Type": "application/pdf"}, buf.getvalue())
        with pytest.raises(UnsupportedContent, match="(?i)text"):
            # A blank page has no text layer: reported honestly, never as ""
            fetch_document(base + "/doc.pdf", cache=cache)

    def test_classify_sniffs_html_without_a_content_type(self):
        assert acquire.classify("", b"<!DOCTYPE html><html>") == "html"


class TestProvenance:
    def test_the_final_url_and_the_redirect_chain_are_recorded(self, site):
        base, routes, _, cache = site
        routes["/old"] = (301, {"Location": "/mid"}, b"")
        routes["/mid"] = (302, {"Location": "/new"}, b"")
        routes["/new"] = (200, {"Content-Type": "text/plain"}, b"moved content")
        doc = fetch_document(base + "/old", cache=cache)
        assert doc.requested_url == base + "/old"
        assert doc.final_url == base + "/new"
        assert doc.redirect_chain == (base + "/mid", base + "/new")

    def test_the_final_url_is_indexed_too(self, site):
        base, routes, hits, cache = site
        routes["/old"] = (301, {"Location": "/new"}, b"")
        routes["/new"] = (200, {"Content-Type": "text/plain"}, b"x")
        fetch_document(base + "/old", cache=cache)
        fetch_document(base + "/new", cache=cache)
        assert hits["/new"] == 1


class TestTruncation:
    def test_an_oversized_body_is_kept_but_marked_truncated(self, site):
        base, routes, _, cache = site
        routes["/huge"] = (200, {"Content-Type": "text/plain"}, b"a" * 5000)
        doc = fetch_document(base + "/huge", cache=cache, max_bytes=1000)
        assert doc.truncated is True
        assert doc.raw_bytes == 1000

    def test_cut_at_word_never_cuts_mid_word(self):
        text, cut = cut_at_word("alpha beta gamma delta", 13)
        assert (text, cut) == ("alpha beta", True)
        assert cut_at_word("short", 100) == ("short", False)
