"""FU: sign-in, setup, hub and product page fixes, in real headless Chrome.

Findings: U1-9 (client side) U1-10 U1-11 U1-12 U1-13 U1-14.
The hub and product pages are served by tools/serve_hub.py, started here on an ephemeral port.
"""

from __future__ import annotations

import http.client
import importlib.util
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from dourmouse.tests.test_fix_FU_common import OLD_UI, ROOT, DemoServer, chrome_usable, install_router, json_body

pytestmark = pytest.mark.skipif(not chrome_usable(), reason="headless Chrome is not available here")

HUB_TOKEN = "fu-test-engine-token"


@pytest.fixture(scope="module")
def srv():
    s = DemoServer()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture(scope="module")
def hub():
    """tools/serve_hub.py on an ephemeral port (the old copy when FU_OLD_UI is set)."""
    tools = (Path(OLD_UI).parent / "tools") if OLD_UI else ROOT / "tools"
    spec = importlib.util.spec_from_file_location("fu_serve_hub", tools / "serve_hub.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TOKEN = HUB_TOKEN
    if OLD_UI:
        mod.UI = Path(OLD_UI)
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server.server_address[1]
    server.shutdown()
    server.server_close()


GOOGLE_URL = "https://accounts.google.com/o/oauth2/v2/auth?client_id=fu&state=s"
WEBVIEW_STUB = """window.__opened = [];
  window.pywebview = { api: { open_external: (u) => { window.__opened.push(u); return Promise.resolve(true); } } };"""


def start_route_json(route, request):
    route.fulfill(**json_body({"ok": True, "url": GOOGLE_URL}))


# ---------------------------------------------------------------- U1-9 login and setup
def test_a_fetch_cannot_follow_the_redirect_to_google(srv, page):
    """The premise of U1-9, shown in the real browser: a 302 to another origin without CORS headers rejects."""
    install_router(page, srv.base, [
        ("/api/auth/google/start", lambda r, q: r.fulfill(status=302, headers={"location": GOOGLE_URL}, body="")),
        ("accounts.google.com", lambda r, q: r.fulfill(status=200, content_type="text/html", body="consent")),
    ])
    page.goto(f"{srv.base}/login")
    out = page.evaluate("fetch('/api/auth/google/start?claim=1', { redirect: 'follow' }).then(() => 'ok', (e) => String(e))")
    assert "Failed to fetch" in out


def test_login_in_the_app_window_opens_the_consent_url_it_was_given(srv, page):
    page.add_init_script(WEBVIEW_STUB)
    install_router(page, srv.base, [
        ("/api/auth/status", lambda r, q: r.fulfill(**json_body({"configured": True, "me": None}))),
        ("/api/auth/google/start", start_route_json),
        ("/api/auth/claim", lambda r, q: r.fulfill(status=404, content_type="application/json", body="{}")),
    ])
    page.goto(f"{srv.base}/login")
    page.wait_for_selector("#ggoogleBtn", state="visible", timeout=8000)
    page.click("#ggoogleBtn")
    page.wait_for_timeout(600)
    assert page.evaluate("window.__opened") == [GOOGLE_URL]
    assert page.url.startswith(f"{srv.base}/login"), "the app window must not navigate itself to Google"


def test_login_says_what_failed_instead_of_navigating_the_app_window(srv, page):
    page.add_init_script(WEBVIEW_STUB)
    install_router(page, srv.base, [
        ("/api/auth/status", lambda r, q: r.fulfill(**json_body({"configured": True, "me": None}))),
        ("/api/auth/google/start", lambda r, q: r.fulfill(status=302, headers={"location": GOOGLE_URL}, body="")),
        ("accounts.google.com", lambda r, q: r.fulfill(status=200, content_type="text/html", body="consent")),
    ])
    page.goto(f"{srv.base}/login")
    page.wait_for_selector("#ggoogleBtn", state="visible", timeout=8000)
    page.click("#ggoogleBtn")
    page.wait_for_timeout(800)
    assert page.url.startswith(f"{srv.base}/login")
    assert "Could not start Google sign-in" in page.inner_text("#ggoogleMsg")
    assert page.evaluate("window.__opened") == []


def test_setup_google_step_opens_the_consent_url_it_was_given(srv, page):
    page.add_init_script(WEBVIEW_STUB)
    install_router(page, srv.base, [("/api/auth/google/start", start_route_json),
                                    ("/api/auth/claim", lambda r, q: r.fulfill(status=404, content_type="application/json", body="{}"))])
    page.goto(f"{srv.base}/setup")
    page.wait_for_function("typeof gStartSignin === 'function'", timeout=8000)
    page.evaluate("gStartSignin()")
    page.wait_for_timeout(600)
    assert page.evaluate("window.__opened") == [GOOGLE_URL]
    assert "Could not reach Google" not in page.inner_text("#gMsg")


# ---------------------------------------------------------------- U1-10 setup keeps only what was proven
def test_setup_continue_drops_when_the_checked_key_is_edited(srv, page):
    install_router(page, srv.base, [("/api/setup/validate-key", lambda r, q: r.fulfill(**json_body({"ok": True, "detail": "key works"})))])
    page.goto(f"{srv.base}/setup")
    page.wait_for_function("typeof pick === 'function'", timeout=8000)
    page.evaluate("show(2); pick('nvidia')")
    page.fill("#nvKey", "nvapi-good")
    page.evaluate("checkKey()")
    page.wait_for_function("!document.getElementById('contBtn').disabled", timeout=5000)
    page.fill("#nvKey", "nvapi-good-but-edited")
    assert page.is_disabled("#contBtn"), "an edited key was never proven"
    assert "Check it again" in page.inner_text("#nvMsg")


def test_setup_continue_drops_when_the_tested_address_is_edited(srv, page):
    install_router(page, srv.base, [("/api/setup/probe-node", lambda r, q: r.fulfill(**json_body({"ok": True, "detail": "reachable"})))])
    page.goto(f"{srv.base}/setup")
    page.wait_for_function("typeof pick === 'function'", timeout=8000)
    page.evaluate("show(2); pick('node')")
    page.fill("#nodeUrl", "10.0.0.5:8000")
    page.evaluate("checkNode()")
    page.wait_for_function("!document.getElementById('contBtn').disabled", timeout=5000)
    page.fill("#nodeUrl", "10.0.0.6:8000")
    assert page.is_disabled("#contBtn")


# ---------------------------------------------------------------- U1-11 hub token exposure
def get(port, path, host=None, extra=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Origin": "https://evil.example"}
    if host:
        headers["Host"] = host
    headers.update(extra or {})
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, body)
    conn.close()
    return out


def test_hub_page_with_the_token_is_not_readable_cross_origin(hub):
    _, port = hub
    status, headers, body = get(port, "/hub.html")
    assert status == 200 and HUB_TOKEN.encode() in body
    assert "access-control-allow-origin" not in headers


def test_hub_refuses_a_rebound_hostname(hub):
    _, port = hub
    status, _, body = get(port, "/hub.html", host="attacker.example")
    assert status == 403 and HUB_TOKEN.encode() not in body
    assert get(port, "/hub.html", host=f"localhost:{port}")[0] == 200
    assert get(port, "/hub.html", host=f"[::1]:{port}")[0] == 200


# ---------------------------------------------------------------- U1-12 hub health dots
def test_hub_health_dots_follow_the_http_status(hub, page):
    base, _ = hub
    page.route("http://127.0.0.1:8790/**", lambda r: r.fulfill(status=401, content_type="application/json", body='{"error":"bad token"}'))
    page.route("http://127.0.0.1:8788/**", lambda r: r.fulfill(status=500, body="boom"))
    page.goto(f"{base}/hub.html")
    page.wait_for_function("document.getElementById('hFeed').className.includes('bad')", timeout=8000)
    assert "bad" in page.get_attribute("#hAtlas", "class")
    assert "ok" not in page.get_attribute("#hAtlas", "class").split()


def test_hub_health_dot_is_ok_for_a_healthy_engine(hub, page):
    base, _ = hub
    page.route("http://127.0.0.1:8790/**", lambda r: r.fulfill(status=200, content_type="application/json", body='{"ok":true,"keys":[]}'))
    page.route("http://127.0.0.1:8788/**", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
    page.goto(f"{base}/hub.html")
    page.wait_for_function("document.getElementById('hFeed').className.includes('ok')", timeout=8000)
    assert "ok" in page.get_attribute("#hAtlas", "class").split()


# ---------------------------------------------------------------- U1-13 / U1-14 product page
def card(cid, outcome="PENDING_APPROVAL", extra=None):
    c = {"id": cid, "ts": "2026-08-11T18:05:21Z", "signal": "sig", "asset": "USDJPY", "direction": "LONG", "size": 0.1,
         "mechanism": "m", "forced_participant": "none", "data_refs": ["A"], "p_value": 0.0007, "outcome": outcome,
         "guardrail": None, "ledger_ref": None, "chain": ["one", "two"]}
    c.update(extra or {})
    return c


def test_product_page_does_not_run_script_from_a_card(hub, page):
    base, _ = hub
    evil = {"cards": [card("x');window.__pwn=1;//"),
                      card("ok-2", outcome='<img src=x onerror="window.__pwn2=1">'),
                      card("ok-3", extra={"p_value": '<img src=x onerror="window.__pwn3=1">'})],
            "graveyard": {"count": 3}}
    page.route("**/decision_cards.json", lambda r: r.fulfill(**json_body(evil)))
    page.goto(f"{base}/product.html")
    page.wait_for_selector("#cards .card", timeout=8000)
    page.locator("#cards .card").first.click()
    page.locator("#cards button.approve").first.click()
    page.wait_for_timeout(500)
    assert page.evaluate("[window.__pwn, window.__pwn2, window.__pwn3]") == [None, None, None]
    assert "Approved on this page only" in page.inner_text("#cards")


def test_product_reject_does_not_claim_an_audit_trail(hub, page):
    base, _ = hub
    page.route("**/decision_cards.json", lambda r: r.fulfill(**json_body({"cards": [card("c1")], "graveyard": {"count": 0}})))
    page.goto(f"{base}/product.html")
    page.wait_for_selector("#cards .card", timeout=8000)
    page.locator("#cards .card").first.click()
    page.locator("#cards button.reject").first.click()
    text = page.inner_text("#cards")
    assert "audit trail" not in text
    assert "nothing was recorded" in text.lower()
