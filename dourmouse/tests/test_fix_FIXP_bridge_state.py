"""FIX-P R5-9 (Claude session) and R5-11a: what the MCP bridge served is carried back into the chat's state."""

from __future__ import annotations

import io
from typing import Any

from dourmouse import code_backends, privacy
from dourmouse.dispatch import ToolSpec
from dourmouse.mcp_bridge import McpBridgeServer


def _bridge() -> McpBridgeServer:
    tools = [ToolSpec(name="gmail_read", description="read", parameters={"type": "object", "properties": {}},
                      handler=lambda a: "From: alice@example.org\nThe merger closes Friday.")]
    return McpBridgeServer(tools=tools, stdin=io.StringIO(""), stdout=io.StringIO(), stderr=io.StringIO())


def _serve(monkeypatch, chat: str) -> Any:
    privacy.PrivacyStore().set_consent("mail", "allow")
    with privacy.chat_scope(chat):
        env, path = privacy.bridge_handoff()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _bridge()._handle_tools_call({"name": "gmail_read", "arguments": {}})
    _bridge()._handle_tools_call({"name": "gmail_read", "arguments": {}})
    return path


def test_bridge_tiers_sources_and_bytes_come_back_and_get_a_ledger_row(monkeypatch):
    path = _serve(monkeypatch, "tab-bs1")
    with privacy.chat_scope("tab-bs1"):
        privacy.bridge_collect(path)
    rows = [r for r in privacy.read_egress(10) if r["origin"] == "claude_cli_bridge_results"]
    assert rows and rows[0]["sources"] == ["mail"] and rows[0]["tiers"]["T2"] == 2 and rows[0]["bytes"] > 0
    assert "alice" not in str(rows[0]) and "merger" not in str(rows[0])


def test_claude_session_restarts_when_chat_turns_private_after_reading_mail(monkeypatch):
    path = _serve(monkeypatch, "tab-bs2")
    with privacy.chat_scope("tab-bs2"):
        privacy.bridge_collect(path)
        first = code_backends._claude_session_args("k-bs2")
        assert first[0] == "--session-id"
        again = code_backends._claude_session_args("k-bs2")
        assert again[0] == "--resume"  # nothing changed: the session continues
        privacy.PrivacyStore().set_chat_private("tab-bs2", True)
        restarted = code_backends._claude_session_args("k-bs2")
        assert restarted[0] == "--session-id" and restarted[1] != first[1]
        # the restart is counted once: the new, empty session continues normally
        assert code_backends._claude_session_args("k-bs2")[0] == "--resume"


def test_claude_session_restarts_when_a_source_it_read_goes_to_never(monkeypatch):
    path = _serve(monkeypatch, "tab-bs3")
    with privacy.chat_scope("tab-bs3"):
        privacy.bridge_collect(path)
        code_backends._claude_session_args("k-bs3")
        privacy.PrivacyStore().set_consent("mail", "deny")
        assert code_backends._claude_session_args("k-bs3")[0] == "--session-id"
