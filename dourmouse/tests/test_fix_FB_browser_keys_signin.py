"""Fix agent FB: P4-8 (Keyboard.press has no timeout argument), P4-9 (signin fills the vault
password into whatever page the navigation lands on) and the page-state behaviour around them.

The P4-8 tests drive the REAL tools (browser_press, browser_submit, browser_signin) against a real
Google Chrome and a real local page, so a Playwright signature mistake cannot hide behind a mock.
They skip when Chrome is not installed. Nothing here touches the Electron shell, port 8765, or the
owner's profile; the vault is a temp file.
"""

from __future__ import annotations

import http.server
import inspect
import json
import socketserver
import threading
import urllib.parse
from pathlib import Path

import pytest

from dourmouse import browser_agent as ba

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
needs_chrome = pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is not installed")

HOME = """<!doctype html><title>FB Home</title>
<input id="q" aria-label="Search box">
<form id="f" action="/search" onsubmit="document.title='submitted:'+document.getElementById('q2').value; return false">
<input id="q2" aria-label="Query"></form>
<script>
window.__keys = [];
document.addEventListener('keydown', e => window.__keys.push(e.key));
</script>"""

LOGIN = """<!doctype html><title>FB Login</title>
<form id="lf" onsubmit="return false">
<input name="email" type="email" aria-label="Email">
<input name="pw" type="password" aria-label="Password">
</form>
<script>
window.__enter = 0;
document.querySelector('[name=pw]').addEventListener('keydown', e => { if (e.key === 'Enter') window.__enter++; });
</script>"""

LOGIN_WITH_BUTTON = """<!doctype html><title>FB Login Button</title>
<form onsubmit="document.title='clicked-submit'; return false">
<input name="email" type="email" aria-label="Email"><input name="pw" type="password" aria-label="Password">
<button type="submit">Sign in</button></form>"""


class _Site:
    def __init__(self) -> None:
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401
                pass

            def do_GET(self):  # noqa: N802
                parts = urllib.parse.urlsplit(self.path)
                site.requests.append(self.path)
                if parts.path == "/redirect":
                    target = urllib.parse.parse_qs(parts.query).get("u", ["/"])[0]
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.end_headers()
                    return
                body = {"/": HOME, "/login": LOGIN, "/loginbtn": LOGIN_WITH_BUTTON}.get(parts.path, "<title>FB other</title>ok")
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.requests: list[str] = []
        self.srv = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.other = f"http://localhost:{self.port}"  # same server, a different host name


@pytest.fixture(scope="module")
def site():
    s = _Site()
    yield s
    s.srv.shutdown()


async def _stop_playwright():
    if ba._PW is not None:
        await ba._PW.stop()


def _reset() -> None:
    if ba._LOOP is not None and not ba._LOOP.is_closed():
        ba._call(lambda: _stop_playwright(), timeout=30)
    ba._PAGE = None
    ba._CONTEXT = None
    ba._PAGE_MODE = None
    ba._BROWSER = None
    ba._PW = None
    ba._WORLDS.clear()
    ba._TARGET_IDS.clear()
    ba._PREPARED_CONTEXTS.clear()
    ba._NEW_PAGES.clear()
    ba._ID_OWNER.clear()
    ba._NOTES.clear()
    ba._PANE_SEEN = {"active": None, "ids": frozenset()}


@pytest.fixture(scope="module")
def chrome(site):
    mp = pytest.MonkeyPatch()
    mp.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
    mp.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
    mp.setenv("DOURMOUSE_BROWSER_HEADLESS", "1")
    _reset()
    ba._ensure_loop()
    yield
    _reset()
    mp.undo()


def _eval(js: str):
    return ba._call(lambda: ba._PAGE.evaluate(js))


# --------------------------------------------------------------------------- #
# P4-8
# --------------------------------------------------------------------------- #


def test_playwright_keyboard_press_has_no_timeout_argument():
    """The premise of the finding, checked on the installed Playwright."""
    from playwright.async_api import Keyboard

    assert "timeout" not in inspect.signature(Keyboard.press).parameters


def test_no_call_site_passes_timeout_to_keyboard_press():
    src = Path(ba.__file__).read_text(encoding="utf-8")
    assert "keyboard.press(" in src
    for line in src.splitlines():
        if "keyboard.press(" in line:
            assert "timeout" not in line, line


@needs_chrome
def test_browser_press_really_sends_the_key(chrome, site):
    ba.browser_open({"url": site.base + "/"})
    out = ba.browser_press({"key": "Tab"})
    assert out.startswith("PRESSED Tab"), out
    out = ba.browser_press({"key": "Escape"})
    assert out.startswith("PRESSED Escape"), out
    out = ba.browser_press({"key": "Shift+a"})
    assert out.startswith("PRESSED Shift+a"), out
    keys = _eval("window.__keys")
    assert "Tab" in keys and "Escape" in keys and "Shift" in keys and "a" in keys, keys


@needs_chrome
def test_browser_press_reports_a_bad_key_as_a_failure_not_a_typeerror(chrome, site):
    ba.browser_open({"url": site.base + "/"})
    with pytest.raises(RuntimeError) as err:
        ba.browser_press({"key": "NoSuchKeyName"})
    assert "BROWSER PRESS FAILED" in str(err.value)
    assert "unexpected keyword" not in str(err.value)


@needs_chrome
def test_browser_submit_on_a_focused_field_really_submits(chrome, site):
    ba.browser_open({"url": site.base + "/"})
    ba.browser_fill({"target": "Query", "value": "hello"})
    out = ba.browser_submit({"note": "search"})
    assert out.startswith("SUBMITTED"), out
    assert _eval("document.title") == "submitted:hello"


@needs_chrome
def test_browser_press_enter_in_a_field_submits(chrome, site):
    ba.browser_open({"url": site.base + "/"})
    ba.browser_fill({"target": "Query", "value": "via-press"})
    # Enter in a form field asks the owner first (W1R-4); this is the owner saying yes.
    assert ba.press_gate({"key": "Enter"}) is not None
    out = ba.browser_press({"key": "Enter"})
    assert out.startswith("PRESSED Enter"), out
    assert _eval("document.title") == "submitted:via-press"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    monkeypatch.setattr(ba, "_VAULT_PATH", path)
    monkeypatch.setattr(ba, "_DATA_DIR", tmp_path)
    return path


@needs_chrome
def test_signin_without_a_submit_button_presses_enter(chrome, site, vault):
    host = f"127.0.0.1:{site.port}"
    vault.write_text(json.dumps({host: {"username": "me@example.com", "password": "pw-1"}}))
    out = ba.browser_signin({"site": site.base + "/login"})
    assert out.startswith("SIGNIN ATTEMPTED"), out
    assert _eval("window.__enter") == 1
    assert _eval("document.querySelector('[name=email]').value") == "me@example.com"


@needs_chrome
def test_signin_with_a_submit_button_still_clicks_it(chrome, site, vault):
    host = f"127.0.0.1:{site.port}"
    vault.write_text(json.dumps({host: {"username": "me@example.com", "password": "pw-1"}}))
    out = ba.browser_signin({"site": site.base + "/loginbtn"})
    assert out.startswith("SIGNIN ATTEMPTED"), out
    assert _eval("document.title") == "clicked-submit"


# --------------------------------------------------------------------------- #
# P4-9
# --------------------------------------------------------------------------- #


@needs_chrome
def test_signin_never_types_the_password_into_a_page_it_was_redirected_to(chrome, site, vault):
    host = f"127.0.0.1:{site.port}"
    vault.write_text(json.dumps({host: {"username": "me@example.com", "password": "SECRET-PW"}}))
    evil = urllib.parse.quote(site.other + "/login", safe="")
    out = ba.browser_signin({"site": f"{site.base}/redirect?u={evil}"})
    assert "SECRET-PW" not in out
    assert out.startswith("REFUSED"), out
    assert "localhost" in out and "127.0.0.1" in out
    # the foreign page was reached but nothing was typed into it
    assert _eval("location.hostname") == "localhost"
    assert _eval("document.querySelector('[name=pw]').value") == ""
    assert _eval("document.querySelector('[name=email]').value") == ""


@needs_chrome
def test_signin_keeps_the_case_of_the_path(chrome, site, vault):
    host = f"127.0.0.1:{site.port}"
    vault.write_text(json.dumps({host: {"username": "u@example.com", "password": "pw"}}))
    ba.browser_signin({"site": site.base + "/Login?Next=AbC"})
    assert any(r.startswith("/Login?Next=AbC") for r in site.requests), site.requests[-5:]


def test_signin_refuses_plain_http_to_a_public_host(vault):
    vault.write_text(json.dumps({"bank.example.com": {"username": "u", "password": "p"}}))
    out = ba.browser_signin({"site": "http://bank.example.com/login"})
    assert out.startswith("REFUSED") and "https" in out, out


def test_signin_ok_check_helper_rules():
    ok = ba._signin_landing_problem
    assert ok("https://bank.com/login", "bank.com") is None
    assert ok("https://bank.com:443/x", "bank.com") is None
    assert ok("https://evil.example/login", "bank.com")
    assert ok("http://bank.com/login", "bank.com")  # downgraded to http
    assert ok("https://bank.com.evil.example/", "bank.com")
    assert ok("https://sub.bank.com/", "bank.com")
    assert ok("about:blank", "bank.com")
    assert ok("http://127.0.0.1:8000/x", "127.0.0.1:8000") is None  # loopback may stay on http
    assert ok("http://127.0.0.1:9000/x", "127.0.0.1:8000")  # a different port is a different site


def test_a_key_press_that_never_finishes_times_out_with_a_readable_error(monkeypatch):
    import asyncio

    class _Keyboard:
        async def press(self, key):
            await asyncio.sleep(30)

    class _Page:
        keyboard = _Keyboard()

    monkeypatch.setattr(ba, "_KEY_PRESS_SECONDS", 0.05)
    with pytest.raises(RuntimeError) as err:
        asyncio.run(ba._press_key(_Page(), "Enter"))
    assert "TIMED OUT" in str(err.value) and "Enter" in str(err.value)
