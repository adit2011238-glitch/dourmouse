"""FIX-R3 W3R-7: the sign-in pages' /api/auth/claim poll stops and explains a 403 (not the app window)
or a 400 (no sign-in started here) instead of polling silently until it times out. A 404 means "not
claimed yet" and keeps polling. The pages' own poll functions are cut out of the HTML and run in node
against a fake fetch, timer and DOM."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is needed to run the page script")

_HARNESS = """
let tick = null, cleared = false;
globalThis.setInterval = (f) => { tick = f; return 1; };
globalThis.clearInterval = () => { cleared = true; };
globalThis.fetch = async () => ({ ok: false, status: STATUS, json: async () => ({}) });
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, innerHTML: '', textContent: '', className: '', disabled: true });
globalThis.document = { getElementById: el };
globalThis.$ = el;
globalThis.location = { href: '' };
CODE
START;
(async () => {
  await tick();
  const texts = Object.values(els).map((e) => e.innerHTML + ' ' + e.textContent).join(' ');
  console.log(JSON.stringify({ cleared, texts }));
})();
"""


def _cut(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _run(page: str, status: int) -> dict:
    src = (UI / page).read_text(encoding="utf-8")
    if page == "login.html":
        code = _cut(src, "function startClaimPoll()", "const claiming =")
        start = "startClaimPoll()"
    else:
        code = "let gClaimTimer = null;\n" + _cut(src, "function gPollClaim(){", "async function gStartSignin")
        start = "gPollClaim()"
    script = _HARNESS.replace("STATUS", str(status)).replace("CODE", code).replace("START", start)
    assert NODE is not None
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("page", ["login.html", "setup.html"])
def test_a_403_stops_the_poll_and_says_to_use_the_app_window(page):
    result = _run(page, 403)
    assert result["cleared"] is True
    assert "app window" in result["texts"]


@pytest.mark.parametrize("page", ["login.html", "setup.html"])
def test_a_400_stops_the_poll_and_says_to_start_again(page):
    result = _run(page, 400)
    assert result["cleared"] is True
    assert "No sign-in was started" in result["texts"]


@pytest.mark.parametrize("page", ["login.html", "setup.html"])
def test_a_404_keeps_waiting(page):
    result = _run(page, 404)
    assert result["cleared"] is False
