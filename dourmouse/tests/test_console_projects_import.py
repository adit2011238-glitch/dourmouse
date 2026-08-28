"""ui/console.html — PROJECTS bookshelf, IMPORTED (Claude Code + Codex CLI)
source (world-monitor-expansion).

``GET /api/projects/imported`` (dourmouse/project_import.py, tested
separately in test_project_import.py) is a real, already-tested backend
endpoint. This file covers the frontend-only work of wiring it into the
existing PROJECTS bookshelf in ui/console.html: real syntax on the
extracted inline script (Rule 2.1 — no fabricated pass), and that the
real behavior this task called for is actually present in the source —
fetching the endpoint, attributing each card to its source tool(s), an
honest "path no longer exists" treatment, and honest text for the
endpoint's own degradation states — rather than a silent empty shelf.

No headless browser here (none is available in this suite); DOM behavior
that would need one is out of scope for this file, matching how the rest
of ui/console.html's frontend logic is tested elsewhere in this repo
(test_ui_local.py checks index.html/map.html/agent.html/login.html the
same way — source-level, not execution-level).
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
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


class TestConsoleScriptSyntax:
    """Real syntax check, not just eyeballing the diff."""

    def test_node_check_passes(self, tmp_path):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        script = _extract_inline_script()
        js_file = tmp_path / "console_extracted.js"
        js_file.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(js_file)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"node --check failed on the extracted console.html script:\n"
            f"{result.stdout}\n{result.stderr}"
        )


class TestImportedProjectsShelf:
    """The real markers a genuine wiring of GET /api/projects/imported
    into the PROJECTS bookshelf must have — not a mockup."""

    def test_fetches_the_real_endpoint(self):
        script = _extract_inline_script()
        assert '"/api/projects/imported"' in script

    def test_wired_into_projects_screen_load(self):
        script = _extract_inline_script()
        # show("PROJECTS") already calls paintProjects(); paintProjects()
        # must call the new imported-shelf painter so it loads on every
        # real visit to the screen, not just once.
        assert 'if(name==="PROJECTS") paintProjects();' in script
        assert "paintImportedShelf(box)" in script
        assert "function paintImportedShelf(" in script
        assert "function loadImportedProjects(" in script

    def test_has_a_real_refresh_action(self):
        script = _extract_inline_script()
        assert 'id="importedRefreshBtn"' in script
        assert "importedRefreshBtn" in script and "onclick=loadImportedProjects" in script

    def test_attributes_each_card_to_its_source(self):
        script = _extract_inline_script()
        assert "VIA CLAUDE CODE" in script
        assert "VIA CODEX" in script
        assert "VIA BOTH" in script
        assert "function importedSourceLabel(" in script

    def test_honest_gone_path_treatment_not_identical_to_live(self):
        script = _extract_inline_script()
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        # exists:false must both look different (a CSS hook + a visible
        # flag) and read differently, not just render the same card.
        assert "p.exists===false" in script or "p.exists === false" in script
        assert '"book imported" + (gone ? " gone" : "")' in script
        assert "PATH GONE" in script
        assert ".book.gone" in html  # a real CSS rule (in <style>), not just a class with no effect

    def test_honest_degraded_states_not_a_silent_empty_shelf(self):
        script = _extract_inline_script()
        # Neither source configured.
        assert "!cc.configured && !cx.configured" in script
        assert "nothing to import yet" in script
        # Configured but zero projects found.
        assert "not found on this machine" in script

    def test_does_not_replace_the_manual_shelf(self):
        script = _extract_inline_script()
        # The existing localStorage-backed shelf logic (PROJECTS_KEY,
        # projectAdd/projectRemoveItem/projectDelete, the SHELVE picker)
        # must still be fully intact — this feature is additive only.
        for marker in ("PROJECTS_KEY", "function projectAdd(",
                       "function projectRemoveItem(", "function projectDelete(",
                       "function shelvePicker("):
            assert marker in script, f"manual bookshelf logic missing: {marker}"

    def test_session_count_and_last_active_rendered(self):
        script = _extract_inline_script()
        assert "function importedRelTime(" in script
        assert "p.stat" in script
        assert 'card.querySelector(".bookwhen").textContent = importedRelTime(p.last_active)' in script
