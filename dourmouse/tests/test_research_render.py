"""Finding #092 (R0-2): headless render for JavaScript-only pages, with every
request the page makes served through the SSRF guard.

Runs a real headless Chrome against a real local server. 127.0.0.1 is
treated as public for this test only (the same narrow relaxation as
test_net_guard.py); every other address keeps the real rule, so the page's
attempt to reach the metadata address is refused for real."""

from __future__ import annotations

import http.server
import ipaddress
import threading

import pytest

from dourmouse import net_guard
from dourmouse.research_pipeline import render as render_mod
from dourmouse.research_pipeline.acquire import DocumentCache, fetch_document

_SPA = b"""<!doctype html><html><head><title>App</title></head><body>
<div id="root"></div>
<script>
  fetch("/api/data").then(r => r.json()).then(d => {
    const el = document.getElementById("root");
    el.innerHTML = "<article><h1>" + d.title + "</h1><p>" + d.body + "</p></article>";
  });
  fetch("http://169.254.169.254/latest/meta-data/").catch(() => {});
</script></body></html>"""

_DATA = (b'{"title": "Rendered heading", "body": "This paragraph only exists after the page\'s '
         b'JavaScript ran, which is exactly the case a plain fetch used to record as an empty '
         b'success. It is long enough to be real content, with commas, clauses, and detail."}')

_STATIC = (b"<html><body><article><h1>Static</h1><p>" + b"Plain server-rendered prose, with commas. " * 20
           + b"</p></article><script>console.log(1)</script></body></html>")


def _chrome_usable() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(channel="chrome", headless=True).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chrome_usable(), reason="headless Chrome is not available here")


@pytest.fixture
def site(monkeypatch, tmp_path):
    real = net_guard.is_public_address
    monkeypatch.setattr(
        net_guard, "is_public_address",
        lambda a: a == ipaddress.ip_address("127.0.0.1") or real(a),
    )
    routes = {
        "/app": (200, "text/html; charset=utf-8", _SPA),
        "/api/data": (200, "application/json", _DATA),
        "/static": (200, "text/html; charset=utf-8", _STATIC),
    }

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status, ctype, body = routes.get(self.path, (404, "text/plain", b"missing"))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", DocumentCache(tmp_path / "raw")
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_javascript_only_page_is_rendered_and_its_content_kept(site):
    base, cache = site
    doc = fetch_document(base + "/app", cache=cache)
    assert doc.rendered is True
    assert "Rendered heading" in doc.text
    assert "only exists after the page's JavaScript ran" in doc.text
    # The server's own bytes are kept too, under their own hash.
    assert cache.read_raw(doc.rendered_from) == _SPA
    assert b"Rendered heading" in cache.read_raw(doc.raw_sha256)  # the stored rendered DOM


def test_the_page_cannot_reach_an_internal_address_while_rendering(site):
    base, _ = site
    result = render_mod.render_page(base + "/app")
    assert any("169.254.169.254" in u for u in result.requests_refused)
    assert result.requests_served >= 2  # the document and /api/data


def test_a_server_rendered_page_is_not_rendered_again(site):
    base, cache = site
    doc = fetch_document(base + "/static", cache=cache)
    assert doc.rendered is False
    assert "Plain server-rendered prose" in doc.text


def test_switched_off_rendering_is_recorded_not_silent(site, monkeypatch):
    base, cache = site
    monkeypatch.setenv("DOURMOUSE_RESEARCH_RENDER", "0")
    doc = fetch_document(base + "/app", cache=cache)
    assert doc.rendered is False
    assert "not rendered" in doc.render_note and "switched off" in doc.render_note


def test_empty_shell_detection():
    assert render_mod.looks_like_an_empty_shell(b"<div id=root></div><script src=a.js></script>", "")
    assert not render_mod.looks_like_an_empty_shell(b"<p>no scripts</p>", "")
    assert not render_mod.looks_like_an_empty_shell(b"<script></script>", "x" * 400)
