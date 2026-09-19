"""dourmouse/mcp_bridge.py — the MCP server giving external CLI agents
(Claude Code, Codex) real access to Dourmouse's own tools.

Hermetic (Rule 2.1): every test drives McpBridgeServer directly against
injected StringIO stdin/stdout and a small fake registry — no real
subprocess, no real `claude`/`codex` CLI, no network. The actual live
handshake against the real installed CLI is verified separately, once,
manually (see this module's own docstring in mcp_bridge.py and the
session's own change log for that verification).
"""

from __future__ import annotations

import io
import json

import pytest

from dourmouse.dispatch import DispatchRegistry, Permission, Subagent, ToolSpec
from dourmouse.mcp_bridge import (
    _EXCLUDED_TOOL_NAMES,
    McpBridgeServer,
    build_mcp_config_file,
    exposed_tools,
)


def _registry_with(*tools: ToolSpec) -> DispatchRegistry:
    r = DispatchRegistry()
    r.register_subagent(
        Subagent(name="test_agent", domain="Test", description="test", tools=tools)
    )
    return r


def _tool(name: str, permission: Permission = Permission.REGULAR, handler=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} tool",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": [],
        },
        handler=handler or (lambda a: f"ran {name} with {a}"),
        permission=permission,
    )


class TestExposedTools:
    def test_regular_tool_is_exposed(self):
        registry = _registry_with(_tool("echo"))
        names = {t.name for t in exposed_tools(registry)}
        assert "echo" in names

    def test_requires_confirmation_tool_is_exposed(self):
        """Regression test for a real live bug: asked to send an email, an
        MCP-connected Claude could not see gmail_send/email_own_send at all
        (both REQUIRES_CONFIRMATION, both excluded here) and improvised with
        send_message -- the INTERNAL inter-agent bus, not an email tool --
        hallucinating a sender name.

        The fix is not to remove the safety boundary, it is to notice the
        boundary was never actually about which tools are LISTED: it comes
        from _execute_tool()'s confirmation_gate check, which this bridge's
        _handle_tools_call() now routes every call through. A gated tool is
        visible and correctly named, and still cannot execute unconfirmed --
        see TestGatedToolsAreVisibleButNeverExecuteUnconfirmed below for the proof."""
        registry = _registry_with(_tool("send_email", Permission.REQUIRES_CONFIRMATION))
        names = {t.name for t in exposed_tools(registry)}
        assert "send_email" in names

    def test_prohibited_tool_is_excluded(self):
        registry = _registry_with(_tool("dangerous", Permission.PROHIBITED))
        names = {t.name for t in exposed_tools(registry)}
        assert "dangerous" not in names

    @pytest.mark.parametrize("name", sorted(_EXCLUDED_TOOL_NAMES))
    def test_structurally_excluded_names_never_exposed_even_if_regular(self, name):
        """delegate_task/delegate_parallel and every code_*/claude_code/
        codex_code tool are excluded by NAME regardless of permission —
        recursion risk (delegate_*) or pointless self-delegation
        (an already-Claude/Codex caller re-invoking a sibling CLI)."""
        registry = _registry_with(_tool(name))  # REGULAR permission on purpose
        names = {t.name for t in exposed_tools(registry)}
        assert name not in names

    def test_real_registry_excludes_nothing_unexpectedly(self):
        """Sanity check against the REAL, full registry (not a fake one) —
        every excluded name must actually exist there (catches a typo'd
        exclusion silently doing nothing), and the exposed set must be
        non-empty (catches an over-broad exclusion silently emptying it)."""
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        all_tool_names = {
            t.name for sub in registry.all_subagents() for t in sub.tools
        }
        for excluded in _EXCLUDED_TOOL_NAMES:
            assert excluded in all_tool_names, (
                f"{excluded!r} is in _EXCLUDED_TOOL_NAMES but not in the real "
                "registry — likely a stale/typo'd exclusion"
            )
        exposed_names = {t.name for t in exposed_tools(registry)}
        assert exposed_names, "exposed_tools() returned nothing against the real registry"
        assert not (exposed_names & _EXCLUDED_TOOL_NAMES)

    def test_sorted_and_deterministic(self):
        registry = _registry_with(_tool("zebra"), _tool("alpha"), _tool("mid"))
        names = [t.name for t in exposed_tools(registry)]
        assert names == sorted(names)

    def test_a_shared_toolspec_object_is_not_duplicated(self):
        """extend_subagent lets one ToolSpec ride multiple agents (the
        real registry does this for query_shared_memory) — exposed_tools
        must not list it twice."""
        shared = _tool("shared_tool")
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="a", domain="Test", description="x", tools=(shared,)))
        r.extend_subagent("a", shared)  # idempotent no-op per its own docstring, but exercise it
        r.register_subagent(Subagent(name="b", domain="Test", description="x", tools=()))
        r.extend_subagent("b", shared)
        names = [t.name for t in exposed_tools(r) if t.name == "shared_tool"]
        assert names == ["shared_tool"]


class TestGatedToolsAreVisibleButNeverExecuteUnconfirmed:
    """The actual safety property this bridge exists to preserve. Not
    "is the tool listed" -- it's "can it run without a human approving it" --
    and that comes from _execute_tool()'s own confirmation_gate check, not
    from hiding the tool name."""

    def test_a_gated_tool_call_reports_confirmation_required_and_never_runs(self):
        ran = {"called": False}

        def handler(args):
            ran["called"] = True
            return "SENT (this must never appear in the test result)"

        registry = _registry_with(_tool("send_email", Permission.REQUIRES_CONFIRMATION, handler=handler))
        server = McpBridgeServer(registry)

        result = server._handle_tools_call({"name": "send_email", "arguments": {}})

        assert ran["called"] is False, "the real handler executed despite no confirmation"
        text = result["content"][0]["text"]
        assert "CONFIRMATION REQUIRED" in text
        assert "NOT executed" in text
        assert result["isError"] is False

    def test_a_prohibited_tool_is_not_reachable_at_all(self):
        registry = _registry_with(_tool("dangerous", Permission.PROHIBITED))
        server = McpBridgeServer(registry)
        result = server._handle_tools_call({"name": "dangerous", "arguments": {}})
        assert "unknown tool" in result["content"][0]["text"]
        assert result["isError"] is True

    def test_the_real_gmail_send_tool_is_exposed_and_still_refuses_to_run(self):
        """Against the REAL registry, not a fake one -- proves the fix
        actually reaches the tool that was reported broken."""
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        names = {t.name for t in exposed_tools(registry)}
        assert "gmail_send" in names
        assert "email_own_send" in names
        assert "drive_create_doc" in names

        server = McpBridgeServer(registry)
        result = server._handle_tools_call({
            "name": "gmail_send",
            "arguments": {"to": "nobody@example.com", "subject": "x", "body": "x"},
        })
        text = result["content"][0]["text"]
        assert "CONFIRMATION REQUIRED" in text
        assert "NOT executed" in text

    def test_a_regular_tool_still_executes_normally_through_the_same_path(self):
        """The gate change must not slow down or alter REGULAR tools --
        they still call straight through to the real handler."""
        registry = _registry_with(_tool("echo"))
        server = McpBridgeServer(registry)
        result = server._handle_tools_call({"name": "echo", "arguments": {}})
        assert result["isError"] is False
        assert "CONFIRMATION REQUIRED" not in result["content"][0]["text"]

    def test_auto_approve_setting_lets_a_gated_tool_actually_run(self, monkeypatch):
        """Real, live-caught bug (2026-09-14): the user's "skip
        confirmations" toggle (auto_approve_enabled(), config.py) had no
        effect at all through this bridge -- confirmation_gate=None was
        hardcoded, so a live gmail_send kept refusing even with the
        toggle on. This is the fix: when the setting is on, the gate
        handed to _execute_tool must approve, and the real handler must
        run."""
        monkeypatch.setattr("dourmouse.config.auto_approve_enabled", lambda: True)
        ran = {"called": False}

        def handler(args):
            ran["called"] = True
            return "SENT for real"

        registry = _registry_with(_tool("send_email", Permission.REQUIRES_CONFIRMATION, handler=handler))
        server = McpBridgeServer(registry)

        result = server._handle_tools_call({"name": "send_email", "arguments": {}})

        assert ran["called"] is True
        assert result["isError"] is False
        assert result["content"][0]["text"] == "SENT for real"

    def test_auto_approve_off_still_refuses_as_before(self, monkeypatch):
        """The default (off) behaviour from the test above this one must
        be completely unchanged."""
        monkeypatch.setattr("dourmouse.config.auto_approve_enabled", lambda: False)
        ran = {"called": False}

        def handler(args):
            ran["called"] = True
            return "SENT (must never appear)"

        registry = _registry_with(_tool("send_email", Permission.REQUIRES_CONFIRMATION, handler=handler))
        server = McpBridgeServer(registry)

        result = server._handle_tools_call({"name": "send_email", "arguments": {}})

        assert ran["called"] is False
        assert "CONFIRMATION REQUIRED" in result["content"][0]["text"]


class TestToolToMcpSchema:
    def test_parameters_reused_as_input_schema_not_re_derived(self):
        from dourmouse.mcp_bridge import _tool_to_mcp_schema

        t = _tool("thing")
        schema = _tool_to_mcp_schema(t)
        assert schema["name"] == "thing"
        assert schema["description"] == "thing tool"
        assert schema["inputSchema"] is t.parameters  # same object, never copied/rebuilt


def _rpc(method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return json.dumps(msg) + "\n"


def _drive(server, lines: str) -> list[dict]:
    stdin = io.StringIO(lines)
    stdout = io.StringIO()
    stderr = io.StringIO()
    server._stdin, server._stdout, server._stderr = stdin, stdout, stderr
    server.serve_forever()
    out = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
    return out


class TestJsonRpcProtocol:
    def _server(self, tools=None):
        registry = _registry_with(*(tools or [_tool("echo")]))
        return McpBridgeServer(registry)

    def test_initialize_returns_real_protocol_info(self):
        server = self._server()
        out = _drive(server, _rpc("initialize", {"protocolVersion": "2024-11-05"}))
        assert len(out) == 1
        result = out[0]["result"]
        assert result["serverInfo"]["name"] == "dourmouse"
        assert "tools" in result["capabilities"]

    def test_notifications_initialized_gets_no_response(self):
        server = self._server()
        out = _drive(server, _rpc("notifications/initialized", id_=None))
        assert out == []

    def test_tools_list_returns_the_real_exposed_set(self):
        """tools/list echoes back whatever this server was constructed
        with -- both a plain and a gated tool are listed. Gating happens at
        CALL time (see TestGatedToolsAreVisibleButNeverExecuteUnconfirmed),
        not by hiding the tool's existence."""
        server = self._server([_tool("read_thing"), _tool("write_thing", Permission.REQUIRES_CONFIRMATION)])
        out = _drive(server, _rpc("tools/list"))
        names = {t["name"] for t in out[0]["result"]["tools"]}
        assert names == {"read_thing", "write_thing"}

    def test_tools_call_runs_the_real_handler(self):
        calls = []

        def handler(args):
            calls.append(args)
            return "REAL RESULT"

        server = self._server([_tool("do_thing", handler=handler)])
        out = _drive(server, _rpc("tools/call", {"name": "do_thing", "arguments": {"x": "y"}}))
        assert calls == [{"x": "y"}]
        result = out[0]["result"]
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "REAL RESULT"}]

    def test_tools_call_unknown_tool_is_an_honest_error_not_a_crash(self):
        server = self._server()
        out = _drive(server, _rpc("tools/call", {"name": "does_not_exist", "arguments": {}}))
        result = out[0]["result"]
        assert result["isError"] is True
        assert "unknown tool" in result["content"][0]["text"]

    def test_tools_call_a_raising_handler_is_an_honest_error_not_a_crash(self):
        """_execute_tool() (dispatch.py) now catches the raise itself and
        turns it into a real, readable "ERROR: tool 'x' failed: ..." text
        result -- with real obs logging behind it -- rather than letting it
        surface as a bare JSON-RPC isError flag with no detail. This matches
        how every other tool-calling path in this codebase already treats a
        failed tool: a normal string result the model reads and reacts to,
        not an exception. isError stays False; the content says ERROR."""
        def boom(args):
            raise RuntimeError("real failure")

        server = self._server([_tool("boom", handler=boom)])
        out = _drive(server, _rpc("tools/call", {"name": "boom", "arguments": {}}))
        result = out[0]["result"]
        assert result["isError"] is False
        assert "ERROR: tool 'boom' failed" in result["content"][0]["text"]
        assert "real failure" in result["content"][0]["text"]

    def test_unknown_method_with_id_gets_a_json_rpc_error_not_a_hang(self):
        server = self._server()
        out = _drive(server, _rpc("bogus/method"))
        assert out[0]["error"]["code"] == -32601

    def test_unknown_notification_is_silently_ignored(self):
        server = self._server()
        out = _drive(server, _rpc("bogus/notification", id_=None))
        assert out == []

    def test_malformed_json_line_is_skipped_not_fatal(self):
        server = self._server()
        lines = "not json at all\n" + _rpc("tools/list")
        out = _drive(server, lines)
        # The malformed line produced nothing; the real request right
        # after it still got answered — one bad line never kills the loop.
        assert len(out) == 1
        assert "tools" in out[0]["result"]

    def test_multiple_requests_in_sequence_each_get_their_own_response(self):
        server = self._server()
        lines = _rpc("initialize", id_=1) + _rpc("tools/list", id_=2) + _rpc("tools/list", id_=3)
        out = _drive(server, lines)
        assert [m["id"] for m in out] == [1, 2, 3]

    def test_a_handler_that_raises_inside_message_dispatch_never_hangs_the_client(self):
        """Not the tool handler itself (already covered) — a bug in THIS
        server's own dispatch code for a known method must still answer
        with a JSON-RPC error, never drop the request silently (a client
        waiting on a response that never comes is worse than an error)."""
        server = self._server()

        def _boom(_params):
            raise RuntimeError("bridge bug")

        server._handle_tools_list = _boom
        out = _drive(server, _rpc("tools/list"))
        assert out[0]["error"]["code"] == -32603


class TestToolcallLogging:
    """The real fix for a live-reproduced Grounded Mode false positive
    (2026-09-19): a tool call executed through this bridge runs in a
    SEPARATE OS subprocess from the dispatch() run that's checking
    tools_used, so it was previously invisible no matter how real the
    result was. ClaudeCliClient (dispatch.py) sets _TOOLCALL_LOG_ENV_VAR to
    a fresh per-invocation temp path before spawning `claude -p`; this is
    the write half of that channel -- see TestClaudeCliClient in
    test_dispatch.py for the read half."""

    def test_no_env_var_set_is_a_silent_no_op(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DOURMOUSE_MCP_TOOLCALL_LOG", raising=False)
        registry = _registry_with(_tool("echo"))
        server = McpBridgeServer(registry)
        result = server._handle_tools_call({"name": "echo", "arguments": {"x": "y"}})
        assert result["isError"] is False
        # No log path was ever configured, so nothing should exist to check --
        # this is really just proving _log_toolcall didn't raise.

    def test_successful_call_is_logged_with_real_name_args_and_result(self, monkeypatch, tmp_path):
        log_path = tmp_path / "toolcalls.ndjson"
        monkeypatch.setenv("DOURMOUSE_MCP_TOOLCALL_LOG", str(log_path))
        registry = _registry_with(_tool("do_thing", handler=lambda a: "REAL RESULT"))
        server = McpBridgeServer(registry)

        result = server._handle_tools_call({"name": "do_thing", "arguments": {"x": "y"}})

        assert result["isError"] is False
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["name"] == "do_thing"
        assert json.loads(record["raw_arguments"]) == {"x": "y"}
        assert record["result_text"] == "REAL RESULT"

    def test_a_raising_handler_is_still_logged(self, monkeypatch, tmp_path):
        """Matches dispatch.py's own convention for its normal tool-calling
        loop: a "tool_use" record is made the instant a call is attempted,
        regardless of outcome -- grounded-mode cares whether the model tried
        to ground its answer, not just whether the attempt succeeded."""
        log_path = tmp_path / "toolcalls.ndjson"
        monkeypatch.setenv("DOURMOUSE_MCP_TOOLCALL_LOG", str(log_path))

        def boom(args):
            raise RuntimeError("real failure")

        registry = _registry_with(_tool("boom", handler=boom))
        server = McpBridgeServer(registry)

        server._handle_tools_call({"name": "boom", "arguments": {}})

        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["name"] == "boom"
        assert "ERROR" in record["result_text"]

    def test_a_gated_unconfirmed_call_is_still_logged(self, monkeypatch, tmp_path):
        log_path = tmp_path / "toolcalls.ndjson"
        monkeypatch.setenv("DOURMOUSE_MCP_TOOLCALL_LOG", str(log_path))
        registry = _registry_with(_tool("send_email", Permission.REQUIRES_CONFIRMATION))
        server = McpBridgeServer(registry)

        server._handle_tools_call({"name": "send_email", "arguments": {}})

        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["name"] == "send_email"
        assert "CONFIRMATION REQUIRED" in record["result_text"]

    def test_unknown_tool_is_never_logged(self, monkeypatch, tmp_path):
        """Nothing real was attempted -- there is no tool to attribute a
        record to, and logging one could misattribute a bogus name into a
        real transcript later."""
        log_path = tmp_path / "toolcalls.ndjson"
        monkeypatch.setenv("DOURMOUSE_MCP_TOOLCALL_LOG", str(log_path))
        registry = _registry_with(_tool("echo"))
        server = McpBridgeServer(registry)

        server._handle_tools_call({"name": "does_not_exist", "arguments": {}})

        assert not log_path.exists()

    def test_multiple_calls_append_as_separate_ndjson_lines(self, monkeypatch, tmp_path):
        log_path = tmp_path / "toolcalls.ndjson"
        monkeypatch.setenv("DOURMOUSE_MCP_TOOLCALL_LOG", str(log_path))
        registry = _registry_with(_tool("a"), _tool("b"))
        server = McpBridgeServer(registry)

        server._handle_tools_call({"name": "a", "arguments": {}})
        server._handle_tools_call({"name": "b", "arguments": {}})

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(l)["name"] for l in lines] == ["a", "b"]

    def test_unwritable_log_path_never_breaks_the_real_tool_call(self, monkeypatch):
        """An observer must never break the thing it's observing -- a log
        directory that doesn't exist must not turn a real, successful tool
        call into a failure."""
        monkeypatch.setenv("DOURMOUSE_MCP_TOOLCALL_LOG", "/no/such/directory/toolcalls.ndjson")
        registry = _registry_with(_tool("do_thing", handler=lambda a: "REAL RESULT"))
        server = McpBridgeServer(registry)

        result = server._handle_tools_call({"name": "do_thing", "arguments": {}})

        assert result["isError"] is False
        assert result["content"][0]["text"] == "REAL RESULT"


class TestConfigFileGeneration:
    def test_config_points_at_this_interpreter_and_this_module(self, tmp_path):
        path = tmp_path / "mcp-config.json"
        build_mcp_config_file(path)
        config = json.loads(path.read_text(encoding="utf-8"))
        server_cfg = config["mcpServers"]["dourmouse"]
        assert server_cfg["args"] == ["-m", "dourmouse.mcp_bridge"]
        import sys
        assert server_cfg["command"] == sys.executable

    def test_config_sets_pythonpath_to_the_repo_root(self, tmp_path):
        """Real, live-reproduced bug (2026-09-14): without this, launching
        `-m dourmouse.mcp_bridge` from any working directory other than
        the repo root (e.g. code_backends.py's own dedicated orchestrator
        workspace subdirectory) fails with "No module named 'dourmouse'"
        and the CLI just reports the server unavailable -- silently
        dropping every dourmouse tool with no visible error. Verified
        live: running the real command from that real subdirectory failed
        with exactly that error; setting PYTHONPATH to the repo root fixed
        it and a real initialize handshake succeeded."""
        from pathlib import Path

        path = tmp_path / "mcp-config.json"
        build_mcp_config_file(path)
        config = json.loads(path.read_text(encoding="utf-8"))
        server_cfg = config["mcpServers"]["dourmouse"]
        repo_root = str(Path(__file__).resolve().parent.parent.parent)
        assert server_cfg["env"]["PYTHONPATH"] == repo_root
        assert (Path(repo_root) / "dourmouse" / "mcp_bridge.py").is_file()
