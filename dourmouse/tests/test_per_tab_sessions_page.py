"""Structural checks for the per-tab session frontend (backlog #7):
console.html's tabId() helper and every real /api/chat + /api/confirm
call site actually sending it. Backend behavior is covered by
TestPerTabSessions in test_webui.py.
"""

from __future__ import annotations

import re
from pathlib import Path


def _console_html() -> str:
    return (Path(__file__).resolve().parents[2] / "ui" / "console.html").read_text(encoding="utf-8")


class TestTabIdHelper:
    def test_function_exists(self):
        html = _console_html()
        assert "function tabId()" in html

    def test_uses_sessionstorage_not_localstorage(self):
        """The real point: sessionStorage is genuinely per-TAB, unlike
        localStorage which is shared across every tab of the origin."""
        html = _console_html()
        m = re.search(r"function tabId\(\)\{(.*?)\n\}", html, re.S)
        assert m, "tabId() body not found"
        body = m.group(1)
        assert "sessionStorage" in body
        assert "localStorage" not in body

    def test_degrades_honestly_when_storage_is_blocked(self):
        html = _console_html()
        m = re.search(r"function tabId\(\)\{(.*?)\n\}", html, re.S)
        assert m
        assert "catch{ return \"\"; }" in m.group(1)


class TestEveryChatAndConfirmCallSendsTabId:
    def test_every_api_chat_call_sends_tab_id(self):
        html = _console_html()
        calls = re.findall(r'fetch\("/api/chat".{0,400}', html, re.S)
        assert calls, "no /api/chat call sites found at all"
        for call in calls:
            body_match = re.search(r"body:JSON\.stringify\(\{.*?\}\)", call, re.S)
            assert body_match, f"no JSON body found in call: {call[:120]}"
            assert "tab_id:tabId()" in body_match.group(0), call[:160]

    def test_every_api_confirm_call_sends_tab_id(self):
        html = _console_html()
        calls = re.findall(r'fetch\("/api/confirm".{0,200}', html, re.S)
        assert calls, "no /api/confirm call sites found at all"
        for call in calls:
            assert "tab_id:tabId()" in call, call[:160]
