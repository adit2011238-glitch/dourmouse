"""FI2 P3-52: a render that overruns closes its browser, and final_url follows redirects."""

from __future__ import annotations

import asyncio
import http.server
import ipaddress
import threading
import time

import pytest

from dourmouse import net_guard
from dourmouse.research_pipeline import render as render_mod


def test_overrunning_render_unwinds_so_the_browser_is_closed(monkeypatch):
    closed = threading.Event()

    async def stalled(url, timeout_ms):
        try:
            await asyncio.sleep(120)  # a page that never goes quiet
        finally:
            closed.set()  # stands in for `await browser.close()`

    monkeypatch.setattr(render_mod, "_render_async", stalled)
    monkeypatch.setattr(render_mod, "_RENDER_GRACE_S", 0)
    t0 = time.monotonic()
    with pytest.raises(render_mod.RenderUnavailable, match="did not finish"):
        render_mod.render_page("https://example.org/", timeout_ms=100)
    assert closed.is_set(), "the coroutine was abandoned, not cancelled: Chrome would stay alive"
    assert time.monotonic() - t0 < 20
    assert not any(t.name == "dourmouse-research-render" and t.is_alive() for t in threading.enumerate())


def _chrome_usable() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(channel="chrome", headless=True).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _chrome_usable(), reason="headless Chrome is not available here")
def test_final_url_is_the_post_redirect_address_and_relative_links_resolve(monkeypatch):
    real = net_guard.is_public_address
    monkeypatch.setattr(net_guard, "is_public_address",
                        lambda a: a == ipaddress.ip_address("127.0.0.1") or real(a))
    page = (b"<html><body><div id=r></div><script>"
            b"fetch('rel.json').then(r=>r.text()).then(t=>{document.getElementById('r').textContent=t});"
            b"</script></body></html>")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/old":
                self.send_response(302)
                self.send_header("Location", "/new/page.html")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body, ctype = {
                "/new/page.html": (page, "text/html"),
                "/new/rel.json": (b"RESOLVED-AGAINST-NEW-BASE", "text/plain"),
            }.get(self.path, (b"missing", "text/plain"))
            self.send_response(200 if body != b"missing" else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        result = render_mod.render_page(base + "/old")
    finally:
        srv.shutdown()
        srv.server_close()
    assert result.final_url == base + "/new/page.html"
    assert "RESOLVED-AGAINST-NEW-BASE" in result.html
