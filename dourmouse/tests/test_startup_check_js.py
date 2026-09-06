"""Real, scoped checks for ui/assets/startup_check.js — backlog #6's
startup animation + claude/codex/Google sign-in popup. Structural checks
only (this is a browser script, not importable Python) — the injection
itself is covered by TestStartupCheckInjection in test_webui.py.
"""

from __future__ import annotations

from pathlib import Path


def _js_path() -> Path:
    return Path(__file__).resolve().parents[2] / "ui" / "assets" / "startup_check.js"


def _js() -> str:
    return _js_path().read_text(encoding="utf-8")


class TestStartupCheckJs:
    def test_file_exists(self):
        assert _js_path().is_file()

    def test_reads_the_real_connections_endpoint_for_claude_and_codex(self):
        js = _js()
        assert "/api/connections" in js
        assert "conns.claude" in js
        assert "conns.codex" in js

    def test_reads_the_real_google_auth_status_endpoint(self):
        js = _js()
        assert "/api/auth/status" in js
        assert "auth.me" in js

    def test_gives_the_exact_real_copy_paste_commands(self):
        """Must match connections.py's own real hint text, not a guess —
        "claude" (running it once prompts /login) and the real codex
        install+login one-liner."""
        js = _js()
        assert '"claude"' in js
        assert "codex login" in js

    def test_google_uses_a_link_not_a_terminal_command(self):
        """Google sign-in is a browser OAuth flow, not a pasteable
        command — must not tell the user to paste something that can't
        actually complete a browser consent screen."""
        js = _js()
        assert "/api/auth/google/start" in js

    def test_no_banned_ai_slop_patterns(self):
        js = _js().lower()
        assert "purple" not in js
        assert "violet" not in js
