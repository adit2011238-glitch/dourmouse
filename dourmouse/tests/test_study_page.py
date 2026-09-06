"""Real, scoped checks for ui/study.html — the Study tab (backlog #9).
Route/endpoint coverage lives in test_webui.py::TestStudyTab; this covers
the page's own structure.
"""

from __future__ import annotations

from pathlib import Path


def _study_html_path() -> Path:
    return Path(__file__).resolve().parents[2] / "ui" / "study.html"


def _html() -> str:
    return _study_html_path().read_text(encoding="utf-8")


class TestStudyPage:
    def test_file_exists(self):
        assert _study_html_path().is_file()

    def test_links_the_shared_design_system_css(self):
        assert 'href="assets/dourmouse-ui.css"' in _html()

    def test_no_banned_ai_slop_patterns(self):
        html = _html().lower()
        assert "border-radius: 999" not in html
        assert "purple" not in html
        assert "violet" not in html
        assert "linear-gradient" not in html

    def test_forces_the_real_study_subagent(self):
        assert "focus_agent: 'study'" in _html()

    def test_polls_the_real_folder_status_endpoint(self):
        assert "/api/study/status" in _html()

    def test_has_focus_visible_styling(self):
        html = _html()
        assert "input[type=text]:focus-visible" in html
        assert "button:focus-visible" in html

    def test_escapes_content_before_dom_insertion(self):
        html = _html()
        assert "function esc(" in html
        assert "esc(t.text)" in html
