# Fast in-app browser pane — architecture note (backlog #8)

Reconciling the user's 4-phase spec against what Dourmouse actually has,
before writing more code on top of an assumption that doesn't hold.

## The real constraint the spec doesn't account for

The spec assumes a Chromium engine with CDP (Chrome DevTools Protocol)
access embedded in the app's own UI window. Dourmouse's desktop shell
(`dourmouse/desktop.py`) is **pywebview**, which uses the OS's native
webview — **WKWebView on macOS**, WebView2 (Chromium-based) on Windows.
WKWebView has no CDP. So "direct CDP injection" cannot be built as a
feature of the main app window itself on Mac — it would behave
completely differently per platform, which the spec explicitly doesn't
want ("Mac and Windows... ideal tech combinations").

## What already exists and already satisfies most of the spec

`dourmouse/browser_agent.py` (v5.25, already real, already tested) is a
**separate, genuine Chromium engine** via Playwright (`channel="chrome"`),
driven by the `browser` subagent's tools (`browser_open`, `browser_click`,
`browser_fill`, `browser_snapshot`, etc.) — real CDP-level control
already, just not exposed as clicks/typing simulation; Playwright issues
these at the protocol level, which is what "Direct CDP Injection" in
Phase 4 is actually asking for.

Checked against each phase:

- **Phase 2, Global Panel Manager / one shared instance**: already true.
  `_PAGE`/`_CONTEXT` are module-level globals — one browser context is
  launched once per process and reused across every tool call, not
  spun up per request. `browser_agent.py`'s own docstring: "ONE
  dedicated asyncio event-loop thread."
- **Phase 4, Ad & Media Blocking**: was missing, now added this pass —
  `_should_block_request()` aborts video/audio (`resource_type ==
  "media"`) and a real list of known tracker domains via
  `context.route()`. Deliberately conservative: images/CSS/scripts are
  NOT blocked wholesale, since `browser_snapshot` needs real page
  structure to read.
- **Phase 4, streaming tool-calling**: genuinely not done, and bigger
  than this pass — the dispatch loop (`dispatch.py::_run_dispatch_loop`)
  waits for a complete model turn before executing any tool call; making
  it execute step 1 while step 2 streams in is a real change to that
  loop's own control flow, not a browser-layer change. Flagged, not
  attempted here.
- **Phase 3, visible UI pane with nav controls the human can also use**:
  genuinely missing. `browser_agent.py` defaults to
  `DOURMOUSE_BROWSER_HEADLESS=1` — invisible by design (it's an
  automation engine, not a user-facing window). Making it visible AND
  giving the human a real nav toolbar (back/forward/refresh/address bar)
  the way the spec describes means either: (a) running headed
  (`DOURMOUSE_BROWSER_HEADLESS=0`) and accepting a second, separate OS
  window (Playwright's Chrome, not embedded inside Dourmouse's own
  pywebview window — there is no way to embed a second Chromium instance
  inside a WKWebView-backed window), or (b) building a real in-page
  `<iframe>`-based lightweight pane for pages that allow framing, which
  covers far fewer real sites (most block framing via CSP/X-Frame-Options)
  and gives no CDP-level speed advantage at all.

## Recommendation, not yet executed

(a) is the only option that keeps CDP-level speed/control. Concretely:
add a `browser_pane_show`/`browser_pane_hide` pair of tools that flips
`DOURMOUSE_BROWSER_HEADLESS` at runtime and brings the (real, separate)
Chrome window forward — a genuine "open the pane" experience even though
it's a second OS window rather than a panel sliding inside Dourmouse's
own frame. This needs real testing of headless↔headed transitions
mid-session (Playwright does not support this cleanly — headed vs
headless is normally chosen at launch, so toggling likely means
relaunching the browser context, losing in-page state) before shipping,
which is why it's a recommendation and not code in this pass.
