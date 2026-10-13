"""FIX-P R5-6 and R5-7: sensitive text read in a request is not forwarded to another company, in the app and in
the MCP bridge (where the Claude or Codex CLI is the brain)."""

from __future__ import annotations

import io
from typing import Any

import pytest

from dourmouse import code_backends, general_roster, model_delegation, privacy
from dourmouse.dispatch import ToolSpec
from dourmouse.mcp_bridge import McpBridgeServer


def _turn(backend: str, sources: set[str]) -> privacy.TurnPrivacy:
    return privacy.TurnPrivacy(chat="tab-fw", private=False, placeholders=privacy.PlaceholderMap(),
                               backend=backend, sources=set(sources))


def test_forward_refusal_rules():
    assert privacy.forward_refusal("claude_cli") is None  # no turn
    with privacy.use_turn(_turn("ollama_cloud", set())):
        assert privacy.forward_refusal("claude_cli") is None  # nothing sensitive read yet
    with privacy.use_turn(_turn("ollama_cloud", {"mail"})):
        assert privacy.forward_refusal("ollama_cloud") is None  # the request's own backend
        text = privacy.forward_refusal("claude_cli")
        assert text and text.startswith("REFUSED") and "Mail" in text


@pytest.mark.parametrize("name,target", [("claude", "claude_cli"), ("codex", "codex_cli")])
def test_code_tools_refuse_to_forward_after_a_sensitive_read(monkeypatch, name, target):
    called: list[str] = []
    monkeypatch.setattr(code_backends, "_run_claude", lambda *a, **k: called.append("c") or "x")
    monkeypatch.setattr(code_backends, "_run_codex", lambda *a, **k: called.append("x") or "x")
    with privacy.use_turn(_turn("ollama_cloud", {"mail"})), pytest.raises(RuntimeError, match="REFUSED"):
        code_backends.run_code_task(name, "summarise the merger mail")
    assert called == []
    with privacy.use_turn(_turn(target, {"mail"})):  # the CLI itself is the request's backend: allowed
        code_backends.run_code_task(name, "continue")
    assert called


def test_other_company_openai_compatible_backend_is_refused_too(monkeypatch):
    monkeypatch.setattr(code_backends, "load_backend", lambda n: ("https://integrate.api.nvidia.com/v1", "k", "m"))
    monkeypatch.setattr(code_backends, "_run_openai_compat", lambda *a, **k: "sent")
    with privacy.use_turn(_turn("ollama_cloud", {"documents"})), pytest.raises(RuntimeError, match="REFUSED"):
        code_backends.run_code_task("nvidia", "quote the contract")
    with privacy.use_turn(_turn("nvidia", {"documents"})):
        assert code_backends.run_code_task("nvidia", "quote the contract") == "sent"


def test_claude_code_and_codex_code_tools_refuse(monkeypatch):
    monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: pytest.fail("must not look for the CLI"))
    with privacy.use_turn(_turn("ollama_cloud", {"memory"})):
        assert general_roster._claude_code_tool({"task": "t"}).startswith("REFUSED")
        assert general_roster._codex_code_tool({"task": "t"}).startswith("REFUSED")


def _bridge(tools: list[ToolSpec]) -> McpBridgeServer:
    return McpBridgeServer(tools=tools, stdin=io.StringIO(""), stdout=io.StringIO(), stderr=io.StringIO())


def test_bridge_delegation_is_refused_once_the_cli_has_read_a_source_and_in_private_mode(monkeypatch):
    ran: list[Any] = []

    def run_roster_dispatch(*a, **k):
        ran.append(1)
        return {"final_text": "x"}

    monkeypatch.setattr("dourmouse.dispatch.run_dispatch_messages", run_roster_dispatch)
    def handler(args: dict[str, Any]) -> str:
        res = model_delegation._run_roster(model_delegation.DelegationTask(prompt="summarise", agent="mail"), 5)
        return res.error or res.text

    server = _bridge([ToolSpec(name="delegate_to_models", description="d",
                               parameters={"type": "object", "properties": {}}, handler=handler)])
    privacy.PrivacyStore().set_consent("mail", "allow")
    # the bridge's chat already read Mail (carried in the handoff file)
    with privacy.chat_scope("tab-br"):
        env, path = privacy.bridge_handoff("claude_cli")
    import json
    data = json.loads(path.read_text())
    data["sources"] = ["mail"]
    path.write_text(json.dumps(data))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    out = server._handle_tools_call({"name": "delegate_to_models", "arguments": {}})["content"][0]["text"]
    assert "REFUSED" in out and ran == []


def test_bridge_installs_the_chats_turn_around_each_call(monkeypatch):
    seen: dict[str, Any] = {}

    def handler(args: dict[str, Any]) -> str:
        turn = privacy.current_turn()
        assert turn is not None
        seen["private"], seen["chat"], seen["backend"] = turn.private, turn.chat, turn.backend
        seen["scope_chat"] = privacy.current_chat()
        return "ok"

    server = _bridge([ToolSpec(name="web_search", description="d",
                               parameters={"type": "object", "properties": {}}, handler=handler)])
    privacy.PrivacyStore().set_chat_private("tab-br2", True)
    with privacy.chat_scope("tab-br2"):
        env, _ = privacy.bridge_handoff("codex_cli")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    server._handle_tools_call({"name": "web_search", "arguments": {}})
    assert seen == {"private": True, "chat": "tab-br2", "backend": "codex_cli", "scope_chat": "tab-br2"}
    assert privacy.current_turn() is None  # not left installed
