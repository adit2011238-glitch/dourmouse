"""ui/console.html — real image rendering in chat replies (2026-09-14,
user-directed: "ability to display... screenshots and images").

Real gap found: browser_screenshot (browser_agent.py) already wrote a
real screenshot served at /api/browser/screenshot?name=... — a real,
working endpoint (this session already fixed a real Unicode crash in
it) — but md(), this file's own hand-rolled markdown renderer, had no
image syntax at all, so nothing ever showed the picture. Fixed on both
ends: md() now understands real ![alt](url) syntax, scoped the same way
its own existing link regex already is (a real http(s) URL, or this
app's own /api/... path — never a bare data:/javascript: URL), and
browser_screenshot's own return text now includes a real markdown image
link at the URL it already returns.

No headless browser here (none available in this suite, matching every
other test_console_*.py file's own stated convention) — source-level
coverage that the real wiring is present and correct.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"


def _extract_inline_script() -> str:
    html = _CONSOLE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


class TestMarkdownImageSupport:
    def test_image_syntax_is_matched_before_the_plain_link_regex(self):
        """Order matters: ![alt](url) starts with the same [text](url)
        shape the plain link regex already matches — image syntax must
        be handled first, or the "!" is left stranded in front of a
        plain <a> tag instead of becoming an <img>."""
        script = _extract_inline_script()
        idx = script.index("function md(src)")
        # A generous window: the real explanatory comment ahead of the
        # image regex pushes its own content well past a tight guess.
        block = script[idx: idx + 1600]
        img_idx = block.index("<img")
        link_idx = block.index('<a href="$2"')
        assert img_idx < link_idx

    def test_image_url_is_scoped_to_http_s_or_this_apps_own_api_path(self):
        """Same discipline the existing plain-link regex already has —
        never a bare data:/javascript: URL."""
        script = _extract_inline_script()
        idx = script.index("function md(src)")
        block = script[idx: idx + 1600]
        assert 'https?:\\/\\/[^\\s)]+|\\/api\\/[^\\s)]+' in block

    def test_alt_text_is_carried_through(self):
        script = _extract_inline_script()
        idx = script.index("function md(src)")
        block = script[idx: idx + 1600]
        assert '<img src="$2" alt="$1" loading="lazy">' in block


class TestImageStyling:
    def test_images_inside_a_reply_are_width_capped(self):
        """A real screenshot must never overflow the chat bubble."""
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        idx = html.index(".turn .body img{")
        rule = html[idx: idx + 200]
        assert "max-width:100%" in rule
