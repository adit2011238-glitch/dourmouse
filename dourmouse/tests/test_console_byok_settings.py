"""ui/console.html — the BYOK (bring your own key) Settings form
(v14, user-directed, 2026-09-12): "commercial, for other people to
use." Each install is now a stranger's own machine, not this
developer's — a shared Ollama/Gemini key would bill and rate-limit
every install against the same account. This is the real Settings UI
for it, replacing "open .env in a text editor" with an actual form.
See dourmouse/webui.py's TestByokApiKeysEndpoint for the backend half.

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


class TestByokFormPresent:
    def test_the_container_exists_in_settings_markup(self):
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        assert 'id="byokRows"' in html
        assert "API KEYS" in html

    def test_both_real_providers_are_listed(self):
        script = _extract_inline_script()
        idx = script.index("const KEYS = [")
        block = script[idx: idx + 400]
        assert '"OLLAMA_API_KEY"' in block
        assert '"GEMINI_API_KEY"' in block


class TestByokNeverPrefillsTheRealValue:
    """The one real security property that matters here: the input
    must start empty even when a key IS already configured — GET
    /api/settings/api-keys only ever reports configured true/false,
    never the value, and the input must never fabricate one either."""

    def test_the_input_value_is_never_set_from_state(self):
        script = _extract_inline_script()
        idx = script.index("const KEYS = [")
        block = script[idx: idx + 1600]
        # A real prefill bug would look like value="${...}" on the
        # input; only the placeholder may vary by configured state.
        assert 'value="' not in block or "input" not in block[block.index('value="') - 20: block.index('value="')]


class TestByokSaveAndClear:
    def test_save_posts_to_the_real_endpoint(self):
        script = _extract_inline_script()
        idx = script.index("saveBtn.onclick")
        block = script[max(0, idx - 900): idx + 200]
        assert '"/api/settings/api-keys"' in block
        assert 'method: "POST"' in block

    def test_save_refuses_an_empty_paste_without_hitting_the_network(self):
        script = _extract_inline_script()
        idx = script.index("saveBtn.onclick")
        block = script[idx: idx + 300]
        assert "Paste a real key first" in block

    def test_clear_button_only_renders_when_a_key_is_already_configured(self):
        script = _extract_inline_script()
        idx = script.index("const KEYS = [")
        block = script[idx: idx + 1600]
        assert 'on ? \'<button class="mini byokClear"' in block

    def test_clear_posts_an_empty_value(self):
        script = _extract_inline_script()
        idx = script.index("if (clearBtn)")
        block = script[idx: idx + 200]
        assert 'post("")' in block

    def test_input_cleared_after_a_successful_save(self):
        """A saved secret must not linger visibly in the input field."""
        script = _extract_inline_script()
        idx = script.index("const post = async")
        block = script[idx: idx + 700]
        assert 'input.value = "";' in block
