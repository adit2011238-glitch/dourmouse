"""ui/console.html — Phase 6 hygiene fixes found while reading the theme
system in full, plus the new custom widgets built alongside the Studio
theme family.

Four unrelated real bugs, each found by reading the actual code rather
than assumed from the plan alone:

1. font-prose was still a mono stack in every theme even though this
   file already ships a real, locally hosted Fira Sans specifically for
   long form prose — it landed on font-prose-alt, a variable nothing
   ever read.
2. Three stale "chrome color" values (the static meta theme-color tag,
   manifest.json's theme_color/background_color, and D2D_DEFAULT_COLOR's
   frozen palette snapshot) never tracked the real retheme that already
   happened elsewhere in this file.
3. .glasscard/.glassbtn (the GLOBE screen's setup card) were the one
   real hardcoded-CSS straggler: a gradient button fill, a colored glow
   filter, and backdrop blur, none theme-token-driven — and the
   secondary button's translucent white fill made it invisible on every
   light theme.
4. No custom widgets beyond the color system itself: a plain OS
   scrollbar, plain OS tooltips, and an unstyled native select were the
   remaining stock-browser surfaces in an otherwise fully themed app.

No headless browser here (none available in this suite, matching every
other test_console_*.py file's own stated convention) — source-level
coverage that the real wiring is present and correct.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"
_MANIFEST = _PROJECT_ROOT / "ui" / "manifest.json"


def _html() -> str:
    return _CONSOLE_HTML.read_text(encoding="utf-8")


def _extract_inline_script() -> str:
    m = re.search(r"<script>(.*?)</script>", _html(), re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


class TestFontProseFix:
    def test_font_prose_points_at_fira_sans(self):
        html = _html()
        idx = html.index("--font-prose:")
        line = html[idx: idx + 200].splitlines()[0]
        assert "'Fira Sans'" in line

    def test_font_prose_alt_is_retired(self):
        assert "--font-prose-alt" not in _html()

    def test_turn_body_font_size_is_in_the_no_slop_ui_body_range(self):
        html = _html()
        idx = html.index(".turn .body{")
        rule = html[idx: idx + 200].split("}")[0]
        m = re.search(r"font-size:(\d+(?:\.\d+)?)px", rule)
        assert m, "no font-size on .turn .body"
        assert 14 <= float(m.group(1)) <= 16

    def test_turn_body_still_reads_font_prose_not_a_literal(self):
        html = _html()
        idx = html.index(".turn .body{")
        rule = html[idx: idx + 200].split("}")[0]
        assert "var(--font-prose)" in rule


class TestStaleChromeColorsFixed:
    def test_meta_theme_color_matches_the_real_default_background(self):
        # A block comment near the top of the file legitimately still
        # names #04080e as history (the pre-retheme value this whole
        # file's own big top-of-file comment explains) — only the live
        # meta tag itself needs to be current.
        html = _html()
        assert 'name="theme-color" content="#09090B"' in html
        assert 'name="theme-color" content="#04080e"' not in html

    def test_manifest_matches_the_real_default_background(self):
        data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        assert data["theme_color"] == "#09090B"
        assert data["background_color"] == "#09090B"

    def test_theme_set_updates_the_meta_tag_live(self):
        script = _extract_inline_script()
        idx = script.index("function themeSet(t){")
        block = script[idx: idx + 900]
        assert 'meta[name="theme-color"]' in block
        assert "cssVar(\"--bg\")" in block

    def test_d2d_default_color_reads_the_live_theme_not_a_frozen_object(self):
        script = _extract_inline_script()
        assert "const D2D_DEFAULT_COLOR = {" not in script
        assert "function d2dDefaultColor(category)" in script
        idx = script.index("function d2dDefaultColor(category)")
        block = script[idx: idx + 150]
        assert "cssVar(" in block

    def test_d2d_call_site_uses_the_new_function(self):
        script = _extract_inline_script()
        assert "d2dDefaultColor(placingCategory)" in script


class TestGlasscardFixed:
    def test_no_gradient_fill(self):
        html = _html()
        idx = html.index(".glasscard{")
        end = html.index(".glassbtn.secondary:hover", idx) + 100
        block = html[idx:end]
        assert "gradient" not in block

    def test_no_blur(self):
        html = _html()
        idx = html.index(".glasscard{")
        end = html.index(".glassbtn.secondary:hover", idx) + 100
        block = html[idx:end]
        assert "blur" not in block

    def test_no_glow_filter(self):
        html = _html()
        idx = html.index(".glasscard{")
        end = html.index(".glassbtn.secondary:hover", idx) + 100
        block = html[idx:end]
        assert "drop-shadow" not in block

    def test_shadow_capped_at_the_no_slop_ui_limit(self):
        html = _html()
        idx = html.index(".glasscard{")
        rule = html[idx: idx + 400]
        assert "box-shadow:0 2px 8px rgba(0,0,0,.08)" in rule

    def test_solid_theme_token_background(self):
        html = _html()
        idx = html.index(".glasscard{")
        rule = html[idx: idx + 400]
        assert "background:var(--panel)" in rule

    def test_primary_button_uses_the_real_established_accent_convention(self):
        """Matches #go's own solid background:var(--amber) pattern
        exactly, rather than a bespoke color."""
        html = _html()
        idx = html.index(".glassbtn.primary{")
        rule = html[idx: idx + 100]
        assert "background:var(--amber)" in rule
        assert "color:var(--amber-ink)" in rule

    def test_secondary_button_is_bordered_not_a_translucent_white_fill(self):
        """The real live bug this replaces: rgba(255,255,255,.08) is
        invisible on every light theme's white ground."""
        html = _html()
        idx = html.index(".glassbtn.secondary{")
        rule = html[idx: idx + 100]
        assert "rgba(255,255,255" not in rule
        assert "border-color:var(--line-2)" in rule

    def test_deliberately_always_dark_code_blocks_are_left_alone(self):
        """The file's own documented exception (.turn .body pre,
        .actdetail, .bookreadera pre, all paired with --on-dark-code) is
        a different, correctly-handled case and must not have been
        touched by this pass."""
        html = _html()
        assert "--on-dark-code:#cfe3f2;" in html


class TestCustomWidgets:
    """Phase 6 was extended, at the user's own request after reviewing
    the mockup, to add real custom widgets beyond the color system
    itself — closer to what a considered desktop app ships instead of
    stock browser controls."""

    def test_scrollbar_is_theme_token_driven(self):
        html = _html()
        assert "scrollbar-color:var(--line-2)" in html
        assert "::-webkit-scrollbar-thumb{background:var(--line-2)" in html

    def test_custom_tooltip_system_exists(self):
        html = _html()
        assert "[data-tip]::after{" in html
        assert "content:attr(data-tip)" in html

    def test_tooltip_has_a_deliberate_delay_not_an_instant_flash(self):
        html = _html()
        idx = html.index("[data-tip]::after{")
        rule = html[idx: idx + 700]
        m = re.search(r"transition:opacity [\d.]+s ease ([\d.]+)s", rule)
        assert m, "no delay on the tooltip transition"
        assert float(m.group(1)) > 0

    def test_composer_chips_use_the_new_tooltip_not_the_os_one(self):
        html = _html()
        assert 'id="micChip" data-tip=' in html
        assert 'id="spkChip" data-tip=' in html
        assert 'id="autoProjectChip" data-tip=' in html
        assert 'id="micChip" title=' not in html

    def test_backend_select_drops_the_native_chrome(self):
        html = _html()
        idx = html.index("#backendPick{")
        rule = html[idx: idx + 500]
        assert "appearance:none" in rule

    def test_backend_select_gets_a_real_custom_chevron(self):
        html = _html()
        assert ".backendsel::after{" in html
        idx = html.index(".backendsel::after{")
        rule = html[idx: idx + 300]
        assert "border-top:5px solid var(--blue-dim)" in rule
