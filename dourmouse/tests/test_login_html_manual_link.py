"""ui/login.html — Phase 6, item 5: the manual-link fallback and the
?claimed=1 overlay.

Two real bugs, one deeper than the plan itself assumed while scoping
this item:

1. showManualLink()'s injected HTML hardcoded old dark-navy/cyan colors
   that clash with the page's current light retheme, and reused a CSS
   class ("gg-btn") that does not exist anywhere in this file's own
   stylesheet, so the retry button rendered completely unstyled.
2. Found while fixing (1): both showManualLink() and the click handler
   right above it call getElementById('claimNote') — an id that does
   not exist anywhere in this page's markup at all. The real element,
   already used for every other status message on this page, is
   ggoogleMsg. The old code's own `if (!w) return` guard meant the
   whole fallback UI silently never rendered, not merely rendered
   unstyled.

The ?claimed=1 success overlay had its own separate, third, inconsistent
gray pair, fixed the same way — real --text/--text-dim tokens instead of
new hardcoded values.

No headless browser here (matching this repo's own test_console_*.py
convention) — source-level coverage that the real wiring is present.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOGIN_HTML = _PROJECT_ROOT / "ui" / "login.html"


def _html() -> str:
    return _LOGIN_HTML.read_text(encoding="utf-8")


class TestManualLinkTargetsARealElement:
    def test_claim_note_id_no_longer_looked_up(self):
        """The dead id this whole bug traced back to. The fix's own
        explanatory comment legitimately still names it in prose, so
        this checks the real failure mode specifically: the actual
        getElementById call, not every mention of the string."""
        assert "document.getElementById('claimNote')" not in _html()

    def test_both_call_sites_target_the_real_status_element(self):
        html = _html()
        assert html.count("getElementById('ggoogleMsg')") >= 2


class TestManualLinkUsesRealTokensAndARealClass:
    def test_no_hardcoded_dark_navy_or_cyan(self):
        html = _html()
        idx = html.index("function showManualLink")
        end = html.index("ggoogleBtn.addEventListener", idx)
        block = html[idx:end]
        for stale in ("#0b0f14", "#7fd8ff", "#233", "#6a7a8a"):
            assert stale not in block, f"{stale} still hardcoded in showManualLink"

    def test_uses_the_real_existing_button_class(self):
        """gbtn.ghost already exists in this file's own stylesheet for
        exactly this case (a normal system button, not Google's branded
        one) — gg-btn never existed anywhere."""
        html = _html()
        assert "gg-btn" not in html
        idx = html.index("function showManualLink")
        end = html.index("ggoogleBtn.addEventListener", idx)
        block = html[idx:end]
        assert 'class="gbtn ghost"' in block

    def test_input_and_paragraph_use_real_tokens(self):
        html = _html()
        idx = html.index("function showManualLink")
        end = html.index("ggoogleBtn.addEventListener", idx)
        block = html[idx:end]
        assert "var(--surface)" in block
        assert "var(--text)" in block
        assert "var(--text-dim)" in block


class TestClaimedOverlayFixed:
    def test_no_third_inconsistent_gray_pair(self):
        html = _html()
        idx = html.index("claimed === '1'")
        block = html[idx: idx + 500]
        assert "#1a1a1a" not in block
        assert "#666" not in block

    def test_uses_the_real_page_tokens(self):
        html = _html()
        idx = html.index("claimed === '1'")
        block = html[idx: idx + 500]
        assert "var(--text)" in block
        assert "var(--text-dim)" in block
