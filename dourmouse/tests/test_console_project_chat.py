"""ui/console.html — Projects open their own real chat (world-monitor-
expansion, "make projects open their own chat like Claude Desktop's
Projects so work can actually take place").

Before this: project cards were purely informational (project_bookkeeper.
open_project's own docstring explicitly noted no client ever routed into a
dedicated conversation). Fix reuses the EXISTING chat screen entirely — no
new UI surface — by overriding the per-tab session key (backlog #7's own
tabId()) with a deterministic per-project one whenever a project is
active. See test_webui.py's TestProjectScopedSessions for the backend
half (the real system-message seeding).

No headless browser here (none available in this suite, matching
test_console_stop_declines_confirmation.py's own stated convention) —
source-level coverage that the real wiring is present and correct.
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


class TestActiveProjectOverridesTabId:
    def test_tabid_checks_active_project_first(self):
        script = _extract_inline_script()
        m = re.search(r"function tabId\(\)\{(.*?)\n\}", script, re.S)
        assert m, "tabId() body not found"
        body = m.group(1)
        assert "activeProject()" in body
        assert "project.tab_id" in body

    def test_active_project_uses_sessionstorage_not_localstorage(self):
        """Genuinely per-BROWSER-TAB, same reasoning as tabId()'s own
        storage choice — opening the same project in two tabs must never
        collide."""
        script = _extract_inline_script()
        m = re.search(r"function activeProject\(\)\{(.*?)\n\}", script, re.S)
        assert m
        assert "sessionStorage" in m.group(1)


class TestOpenProjectChat:
    def test_open_project_chat_calls_the_real_api(self):
        script = _extract_inline_script()
        assert "/api/projects/open" in script
        m = re.search(r"async function openProjectChat\(project\)\{(.*?)\n\}", script, re.S)
        assert m, "openProjectChat(project) not found"
        body = m.group(1)
        assert "setActiveProject(" in body
        assert 'show("HOME")' in body

    def test_project_card_click_opens_its_chat(self):
        """The card itself IS the real 'Open' action — there was none
        before this (bookkeeperCard only ever wired the ✕ remove
        button)."""
        script = _extract_inline_script()
        m = re.search(r"function bookkeeperCard\(p\)\{(.*?)\n  return card;", script, re.S)
        assert m, "bookkeeperCard(p) not found"
        body = m.group(1)
        assert "card.onclick = () => openProjectChat(p);" in body

    def test_delete_button_still_stops_propagation_to_the_new_card_click(self):
        """The ✕ button must keep working exactly as before — removing a
        project must never also open its chat."""
        script = _extract_inline_script()
        assert 'card.querySelector(".bookx").onclick=(e)=>{ e.stopPropagation();' in script


class TestLeavingAProject:
    def test_clicking_home_again_while_a_project_is_active_leaves_it(self):
        script = _extract_inline_script()
        m = re.search(r"function show\(name\)\{(.*?)// v8\.28:", script, re.S)
        assert m, "the leave-project check at the top of show() not found"
        body = m.group(1)
        assert 'name === "HOME"' in body
        assert "activeProject()" in body
        assert "leaveActiveProject();" in body

    def test_leave_clears_the_stored_project(self):
        script = _extract_inline_script()
        m = re.search(r"function leaveActiveProject\(\)\{(.*?)\n\}", script, re.S)
        assert m
        assert "sessionStorage.removeItem(ACTIVE_PROJECT_KEY)" in m.group(1)


class TestProjectChromeSync:
    """The composer placeholder + document title are the one honest,
    always-visible 'where am I' signal — no new UI element, same existing
    textbox and browser tab, same idea as an IDE reflecting its open
    project in its own window title."""

    def test_placeholder_reflects_the_active_project(self):
        script = _extract_inline_script()
        m = re.search(r"function syncProjectChrome\(\)\{(.*?)\n\}", script, re.S)
        assert m, "syncProjectChrome() not found"
        body = m.group(1)
        assert "ta.placeholder" in body
        assert "document.title" in body

    def test_synced_on_boot_so_a_reload_keeps_the_project_visible(self):
        script = _extract_inline_script()
        assert "syncProjectChrome();" in script
        # Called at least twice: once inside openProjectChat/leave, once
        # unconditionally at boot (a reload in the same tab must not
        # silently drop back to looking like no project is active).
        assert script.count("syncProjectChrome()") >= 3
