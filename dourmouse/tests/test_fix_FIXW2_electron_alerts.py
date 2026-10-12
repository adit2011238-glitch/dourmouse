"""FIX-W2 A-17 hardening: the Electron alert stream (/api/events) reconnects after it ends or fails,
only while it is the current stream and the app is not quitting. main.js is loaded under plain node
with the FB test harness; the reconnect delay is shortened by wrapping setTimeout in the scenario."""
from __future__ import annotations

import pytest

from dourmouse.tests.b2_harness import NODE
from dourmouse.tests.test_fix_FB_electron_shell import run_scenario

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_PRE = r'''
// the harness already runs a server on the UI port; give it the route main.js listens to
function route(handler) {
  visionServer.removeAllListeners("request");
  visionServer.on("request", handler);
}
const realSetTimeout = global.setTimeout;
const waits = [];
global.setTimeout = (fn, ms, ...rest) => {
  if (ms === 5000) { waits.push(ms); return realSetTimeout(fn, 25, ...rest); }
  return realSetTimeout(fn, ms, ...rest);
};
const sleep = (ms) => new Promise((r) => realSetTimeout(r, ms));
'''


def test_a_stream_the_server_ends_is_opened_again_after_five_seconds(tmp_path):
    out = run_scenario(tmp_path, _PRE + r'''
main(async () => {
  let hits = 0;
  route((req, res) => {
    if (req.url === "/api/events") { hits += 1; res.writeHead(200, { "Content-Type": "text/event-stream" }); res.end(); }
    else { res.writeHead(404); res.end(); }
  });
  T.fn("startAlertNotifications")();
  await sleep(600);
  R.hits = hits;
  R.waits = waits.length;
  appEvents["before-quit"]();
  const before = hits;
  await sleep(400);
  R.afterQuit = hits - before;
});
''')
    assert out["hits"] >= 2, out  # the first connection, then at least one reconnect
    assert out["waits"] >= 1  # the configured delay is the 5000 ms one
    assert out["afterQuit"] <= 1  # one already-planned timer at most; it checks the quit flag and does nothing


def test_a_failed_connection_is_retried_until_the_server_is_back(tmp_path):
    out = run_scenario(tmp_path, _PRE + r'''
main(async () => {
  let attempts = 0, live = 0;
  route((req, res) => {
    if (req.url !== "/api/events") { res.writeHead(404); res.end(); return; }
    attempts += 1;
    if (attempts <= 2) { req.socket.destroy(); return; }   // the server is briefly unreachable
    live += 1;
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    res.write(": hi\\n\\n");
  });
  T.fn("startAlertNotifications")();
  await sleep(700);
  R.attempts = attempts;
  R.live = live;
  appEvents["before-quit"]();
});
''')
    assert out["live"] == 1, out  # it came back, and one live stream is not reconnected again
    assert out["attempts"] == 3


def test_the_source_checks_the_stream_is_still_current_and_the_app_is_not_quitting():
    from dourmouse.tests.test_fix_FB_electron_shell import MAIN_CODE

    assert "alertStream !== req || serverQuitting" in MAIN_CODE
    assert "ALERT_RECONNECT_MS = 5000" in MAIN_CODE
