"""Finding #190: signing in to Google from the app window.

Live report (owner, 2026-10-31): pressing "Sign in with Google" in the app opened Google in the
system browser, the owner approved it, and the callback page answered
{"ok": false, "error": "this sign-in was not started in this browser - start the login again"}.

Cause: the sign-in banner was a plain link to /api/auth/google/start. The app window followed it,
the server bound its cookie to THAT window, and the shell then sent the redirect to Google out to
the system browser, which had no cookie. login.html already did this right (claim bridge); the
banner and the classic console popup did not. They now share one module with the same flow.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OS = _ROOT / "ui" / "assets" / "os"
_NODE = shutil.which("node") or ""
needs_node = pytest.mark.skipif(not _NODE, reason="node not on PATH in this environment")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _run(tmp_path: Path, body: str) -> dict:
    uri = (_OS / "chrome" / "google-signin.js").as_uri()
    script = tmp_path / "harness.mjs"
    script.write_text(
        f"import {{ startGoogleSignIn, bridgeAvailable }} from '{uri}';\nconst R = {{}};\n"
        + body + "\nconsole.log(JSON.stringify(R));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


# A tiny stand-in for the app window and the server, shared by the harnesses below.
_FAKES = """
const CONSENT = 'https://accounts.google.com/o/oauth2/v2/auth?state=abc';
function makeWin({ open = async () => true } = {}) {
  const w = { opened: [], location: { href: '' }, pywebview: { api: { open_external: async (u) => { w.opened.push(u); return open(u); } } } };
  return w;
}
function makeFetch(claimStatuses, startReply) {
  const calls = [];
  let i = 0;
  const f = async (url) => {
    calls.push(url);
    if (String(url).startsWith('/api/auth/google/start')) {
      const reply = startReply || { status: 200, body: { ok: true, url: CONSENT } };
      return { ok: reply.status < 400, status: reply.status, json: async () => reply.body };
    }
    const status = claimStatuses[Math.min(i, claimStatuses.length - 1)]; i += 1;
    if (status === 'throw') throw new Error('server restarting');
    return { ok: status === 200, status, json: async () => ({}) };
  };
  f.calls = calls;
  return f;
}
const noSleep = async () => {};
"""


@needs_node
class TestTheBridgeFlow:
    def test_inside_the_app_it_asks_for_the_bridge_opens_the_browser_and_waits_for_the_claim(self, tmp_path):
        out = _run(tmp_path, _FAKES + """
const win = makeWin(); const fetchFn = makeFetch([404, 404, 200]); const seen = [];
const res = await startGoogleSignIn({ win, fetchFn, onStatus: (s) => seen.push(s.state), sleep: noSleep });
R.res = res; R.seen = seen; R.opened = win.opened; R.calls = fetchFn.calls; R.href = win.location.href;
""")
        assert out["res"] == {"ok": True, "outcome": "signed-in"}
        assert out["seen"] == ["starting", "waiting", "signed-in"]
        assert out["opened"] == ["https://accounts.google.com/o/oauth2/v2/auth?state=abc"]
        assert out["calls"][0] == "/api/auth/google/start?claim=1&format=json"
        assert out["calls"][1:] == ["/api/auth/claim"] * 3
        assert out["href"] == "", "the app window must never navigate to the start route itself: that is the bug"

    def test_in_a_real_browser_tab_the_plain_redirect_is_used(self, tmp_path):
        out = _run(tmp_path, """
const win = { location: { href: '' } };
let fetched = 0;
R.res = await startGoogleSignIn({ win, fetchFn: async () => { fetched += 1; } });
R.href = win.location.href; R.fetched = fetched; R.bridge = [bridgeAvailable(win), bridgeAvailable(null), bridgeAvailable({ pywebview: {} })];
""")
        assert out["res"] == {"ok": True, "outcome": "redirect"} and out["fetched"] == 0
        assert out["href"] == "/api/auth/google/start"
        assert out["bridge"] == [False, False, False]

    def test_a_browser_that_cannot_be_opened_shows_the_link_and_still_waits(self, tmp_path):
        out = _run(tmp_path, _FAKES + """
const win = makeWin({ open: async () => false }); const seen = [];
const res = await startGoogleSignIn({ win, fetchFn: makeFetch([200]), onStatus: (s) => seen.push(s), sleep: noSleep });
R.res = res; R.second = seen[1];
""")
        assert out["res"]["outcome"] == "signed-in"
        assert out["second"] == {"state": "manual", "url": "https://accounts.google.com/o/oauth2/v2/auth?state=abc"}

    def test_the_server_refusing_to_start_is_said_plainly_and_nothing_is_opened(self, tmp_path):
        out = _run(tmp_path, _FAKES + """
const win = makeWin(); const seen = [];
const res = await startGoogleSignIn({ win, fetchFn: makeFetch([404], { status: 400, body: { ok: false, error: 'Google OAuth NOT CONFIGURED' } }),
  onStatus: (s) => seen.push(s), sleep: noSleep });
R.res = res; R.opened = win.opened; R.last = seen[seen.length - 1];
""")
        assert out["res"] == {"ok": False, "outcome": "error", "message": "Google OAuth NOT CONFIGURED"}
        assert out["opened"] == [] and out["last"]["state"] == "error"

    def test_an_address_that_is_not_https_is_never_opened(self, tmp_path):
        out = _run(tmp_path, _FAKES + """
const win = makeWin();
const res = await startGoogleSignIn({ win, fetchFn: makeFetch([404], { status: 200, body: { ok: true, url: 'javascript:alert(1)' } }), sleep: noSleep });
R.res = res.outcome; R.opened = win.opened;
""")
        assert out["res"] == "error" and out["opened"] == []

    @pytest.mark.parametrize("status", [403, 400])
    def test_a_claim_that_waiting_cannot_fix_stops_the_wait(self, tmp_path, status):
        out = _run(tmp_path, _FAKES + f"""
const fetchFn = makeFetch([404, {status}]);
const res = await startGoogleSignIn({{ win: makeWin(), fetchFn, sleep: noSleep }});
R.res = res; R.polls = fetchFn.calls.filter((c) => c === '/api/auth/claim').length;
""")
        assert out["res"] == {"ok": False, "outcome": "dead-end", "status": status} and out["polls"] == 2

    def test_it_gives_up_after_the_poll_limit_and_survives_a_restarting_server(self, tmp_path):
        out = _run(tmp_path, _FAKES + """
const fetchFn = makeFetch(['throw', 404, 404]);
const res = await startGoogleSignIn({ win: makeWin(), fetchFn, sleep: noSleep, maxPolls: 4 });
R.res = res; R.polls = fetchFn.calls.filter((c) => c === '/api/auth/claim').length;
""")
        assert out["res"] == {"ok": False, "outcome": "timeout"} and out["polls"] == 4


class TestEverySignInPointUsesIt:
    def test_the_shell_banner_runs_the_bridge_instead_of_linking_to_the_start_route(self):
        src = _read("ui/assets/os/chrome/startup-check.js")
        assert "import { startGoogleSignIn } from './google-signin.js';" in src
        assert "data-google-signin" in src
        assert not re.search(r"<a [^>]*href=\"\$\{it\.href\}\"", src), "a plain link to the start route is the bug"
        # the pure function keeps its shape: the href is still the plain-browser fallback
        assert "href: '/api/auth/google/start'" in src

    def test_the_classic_console_popup_runs_the_bridge_too(self):
        src = _read("ui/assets/startup_check.js")
        assert "/assets/os/chrome/google-signin.js" in src and "startGoogleSignIn" in src
        assert 'el("a", { class: "dm-google-link", href: it.href }' not in src

    def test_the_module_is_precached_with_the_rest_of_the_shell(self):
        assert "'/assets/os/chrome/google-signin.js'" in _read("ui/sw.js")

    def test_the_module_follows_the_house_style(self):
        src = _read("ui/assets/os/chrome/google-signin.js")
        assert "—" not in src and "–" not in src

    def test_the_wrong_browser_message_says_what_to_do(self):
        src = _read("dourmouse/webui.py")
        assert "Go back to the Dourmouse app, " in src and "press Sign in with Google there" in src
