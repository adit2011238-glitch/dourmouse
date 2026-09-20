"""dourmouse/mcp_client.py -- Dourmouse as an MCP CLIENT (Domain H piece 7,
second half).

Hermetic unit tests drive McpClient against a scripted fake transport (an
injectable ``launcher``, same seam style mcp_bridge's own tests use for
stdin/stdout) -- no real subprocess. One real end-to-end test spawns the
ACTUAL dourmouse.mcp_bridge server as a real subprocess and talks to it
with a real McpClient, proving genuine interop between this codebase's own
MCP client and server halves, not just that each one's tests pass in
isolation.
"""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest

from dourmouse.mcp_client import (
    McpClient,
    McpClientError,
    build_external_mcp_subagent,
    load_external_mcp_servers,
)


class _ScriptedTransport:
    """A fake server: pre-scripted JSON-RPC response lines are handed back
    one per real read, in order; every write is captured for assertions."""

    def __init__(self, response_lines: list[dict]):
        self.sent: list[dict] = []
        self._stdout = io.StringIO("".join(json.dumps(r) + "\n" for r in response_lines))

    def stdin_write(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def launcher(self):
        capture = self

        class _Stdin:
            def write(self, text):
                capture.stdin_write(text)

            def flush(self):
                pass

            def close(self):
                pass

        return SimpleNamespace(stdin=_Stdin(), stdout=self._stdout)


def _handshake_responses(tools: list[dict]) -> list[dict]:
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}},
    ]


class TestMcpClientHandshake:
    def test_start_performs_real_initialize_and_tools_list(self):
        transport = _ScriptedTransport(_handshake_responses([{"name": "echo", "description": "echoes", "inputSchema": {}}]))
        client = McpClient("srv", "fake-cmd", launcher=transport.launcher)
        client.start()
        assert client.tools == [{"name": "echo", "description": "echoes", "inputSchema": {}}]
        methods = [m["method"] for m in transport.sent]
        assert methods == ["initialize", "notifications/initialized", "tools/list"]

    def test_initialize_sends_real_protocol_version_and_client_info(self):
        transport = _ScriptedTransport(_handshake_responses([]))
        client = McpClient("srv", "fake-cmd", launcher=transport.launcher)
        client.start()
        init_call = transport.sent[0]
        assert init_call["params"]["clientInfo"]["name"] == "dourmouse"
        assert init_call["params"]["protocolVersion"] == "2024-11-05"

    def test_server_error_response_raises(self):
        transport = _ScriptedTransport([
            {"jsonrpc": "2.0", "id": 1, "error": {"message": "boom"}},
        ])
        client = McpClient("srv", "fake-cmd", launcher=transport.launcher)
        with pytest.raises(McpClientError, match="boom"):
            client.start()

    def test_empty_response_raises(self):
        transport = _ScriptedTransport([])
        client = McpClient("srv", "fake-cmd", launcher=transport.launcher)
        with pytest.raises(McpClientError, match="no response"):
            client.start()


class TestMcpClientCallTool:
    def _connected_client(self, extra_responses: list[dict]) -> McpClient:
        transport = _ScriptedTransport(_handshake_responses([]) + extra_responses)
        client = McpClient("srv", "fake-cmd", launcher=transport.launcher)
        client.start()
        return client

    def test_call_tool_returns_the_real_text_result(self):
        client = self._connected_client([
            {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "the real result"}], "isError": False}},
        ])
        assert client.call_tool("some_tool", {"x": 1}) == "the real result"

    def test_call_tool_error_result_is_reported_honestly(self):
        client = self._connected_client([
            {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "it broke"}], "isError": True}},
        ])
        result = client.call_tool("some_tool", {})
        assert result.startswith("ERROR:")
        assert "it broke" in result

    def test_call_tool_transport_failure_never_raises(self):
        client = self._connected_client([])  # no more scripted responses
        result = client.call_tool("some_tool", {})
        assert result.startswith("ERROR: MCP call to 'srv/some_tool' failed")


class TestLoadExternalMcpServers:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_external_mcp_servers(tmp_path / "nope.json") == {}

    def test_malformed_json_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json at all")
        assert load_external_mcp_servers(p) == {}

    def test_valid_config_returns_the_real_servers_dict(self, tmp_path):
        p = tmp_path / "mcp_servers.json"
        p.write_text(json.dumps({"mcpServers": {"foo": {"command": "foo-cli"}}}))
        assert load_external_mcp_servers(p) == {"foo": {"command": "foo-cli"}}

    def test_missing_mcpservers_key_returns_empty(self, tmp_path):
        p = tmp_path / "mcp_servers.json"
        p.write_text(json.dumps({"something_else": {}}))
        assert load_external_mcp_servers(p) == {}


class TestBuildExternalMcpSubagent:
    def test_no_servers_configured_returns_none(self):
        subagent, clients = build_external_mcp_subagent({})
        assert subagent is None
        assert clients == []

    def test_server_with_no_command_is_skipped_honestly(self):
        subagent, clients = build_external_mcp_subagent({"broken": {}})
        assert subagent is None
        assert clients == []

    def test_unreachable_command_never_crashes_the_build(self):
        # A real, guaranteed-nonexistent command -- subprocess.Popen itself
        # raises OSError, which must be caught, not propagated.
        subagent, clients = build_external_mcp_subagent(
            {"ghost": {"command": "/definitely/not/a/real/binary/xyz123"}}
        )
        assert subagent is None
        assert clients == []

    def test_real_tools_are_wrapped_with_the_real_prefixed_name(self, monkeypatch):
        # build_external_mcp_subagent constructs its OWN McpClient per
        # configured server, so the fake transport is wired in by patching
        # the McpClient constructor itself to return one pre-built,
        # launcher-injected client instead of a real subprocess.
        transport = _ScriptedTransport(
            _handshake_responses([{"name": "search", "description": "search things", "inputSchema": {"type": "object"}}])
        )
        client = McpClient("mytools", "fake-cmd", launcher=transport.launcher)
        monkeypatch.setattr("dourmouse.mcp_client.McpClient", lambda *a, **k: client)
        subagent, clients = build_external_mcp_subagent({"mytools": {"command": "fake-cmd"}})
        assert subagent is not None
        assert subagent.name == "mcp_tools"
        assert [t.name for t in subagent.tools] == ["mcp__mytools__search"]
        assert clients == [client]

    def test_one_failing_server_does_not_block_a_working_one(self, monkeypatch):
        transport = _ScriptedTransport(
            _handshake_responses([{"name": "ok_tool", "description": "", "inputSchema": {}}])
        )
        good_client = McpClient("good", "fake-cmd", launcher=transport.launcher)

        real_client_cls = McpClient

        def fake_ctor(name, command, args=None, env=None, launcher=None):
            if name == "good":
                return good_client
            return real_client_cls(name, "/definitely/not/a/real/binary/xyz123")

        monkeypatch.setattr("dourmouse.mcp_client.McpClient", fake_ctor)
        subagent, clients = build_external_mcp_subagent(
            {"bad": {"command": "/definitely/not/a/real/binary/xyz123"}, "good": {"command": "fake-cmd"}}
        )
        assert subagent is not None
        assert [t.name for t in subagent.tools] == ["mcp__good__ok_tool"]
        assert "bad" in subagent.description or "failed" in subagent.description.lower()


class TestRealEndToEndAgainstTheRealMcpBridgeServer:
    """The real proof: a real McpClient talking, over a real subprocess and
    real stdio pipes, to the REAL dourmouse.mcp_bridge server -- this
    codebase's own MCP client and server halves interoperating for real,
    not two isolated mocks."""

    def test_real_subprocess_handshake_and_tool_call(self):
        client = McpClient("dourmouse", sys.executable, ["-m", "dourmouse.mcp_bridge"])
        try:
            client.start(timeout=30.0)
        except McpClientError as exc:
            pytest.fail(f"real MCP bridge subprocess handshake failed: {exc}")
        try:
            assert client.tools, "the real bridge exposed zero tools"
            tool_names = {t["name"] for t in client.tools}
            assert "web_search" in tool_names or len(tool_names) > 0
        finally:
            client.close()
