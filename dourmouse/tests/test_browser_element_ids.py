"""Phase C1: stable element ids for the browser agent.

browser_snapshot gives every interactive element a short id (e12). The ids are kept by a script
that runs in an ISOLATED WORLD of the page, so no page script can read or forge them and nothing is
written to the DOM. A click, fill, type, select or extract by id re-resolves the element at that
moment and refuses, saying why, when it is gone, hidden, covered or stale (the page navigated).

Two layers here:

* pure unit tests (id parsing, the refusal texts, a static check of the injected script and of the
  redaction rules it must keep from finding #163), no browser;
* the REAL tools (browser_open, browser_snapshot, browser_click, browser_fill, ...) driven against
  a real Google Chrome and a real local page served from a temp dir. These skip when Chrome is not
  installed. Nothing here touches the Electron shell, port 8765 or the owner's profile.

What this cannot prove is the Electron pane itself (its own Chromium, its own tab bar): that is
recorded as seen live in finding #165.
"""

from __future__ import annotations

import http.server
import re
import shutil
import socketserver
import threading
from pathlib import Path

import pytest

from dourmouse import browser_agent as ba

SCRIPT = Path(ba.__file__).resolve().parent / "browser_scripts" / "element_ids.js"


# --------------------------------------------------------------------------- #
# pure unit tests
# --------------------------------------------------------------------------- #


class TestElementIdParsing:
    @pytest.mark.parametrize(
        ("target", "number"),
        [("e12", 12), ("E7", 7), ("id:e3", 3), ("@e44", 44), ("[e5]", 5), ("  e9  ", 9), ("e1234567", 1234567)],
    )
    def test_id_forms(self, target, number):
        assert ba._element_id(target) == number

    @pytest.mark.parametrize(
        "target",
        ["", "Email address", "css:#e12", "e", "e12x", "x12", "e-1", "e12345678", "#e12", "email", "Save e12", "e 12"],
    )
    def test_not_ids(self, target):
        assert ba._element_id(target) is None


class TestRefusalTexts:
    """Every refusal says what happened and what to do, and never claims an action was taken."""

    def _page(self):
        return object()

    def test_stale_after_navigation(self):
        err = ba._refusal("navigated", 7, {"was": "http://a/1", "now": "http://a/2", "name": "Save"}, self._page())
        assert err.code == "navigated"
        assert "e7" in str(err) and "'Save'" in str(err)
        assert "navigated" in str(err) and "http://a/1" in str(err) and "http://a/2" in str(err)
        assert "browser_snapshot" in str(err)

    def test_gone_hidden_disabled_readonly(self):
        for code, word in (("gone", "no longer on the page"), ("hidden", "hidden"), ("disabled", "disabled"), ("readonly", "read-only")):
            err = ba._refusal(code, 3, {"name": "X"}, self._page())
            assert str(err).startswith("REFUSED:") and word in str(err), (code, str(err))

    def test_obscured_names_the_cover(self):
        err = ba._refusal("obscured", 3, {"name": "Pay", "by": "div#overlay"}, self._page())
        assert "div#overlay" in str(err) and "covered" in str(err)

    def test_unknown_id_is_never_confused_with_a_label(self):
        err = ba._refusal("unknown", 10**6, {}, self._page())
        assert "never issued" in str(err)

    def test_wrong_type_and_missing_option(self):
        assert "not a text field" in str(ba._refusal("wrongtype", 1, {"detail": "it is not a text field (a button)"}, self._page()))
        assert "'Red'" in str(ba._refusal("nooption", 1, {"options": ["Red", "Green"]}, self._page()))


class TestInjectedScript:
    """A static check of the file that runs in the page's isolated world."""

    def test_the_file_exists_and_is_one_expression(self):
        text = SCRIPT.read_text(encoding="utf-8")
        assert text.lstrip().startswith("//")
        assert text.rstrip().endswith("})()")

    def test_it_never_writes_to_the_dom(self):
        code = "\n".join(
            line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("//")
        )
        for forbidden in ("setAttribute(", "removeAttribute(", ".dataset", "classList.", ".className", "innerHTML", "insertAdjacent", "appendChild(", "document.write"):
            assert forbidden not in code, forbidden

    def test_no_page_reachable_global_other_than_the_api(self):
        text = SCRIPT.read_text(encoding="utf-8")
        assert text.count("globalThis.__dmAgent") >= 1
        assert "window." not in text.replace("// ", "")
        # defined non-enumerable, non-writable, non-configurable
        assert "enumerable: false, configurable: false, writable: false" in text

    def test_redaction_rules_from_finding_163_are_kept_exactly(self):
        text = SCRIPT.read_text(encoding="utf-8")
        old = (
            "/^(current-password|new-password|one-time-code|cc-.*|name|given-name|family-name|email|tel.*|"
            "street-address|address-line.*|postal-code|address-level.*|country.*)$/"
        )
        assert old in text
        assert "/pass|pwd|token|secret|otp|csrf|cc-?num|cvv|cvc/" in text
        assert 'el.type === "password" || el.type === "hidden"' in text

    def test_the_agent_module_has_no_second_copy_of_the_old_listing(self):
        src = Path(ba.__file__).read_text(encoding="utf-8")
        assert "els.slice(0, 60)" not in src


# --------------------------------------------------------------------------- #
# the real tools against a real Chrome
# --------------------------------------------------------------------------- #

CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)


def _have_chrome() -> bool:
    return any(Path(p).exists() for p in CHROME_PATHS) or shutil.which("google-chrome") is not None


needs_chrome = pytest.mark.skipif(not _have_chrome(), reason="Google Chrome is not installed")

INDEX = """<!doctype html><html><head><title>C1 Home</title></head><body>
<h1>C1 test site</h1>
<form onsubmit="return false">
<label for="email">Email address</label><input id="email" name="email_x" type="text" autocomplete="off">
<label for="pw">Password</label><input id="pw" name="pw" type="password">
<label>Notes <textarea id="notes"></textarea></label>
<label for="sel">Colour</label><select id="sel"><option value="r">Red</option><option value="g">Green</option></select>
<input type="checkbox" id="chk"><label for="chk">Agree</label>
<input type="hidden" name="csrf_token" value="SECRETTOKEN123">
<input id="cc" autocomplete="cc-number" aria-label="Card number" value="4111111111111111">
<input id="otp" autocomplete="one-time-code" aria-label="Sms code" value="918273">
<input id="street" autocomplete="street-address" aria-label="Street line" value="12 Hidden Lane">
<input id="tok" name="api_token" aria-label="Api field" value="tok-abc-123">
<button id="go" type="button" onclick="document.getElementById('out').textContent='clicked go: '+document.getElementById('email').value">Go</button>
</form>
<p id="out">idle</p>
<a id="blank" href="/popup.html" target="_blank">Open popup page</a><br>
<a id="same" href="/page2.html">Go to page 2</a><br>
<button id="winopen" onclick="window.open('/popup.html','_blank')">window.open</button>
<button id="remove" onclick="this.remove()">Remove me</button>
<button id="rerender" onclick="var n=document.getElementById('rr'); n.replaceWith(n.cloneNode(true))">Rerender</button>
<button id="rr" onclick="document.getElementById('out').textContent='rr clicked'">Rr target</button>
<button id="hide" onclick="document.getElementById('victim').style.display='none'">Hide victim</button>
<button id="victim" onclick="document.getElementById('out').textContent='victim clicked'">Victim</button>
<div style="position:relative"><button id="covered" onclick="document.getElementById('out').textContent='covered clicked'">Covered</button><div style="position:absolute;inset:0" id="overlay"></div></div>
<button id="disabled" disabled>Disabled one</button>
<button id="noname"></button>
</body></html>"""

PAGE2 = "<!doctype html><title>C1 Page 2</title><h1>Page two</h1><button id='b2'>Page2 button</button>"
POPUP = "<!doctype html><title>C1 Popup</title><h1>Popup page</h1><input id='pin' placeholder='popup input'><button id='pb'>Popup button</button>"
EDIT = """<!doctype html><title>C1 Edit</title>
<div id="ed" contenteditable="true" role="textbox" aria-label="Document body">start</div>
<input id="d" type="date" aria-label="Due date">
<div id="host"></div>
<iframe src="/popup.html" width="300" height="80"></iframe>
<script>
const r=document.getElementById('host').attachShadow({mode:'open'});
r.innerHTML='<button id="sb">Shadow button</button><input aria-label="Shadow input">';
r.getElementById('sb').addEventListener('click',()=>{document.title='shadow-clicked'});
</script>"""
SPOOF = """<!doctype html><title>C1 Spoof</title>
<button id="b" data-agent-id="e1" data-id="e1" id="e1" class="e1" onclick="document.title='real-b'">Real button</button>
<button id="e2" data-agent-id="e1" onclick="document.title='spoof-clicked'">Spoof</button>
<script>
window.__dmAgent = {run(){ return {ok:true, x:1, y:1}; }};
Object.defineProperty(window, '__dmAgent', {value: 'forged', writable:true});
</script>"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("c1site")
    for name, body in (("index.html", INDEX), ("page2.html", PAGE2), ("popup.html", POPUP), ("edit.html", EDIT), ("spoof.html", SPOOF)):
        (root / name).write_text(body, encoding="utf-8")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *args):
            pass

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _reset_browser_state() -> None:
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


async def _stop_playwright():
    if ba._PW is not None:
        await ba._PW.stop()


@pytest.fixture(scope="module")
def chrome(site):
    mp = pytest.MonkeyPatch()
    mp.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
    mp.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
    mp.setenv("DOURMOUSE_BROWSER_HEADLESS", "1")
    _reset_browser_state()
    ba._ensure_loop()
    yield
    _reset_browser_state()
    mp.undo()


def _id(snapshot: str, label: str) -> str:
    for line in snapshot.splitlines():
        m = re.match(r"- (e\d+) \[[^\]]+\] '" + re.escape(label) + "'", line)
        if m:
            return m.group(1)
    raise AssertionError(f"no id for {label!r} in\n{snapshot}")


def _eval(js: str):
    return ba._call(lambda: ba._PAGE.evaluate(js))


@needs_chrome
class TestSnapshotWithIds:
    def test_every_interactive_element_gets_an_id_and_ids_are_stable(self, chrome, site):
        first = ba.browser_open({"url": site + "/index.html"})
        assert "SNAPSHOT: #" in first and "ELEMENTS:" in first
        email, go = _id(first, "Email address"), _id(first, "Go")
        again = ba.browser_snapshot({})
        assert _id(again, "Email address") == email and _id(again, "Go") == go
        assert re.search(r"SNAPSHOT: #(\d+)", again).group(1) != re.search(r"SNAPSHOT: #(\d+)", first).group(1)

    def test_ids_do_not_collide_across_snapshots(self, chrome, site):
        a = ba.browser_open({"url": site + "/index.html"})
        b = ba.browser_open({"url": site + "/page2.html"})
        ids_a = set(re.findall(r"- (e\d+) ", a))
        ids_b = set(re.findall(r"- (e\d+) ", b))
        assert ids_a and ids_b and not (ids_a & ids_b)

    def test_state_flags_options_and_hidden_input_listing(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        assert re.search(r"\[input:checkbox\] 'Agree'  \(unchecked\)", snap)
        assert "options=['Red', 'Green']" in snap
        assert re.search(r"\(disabled\)", snap)
        # an element without any name is left out; a hidden input is listed without an id
        assert re.search(r"^- \[input:hidden\] ", snap, re.M)
        assert "IFRAMES" not in snap

    def test_redaction_from_finding_163_holds_in_the_new_snapshot(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        ba.browser_fill({"target": _id(snap, "Password"), "value": "hunter2-typed"})
        after = ba.browser_snapshot({})
        for secret in ("hunter2-typed", "SECRETTOKEN123", "4111111111111111", "918273", "12 Hidden Lane", "tok-abc-123"):
            assert secret not in after, secret
        assert after.count("value='[hidden]'") >= 5  # password, csrf, card, code, street, api token
        # the redacted values are not reachable through the new id actions either
        for label in ("Password", "Card number", "Sms code", "Street line", "Api field"):
            text = ba.browser_extract({"target": _id(after, label)})
            assert "hunter2-typed" not in text and "4111" not in text and "918273" not in text and "Hidden Lane" not in text and "tok-abc" not in text, label
            assert "[hidden]" in text

    def test_the_fill_log_still_records_only_a_length(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        ba.browser_fill({"target": _id(snap, "Password"), "value": "log-me-not-123"})
        text = " ".join(entry["text"] for entry in ba.browser_activity(20))
        assert "log-me-not-123" not in text and "characters" in text

    def test_max_elements_is_honoured_and_reported(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        small = ba.browser_snapshot({"max_elements": 3})
        assert len(re.findall(r"^- ", small, re.M)) == 3
        assert "more elements not shown" in small
        assert len(re.findall(r"^- ", snap, re.M)) > 3


@needs_chrome
class TestActionsById:
    def test_fill_click_select_type_extract(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        email, go, sel, notes, chk = (_id(snap, x) for x in ("Email address", "Go", "Colour", "Notes", "Agree"))
        out = ba.browser_fill({"target": email, "value": "me@example.com"})
        assert out.startswith("FILLED e") and "element id" in out
        assert _eval("document.getElementById('email').value") == "me@example.com"
        clicked = ba.browser_click({"target": go})
        assert clicked.startswith("CLICKED e") and "SNAPSHOT: #" in clicked
        assert _eval("document.getElementById('out').textContent") == "clicked go: me@example.com"
        assert "SELECTED 'Green'" in ba.browser_select({"target": sel, "value": "Green"})
        assert _eval("document.getElementById('sel').value") == "g"
        assert "SELECTED 'Red'" in ba.browser_select({"target": sel, "value": "r"})  # by value too
        ba.browser_click({"target": chk})
        assert _eval("document.getElementById('chk').checked") is True
        assert ba.browser_type({"target": notes, "text": "one\ntwo"}).startswith("TYPED 7 characters")
        assert _eval("document.getElementById('notes').value") == "one\ntwo"
        ba.browser_type({"target": notes, "text": "!"})
        assert _eval("document.getElementById('notes').value") == "one\ntwo!"
        ba.browser_type({"target": notes, "text": "X", "clear": True})
        assert _eval("document.getElementById('notes').value") == "X"
        assert "me@example.com" in ba.browser_extract({"target": email})

    def test_fill_with_empty_value_clears(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        email = _id(snap, "Email address")
        ba.browser_fill({"target": email, "value": "abc"})
        ba.browser_fill({"target": email, "value": ""})
        assert _eval("document.getElementById('email').value") == ""

    def test_fill_form_accepts_ids_and_labels_together(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        out = ba.browser_fill_form({"fields": {_id(snap, "Email address"): "ids@example.com", "Notes": "label route"}})
        assert out.startswith("FILLED 2 fields")
        assert _eval("document.getElementById('email').value") == "ids@example.com"
        assert _eval("document.getElementById('notes').value") == "label route"

    @pytest.mark.parametrize("form", ["{id}", "id:{id}", "@{id}", "[{id}]"])
    def test_every_id_spelling_is_accepted(self, chrome, site, form):
        snap = ba.browser_open({"url": site + "/index.html"})
        ident = _id(snap, "Email address")
        ba.browser_fill({"target": form.format(id=ident), "value": "spelled"})
        assert _eval("document.getElementById('email').value") == "spelled"

    def test_type_refuses_a_line_break_outside_a_textarea(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        email = _id(snap, "Email address")
        out = _expect_refusal(lambda: ba.browser_type({"target": email, "text": "a\nb"}))
        assert "REFUSED" in out and "textarea" in out
        assert _eval("document.getElementById('email').value") == ""

    def test_type_needs_an_id_or_focus(self, chrome, site):
        ba.browser_open({"url": site + "/index.html"})
        assert "element id" in ba.browser_type({"target": "Email address", "text": "x"})
        assert ba.browser_type({"text": None}).startswith("ERROR")
        assert "nothing has focus" in _expect_refusal(lambda: ba.browser_type({"text": "x"}))

    def test_select_on_a_non_select_and_fill_on_a_checkbox_are_refused(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        out = _expect_refusal(lambda: ba.browser_select({"target": _id(snap, "Go"), "value": "x"}))
        assert "not a <select>" in out
        out = _expect_refusal(lambda: ba.browser_fill({"target": _id(snap, "Agree"), "value": "x"}))
        assert "not a text field" in out

    def test_old_target_forms_keep_working(self, chrome, site):
        ba.browser_open({"url": site + "/index.html"})
        assert ba.browser_fill({"target": "Email address", "value": "label@example.com"}).startswith("FILLED 'Email address'")
        assert ba.browser_fill({"target": "css:#email", "value": "css@example.com"}).startswith("FILLED 'css:#email'")
        ba.browser_click({"target": "Go"})
        assert _eval("document.getElementById('out').textContent") == "clicked go: css@example.com"
        assert "css@example.com" in ba.browser_extract({"target": "css:#out"}) or "clicked go" in ba.browser_extract({"target": "css:#out"})

    def test_contenteditable_shadow_dom_and_date_inputs(self, chrome, site):
        snap = ba.browser_open({"url": site + "/edit.html"})
        assert "IFRAMES: 1" in snap
        ed, due, sb, si = (_id(snap, x) for x in ("Document body", "Due date", "Shadow button", "Shadow input"))
        ba.browser_fill({"target": ed, "value": "replaced"})
        assert _eval("document.getElementById('ed').textContent") == "replaced"
        ba.browser_type({"target": ed, "text": " more"})
        assert _eval("document.getElementById('ed').textContent") == "replaced more"
        ba.browser_fill({"target": due, "value": "2026-12-25"})
        assert _eval("document.getElementById('d').value") == "2026-12-25"
        ba.browser_click({"target": sb})
        assert _eval("document.title") == "shadow-clicked"
        ba.browser_fill({"target": si, "value": "typed in shadow"})
        assert _eval("document.getElementById('host').shadowRoot.querySelector('input').value") == "typed in shadow"


def _expect_refusal(call) -> str:
    """The text of the RuntimeError a tool raises (the roster turns it into 'BROWSER (reported honestly)')."""
    try:
        result = call()
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError(f"expected a refusal, got a result: {result!r}")


@needs_chrome
class TestStaleIdsAreRefused:
    def test_removed_element(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        remove = _id(snap, "Remove me")
        ba.browser_click({"target": remove})
        out = _expect_refusal(lambda: ba.browser_click({"target": remove}))
        assert out.startswith("REFUSED") and "no longer on the page" in out

    def test_re_rendered_element_is_not_mistaken_for_its_replacement(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        rr, rerender = _id(snap, "Rr target"), _id(snap, "Rerender")
        ba.browser_click({"target": rerender})
        out = _expect_refusal(lambda: ba.browser_click({"target": rr}))
        assert "no longer on the page" in out
        assert _eval("document.getElementById('out').textContent") != "rr clicked"

    def test_hidden_element(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        hide, victim = _id(snap, "Hide victim"), _id(snap, "Victim")
        ba.browser_click({"target": hide})
        out = _expect_refusal(lambda: ba.browser_click({"target": victim}))
        assert "hidden" in out
        assert _eval("document.getElementById('out').textContent") != "victim clicked"

    def test_covered_element_is_not_clicked(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        out = _expect_refusal(lambda: ba.browser_click({"target": _id(snap, "Covered")}))
        assert "covered by <div#overlay>" in out
        assert _eval("document.getElementById('out').textContent") == "idle"

    def test_disabled_element(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        out = _expect_refusal(lambda: ba.browser_click({"target": _id(snap, "Disabled one")}))
        assert "disabled" in out

    def test_navigation_makes_every_older_id_stale(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        go, same = _id(snap, "Go"), _id(snap, "Go to page 2")
        result = ba.browser_click({"target": same})
        assert "TITLE: C1 Page 2" in result
        out = _expect_refusal(lambda: ba.browser_click({"target": go}))
        assert out.startswith("REFUSED") and "navigated" in out
        assert _eval("document.getElementById('out')") is None  # nothing on page 2 was touched

    def test_a_same_document_navigation_also_invalidates(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        go = _id(snap, "Go")
        _eval("history.pushState({}, '', '/index.html?moved=1')")
        out = _expect_refusal(lambda: ba.browser_click({"target": go}))
        assert "navigated" in out

    def test_an_id_that_was_never_issued(self, chrome, site):
        ba.browser_open({"url": site + "/index.html"})
        out = _expect_refusal(lambda: ba.browser_click({"target": "e9999999"}))
        assert "never issued" in out
        assert _eval("document.getElementById('out').textContent") == "idle"

    def test_a_refusal_does_nothing(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        email, remove = _id(snap, "Email address"), _id(snap, "Remove me")
        ba.browser_click({"target": remove})
        before = _eval("document.getElementById('email').value")
        _expect_refusal(lambda: ba.browser_fill({"target": remove, "value": "typed"}))
        assert _eval("document.getElementById('email').value") == before
        assert email.startswith("e")


@needs_chrome
class TestPageCannotSeeOrForgeIds:
    def test_nothing_is_written_to_the_dom_or_the_page_world(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        ba.browser_click({"target": _id(snap, "Go")})
        assert _eval("typeof window.__dmAgent") == "undefined"
        assert _eval("Object.getOwnPropertyNames(window).filter(k => /^__dm/i.test(k)).length") == 0
        assert _eval("Array.from(document.querySelectorAll('*')).filter(e => Array.from(e.attributes).some(a => /^data-/.test(a.name))).length") == 0
        assert not re.search(r'="e\d+"', _eval("document.documentElement.outerHTML"))

    def test_a_page_that_plants_ids_and_a_fake_api_cannot_redirect_a_click(self, chrome, site):
        snap = ba.browser_open({"url": site + "/spoof.html"})
        real = _id(snap, "Real button")
        spoof = _id(snap, "Spoof")
        assert real != spoof
        ba.browser_click({"target": real})
        assert _eval("document.title") == "real-b"
        ba.browser_click({"target": spoof})
        assert _eval("document.title") == "spoof-clicked"  # its own button, not mixed up with the planted data-agent-id

    def test_the_page_global_the_script_would_use_is_unreachable_for_the_page(self, chrome, site):
        ba.browser_open({"url": site + "/spoof.html"})
        # the page defined its own window.__dmAgent = 'forged'; the isolated world has its own
        assert _eval("window.__dmAgent") == "forged"
        snap = ba.browser_snapshot({})
        assert "Real button" in snap


@needs_chrome
class TestPopupFollowingInSeparateChrome:
    """No Electron pane here: a target=_blank link opens a new page in the context and the agent follows it."""

    def test_a_target_blank_click_is_followed_and_told(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        out = ba.browser_click({"target": _id(snap, "Open popup page")})
        assert "TITLE: C1 Popup" in out
        assert "NOTE: your action opened a new tab" in out and "switched to it" in out
        assert ba._PAGE.url.endswith("/popup.html")
        pin = _id(out, "popup input")
        ba.browser_fill({"target": pin, "value": "in popup"})
        assert _eval("document.getElementById('pin').value") == "in popup"

    def test_window_open_is_followed(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        out = ba.browser_click({"target": _id(snap, "window.open")})
        assert "TITLE: C1 Popup" in out and "NOTE:" in out

    def test_the_old_tab_ids_are_refused_on_the_new_tab(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        email = _id(snap, "Email address")
        ba.browser_click({"target": _id(snap, "Open popup page")})
        out = _expect_refusal(lambda: ba.browser_fill({"target": email, "value": "x"}))
        assert "different tab" in out

    def test_closing_the_followed_tab_continues_on_the_most_recent_one(self, chrome, site):
        snap = ba.browser_open({"url": site + "/index.html"})
        ba.browser_click({"target": _id(snap, "Open popup page")})
        ba._call(lambda: ba._PAGE.close())
        out = ba.browser_snapshot({})
        assert "URL:" in out and "NOTE: the previous tab was closed" in out
        assert not ba._PAGE.is_closed()
