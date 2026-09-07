"""Structural checks for the embedded browser pane's real markup/JS in
ui/console.html (backlog #8). The backend trigger is covered by
test_browser_pane.py / test_browser_pane_wiring.py; this covers the
frontend piece actually shipped alongside it.
"""

from __future__ import annotations

from pathlib import Path


def _console_html() -> str:
    return (Path(__file__).resolve().parents[2] / "ui" / "console.html").read_text(encoding="utf-8")


class TestBrowserPaneMarkup:
    def test_pane_element_exists_hidden_by_default(self):
        html = _console_html()
        assert 'id="browserPane" class="bp-pane" hidden' in html

    def test_has_nav_controls(self):
        html = _console_html()
        for control_id in ("bpBack", "bpFwd", "bpReload", "bpAddr", "bpGo", "bpClose"):
            assert f'id="{control_id}"' in html, control_id

    def test_has_an_iframe_with_a_sandbox_attribute(self):
        html = _console_html()
        assert 'id="bpFrame"' in html
        assert "sandbox=" in html

    def test_has_a_real_fallback_for_frames_that_refuse_embedding(self):
        html = _console_html()
        assert 'id="bpFallback"' in html
        assert 'id="bpFallbackLink"' in html

    def test_hidden_attribute_actually_hides_the_pane(self):
        """Real bug found in live-preview testing: `.bp-pane{display:flex}`
        alone lets the `hidden` attribute get overridden by the class's own
        `display`, since an author stylesheet always beats the UA default
        `[hidden]{display:none}` rule regardless of selector specificity.
        The other overlays in this file (#picker, #boot) already carry an
        explicit `[hidden]{display:none}` override — the pane needs the
        same, or it renders on load and blocks the tab strip underneath it.
        """
        html = _console_html()
        assert ".bp-pane[hidden]{display:none}" in html


class TestBrowserPaneJs:
    def test_open_closes_reuse_one_shared_iframe(self):
        """Global Panel Manager (Phase 2 of the user's own spec): one
        shared instance, not one per tab."""
        html = _console_html()
        assert "function openBrowserPane(url)" in html
        assert "function closeBrowserPane()" in html

    def test_close_actually_unloads_the_frame(self):
        """Real RAM-back semantics, not just hiding a live page."""
        html = _console_html()
        assert 'frame.src = ""' in html

    def test_listens_for_the_real_sse_event_type(self):
        html = _console_html()
        assert '"browser_pane_open"' in html

    def test_autofocuses_on_open(self):
        html = _console_html()
        assert "frame.focus()" in html

    def test_has_a_bounded_fallback_timeout(self):
        html = _console_html()
        assert "BP_LOAD_TIMEOUT_MS" in html

    def test_checks_frameability_before_committing_to_the_iframe(self):
        """Real bug found live-testing this pane: the iframe's own `load`
        event fires even when X-Frame-Options/CSP blocks the site from
        rendering, so the timeout-only fallback never triggered and the
        pane went permanently blank. openBrowserPane must ask the backend
        first."""
        html = _console_html()
        assert "/api/browser-pane/check" in html
        assert "_bpOpenSeq" in html  # guards a stale check racing a newer open()


class TestBrowserPaneClose:
    def test_close_invalidates_any_in_flight_frameability_check(self):
        """A check_frameable() call in flight when the user closes the
        pane (or opens a different URL) must not resurrect it later."""
        html = _console_html()
        assert "_bpOpenSeq++" in html
