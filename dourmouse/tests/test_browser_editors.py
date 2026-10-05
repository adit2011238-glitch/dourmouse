"""Phase C2: browser_type reaches editors that browser_fill does not, Google Docs' shape included.

A local test site reproduces the three shapes that matter:

* a Docs-like editor: the document is drawn on a <canvas>, and the keyboard goes to a hidden,
  same-origin iframe (class ``docs-texteventtarget-iframe``, like Google Docs) whose body is
  contenteditable. The page reads what arrives there (input events), keeps its own text model and
  redraws the canvas. Setting a value cannot work here: there is no value. Clicking the canvas is
  what focuses the hidden frame, as in Docs.
* a plain contenteditable and a textarea.

The REAL tools run against a real Google Chrome (headless, launched by the agent itself), so the
text arrives through CDP ``Input.insertText`` exactly as in the agent's own Chrome; in the shared
pane the same text goes through the shell's ``/control/type`` (``webContents.insertText``), which
was seen live and is recorded in the C2 finding. Real Google Docs needs the owner's sign-in and is
checked by the owner with scripts/live_checks/docs_and_youtube.md, never here.
"""

from __future__ import annotations

import http.server
import re
import socketserver
import threading

import pytest

from dourmouse import browser_agent as ba
from dourmouse.tests.test_browser_element_ids import _eval, _id, _reset_browser_state, needs_chrome

DOCS = """<!doctype html><html><head><title>Fake Docs - Editor</title>
<style>body{font-family:sans-serif} #app{position:relative;width:520px;height:220px;border:1px solid #888;cursor:text}</style></head><body>
<div id="menu"><button id="share" onclick="document.title='share clicked'">Share</button></div>
<div id="app" role="textbox" aria-label="Document content" aria-multiline="true"><canvas id="cv" width="520" height="220"></canvas></div>
<iframe class="docs-texteventtarget-iframe" id="tet" src="/tet.html" tabindex="-1" title="Text event target"
  style="position:absolute;top:-10000px;left:0;width:20px;height:20px;border:0"></iframe>
<script>
window.__doc = { paras: [""], draws: 0, kinds: [] };
function draw() {
  const c = document.getElementById('cv').getContext('2d');
  c.clearRect(0, 0, 520, 220); c.font = '14px sans-serif';
  __doc.paras.forEach((p, i) => c.fillText(p, 8, 20 + i * 18));
  __doc.draws += 1;
}
function focusEditor() {
  const f = document.getElementById('tet');
  f.contentWindow.focus();
  f.contentDocument.body.focus();
}
document.getElementById('app').addEventListener('mousedown', (e) => { e.preventDefault(); focusEditor(); });
window.__docInput = (kind, data) => {
  __doc.kinds.push(kind);
  if (kind === 'insertText' || kind === 'insertCompositionText' || kind === 'insertReplacementText') {
    const parts = String(data || '').split('\\n');
    __doc.paras[__doc.paras.length - 1] += parts[0];
    for (const p of parts.slice(1)) __doc.paras.push(p);
  } else if (kind === 'insertParagraph' || kind === 'insertLineBreak') {
    __doc.paras.push('');
  } else if (kind === 'deleteContentBackward') {
    const last = __doc.paras.length - 1;
    if (__doc.paras[last]) __doc.paras[last] = __doc.paras[last].slice(0, -1);
    else if (last) __doc.paras.pop();
  }
  draw();
};
window.__docText = () => __doc.paras.join('\\n');
draw();
</script></body></html>"""

# The hidden text target: what is about to be inserted (beforeinput) is handed to the parent, and
# the frame is emptied again after the browser inserted it, which is how such editors keep their own
# model. (Measured here: one insertText of "ab\ncd" raises ONE beforeinput with the whole text and
# then three input events with no line break in their data, so a model built from input events
# alone would lose the paragraph breaks.)
TET = """<!doctype html><html><body contenteditable="true" spellcheck="false" style="margin:0"></body><script>
document.body.addEventListener('beforeinput', (e) => {
  if (e.cancelable) e.preventDefault();
  parent.__docInput(e.inputType, e.data);
});
document.body.addEventListener('input', () => { document.body.textContent = ''; });
</script></html>"""

PLAIN = """<!doctype html><title>Plain editors</title>
<div id="ce" contenteditable="true" aria-label="Note body" style="min-height:40px;border:1px solid #888"></div>
<label>Comments <textarea id="ta"></textarea></label>
<label>Subject <input id="subj" type="text"></label>
<label>Jumpy <textarea id="jumpy"></textarea></label>
<input id="other" aria-label="Other field">
<button id="btn">Plain button</button>
<script>
document.getElementById('jumpy').addEventListener('input', (e) => {
  if (e.target.value.length > 30) document.getElementById('other').focus();
});
</script>"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("c2editors")
    for name, body in (("docs.html", DOCS), ("tet.html", TET), ("plain.html", PLAIN)):
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


def _doc_text() -> str:
    return _eval("window.__docText()")


def _open_docs(site: str) -> str:
    snap = ba.browser_open({"url": site + "/docs.html"})
    ba._call(lambda: ba._PAGE.wait_for_function("document.getElementById('tet').contentDocument && document.getElementById('tet').contentDocument.readyState === 'complete'"))
    return snap


@needs_chrome
class TestDocsShapedEditor:
    def test_fill_cannot_reach_it_and_says_to_type(self, chrome, site):
        snap = _open_docs(site)
        doc = _id(snap, "Document content")
        with pytest.raises(RuntimeError) as err:
            ba.browser_fill({"target": doc, "value": "set a value"})
        assert "editor surface" in str(err.value) and "browser_type" in str(err.value)
        assert _doc_text() == ""

    def test_type_by_id_clicks_into_the_canvas_and_types_through_the_hidden_frame(self, chrome, site):
        snap = _open_docs(site)
        doc = _id(snap, "Document content")
        draws = _eval("window.__doc.draws")
        out = ba.browser_type({"target": doc, "text": "Hello from the agent.", "delay_ms": 0})
        assert out == f"TYPED 21 characters into {doc} ('Document content') through its editor frame."
        assert _doc_text() == "Hello from the agent."
        assert _eval("window.__doc.draws") > draws  # the canvas was redrawn from the model
        assert _eval("document.activeElement.id") == "tet"

    def test_a_paragraph_with_line_breaks_becomes_paragraphs_and_presses_no_enter(self, chrome, site):
        snap = _open_docs(site)
        text = "First paragraph, long enough to need more than one chunk of text.\nSecond one.\nThird."
        ba.browser_type({"target": _id(snap, "Document content"), "text": text, "delay_ms": 0})
        assert _doc_text() == text
        assert _eval("window.__doc.paras.length") == 3

    def test_after_a_click_on_the_canvas_no_target_types_into_the_focused_frame(self, chrome, site):
        _open_docs(site)
        ba.browser_click({"target": "css:#cv"})
        out = ba.browser_type({"text": "typed into focus", "delay_ms": 0})
        assert out == "TYPED 16 characters into the focused element inside a frame (contenteditable body)."
        assert _doc_text() == "typed into focus"
        ba.browser_type({"text": " and more", "delay_ms": 0})
        assert _doc_text() == "typed into focus and more"

    def test_keys_mode_also_reaches_it(self, chrome, site):
        snap = _open_docs(site)
        ba.browser_type({"target": _id(snap, "Document content"), "text": "key by key", "mode": "keys", "delay_ms": 0})
        assert _doc_text() == "key by key"

    def test_keys_mode_refuses_a_line_break_and_clear_is_refused_on_a_surface(self, chrome, site):
        snap = _open_docs(site)
        doc = _id(snap, "Document content")
        with pytest.raises(RuntimeError, match="textarea"):
            ba.browser_type({"target": doc, "text": "a\nb", "mode": "keys"})
        with pytest.raises(RuntimeError, match="cannot be cleared"):
            ba.browser_type({"target": doc, "text": "x", "clear": True})
        assert _doc_text() == ""


@needs_chrome
class TestPlainEditors:
    def test_contenteditable_and_textarea_with_line_breaks(self, chrome, site):
        snap = ba.browser_open({"url": site + "/plain.html"})
        ce, ta = _id(snap, "Note body"), _id(snap, "Comments")
        ba.browser_type({"target": ce, "text": "line one\nline two", "delay_ms": 0})
        assert _eval("document.getElementById('ce').innerText").replace("\r", "").strip() == "line one\nline two"
        ba.browser_type({"target": ta, "text": "a\nb\nc", "delay_ms": 0})
        assert _eval("document.getElementById('ta').value") == "a\nb\nc"

    def test_a_line_break_into_a_one_line_input_is_refused_and_nothing_is_typed(self, chrome, site):
        snap = ba.browser_open({"url": site + "/plain.html"})
        with pytest.raises(RuntimeError) as err:
            ba.browser_type({"target": _id(snap, "Subject"), "text": "a\nb"})
        assert "one-line field" in str(err.value) and "textarea" in str(err.value)
        assert _eval("document.getElementById('subj').value") == ""

    def test_a_page_that_moves_the_focus_stops_the_typing_with_the_count(self, chrome, site):
        snap = ba.browser_open({"url": site + "/plain.html"})
        text = "x" * 100
        with pytest.raises(RuntimeError) as err:
            ba.browser_type({"target": _id(snap, "Jumpy"), "text": text, "delay_ms": 0})
        msg = str(err.value)
        assert msg.startswith("STOPPED: the keyboard focus left the element")
        assert "Typed 48 of 100 characters" in msg
        assert _eval("document.getElementById('jumpy').value") == "x" * 48
        assert _eval("document.getElementById('other').value") == ""  # nothing went somewhere else

    def test_text_mode_refuses_when_what_has_focus_takes_no_text(self, chrome, site):
        ba.browser_open({"url": site + "/plain.html"})
        _eval("document.getElementById('btn').focus()")
        with pytest.raises(RuntimeError, match="does not take text"):
            ba.browser_type({"text": "lost"})

    def test_bad_mode(self, chrome, site):
        assert ba.browser_type({"text": "x", "mode": "telepathy"}).startswith("ERROR: browser_type mode")


class TestScriptAdditions:
    def test_the_id_script_still_writes_nothing_to_the_dom(self):
        code = "\n".join(line for line in ba._SCRIPT_PATH.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("//"))
        for forbidden in ("setAttribute(", "removeAttribute(", ".dataset", "classList.", ".className", "innerHTML", "appendChild(", "document.write"):
            assert forbidden not in code, forbidden
        assert re.search(r'if \(op === "focusCheck"\) return focusCheck', code)
