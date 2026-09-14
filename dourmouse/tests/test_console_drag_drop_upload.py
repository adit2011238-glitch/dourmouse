"""ui/console.html — real drag-and-drop file/image upload into the chat
composer (2026-09-14, user-directed: "allow files and images to be
dropped into the chat as well").

Real gap found: POST /api/upload (webui.py's own real, sandboxed uploads
endpoint, served back at /uploads/<name>) already existed and needed no
backend changes at all — nothing in console.html ever called it. This
file covers the frontend-only fix: a real (ATTACH) button (native file
picker) plus real dragover/drop listeners on the composer, both
funneling through one uploadAndInsert() call that POSTs the raw file
bytes and inserts a real markdown link (an image link for image
extensions, rendered inline by md()'s own regex — widened in the same
change to also accept /uploads/... — a plain link otherwise).

No headless browser here for the DOM-wiring tests (none available in
this suite, matching every other test_console_*.py file's own stated
convention) — source-level coverage that the real wiring is present and
correct. One real node-executed functional test proves uploadAndInsert
itself actually POSTs the right bytes to the right URL and inserts the
right markdown, the same style test_console_typewriter_reveal.py and
test_console_projects_import.py already established for this file.
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


class TestAttachButtonAndHiddenInput:
    def test_attach_button_and_file_input_exist(self):
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        assert 'id="attachChip"' in html
        assert 'id="attachInput"' in html
        assert 'type="file"' in html

    def test_attach_button_opens_the_native_file_picker(self):
        script = _extract_inline_script()
        assert '$("attachChip").onclick=()=>$("attachInput").click();' in script

    def test_selecting_files_uploads_every_one(self):
        script = _extract_inline_script()
        m = re.search(r'\$\("attachInput"\)\.onchange=\(\)=>\{(.*?)\};', script, re.S)
        assert m, "attachInput.onchange not found"
        body = m.group(1)
        assert "forEach(uploadAndInsert)" in body


class TestDragAndDrop:
    def test_drop_on_the_composer_uploads_every_dropped_file(self):
        script = _extract_inline_script()
        m = re.search(r'\$\("ta"\)\.addEventListener\("drop", e=>\{(.*?)\}\);', script, re.S)
        assert m, "the drop listener on the composer was not found"
        body = m.group(1)
        assert "e.dataTransfer" in body
        assert "forEach(uploadAndInsert)" in body

    def test_default_browser_navigation_is_prevented_on_drop(self):
        """Without preventDefault, dropping a file onto the page navigates
        the whole tab to that file instead of uploading it."""
        script = _extract_inline_script()
        m = re.search(r'\$\("ta"\)\.addEventListener\("drop", e=>\{(.*?)\}\);', script, re.S)
        assert m
        assert "e.preventDefault()" in m.group(1)

    def test_dragover_gives_real_visual_feedback(self):
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        assert "#ta.dragover" in html
        script = _extract_inline_script()
        assert 'classList.add("dragover")' in script
        assert 'classList.remove("dragover")' in script


class TestUploadAndInsert:
    def test_posts_raw_bytes_to_the_real_upload_endpoint(self):
        script = _extract_inline_script()
        m = re.search(r"async function uploadAndInsert\(file\)\{(.*?)\n\}", script, re.S)
        assert m, "uploadAndInsert not found"
        body = m.group(1)
        assert "/api/upload?name=" in body
        assert "method:\"POST\", body:file" in body

    def test_image_extensions_insert_a_real_markdown_image_link(self):
        script = _extract_inline_script()
        assert "_IMAGE_EXT_RE" in script
        m = re.search(r"async function uploadAndInsert\(file\)\{(.*?)\n\}", script, re.S)
        assert m
        body = m.group(1)
        assert "`![${r.name}](${url})`" in body
        assert "`[${r.name}](${url})`" in body

    def test_filename_is_sanitized_to_match_the_server_side_whitelist(self):
        """webui.py's own _UPLOAD_NAME_RE is ^[A-Za-z0-9._-]{1,120}$ —
        sanitizing client-side too means a real-world filename (spaces,
        unicode, parentheses) uploads cleanly instead of bouncing off the
        server with an error the user has to decode."""
        script = _extract_inline_script()
        m = re.search(r"function _sanitizedUploadName\(rawName\)\{(.*?)\n\}", script, re.S)
        assert m, "_sanitizedUploadName not found"
        assert "[^A-Za-z0-9._-]" in m.group(1)

    def test_functionally_uploads_and_inserts_a_real_markdown_link(self, tmp_path):
        """Real execution: drive the actual extracted uploadAndInsert
        against a fake fetch and a fake composer, and confirm the right
        bytes go to the right URL and the right markdown comes back."""
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        script = _extract_inline_script()
        sanitize_m = re.search(r"(function _sanitizedUploadName\(rawName\)\{.*?\n\})", script, re.S)
        upload_m = re.search(r"(async function uploadAndInsert\(file\)\{.*?\n\})", script, re.S)
        assert sanitize_m and upload_m
        harness = """
const _IMAGE_EXT_RE = /\\.(png|jpe?g|gif|webp|svg|bmp)$/i;
""" + sanitize_m.group(1) + "\n" + upload_m.group(1) + """
const events = [];
const store = { ta: { value: "", dispatchEvent: (e)=>events.push(e.type), focus: ()=>{} } };
function $(id){ return store[id]; }
let sentUrl = null, sentBody = null;
async function fetch(url, opts){
  sentUrl = url; sentBody = opts.body;
  // A real server echoes back exactly the (already-sanitized) name it
  // received in the query string -- webui.py's _handle_upload returns
  // {"ok": true, "name": <the name it parsed>, ...}.
  return { ok: true, json: async () => ({ok: true, name: "my-photo.PNG"}) };
}
uploadAndInsert({name: "my photo.PNG", size: 3}).then(() => {
  const ok = sentUrl === "/api/upload?name=my-photo.PNG"
    && sentBody && sentBody.size === 3
    && store.ta.value.includes("![my-photo.PNG](/uploads/my-photo.PNG)");
  console.log(JSON.stringify({ok, url: sentUrl, value: store.ta.value}));
  process.exit(ok ? 0 : 1);
});
"""
        js_file = tmp_path / "upload_harness.js"
        js_file.write_text(harness, encoding="utf-8")
        result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"upload-and-insert did not behave correctly:\n{result.stdout}\n{result.stderr}"
