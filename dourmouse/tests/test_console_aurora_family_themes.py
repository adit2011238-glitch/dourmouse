"""ui/console.html — the AURORA color-scheme family (v14, user-directed,
2026-09-12): "make 4 different variations of the UI where everything is
the same except for color scheme, that's all."

Real design-token discipline: Aurora's own SHAPE (frosted glass, 18px
radius, backdrop blur, soft depth, SF-system type) already existed as
the app's one Apple-inspired theme. Rather than four hand-copied
variants, every structural rule now applies identically to all four
aurora-* theme ids — only each one's own :root token block (color,
gradient mesh, and for Citrus alone, a warm-toned glass shadow) differs.
Two of the four are direct token-language matches for the user's own
attached reference designs: AURORA EMERALD (dark, teal accent) for the
dark analytics-dashboard reference, AURORA CITRUS (light, lime accent)
for the light CRM reference. AURORA VIOLET and the renamed AURORA BLUE
(was plain "AURORA") round the family out to four.

No headless browser here (none available in this suite, matching every
other test_console_*.py file's own stated convention) — source-level
coverage that the real wiring is present and correct.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"
_FAMILY = ["aurora", "aurora-emerald", "aurora-violet", "aurora-citrus"]


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
            assert f'[data-theme="{theme_id}"]{{' in html, f"{theme_id} has no :root token block"

    def test_every_family_member_is_in_the_js_themes_registry(self):
        script = _extract_inline_script()
        for theme_id in _FAMILY:
            key = theme_id if theme_id == "aurora" else f'"{theme_id}"'
            assert f"{key}: {{" in script, f"{theme_id} missing from THEMES"

    def test_names_are_distinct_and_carry_the_family_prefix(self):
        script = _extract_inline_script()
        for label in ("AURORA BLUE", "AURORA EMERALD", "AURORA VIOLET", "AURORA CITRUS"):
            assert f'name:"{label}"' in script


class TestSharedShapeAppliesToTheWholeFamily:
    """The actual point of this design: one structural rule, four
    color schemes — never four independently-maintained copies."""

    def test_glass_card_radius_and_blur_cover_every_family_member(self):
        html = _html()
        idx = html.index('border-radius:18px;')
        # The selector list immediately above this declaration must name
        # all four theme ids against .card (not just the original aurora).
        block = html[max(0, idx - 900): idx]
        for theme_id in _FAMILY:
            assert f'[data-theme="{theme_id}"] .card' in block, f"{theme_id} not in the shared glass-card selector"

    def test_ambient_rain_grain_texture_is_off_for_the_whole_family(self):
        html = _html()
        assert 'aurora-emerald"] #tex{opacity:0}' not in html  # sanity: not a single-line hack
        idx = html.index('[data-theme="arc"] #tex,[data-theme="aether"] #tex,[data-theme="aurora"] #tex,')
        block = html[idx: idx + 300]
        for theme_id in ("aurora-emerald", "aurora-violet", "aurora-citrus"):
            assert theme_id in block


class TestCitrusIsGenuinelyLight:
    """The one light member needs real light-glass treatment, not a
    dark-mode inversion — a black shadow under a translucent white
    panel over a light ground reads as dirt, not depth."""

    def test_citrus_background_is_light(self):
        html = _html()
        idx = html.index('[data-theme="aurora-citrus"]{')
        block = html[idx: idx + 400]
        assert "--bg:#F6F7F1" in block

    def test_citrus_gets_its_own_warm_toned_glass_shadow(self):
        html = _html()
        idx = html.index('[data-theme="aurora-citrus"]{')
        block = html[idx: idx + 900]
        assert "--glass-shadow:" in block
        assert "rgba(60,64,40" in block  # warm-toned, not the dark themes' pure black

    def test_the_shared_shadow_rule_actually_reads_the_override_variable(self):
        html = _html()
        idx = html.index("box-shadow:var(--glass-shadow")
        assert idx > 0


class TestEmeraldMatchesTheDarkReferenceLanguage:
    def test_emerald_accent_is_teal(self):
        html = _html()
        idx = html.index('[data-theme="aurora-emerald"]{')
        block = html[idx: idx + 400]
        assert "--amber:#00E6A6" in block

    def test_emerald_stays_dark(self):
        html = _html()
        idx = html.index('[data-theme="aurora-emerald"]{')
        block = html[idx: idx + 400]
        assert "--bg:#0a1210" in block
