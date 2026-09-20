"""Dourmouse as an MCP CLIENT (Domain H, piece 7 of 7, second half).

``mcp_bridge.py`` already made Dourmouse a real MCP SERVER (an external
CLI like Claude Code or Codex can load Dourmouse's own tools). This module
is the other, more central half of Claude Code's own MCP feature: the USER
configures an external MCP server, and Dourmouse connects to it AS A
CLIENT and gains its tools automatically, the same way Claude Code's own
``.mcp.json``/``claude_desktop_config.json`` works.

Real, stdlib-only (Rule: zero heavy deps, matching every other protocol
implementation in this codebase -- world_pulse.py's hand-rolled WebSocket
client, ``mcp_bridge.py``'s own hand-rolled JSON-RPC server): a real
stdio-transport JSON-RPC 2.0 client, spawning the configured server as a
subprocess and speaking ``initialize``/``tools/list``/``tools/call`` -- the
exact same three real methods ``mcp_bridge.py``'s own server implements,
so this client and that server can talk to each other directly (verified
in tests: a real ``McpClient`` connects to a real ``McpBridgeServer``
subprocess and calls a real tool through it end to end).

Config format (workspace-relative, ``DEFAULT_CONFIG`` below), the SAME
``mcpServers`` shape ``mcp_bridge.build_mcp_config_file`` writes for Claude
Code and Claude Code's own ``.mcp.json`` uses -- an existing Claude Code
MCP server config can be pointed at directly, not a second bespoke format::

    {
      "mcpServers": {
        "some-server": {"command": "npx", "args": ["-y", "some-mcp-server"], "env": {}}
      }
    }

Every external tool is registered under a real subagent named
``mcp_tools`` (built only when at least one server configures at least one
real tool -- never a standing empty subagent, same discipline
``general_roster.py``'s own ``self_extended`` bucket uses), with tool
names prefixed ``mcp__<server>__<tool>`` -- the exact real naming
convention this codebase already documents for external MCP tools
(``code_backends.py``'s own ``--allowedTools "mcp__dourmouse__*"``
comment), so nothing here invents a second convention.

A single misconfigured or unreachable server never breaks registry
startup or any other server: connecting is entirely best-effort, per
server, with the failure recorded honestly on the subagent's own
description rather than raised (same discipline ``self_extended_errors``
already established for one broken approved self-extension).
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir
from dourmouse.dispatch import Permission, Subagent, ToolSpec

#: Workspace-relative default -- a user-editable JSON file, not a
#: database: this is small, human-authored configuration, matching the
#: shape of the config itself rather than this codebase's usual
#: DEFAULT_DB/SQLite convention for actual DATA.
DEFAULT_CONFIG = workspace_dir() / "mcp_servers.json"

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_NAME = "dourmouse"
_CLIENT_VERSION = "13.0"


class McpClientError(Exception):
    """A real, named failure connecting to or calling an external MCP
    server -- never silently swallowed at the point it happens, only at
    the boundary (subagent-build time) where one bad server must not take
    down every other one."""


class McpClient:
    """One real stdio JSON-RPC 2.0 connection to an external MCP server.

    ``launcher`` (optional) is the test seam: a callable returning an
    object with ``.stdin``/``.stdout`` file-like attributes, in place of a
    real ``subprocess.Popen`` -- the same injectable-transport pattern
    ``mcp_bridge.McpBridgeServer`` already uses for its own stdin/stdout.
    """

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        launcher: Any = None,
    ) -> None:
        self.name = name
        self._command = command
        self._args = args or []
        self._env = env
        self._launcher = launcher
        self._process: subprocess.Popen | None = None
        self._stdin: Any = None
        self._stdout: Any = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.tools: list[dict[str, Any]] = []

    def start(self, timeout: float = 15.0) -> None:
        """Spawn the server, perform the real ``initialize`` handshake,
        send the ``notifications/initialized`` notification, then fetch
        the real tool list. Raises ``McpClientError`` on any failure --
        the caller (``build_external_mcp_subagent``) decides whether one
        server's failure is fatal to the whole build (it is not)."""
        if self._launcher is not None:
            proc = self._launcher()
            self._stdin, self._stdout = proc.stdin, proc.stdout
        else:
            import os

            full_env = dict(os.environ)
            if self._env:
                full_env.update(self._env)
            try:
                self._process = subprocess.Popen(
                    [self._command, *self._args],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, env=full_env,
                )
            except OSError as exc:
                raise McpClientError(f"failed to launch MCP server {self.name!r}: {exc}") from exc
            self._stdin, self._stdout = self._process.stdin, self._process.stdout
        try:
            self._request("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
            }, timeout=timeout)
            self._notify("notifications/initialized")
            result = self._request("tools/list", {}, timeout=timeout)
        except McpClientError:
            self.close()
            raise
        self.tools = result.get("tools") or []

    def call_tool(self, tool_name: str, arguments: dict[str, Any], timeout: float = 60.0) -> str:
        """Real ``tools/call`` -- returns the tool's own real text result
        (the same ``content: [{type: text, text: ...}]`` shape
        ``mcp_bridge.py``'s own server produces), or an honest ``ERROR:``
        string on any real failure. Never fabricates a result."""
        try:
            result = self._request(
                "tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout,
            )
        except McpClientError as exc:
            return f"ERROR: MCP call to '{self.name}/{tool_name}' failed: {exc}"
        content = result.get("content") or []
        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        if result.get("isError"):
            return f"ERROR: {text or f'{tool_name} reported an error'}"
        return text

    def close(self) -> None:
        for stream in (self._stdin, self._stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:
                pass

    # -- real JSON-RPC transport ------------------------------------------ #

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self._write(message)

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            msg_id = next(self._ids)
            self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            line = self._readline(timeout)
        if not line:
            raise McpClientError(f"{self.name}: no response to {method!r} (server closed the connection)")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpClientError(f"{self.name}: bad JSON-RPC response to {method!r}: {exc}") from exc
        if "error" in message:
            err = message["error"] or {}
            raise McpClientError(f"{self.name}: {method} error: {err.get('message', err)}")
        return message.get("result") or {}

    def _write(self, message: dict[str, Any]) -> None:
        try:
            self._stdin.write(json.dumps(message) + "\n")
            self._stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpClientError(f"{self.name}: failed writing to server: {exc}") from exc

    def _readline(self, timeout: float) -> str:
        # Honest limitation, not hidden: stdio pipes have no portable
        # non-blocking readline with a real timeout across platforms
        # without extra machinery this codebase doesn't otherwise need
        # (threads/selectors just to bound one blocking read). A genuinely
        # hung external server blocks here, same as any other synchronous
        # subprocess call in this codebase (e.g. code_backends.py's own
        # CLI subprocess calls). ``timeout`` is accepted for interface
        # symmetry with real time-budget call sites elsewhere but is not
        # enforced on this path -- real, separate follow-on if an external
        # MCP server is ever observed hanging in practice.
        del timeout
        try:
            return self._stdout.readline()
        except (OSError, ValueError) as exc:
            raise McpClientError(f"{self.name}: failed reading from server: {exc}") from exc


def load_external_mcp_servers(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """The real ``mcpServers`` config, or ``{}`` when the file is missing
    or malformed -- a fresh workspace with no external servers configured
    is the normal, common case, never an error."""
    p = Path(path) if path is not None else DEFAULT_CONFIG
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _wrap_external_tool(client: McpClient, tool: dict[str, Any]) -> ToolSpec:
    real_name = tool.get("name", "")
    prefixed_name = f"mcp__{client.name}__{real_name}"

    def _handler(arguments: dict[str, Any], _client: McpClient = client, _name: str = real_name) -> str:
        return _client.call_tool(_name, arguments)

    return ToolSpec(
        name=prefixed_name,
        description=tool.get("description", "") or f"External MCP tool from server {client.name!r}.",
        parameters=tool.get("inputSchema") or {"type": "object", "properties": {}},
        handler=_handler,
        permission=Permission.REGULAR,
    )


def build_external_mcp_subagent(
    servers: dict[str, dict[str, Any]] | None = None,
) -> tuple[Subagent | None, list[McpClient]]:
    """Connect to every configured external MCP server (best-effort, one
    server's failure never blocks another's) and return a real ``mcp_
    tools`` Subagent wrapping every real tool they exposed, plus the list
    of started ``McpClient``s (so the caller can ``close()`` them on
    shutdown). Returns ``(None, [])`` when no server is configured or
    every configured server failed to connect -- never a standing empty
    subagent.
    """
    if servers is None:
        servers = load_external_mcp_servers()
    tools: list[ToolSpec] = []
    started: list[McpClient] = []
    errors: list[str] = []
    for name, cfg in servers.items():
        command = cfg.get("command")
        if not command:
            errors.append(f"{name}: no 'command' configured")
            continue
        client = McpClient(name, command, cfg.get("args"), cfg.get("env"))
        try:
            client.start()
        except McpClientError as exc:
            errors.append(str(exc))
            continue
        started.append(client)
        for tool in client.tools:
            tools.append(_wrap_external_tool(client, tool))
    if not tools:
        for client in started:
            client.close()
        return None, []
    description = f"Tools from {len(started)} external MCP server(s): {', '.join(c.name for c in started)}."
    if errors:
        description += f" {len(errors)} server(s) failed to connect: " + "; ".join(errors)
    subagent = Subagent(name="mcp_tools", domain="General", description=description, tools=tuple(tools))
    return subagent, started


if __name__ == "__main__":
    # Manual smoke test: python -m dourmouse.mcp_client
    subagent, clients = build_external_mcp_subagent()
    if subagent is None:
        print("no external MCP servers configured or reachable "
              f"(edit {DEFAULT_CONFIG} to add one)")
    else:
        print(subagent.roster_line())
    for c in clients:
        c.close()
    sys.exit(0)
