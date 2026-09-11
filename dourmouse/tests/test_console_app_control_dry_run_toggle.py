"""ui/console.html — Settings UI toggle for App Control Dry Run (v14,
user-directed, 2026-09-08): "Consider adding a 'dry run' mode where it
shows what would be clicked without actually clicking."

Same chip-pair pattern as GROUNDED MODE / CLAUDE FRONT MODE already in
Settings — see test_webui.py's TestAppControlDryRunEndpoint for the
backend half.

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


class TestAppControlDryRunToggle:
    def test_toolbar_row_present_in_settings_markup(self):
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        assert 'id="appDryRunRow"' in html
        assert 'id="appDryRunNote"' in html
        assert "APP CONTROL DRY RUN" in html

    def test_fetches_the_real_endpoint(self):
        script = _extract_inline_script()
        assert '"/api/settings/app-control-dry-run"' in script

    def test_posts_the_chosen_value_on_click(self):
        script = _extract_inline_script()
        # Find the IIFE that owns appDryRunRow specifically, not the
        # Claude Front Mode one right above it in the file.
        idx = script.index('$("appDryRunRow")')
        block = script[idx: idx + 2000]
        assert 'method: "POST"' in block
        assert "JSON.stringify({enabled: val})" in block

    def test_off_is_the_default_reported_state_when_unconfigured(self):
        """The note text for the OFF branch must not claim anything is
        being blocked when it isn't — matches the real opt-in/OFF-by-
        default polarity (config.app_control_dry_run_enabled)."""
        script = _extract_inline_script()
        idx = script.index('$("appDryRunRow")')
        block = script[idx: idx + 2000]
        assert "app-control actions run for real" in block
