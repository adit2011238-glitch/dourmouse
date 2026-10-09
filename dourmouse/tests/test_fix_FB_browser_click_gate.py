"""Fix agent FB: H-1, a click on a submit-like control asks the owner first (owner decision 2026-10-09).

Three layers:
* the judgement (_click_reasons) for every trigger word, for a plain navigation link and for plain
  buttons, on facts shaped like what the page script reports;
* the REAL tools against a real Google Chrome and a local page: click_gate() (the hook for
  dispatch._argument_gate) answers "confirm" with the control and the page named, browser_click
  refuses without an approval and clicks with one, an element id from browser_snapshot is judged the
  same way, and a plain link is never held up;
* the wording of the refusal.
Nothing here touches the Electron shell, port 8765 or the owner's profile.
"""

from __future__ import annotations

import http.server
import re
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


# --------------------------------------------------------------------------- #
# the judgement
# --------------------------------------------------------------------------- #

TRIGGER_LABELS = [
    "Send", "Send message", "Submit", "Buy", "Buy now", "Pay", "Pay now", "Place order", "Place your order",
    "Confirm", "Confirm and pay", "Transfer", "Transfer money", "Delete", "Delete account", "Purchase",
    "Checkout", "Check out", "Withdraw", "Donate", "Subscribe", "Publish", "Post", "Reply", "Reserve", "Book",
    "Book a table", "Approve", "Accept all", "I agree", "Allow", "Sign in", "Log in", "Sign up", "Create account",
    "Order now", "Register", "SEND", "  send  ",
]


@pytest.mark.parametrize("label", TRIGGER_LABELS)
def test_every_trigger_word_asks_for_a_button(label):
    assert ba._click_reasons(_facts(label)), label


@pytest.mark.parametrize("label", ["Cancel", "Go", "Next", "Back", "Close", "Menu", "Open filters", "Show more", "Sort by price"])
def test_plain_buttons_are_not_held_up(label):
    assert ba._click_reasons(_facts(label)) == [], label


@pytest.mark.parametrize("label", ["Home", "About us", "Order history", "Post office hours", "My orders", "Contact", "Sign in", "Pricing", "Next page"])
def test_plain_navigation_links_are_not_held_up(label):
    assert ba._click_reasons(_facts(label, tag="a", href="https://shop.example/about")) == [], label


@pytest.mark.parametrize("label", ["Buy now", "Pay now", "Checkout", "Delete", "Confirm", "Place order", "Send"])
def test_a_link_that_does_the_thing_asks(label):
    assert ba._click_reasons(_facts(label, tag="a", href="https://shop.example/x")), label


def test_a_link_to_a_destructive_path_asks_whatever_it_says():
    assert ba._click_reasons(_facts("Remove this", tag="a", href="https://shop.example/account/delete?id=4"))
    assert ba._click_reasons(_facts("Go", tag="a", href="https://shop.example/transfer"))
    assert ba._click_reasons(_facts("Go", tag="a", href="https://shop.example/orders/history")) == []
    assert ba._click_reasons(_facts("Go", tag="a", href="https://shop.example/?q=delete")) == []  # a query is not a path


def test_a_submit_button_asks_whatever_it_is_called():
    r = ba._click_reasons(_facts("Save", submits=True, action="https://shop.example/checkout?x=1"))
    assert r and "submits a form" in r[0] and "https://shop.example/checkout" in r[0] and "x=1" not in r[0]


@pytest.mark.parametrize("words", ["btn btn-buy", "place-order", "submitBtn", "x checkout-button", "payNow", "data-send"])
def test_id_name_and_class_words_ask(words):
    assert ba._click_reasons(_facts("Go", words=words)), words


@pytest.mark.parametrize("words", ["btn btn-primary", "nav-toggle", "sender-info", "ordered-list", "posts-menu"])
def test_other_class_words_do_not(words):
    assert ba._click_reasons(_facts("Go", words=words)) == [], words


def test_nothing_found_is_not_a_reason():
    assert ba._click_reasons({"found": False}) == []
    assert ba._click_reasons({}) == []


def test_the_prompt_names_the_control_the_page_and_the_reason():
    f = _facts("Place order", submits=True, action="https://shop.example/checkout")
    text = ba._click_prompt(f, ba._click_reasons(f), "https://shop.example/cart?token=SECRET#x", "Your cart")
    assert 'button "Place order"' in text and "https://shop.example/cart" in text and "Your cart" in text
    assert "SECRET" not in text and "submits a form" in text
    assert "\n" not in text


def test_a_page_controlled_label_cannot_flood_or_break_the_prompt():
    f = _facts("Send\n\n" + "x" * 500)
    text = ba._click_prompt(f, ba._click_reasons(f), "https://a.example/", "T\nitle")
    assert "\n" not in text and len(text) < 600


def test_approvals_are_one_shot_and_expire(monkeypatch):
    key = ba._approval_key("e5", "https://a/b#frag", _facts("Send"))
    assert key == ba._approval_key("e5", "https://a/b", _facts("Send"))
    assert not ba._take_click_approval(key)
    ba._grant_click_approval(key)
    assert ba._take_click_approval(key)
    assert not ba._take_click_approval(key)
    ba._grant_click_approval(key)
    monkeypatch.setattr(ba, "_CLICK_APPROVAL_SECONDS", -1.0)
    assert not ba._take_click_approval(key)


# --------------------------------------------------------------------------- #
# real Chrome
# --------------------------------------------------------------------------- #

PAGE = """<!doctype html><title>Gate shop</title>
<h1>Gate shop</h1>
<p id="out">idle</p>
<form onsubmit="n('form submitted'); return false"><input aria-label="Query"><button type="submit">Save changes</button></form>
<button type="button" onclick="n('send')">Send</button>
<button type="button" onclick="n('pay')">Pay</button>
<button type="button" onclick="n('plain')">Open filters</button>
<button type="button" aria-label="Delete item" onclick="n('delete')"><svg width="10" height="10"></svg></button>
<div role="button" class="btn place-order" onclick="n('div order')">Go</div>
<input type="submit" value="Go" form="ff"><form id="ff" onsubmit="n('input submit'); return false"></form>
<a href="/about">About us</a>
<a href="/account/delete?id=1">Close it</a>
<script>
window.__n = [];
function n(x) { window.__n.push(x); document.getElementById('out').textContent = x; }
</script>"""


class _Site:
    def __init__(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                body = (PAGE if self.path.startswith("/shop") else "<title>other " + self.path + "</title>other").encode()
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


@pytest.fixture
def shop(chrome, site):
    ba._CLICK_APPROVALS.clear()
    ba.browser_open({"url": site.base + "/shop"})
    return site


def _log() -> list[str]:
    return ba._call(lambda: ba._PAGE.evaluate("window.__n"))


def _id(snapshot: str, label: str) -> str:
    for line in snapshot.splitlines():
        m = re.match(r"- (e\d+) \[[^\]]+\] '" + re.escape(label) + "'", line)
        if m:
            return m.group(1)
    raise AssertionError(f"no id for {label!r} in\n{snapshot}")


@needs_chrome
def test_the_hook_asks_for_send_and_names_the_control_and_the_page(shop):
    decision = ba.click_gate({"target": "Send"})
    assert decision is not None and decision[0] == "confirm"
    assert 'button "Send"' in decision[1] and "/shop" in decision[1] and "Gate shop" in decision[1]
    assert _log() == [], "asking must not click"


@needs_chrome
def test_a_click_without_an_approval_is_refused_and_does_nothing(shop):
    out = ba.browser_click({"target": "Send"})
    assert out.startswith("CONFIRMATION REQUIRED"), out
    assert 'button "Send"' in out and "NOT clicked" in out
    assert _log() == []


@needs_chrome
def test_a_click_after_the_hook_was_asked_goes_through_once(shop):
    assert ba.click_gate({"target": "Send"}) is not None  # dispatch asks the owner; the owner says yes
    out = ba.browser_click({"target": "Send"})
    assert out.startswith("CLICKED"), out
    assert _log() == ["send"]
    again = ba.browser_click({"target": "Send"})  # the approval was for one click
    assert again.startswith("CONFIRMATION REQUIRED"), again
    assert _log() == ["send"]


@needs_chrome
def test_an_approval_for_one_control_does_not_cover_another(shop):
    assert ba.click_gate({"target": "Send"}) is not None
    out = ba.browser_click({"target": "Pay"})
    assert out.startswith("CONFIRMATION REQUIRED"), out
    assert _log() == []


@needs_chrome
def test_an_element_id_from_a_snapshot_is_judged_the_same_way(shop):
    snap = ba.browser_snapshot({})
    pay = _id(snap, "Pay")
    decision = ba.click_gate({"target": pay})
    assert decision is not None and 'button "Pay"' in decision[1]
    assert ba.browser_click({"target": pay}).startswith("CLICKED")
    assert _log() == ["pay"]
    send = _id(snap, "Send")
    refused = ba.browser_click({"target": send})
    assert refused.startswith("CONFIRMATION REQUIRED"), refused
    assert _log() == ["pay"]


@needs_chrome
def test_a_submit_button_is_judged_by_its_type_not_its_label(shop):
    decision = ba.click_gate({"target": "Save changes"})
    assert decision is not None and "submits a form" in decision[1]
    assert ba.browser_click({"target": "Save changes"}).startswith("CLICKED")
    assert _log() == ["form submitted"]


@needs_chrome
def test_an_icon_button_is_judged_by_its_aria_label(shop):
    snap = ba.browser_snapshot({})
    decision = ba.click_gate({"target": _id(snap, "Delete item")})
    assert decision is not None and "Delete item" in decision[1]


@needs_chrome
def test_an_unlabelled_div_button_is_judged_by_its_class(shop):
    decision = ba.click_gate({"target": "css:.place-order"})
    assert decision is not None and "place order" in decision[1]
    assert ba.browser_click({"target": "css:.place-order"}).startswith("CLICKED")
    assert _log() == ["div order"]


@needs_chrome
def test_a_submit_input_is_judged(shop):
    assert ba.click_gate({"target": "css:input[type=submit]"}) is not None


@needs_chrome
def test_a_link_to_a_delete_path_asks(shop):
    decision = ba.click_gate({"target": "Close it"})
    assert decision is not None and "delete" in decision[1]


@needs_chrome
def test_a_plain_button_and_a_plain_link_are_never_held_up(shop):
    assert ba.click_gate({"target": "Open filters"}) is None
    assert ba.browser_click({"target": "Open filters"}).startswith("CLICKED")
    assert _log() == ["plain"]
    assert ba.click_gate({"target": "About us"}) is None
    out = ba.browser_click({"target": "About us"})
    assert out.startswith("CLICKED") and "/about" in out


def test_the_hook_does_not_open_a_browser_when_there_is_no_page(monkeypatch):
    monkeypatch.setattr(ba, "_PAGE", None)
    monkeypatch.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
    monkeypatch.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
    assert ba.click_gate({"target": "Send"}) is None


def test_the_hook_ignores_an_empty_target():
    assert ba.click_gate({}) is None
    assert ba.click_gate({"target": "  "}) is None
