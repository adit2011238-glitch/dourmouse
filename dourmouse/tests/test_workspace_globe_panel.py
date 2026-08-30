"""ui/workspace.html — the 3D VIEW panel (God's Eye View), added so
"vision" in DourMouse includes real 3D model rendering the companion/voice
layer can actually pull open, not a token viewer bolted on separately.

Reuses console.html's own already-proven pattern for this exact embed
(no-cors reachability probe -> live iframe or an honest "not running"
instructions card) rather than inventing a second one — see
ui/console.html's paintGlobe() for the original.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_HTML = _PROJECT_ROOT / "ui" / "workspace.html"


def _script() -> str:
    html = _WORKSPACE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "ui/workspace.html has no inline <script>...</script> block"
    return m.group(1)


class TestGlobeDockButtonWired:
    def test_dock_button_present(self):
        html = _WORKSPACE_HTML.read_text(encoding="utf-8")
        assert '<button class="dockbtn" type="button" data-open="globe">+ 3D VIEW</button>' in html

    def test_open_panel_routes_globe_to_the_real_loader(self):
        script = _script()
        m = re.search(r"function openPanel\(type\)\{(.*?)\n\}\n", script, re.S)
        assert m, "openPanel() not found"
        body = m.group(1)
        assert 'globe: "3D VIEW' in body
        assert 'else if(type === "globe") loadGlobePanel(p);' in body


class TestGlobePanelIsRealNotDecorative:
    def test_loader_checks_reachability_before_claiming_live(self):
        script = _script()
        m = re.search(r"async function loadGlobePanel\(p\)\{(.*?)\n\}\n", script, re.S)
        assert m, "loadGlobePanel() not found"
        body = m.group(1)
        # Same honest pattern as console.html's paintGlobe(): a real fetch
        # decides live-iframe vs a genuine "isn't running" card -- never a
        # hard-coded iframe with no reachability check at all.
        assert 'mode: "no-cors"' in body
        assert "reachable = true" in body
        assert "reachable = false" in body
        assert "<iframe" in body
        assert "isn't running" in body

    def test_globe_url_matches_the_real_vendored_dev_server(self):
        script = _script()
        assert 'const GODS_EYE_URL = "http://localhost:4173/"' in script

    def test_recheck_button_actually_retries(self):
        script = _script()
        m = re.search(r"async function loadGlobePanel\(p\)\{(.*?)\n\}\n", script, re.S)
        body = m.group(1)
        assert "loadGlobePanel(p)" in body  # the recheck button's own handler


class TestGlobeVoiceCommand:
    """dourmouse/voice_commands.py's own aliases are the real grammar test
    (test_voice_commands.py::TestResolvePanel::test_globe_panel_aliases) --
    this only confirms the two sides actually agree on the panel id."""

    def test_panel_id_matches_voice_commands_grammar(self):
        import dourmouse.voice_commands as vc

        assert vc.resolve_panel("globe") == "globe"
        script = _script()
        assert 'globe: "3D VIEW' in script  # same id, both sides
