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
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"


def _extract_inline_script() -> str:
    html = _CONSOLE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


def _extract_esc_source(script: str) -> str:
    """The real esc() helper's source, for node harnesses that exercise
    renderYouText(). A lazy ``.*?;`` regex is the wrong tool here: esc()'s
    own body contains the literal string "&amp;" -- which itself ends in
    a semicolon -- so a non-greedy match up to "the first ;" stops
    mid-object-literal, not at the statement's real end. Slicing from its
    known start up to the start of the next real statement is correct
    without guessing a length."""
    start = script.index("const esc = (s) =>")
    end = script.index("\nconst api", start)
    return script[start:end]


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
        block = script[idx: idx + 2200]
        img_idx = block.index("<img")
        link_idx = block.index('<a href="$2"')
        assert img_idx < link_idx

    def test_image_url_is_scoped_to_http_s_or_this_apps_own_api_path(self):
        """Same discipline the existing plain-link regex already has —
        never a bare data:/javascript: URL."""
        script = _extract_inline_script()
        idx = script.index("function md(src)")
        block = script[idx: idx + 2200]
        assert 'https?:\\/\\/[^\\s)]+|\\/api\\/[^\\s)]+' in block

    def test_alt_text_is_carried_through(self):
        script = _extract_inline_script()
        idx = script.index("function md(src)")
        block = script[idx: idx + 2200]
        assert '<img src="$2" alt="$1" loading="lazy">' in block

    def test_image_url_also_accepts_this_apps_own_uploads_path(self):
        """2026-09-14: real drag-and-drop file/image uploads go through
        the existing POST /api/upload, served back at /uploads/<name> —
        a real, working endpoint that predates this feature. Without
        this, a dropped image would upload fine but never render inline,
        the same gap browser_screenshot's own images had before this
        file's original fix."""
        script = _extract_inline_script()
        idx = script.index("function md(src)")
        block = script[idx: idx + 2200]
        assert '\\/uploads\\/[^\\s)]+' in block


class TestYourOwnAttachedImageRendersToo:
    """2026-09-14 (live-caught, user-directed: "allow files and images to
    be dropped into the chat"): a real live test dragged a real image
    onto the composer, sent it, and the user's OWN turn rendered it as
    raw text ("![name](url)") instead of a picture -- the drop and
    upload both genuinely worked, the user just never saw what they
    attached. addYou() used plain textContent (never ran through any
    image regex at all); it now escapes first (same discipline md() —
    itself renders images inline only for OTHER images, never full
    markdown, so the rest of a typed message can't have **bold**/links
    silently reinterpreted out from under the user."""

    def test_render_you_text_exists_and_escapes_before_matching(self):
        script = _extract_inline_script()
        m = re.search(r"function renderYouText\(text\)\{(.*?)\n\}", script, re.S)
        assert m, "renderYouText not found"
        body = m.group(1)
        assert "esc(text)" in body
        assert '<img src="$2" alt="$1" loading="lazy">' in body

    def test_addyou_uses_render_you_text_not_plain_textcontent(self):
        script = _extract_inline_script()
        m = re.search(r"function addYou\(threadEl, text\)\{(.*?)\n\}", script, re.S)
        assert m, "addYou not found"
        body = m.group(1)
        assert "renderYouText(text)" in body
        assert ".textContent=text" not in body

    def test_functionally_renders_an_attached_image_not_raw_markdown(self, tmp_path):
        """Real execution: the exact live scenario -- a message that is
        ONLY a dropped image's markdown link must become a real <img>,
        not escaped text."""
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        script = _extract_inline_script()
        esc_src = _extract_esc_source(script)
        render_m = re.search(r"(function renderYouText\(text\)\{.*?\n\})", script, re.S)
        assert render_m
        harness = esc_src + "\n" + render_m.group(1) + """
const out = renderYouText("![live-drop-test.png](/uploads/live-drop-test.png)");
const ok = out === '<img src="/uploads/live-drop-test.png" alt="live-drop-test.png" loading="lazy">';
console.log(JSON.stringify({ok, out}));
process.exit(ok ? 0 : 1);
"""
        js_file = tmp_path / "render_you_harness.js"
        js_file.write_text(harness, encoding="utf-8")
        result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"attached image did not render:\n{result.stdout}\n{result.stderr}"

    def test_a_typed_message_that_merely_looks_like_markdown_is_left_alone(self, tmp_path):
        """The conservative scope: only a real image link renders — bold,
        code, and plain links in a user's own typed text must NOT be
        silently reinterpreted, only escaped for safety like before."""
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        script = _extract_inline_script()
        esc_src = _extract_esc_source(script)
        render_m = re.search(r"(function renderYouText\(text\)\{.*?\n\})", script, re.S)
        assert render_m
        harness = esc_src + "\n" + render_m.group(1) + """
const out = renderYouText("**not bold** and <script>alert(1)</script>");
const ok = out.includes("**not bold**")
  && !out.includes("<strong>")
  && !out.includes("<script>")
  && out.includes("&lt;script&gt;");
console.log(JSON.stringify({ok, out}));
process.exit(ok ? 0 : 1);
"""
        js_file = tmp_path / "render_you_safe_harness.js"
        js_file.write_text(harness, encoding="utf-8")
        result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"plain text was not left alone / not escaped:\n{result.stdout}\n{result.stderr}"


class TestImageStyling:
    def test_images_inside_a_reply_are_width_capped(self):
        """A real screenshot must never overflow the chat bubble."""
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        idx = html.index(".turn .body img{")
        rule = html[idx: idx + 200]
        assert "max-width:100%" in rule
