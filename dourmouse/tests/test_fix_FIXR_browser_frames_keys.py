"""FIX-R R-3 / R-4: the click gate sees into frames, and Enter / Space ask only when they would do something.

Real Google Chrome against local pages (skipped when Chrome is not installed): a same-origin frame
holding a "Pay now" button, a frame from another origin on a checkout page and on a plain page,
and keys pressed with different elements focused. Nothing here touches the Electron shell, port
8765 or the owner's profile.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from dourmouse import browser_agent as ba
from dourmouse.dispatch import _argument_gate

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
needs_chrome = pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is not installed")

FRAME_STYLE = "position:absolute;left:20px;top:200px;width:300px;height:100px;border:2px solid #333"

SHOP = f"""<!doctype html><title>Frame shop</title>
<iframe id="own" src="/inner-pay" style="{FRAME_STYLE}"></iframe>
<iframe id="plain" src="/inner-plain" style="position:absolute;left:400px;top:200px;width:300px;height:100px"></iframe>
<iframe id="nested" src="/outer" style="position:absolute;left:20px;top:400px;width:320px;height:140px"></iframe>
"""
OUTER = """<!doctype html><body style="margin:0"><iframe id="deep" src="/inner-pay" style="width:300px;height:100px"></iframe>"""
INNER_PAY = """<!doctype html><body style="margin:0"><button style="position:fixed;inset:0;width:100%;height:100%"
onclick="fetch('/clicked-pay')">Pay now</button>"""
INNER_PLAIN = """<!doctype html><body style="margin:0"><button style="position:fixed;inset:0;width:100%;height:100%"
onclick="fetch('/clicked-plain')">Open filters</button>"""
CHECKOUT = """<!doctype html><title>Checkout</title>
<iframe id="card" src="{other}/widget" style="position:absolute;left:20px;top:100px;width:300px;height:100px"></iframe>"""
VIDEO = """<!doctype html><title>A video</title>
<iframe id="vid" src="{other}/widget" style="position:absolute;left:20px;top:100px;width:300px;height:100px"></iframe>"""
LOGIN = """<!doctype html><title>Welcome</title><input type="password" aria-label="Password">
<iframe id="sso" src="{other}/widget" style="position:absolute;left:20px;top:100px;width:300px;height:100px"></iframe>"""
WIDGET = """<!doctype html><body style="margin:0"><button style="position:fixed;inset:0;width:100%;height:100%"
onclick="fetch('/clicked-widget')">Go</button>"""
KEYS = """<!doctype html><title>Keys page</title>
<p>top</p>
<form onsubmit="document.title='submitted'; return false"><input id="q" aria-label="Query"></form>
<button id="plainbtn" type="button" onclick="document.title='plain pressed'">Open filters</button>
<button id="sendbtn" type="button" onclick="document.title='send pressed'">Send</button>
<input id="chk" type="checkbox" aria-label="Remember me">
<div style="height:4000px"></div>"""


class _Site:
    def __init__(self, pages: dict[str, str]) -> None:
        site = self
        self.requests: list[str] = []
        self.pages = pages

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                site.requests.append(path)
                body = site.pages.get(path, "ok").encode()
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
def sites():
    other = _Site({"/widget": WIDGET})
    main = _Site({
        "/shop": SHOP, "/outer": OUTER, "/inner-pay": INNER_PAY, "/inner-plain": INNER_PLAIN,
        "/checkout": CHECKOUT.format(other=other.base), "/video": VIDEO.format(other=other.base),
        "/welcome": LOGIN.format(other=other.base), "/keys": KEYS,
    })
    yield main, other
    main.srv.shutdown()
    other.srv.shutdown()


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
def chrome(sites):
    mp = pytest.MonkeyPatch()
    mp.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
    mp.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
    mp.setenv("DOURMOUSE_BROWSER_HEADLESS", "1")
    _reset()
    ba._ensure_loop()
    yield
    _reset()
    mp.undo()


def _open(sites, path: str):
    ba._CLICK_APPROVALS.clear()
    sites[0].requests.clear()
    sites[1].requests.clear()
    ba.browser_open({"url": sites[0].base + path})


def _eval(js: str):
    return ba._call(lambda: ba._PAGE.evaluate(js))


def _focus(selector: str) -> None:
    _eval(f"document.querySelector({selector!r}).focus()")


def _spec(name):
    from dourmouse.general_roster import build_general_registry

    registry = build_general_registry()
    subs = [registry.get_subagent(n) for n in registry.subagent_names]
    return next(t for sub in subs if sub is not None for t in sub.tools if t.name == name)


# --------------------------------------------------------------------------- #
# R-3: frames
# --------------------------------------------------------------------------- #

def test_a_frame_the_script_cannot_read_is_judged_by_the_page_it_is_on():
    base = {"found": True, "tag": "iframe", "frame": True, "name": "card", "words": "", "frameSrc": "https://js.stripe.com/v3/x",
            "frameHost": "js.stripe.com", "pageUrl": "https://shop.example/cart", "pageTitle": "Cart", "pagePassword": False}
    assert ba._click_reasons(base) and "payment" in ba._click_reasons(base)[0]
    quiet = dict(base, frameSrc="https://player.example/embed", frameHost="player.example",
                 pageUrl="https://news.example/story", pageTitle="A story", name="video")
    assert ba._click_reasons(quiet) == []
    login = dict(quiet, pagePassword=True)
    assert "sign-in" in ba._click_reasons(login)[0]


@needs_chrome
def test_a_pay_button_inside_a_same_origin_frame_asks(chrome, sites):
    _open(sites, "/shop")
    decision = ba.click_gate({"target": "css:iframe#own"})
    assert decision is not None and decision[0] == "confirm", decision
    assert 'button "Pay now"' in decision[1]
    assert "/clicked-pay" not in sites[0].requests


@needs_chrome
def test_a_same_origin_frame_click_is_refused_without_approval_and_goes_through_once_with_it(chrome, sites):
    _open(sites, "/shop")
    out = ba.browser_click({"target": "css:iframe#own"})
    assert out.startswith("CONFIRMATION REQUIRED"), out
    assert "/clicked-pay" not in sites[0].requests
    assert ba.click_gate({"target": "css:iframe#own"}) is not None
    assert ba.browser_click({"target": "css:iframe#own"}).startswith("CLICKED")
    _wait_for(lambda: "/clicked-pay" in sites[0].requests)


@needs_chrome
def test_a_pay_button_two_frames_deep_asks(chrome, sites):
    _open(sites, "/shop")
    decision = ba.click_gate({"target": "css:iframe#nested"})
    assert decision is not None and 'button "Pay now"' in decision[1], decision


@needs_chrome
def test_a_plain_button_inside_a_frame_is_not_held_up(chrome, sites):
    _open(sites, "/shop")
    assert ba.click_gate({"target": "css:iframe#plain"}) is None
    assert ba.browser_click({"target": "css:iframe#plain"}).startswith("CLICKED")
    _wait_for(lambda: "/clicked-plain" in sites[0].requests)


@needs_chrome
def test_a_cross_origin_frame_on_a_checkout_page_asks_and_is_not_clicked(chrome, sites):
    _open(sites, "/checkout")
    decision = ba.click_gate({"target": "css:iframe#card"})
    assert decision is not None and decision[0] == "confirm", decision
    assert "embedded frame" in decision[1] and "payment" in decision[1] and "cannot be read" in decision[1]
    out = ba.browser_click({"target": "css:iframe#card"})  # the approval above is the owner's yes; spend it the other way round below
    assert out.startswith("CLICKED"), out
    _wait_for(lambda: "/clicked-widget" in sites[1].requests)
    ba._CLICK_APPROVALS.clear()
    sites[1].requests.clear()
    refused = ba.browser_click({"target": "css:iframe#card"})
    assert refused.startswith("CONFIRMATION REQUIRED"), refused
    import time
    time.sleep(0.5)
    assert "/clicked-widget" not in sites[1].requests


@needs_chrome
def test_a_cross_origin_frame_on_a_login_page_asks(chrome, sites):
    _open(sites, "/welcome")
    decision = ba.click_gate({"target": "css:iframe#sso"})
    assert decision is not None and "sign-in" in decision[1], decision


@needs_chrome
def test_a_cross_origin_frame_on_an_ordinary_page_is_left_alone(chrome, sites):
    _open(sites, "/video")
    assert ba.click_gate({"target": "css:iframe#vid"}) is None
    assert ba.browser_click({"target": "css:iframe#vid"}).startswith("CLICKED")
    _wait_for(lambda: "/clicked-widget" in sites[1].requests)


def _wait_for(cond, seconds: float = 5.0) -> None:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("the expected request never reached the server")


# --------------------------------------------------------------------------- #
# R-4: Enter and Space
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("key,kind", [
    ("Enter", "enter"), ("return", "enter"), ("NumpadEnter", "enter"), ("Control+Enter", "enter"), (" Return ", "enter"),
    ("Space", "space"), (" ", "space"), ("spacebar", "space"), ("Shift+Space", "space"),
    ("Tab", ""), ("Escape", ""), ("a", ""), ("PageDown", ""), ("", ""),
])
def test_press_kind(key, kind):
    assert ba.press_kind(key) == kind


def test_judgement_by_focus():
    field = {"found": True, "tag": "input", "type": "text", "name": "Query", "editable": True, "fieldInForm": True, "submits": False,
             "words": "", "role": "", "href": "", "action": ""}
    button = {"found": True, "tag": "button", "type": "button", "name": "Send", "editable": False, "submits": False,
              "words": "", "role": "", "href": "", "action": ""}
    plain = dict(button, name="Open filters")
    link = {"found": True, "tag": "a", "name": "Delete account", "words": "", "role": "", "href": "https://x.example/home",
            "submits": False, "editable": False}
    assert ba._press_reasons("enter", field) and ba._press_reasons("space", field) == []
    assert ba._press_reasons("enter", button) and ba._press_reasons("space", button)
    assert ba._press_reasons("enter", plain) == [] and ba._press_reasons("space", plain) == []
    assert ba._press_reasons("enter", link) and ba._press_reasons("space", link) == []
    assert ba._press_reasons("enter", {"found": False}) == [] and ba._press_reasons("space", {"found": False}) == []


@needs_chrome
def test_space_with_nothing_focused_scrolls_and_does_not_ask(chrome, sites):
    _open(sites, "/keys")
    assert ba.press_gate({"key": "Space"}) is None
    assert _argument_gate(_spec("browser_press"), {"key": "Space"}, "orchestrator") is None
    out = ba.browser_press({"key": "Space"})
    assert out.startswith("PRESSED Space"), out
    assert _eval("window.scrollY") > 100


@needs_chrome
def test_space_on_a_plain_button_and_a_checkbox_does_not_ask(chrome, sites):
    _open(sites, "/keys")
    _focus("#plainbtn")
    assert ba.press_gate({"key": "Space"}) is None
    _focus("#chk")
    assert ba.press_gate({"key": "Space"}) is None
    assert ba.press_gate({"key": "Tab"}) is None


@needs_chrome
def test_space_in_a_text_field_does_not_ask(chrome, sites):
    _open(sites, "/keys")
    _focus("#q")
    assert ba.press_gate({"key": "Space"}) is None


@needs_chrome
def test_enter_in_a_form_field_asks_and_the_prompt_says_which_key_and_where(chrome, sites):
    _open(sites, "/keys")
    _focus("#q")
    decision = _argument_gate(_spec("browser_press"), {"key": "Enter"}, "orchestrator")
    assert decision is not None and decision[0] == "confirm", decision
    assert "Press Enter" in decision[1] and "text field" in decision[1] and "Keys page" in decision[1]
    chord = ba.press_gate({"key": "Control+Enter"})
    assert chord is not None and "Press Control+Enter" in chord[1]


@needs_chrome
def test_enter_without_an_approval_is_refused_and_with_one_it_submits_once(chrome, sites):
    _open(sites, "/keys")
    _focus("#q")
    out = ba.browser_press({"key": "Enter"})
    assert out.startswith("CONFIRMATION REQUIRED"), out
    assert _eval("document.title") == "Keys page"
    assert ba.press_gate({"key": "Enter"}) is not None
    assert ba.browser_press({"key": "Enter"}).startswith("PRESSED Enter")
    assert _eval("document.title") == "submitted"


@needs_chrome
def test_space_on_a_send_button_asks_in_space_words(chrome, sites):
    _open(sites, "/keys")
    _focus("#sendbtn")
    decision = _argument_gate(_spec("browser_press"), {"key": "Space"}, "orchestrator")
    assert decision is not None and decision[0] == "confirm", decision
    assert "Press Space" in decision[1] and "Enter" not in decision[1] and 'button "Send"' in decision[1]
    assert _eval("document.title") == "Keys page"
    assert ba.browser_press({"key": "Space"}).startswith("PRESSED Space")
    assert _eval("document.title") == "send pressed"
    again = ba.browser_press({"key": "Space"})
    assert again.startswith("CONFIRMATION REQUIRED"), again


@needs_chrome
def test_enter_on_a_plain_button_and_with_nothing_focused_does_not_ask(chrome, sites):
    _open(sites, "/keys")
    assert ba.press_gate({"key": "Enter"}) is None
    _focus("#plainbtn")
    assert ba.press_gate({"key": "Enter"}) is None


def test_with_no_page_both_keys_ask_in_generic_words(monkeypatch):
    monkeypatch.setattr(ba, "_PAGE", None)
    monkeypatch.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
    monkeypatch.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
    enter = ba.press_gate({"key": "Enter"})
    space = ba.press_gate({"key": "Space"})
    assert enter is not None and "Enter can submit a form" in enter[1]
    assert space is not None and "Space presses a focused button" in space[1] and "Enter" not in space[1]
    assert ba.press_gate({"key": "Tab"}) is None


@needs_chrome
def test_a_bare_space_character_is_the_space_key(chrome, sites):
    _open(sites, "/keys")
    out = ba.browser_press({"key": " "})
    assert out.startswith("PRESSED Space"), out
    assert _eval("window.scrollY") > 100
