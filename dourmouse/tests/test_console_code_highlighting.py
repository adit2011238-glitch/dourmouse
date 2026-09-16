"""ui/console.html — real syntax highlighting for fenced code blocks.

Phase 3 (docs/DESIGN_SYSTEM.md gap 2 / docs/UI_SOURCE_MAP.md's own finding:
"No syntax highlighting at all... the language identifier is parsed and
discarded"). Hand-rolled on purpose, matching md() itself (also hand-rolled
regex, no library) rather than pulling a highlighting library into an
offline-first app that self-hosts even its fonts.

No headless browser here (none available in this suite, matching every
other test_console_*.py file's own stated convention) — source-level
coverage that the tokenizer and its wiring into md() are present and
correct. The actual tokenizer BEHAVIOR (keyword/string/comment matching
across python/javascript/bash/json, the XSS-safety property, the "hash
inside a string is not a comment" edge case) was verified directly this
session by extracting this exact function from the live-served file and
executing it against real inputs — that live/behavioral check is not
re-implemented here as a Python-side test since it would require a JS
runtime this suite deliberately does not depend on (see this file's own
docstring above); this file guards the source shape staying intact.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"


def _html() -> str:
    return _CONSOLE_HTML.read_text(encoding="utf-8")


def _extract_inline_script() -> str:
    m = re.search(r"<script>(.*?)</script>", _html(), re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


class TestHighlighterDefinitions:
    def test_lang_rules_cover_the_languages_this_apps_own_tools_produce(self):
        script = _extract_inline_script()
        idx = script.index("const LANG_RULES=")
        block = script[idx: idx + 1200]
        for lang in ("python:", "javascript:", "bash:", "json:"):
            assert lang in block, f"LANG_RULES is missing {lang!r}"

    def test_lang_aliases_map_common_shorthand(self):
        script = _extract_inline_script()
        idx = script.index("const LANG_ALIASES=")
        line = script[idx: idx + 250]
        for alias in ("py:", "js:", "ts:", "sh:"):
            assert alias in line, f"LANG_ALIASES is missing {alias!r}"

    def test_highlight_code_falls_back_to_plain_text_for_unknown_languages(self):
        """Never guess a language it doesn't have real rules for."""
        script = _extract_inline_script()
        idx = script.index("function highlightCode(escaped,lang){")
        body = script[idx: idx + 200]
        assert "if(!rules) return escaped;" in body

    def test_highlighter_operates_on_already_escaped_text(self):
        """The whole md() pipeline escapes the source ONCE up front;
        highlightCode must never re-escape or unescape, only wrap the
        existing safe text in <span> tags — real XSS-safety property,
        verified behaviorally this session against a literal
        "<script>" payload inside a highlighted string."""
        script = _extract_inline_script()
        idx = script.index("function highlightCode(escaped,lang){")
        body = script[idx: idx + 900]
        assert "escaped.replace(re" in body
        assert ".innerHTML=" not in body


class TestMdIntegration:
    def test_code_fence_captures_the_language_instead_of_discarding_it(self):
        script = _extract_inline_script()
        assert 'chunk.match(/^([\\w-]*)\\n/)' in script

    def test_code_fence_calls_the_real_highlighter(self):
        script = _extract_inline_script()
        idx = script.index("if(i%2){")
        block = script[idx: idx + 400]
        assert "highlightCode(code, lang)" in block

    def test_language_is_surfaced_as_a_data_attribute_not_swallowed(self):
        script = _extract_inline_script()
        idx = script.index("if(i%2){")
        block = script[idx: idx + 400]
        assert 'data-lang="${lang}"' in block


class TestHighlightTokenStyling:
    def test_token_classes_use_real_existing_theme_tokens_not_new_hardcoded_colors(self):
        """Extends the established palette (--amber/--ok/--blue-dim,
        already used elsewhere in this file) rather than introducing a
        second, competing color system for code specifically."""
        html = _html()
        assert ".tok-kw{color:var(--amber)}" in html
        assert ".tok-str{color:var(--ok)}" in html
        assert "color:var(--blue-dim)" in html.split(".tok-com{")[1][:40]

    def test_comments_are_visually_deemphasized(self):
        html = _html()
        idx = html.index(".tok-com{")
        rule = html[idx: idx + 60]
        assert "font-style:italic" in rule

    def test_language_label_reuses_the_existing_always_dark_code_block_scope(self):
        """Must live under the SAME .turn .body pre scope as the rest of
        the code-block styling (line 685 area) — not a new, independent
        selector that could drift from it."""
        html = _html()
        assert ".turn .body pre[data-lang]::before{content:attr(data-lang)" in html

    def test_label_leaves_room_so_it_never_overlaps_the_existing_copy_button(self):
        html = _html()
        assert ".turn .body pre[data-lang]{padding-top:26px}" in html
