"""FIX-R R-9: the click gate stops asking about ordinary controls, and still asks about the real ones.

The judgement on facts shaped like what the page script reports, plus real Google Chrome for the one
fact only a browser can give (a search form that reads, against a form that posts).
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from dourmouse import browser_agent as ba

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
needs_chrome = pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is not installed")


def _facts(name="", tag="button", **kw):
    base = {"found": True, "tag": tag, "type": "button", "role": "", "submits": False, "name": name, "words": "",
            "href": "", "action": ""}
    base.update(kw)
    return base


@pytest.mark.parametrize("label", [
    "Confirm password visibility", "Order by date", "Order by price", "Sort by name", "Deposit slip help", "Login help",
    "Pay attention", "Publish date", "Post date", "Order number", "Order status", "Order history", "Buy FAQ",
])
def test_labels_that_are_help_toggles_or_sort_controls_do_not_ask(label):
    assert ba._click_reasons(_facts(label)) == [], label


@pytest.mark.parametrize("label", [
    "Send details", "Confirm details", "Delete history", "Delete policy", "Book time slot", "Send reset instructions",
    "Order now", "Place order", "Pay now", "Confirm and pay", "Confirm password", "Publish", "Post", "Reply", "Send",
    "Order", "Buy", "Book", "Delete account",
])
def test_labels_that_do_the_thing_still_ask(label):
    assert ba._click_reasons(_facts(label)), label


@pytest.mark.parametrize("words", ["pay-later-banner", "email-subscribe", "newsletter-subscribe-form", "checkout-summary-panel",
                                   "send-help-link", "order-list", "pay"])
def test_class_words_inside_a_container_or_a_long_name_do_not_ask(words):
    got = ba._click_reasons(_facts("Go", words=words))
    if words == "pay":
        assert got, "a bare class of exactly pay is the control itself"
    else:
        assert got == [], words


@pytest.mark.parametrize("words", ["btn btn-buy", "place-order", "submitBtn", "x checkout-button", "payNow", "data-send",
                                   "btn-primary delete-btn", "id-checkout"])
def test_class_words_of_a_control_still_ask(words):
    assert ba._click_reasons(_facts("Go", words=words)), words


def test_a_get_form_that_only_reads_is_not_a_submit_but_a_post_form_is():
    read = _facts("Go", submits=True, formGet=True, action="https://x.example/search")
    post = _facts("Go", submits=True, formGet=False, action="https://x.example/checkout")
    assert ba._click_reasons(read) == []
    assert ba._click_reasons(post) and "submits a form" in ba._click_reasons(post)[0]
    # the label still decides on a form that reads
    assert ba._click_reasons(_facts("Delete", submits=True, formGet=True))


PAGE = """<!doctype html><title>Noise shop</title>
<form method="get" action="/search"><input name="q" aria-label="Query"><button type="submit">Search</button></form>
<form method="get" action="/search2"><input name="q2" aria-label="Query two"><button type="submit">Find</button></form>
<form method="get" action="/login"><input type="password" name="pw" aria-label="Pw"><button type="submit">Go on</button></form>
<form method="post" action="/cart"><button type="submit">Continue</button></form>
<form action="/implicit-get"><button>Lookup</button></form>
"""


class _Site:
    def __init__(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                body = (PAGE if self.path.startswith("/shop") else "<title>other</title>ok").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.srv = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"


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
    ba._CLICK_APPROVALS.clear()
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


@needs_chrome
def test_in_a_real_browser_a_search_button_is_not_held_up_and_a_post_button_is(chrome, site):
    ba._CLICK_APPROVALS.clear()
    ba.browser_open({"url": site.base + "/shop"})
    assert ba.click_gate({"target": "Search"}) is None
    assert ba.click_gate({"target": "Find"}) is None
    assert ba.click_gate({"target": "Lookup"}) is None  # a form with no method reads too
    held = ba.click_gate({"target": "Continue"})
    assert held is not None and "submits a form" in held[1]
    with_password = ba.click_gate({"target": "Go on"})
    assert with_password is not None and "submits a form" in with_password[1], "a GET form that carries a password is still a sign-in"
