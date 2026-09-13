"""ui/console.html — the STUDIO theme family (Phase 6 of the original
6-phase plan). Approved from a published HTML mockup before any of this
landed (the explicit gate the user asked for on the UI redesign).

Same design-token discipline Aurora already proved: one shared structural
rule block applies identically to all four studio-* theme ids, and only
each one's own token block differs. Where Aurora changes shape toward
glass (blur, glow, 18px radius, system type), Studio changes shape toward
a plain modern app: solid surfaces, hairline borders, a small shadow only
where something genuinely lifts off the page, 6 to 10px radius, one sans
face for both interface chrome and prose. Built to the no-slop-ui
reference rules the user asked this pass to follow, and deliberately
filtered against every purple, violet, or indigo leaning palette in that
reference table, per the user's own no-purple instruction.

No headless browser here (none available in this suite, matching every
other test_console_*.py file's own stated convention) — source-level
coverage that the real wiring is present and correct.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"
_FAMILY = ["studio-void", "studio-ink", "studio-paper", "studio-frost"]
_DARK = ["studio-void", "studio-ink"]
_LIGHT = ["studio-paper", "studio-frost"]


def _html() -> str:
    return _CONSOLE_HTML.read_text(encoding="utf-8")


def _extract_inline_script() -> str:
    m = re.search(r"<script>(.*?)</script>", _html(), re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


class TestAllFourThemesRegistered:
    def test_every_family_member_has_a_root_token_block(self):
        html = _html()
        for theme_id in _FAMILY:
            assert f'[data-theme="{theme_id}"]{{' in html, f"{theme_id} has no root token block"

    def test_every_family_member_is_in_the_js_themes_registry(self):
        script = _extract_inline_script()
        for theme_id in _FAMILY:
            assert f'"{theme_id}": {{' in script, f"{theme_id} missing from THEMES"

    def test_names_are_distinct_and_carry_the_family_prefix(self):
        script = _extract_inline_script()
        for label in ("STUDIO VOID", "STUDIO INK", "STUDIO PAPER", "STUDIO FROST"):
            assert f'name:"{label}"' in script


class TestSharedShapeAppliesToTheWholeFamily:
    """The actual point of this design: one structural rule, four color
    schemes, never four independently maintained copies."""

    def test_the_shared_selector_covers_all_four_ids_at_once(self):
        html = _html()
        idx = html.index('[data-theme^="studio-"] .card')
        block = html[idx: idx + 400]
        assert 'border-radius:8px' in block

    def test_radius_stays_inside_the_no_slop_ui_range(self):
        """6 to 10px, never Aurora's 18px and never the base theme's 2
        to 3px hairline look — Studio is deliberately its own middle
        ground."""
        html = _html()
        idx = html.index('[data-theme^="studio-"] .card')
        block = html[idx: idx + 400]
        m = re.search(r"border-radius:(\d+)px", block)
        assert m, "no radius set on the shared studio card rule"
        assert 6 <= int(m.group(1)) <= 10

    def test_no_backdrop_blur_anywhere_in_the_family_block(self):
        html = _html()
        start = html.index('STUDIO: a restrained fifth family')
        end = html.index('studio-frost"]{', start)
        end = html.index('}', html.index('--accent2', end)) + 1
        block = html[start:end]
        assert "backdrop-filter" not in block

    def test_no_glow_shadow_anywhere_in_the_family_block(self):
        html = _html()
        start = html.index('STUDIO: a restrained fifth family')
        end = html.index('studio-frost"]{', start)
        end = html.index('}', html.index('--accent2', end)) + 1
        block = html[start:end]
        assert "box-shadow" not in block or "box-shadow:none" in block

    def test_studio_shares_the_real_prose_font_fix_not_a_second_face(self):
        """The font fix (font-prose now Fira Sans everywhere) already
        covers Studio's prose. mono/display point at the same face here
        rather than adding a second one — this file's fonts are bundled
        locally with no CDN fallback, and no-slop-ui's own guidance is a
        single sans face, not a mono chrome over a sans body."""
        html = _html()
        idx = html.index('[data-theme="studio-void"]{')
        block = html[idx: idx + 900]
        assert "'Fira Sans'" in block

    def test_emphasis_inside_prose_uses_the_second_accent(self):
        html = _html()
        assert '[data-theme^="studio-"] .turn .body b' in html
        assert "var(--accent2)" in html


class TestVoidAndInkAreGenuinelyDark:
    def test_backgrounds_are_dark(self):
        html = _html()
        for theme_id, bg in (("studio-void", "#0d1117"), ("studio-ink", "#0f0f0f")):
            idx = html.index(f'[data-theme="{theme_id}"]{{')
            block = html[idx: idx + 200]
            assert f"--bg:{bg}" in block


class TestPaperAndFrostAreGenuinelyLight:
    def test_backgrounds_are_light(self):
        html = _html()
        for theme_id, bg in (("studio-paper", "#fcfcfc"), ("studio-frost", "#f1f5f9")):
            idx = html.index(f'[data-theme="{theme_id}"]{{')
            block = html[idx: idx + 200]
            assert f"--bg:{bg}" in block


class TestNoPurpleAnywhereInTheFamily:
    """The user's own explicit instruction, checked as a real regression
    guard rather than trusted to hold by construction alone."""

    _PURPLE_ISH = ("purple", "violet", "indigo", "#a259ff", "#8b5cf6", "#7c3aed", "#6366f1")

    def test_no_purple_violet_or_indigo_token_in_any_studio_block(self):
        html = _html()
        for theme_id in _FAMILY:
            idx = html.index(f'[data-theme="{theme_id}"]{{')
            end = html.index("}", idx)
            block = html[idx:end].lower()
            for term in self._PURPLE_ISH:
                assert term not in block, f"{theme_id} contains {term!r}"
