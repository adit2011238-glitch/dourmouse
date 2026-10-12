"""PRIV: the Claude CLI path. With Claude front mode, Claude calls Dourmouse's tools through the MCP
bridge (its own process) and the results go straight to Anthropic, never through the dispatch loop.
The bridge applies the same gate; the chat's Private mode and placeholder map reach it through the
CLI's environment. Also H-PRIV-1 (the model call worker lost the caller's context)."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from dourmouse import code_backends, privacy
from dourmouse.dispatch import ToolSpec
from dourmouse.mcp_bridge import McpBridgeServer

MAILBOX = "From: alice@example.org\nThe merger closes Friday. Card 4111 1111 1111 1111."


def _bridge(sent: list[dict[str, Any]]) -> McpBridgeServer:
    def send(a: dict[str, Any]) -> str:
        sent.append(a)
        return "SENT"

    tools = [
        ToolSpec(name="gmail_read", description="read", parameters={"type": "object", "properties": {}},
                 handler=lambda a: MAILBOX),
        ToolSpec(name="gmail_send", description="send",
                 parameters={"type": "object", "properties": {"to": {"type": "string"}}},
                 handler=send),
        ToolSpec(name="web_search", description="search", parameters={"type": "object", "properties": {}},
                 handler=lambda a: "public page"),
    ]
    return McpBridgeServer(tools=tools, stdin=io.StringIO(""), stdout=io.StringIO(), stderr=io.StringIO())


def _call(server: McpBridgeServer, name: str, args: dict[str, Any] | None = None) -> str:
    out = server._handle_tools_call({"name": name, "arguments": args or {}})
    return out["content"][0]["text"]


def _env(monkeypatch, chat: str = "tab-b", private: bool = False) -> tuple[dict[str, str], Path]:
    with privacy.chat_scope(chat):
        if private:
            privacy.PrivacyStore().set_chat_private(chat, True)
        env, path = privacy.bridge_handoff()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env, path


def test_bridge_refuses_t2_until_consent_and_never_asks(monkeypatch):
    _env(monkeypatch)
    text = _call(_bridge([]), "gmail_read")
    assert text.startswith("NOT SENT: the owner has not allowed Mail")
    assert "merger" not in text


def test_bridge_swaps_identifiers_and_shares_the_chat_map(monkeypatch):
    privacy.PrivacyStore().set_consent("mail", "allow")
    with privacy.chat_scope("tab-b"):
        # The server already minted [EMAIL_1] for someone else in this chat.
        privacy.placeholder_map("tab-b").placeholder("EMAIL", "bob@example.org")
    _, path = _env(monkeypatch)
    sent: list[dict[str, Any]] = []
    server = _bridge(sent)
    text = _call(server, "gmail_read")
    assert "alice@example.org" not in text and "4111 1111 1111 1111" not in text
    assert "[EMAIL_2]" in text  # continues the chat's numbering, no collision with bob
    # Claude writes the placeholder back: the bridge sends to the real address.
    _call(server, "gmail_send", {"to": "[EMAIL_2]"})
    assert sent == [{"to": "alice@example.org"}]
    # The server reads the bridge's additions back and can show the owner the real value.
    with privacy.chat_scope("tab-b"):
        privacy.bridge_collect(path)
        assert privacy.placeholder_map("tab-b").restore_text("[EMAIL_2] and [EMAIL_1]") == (
            "alice@example.org and bob@example.org")
    assert not path.exists()


def test_bridge_private_mode_hides_t2_tools_and_refuses_them(monkeypatch):
    privacy.PrivacyStore().set_consent("mail", "allow")
    _env(monkeypatch, chat="tab-p", private=True)
    server = _bridge([])
    names = {t["name"] for t in server._handle_tools_list()["tools"]}
    assert "gmail_read" not in names and "web_search" in names
    assert _call(server, "gmail_read").startswith("NOT SENT: Private mode is on")


def test_bridge_without_a_map_cannot_collide(monkeypatch):
    for k in (privacy.BRIDGE_CHAT_ENV, privacy.BRIDGE_PRIVATE_ENV, privacy.BRIDGE_MAP_ENV):
        monkeypatch.delenv(k, raising=False)
    privacy.PrivacyStore().set_consent("mail", "allow")
    text = _call(_bridge([]), "gmail_read")
    assert "[EMAIL_9001]" in text


def test_bridge_refuses_t3(monkeypatch):
    _env(monkeypatch)
    tools = [ToolSpec(name="read_path", description="r", parameters={"type": "object", "properties": {}},
                      handler=lambda a: "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----")]
    server = McpBridgeServer(tools=tools, stdin=io.StringIO(""), stdout=io.StringIO(), stderr=io.StringIO())
    assert _call(server, "read_path", {"path": "/tmp/k"}).startswith("NOT SENT: this result holds a private key")


def test_claude_cli_call_hands_over_the_chat_and_records_egress(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run(argv, **kw):
        env = kw["env"]
        seen["env"] = {k: env.get(k) for k in (privacy.BRIDGE_CHAT_ENV, privacy.BRIDGE_PRIVATE_ENV,
                                               privacy.BRIDGE_MAP_ENV)}
        seen["map_mode"] = oct(os.stat(env[privacy.BRIDGE_MAP_ENV]).st_mode & 0o777)
        # The bridge adds a value during the call.
        data = json.loads(Path(env[privacy.BRIDGE_MAP_ENV]).read_text())
        data["values"].append(["[EMAIL_1]", "EMAIL", "carol@example.org"])
        data["next"]["EMAIL"] = 1
        Path(env[privacy.BRIDGE_MAP_ENV]).write_text(json.dumps(data))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(code_backends, "_claude_tool_args", lambda: (["--mcp-config", "x"], True))
    monkeypatch.setattr(code_backends.subprocess, "run", fake_run)
    with privacy.chat_scope("tab-cli"), privacy.turn_scope() as turn:
        code_backends._run_claude_once("claude", "summarise my day", [], cwd=None, timeout=5)
        assert turn.placeholders.restore_text("[EMAIL_1]") == "carol@example.org"
    assert seen["env"][privacy.BRIDGE_CHAT_ENV] == "tab-cli"
    assert seen["env"][privacy.BRIDGE_PRIVATE_ENV] == "0"
    assert seen["map_mode"] == "0o600"
    assert not Path(seen["env"][privacy.BRIDGE_MAP_ENV]).exists()
    row = privacy.read_egress(1)[0]
    assert row["backend"] == "claude_cli" and "summarise" not in json.dumps(row)


def test_model_call_worker_keeps_the_callers_context():
    """H-PRIV-1: _call_with_retry ran the model call in a worker thread started
    without the caller's context, so a context variable (the privacy turn, the
    project scope) was empty inside the client."""
    from dourmouse import dispatch

    seen: list[Any] = []

    class _Client:
        def __init__(self) -> None:
            self.chat = type("C", (), {"completions": self})()

        def create(self, **kw: Any) -> Any:
            seen.append(privacy.current_turn())
            msg = type("M", (), {"content": "ok", "tool_calls": None})()
            return type("R", (), {"choices": [type("Ch", (), {"message": msg})()]})()

    with privacy.turn_scope("tab-ctx") as turn:
        dispatch._call_with_retry(_Client(), model="m", messages=[], tools=[], config=None)
    assert seen == [turn]
