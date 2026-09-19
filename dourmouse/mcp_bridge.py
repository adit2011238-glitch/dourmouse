"""MCP bridge (v13) — gives external CLI agents (Claude Code, Codex) real,
live access to Dourmouse's own tools.

The gap this closes, stated directly by the user: Claude Code CLI and
Codex CLI, as wired everywhere else in this codebase (code_backends.py's
run_code_task, general_roster.py's claude_code/codex_code tools), are
self-contained coding agents with their OWN generic tools (bash, file
read/write) and ZERO knowledge that Dourmouse's own tool registry even
exists. Handing either CLI a task like "summarize my inbox" or "what's
happening on the world monitor" gets an honest "I can't access that" —
correct behavior for what those CLIs actually have, but it means neither
one can be used AS Dourmouse's brain for anything outside coding, no
matter how much faster or more reliable it is than the local model.

The real fix: both CLIs support loading external tool servers over the
Model Context Protocol (confirmed live on this machine: `claude --help`
lists --mcp-config; Codex CLI has the equivalent). This module IS that
server — a real, stdlib-only (Rule: zero heavy deps, matching every other
network/protocol implementation in this codebase — world_pulse.py's own
hand-rolled WebSocket client is the same house convention) JSON-RPC 2.0
server speaking MCP's stdio transport, run as a subprocess the CLI itself
launches and talks to over stdin/stdout.

What it exposes: every REGULAR-permission tool already registered in
build_general_registry() (dourmouse/general_roster.py) — the SAME real
handlers dispatch.py's own tool-calling loop calls, not a second,
separately-maintained tool surface. Whatever Dourmouse's own orchestrator
can do, an MCP-connected Claude/Codex can now do too, automatically,
forever in sync with the real registry (no hand-curated list to drift).

Deliberately EXCLUDED, for real safety reasons, not laziness:

- PROHIBITED tools (never execute, by policy, full stop).
- delegate_task / delegate_parallel (Dourmouse's OWN recursive
  self-dispatch tools — an external CLI calling back INTO Dourmouse's own
  orchestration loop has no budget tracker, no depth guard, and no
  connection to THIS run's cost/turn caps; genuinely unsafe to expose.)
- code_claude / code_codex / code_nvidia / code_deepseek / code_ollama and
  claude_code / codex_code (all of these shell out to ANOTHER LLM CLI/API
  — if the caller reaching this bridge IS Claude or Codex already, letting
  it re-invoke itself or a sibling CLI through Dourmouse is pointless at
  best and a real recursion risk at worst.)

REQUIRES_CONFIRMATION tools (gmail_send, drive_create_doc, gmail_trash, ...)
ARE exposed, as of the fix for a real live bug: asked to send an email, an
MCP-connected Claude could not see gmail_send/email_own_send at all (they
were excluded here), so it improvised with send_message -- the INTERNAL
inter-agent bus, not an email tool -- hallucinating a sender name and
producing "REFUSED: unknown sender 'Dourmouse'". Excluding the tool did not
protect anything; it just meant Claude reached for the nearest wrong one.

The safety property this bridge exists to preserve -- no destructive action
without a human confirming it -- was never actually about which tools are
listed. It comes from _execute_tool()'s own confirmation_gate check
(dispatch.py): a REQUIRES_CONFIRMATION tool called with confirmation_gate=
None (this subprocess has no synchronous UI channel back to a browser)
returns "CONFIRMATION REQUIRED: <prompt> (no confirmation channel attached;
NOT executed)" and genuinely never runs the handler. _handle_tools_call()
below routes every call through that exact function rather than invoking
tool.handler() directly, so gated tools are now visible, correctly named,
AND still cannot execute unconfirmed -- the same "draft, never send"
contract the rest of this codebase already holds Claude to (see this
module's own general_roster.py system-prompt Rule 1).

Run standalone (mostly for manual testing — normally launched by the CLI
itself via --mcp-config, see build_mcp_config_file() below):
    .venv/bin/python -m dourmouse.mcp_bridge
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from dourmouse.dispatch import DispatchRegistry, Permission, ToolSpec, _execute_tool

#: Env var name ClaudeCliClient (dispatch.py) sets to a fresh, per-invocation
#: temp file path when it wants a real record of what this bridge actually
#: executed during one `claude -p` call. Verified live (2026-09-19): the
#: real `claude` CLI inherits its own process env into an MCP stdio server
#: it spawns via --mcp-config and merges the config's own "env" dict on top
#: rather than replacing it, so a var set only on that one subprocess call
#: (never touching the cached, shared mcp-config.json) reaches this bridge
#: correctly scoped to that single invocation. Unset (the normal case for a
#: manual/standalone run of this bridge, or any non-ClaudeCliClient caller
#: of run_code_task) means _log_toolcall is a no-op. Must match the literal
#: string code_backends.py's _run_claude_once sets.
_TOOLCALL_LOG_ENV_VAR = "DOURMOUSE_MCP_TOOLCALL_LOG"

#: MCP protocol version this server speaks (the current stable spec
#: version at the time this was built — the handshake is a real
#: negotiation, not a hardcoded assumption the client must accept: see
#: _handle_initialize's own comment on version mismatch).
_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "dourmouse"
_SERVER_VERSION = "13.0"

#: Tool names never exposed over MCP even though they carry REGULAR
#: permission — see this module's own docstring for why each category is
#: excluded (recursion risk / pointless self-delegation, not safety-gated
#: but structurally wrong to hand to an external CLI).
_EXCLUDED_TOOL_NAMES = {
    "delegate_task", "delegate_parallel",
    "claude_code", "codex_code",
    "code_claude", "code_codex", "code_nvidia", "code_deepseek", "code_ollama",
}


def exposed_tools(registry: DispatchRegistry) -> list[ToolSpec]:
    """The real, live tool set this bridge exposes — every tool in the
    registry that is not PROHIBITED and not structurally excluded above.
    REQUIRES_CONFIRMATION tools ARE included; see the module docstring for
    why that is safe. Deterministic, sorted by name so tools/list responses
    are stable across restarts (Rule 2.8-adjacent: nothing here should look
    random to a client diffing tool lists between sessions)."""
    seen: dict[str, ToolSpec] = {}
    for sub in registry.all_subagents():
        for tool in sub.tools:
            if tool.name in _EXCLUDED_TOOL_NAMES:
                continue
            # PROHIBITED tools never execute regardless of caller, so there
            # is nothing to gain by listing them. REQUIRES_CONFIRMATION
            # tools ARE included -- see the module docstring for why this is
            # safe: _handle_tools_call() routes every call through the same
            # confirmation-gate check the rest of the codebase uses, so a
            # gated tool called here reports CONFIRMATION REQUIRED and never
            # actually runs.
            if tool.permission is Permission.PROHIBITED:
                continue
            seen[tool.name] = tool
    return [seen[name] for name in sorted(seen)]


def _tool_to_mcp_schema(tool: ToolSpec) -> dict[str, Any]:
    """MCP's tools/list entry shape: {name, description, inputSchema}.
    ToolSpec.parameters is ALREADY a real JSON Schema object (the same one
    ToolSpec.openai_spec() feeds the OpenAI-compatible dispatch loop) —
    reused as-is, not re-derived, so the two callers can never drift."""
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.parameters,
    }


class McpBridgeServer:
    """A real JSON-RPC 2.0 server over stdio, speaking just enough of MCP
    to serve tools/list and tools/call — the two methods an agentic CLI
    actually needs to call Dourmouse's own tools. initialize/notifications
    are handled honestly (a real handshake, not skipped), but this is
    deliberately NOT a general-purpose MCP SDK: no resources, no prompts,
    no sampling — those aren't what this bridge exists for.

    ``registry``/``tools`` are injectable (tests never touch real network/
    filesystem-backed tool handlers); ``stdin``/``stdout`` are injectable
    file-like objects so tests can drive the protocol without a real
    subprocess.
    """

    def __init__(
        self,
        registry: DispatchRegistry | None = None,
        *,
        tools: list[ToolSpec] | None = None,
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
    ) -> None:
        if tools is not None:
            self._tools = list(tools)
        else:
            from dourmouse.general_roster import build_general_registry

            self._tools = exposed_tools(registry or build_general_registry())
        self._by_name = {t.name: t for t in self._tools}
        self._stdin = stdin if stdin is not None else sys.stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr

    def serve_forever(self) -> None:
        """Read one JSON-RPC message per line until stdin closes (EOF —
        the client process exiting/disconnecting, the normal shutdown
        path for a stdio-transport MCP server). A malformed line is
        reported to stderr (never stdout — that would corrupt the
        protocol stream the client is parsing) and skipped, never crashes
        the server."""
        for line in self._stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"dourmouse mcp_bridge: bad JSON-RPC line: {exc}", file=self._stderr)
                continue
            response = self._handle_message(message)
            if response is not None:
                self._write(response)

    def _write(self, message: dict[str, Any]) -> None:
        self._stdout.write(json.dumps(message) + "\n")
        self._stdout.flush()

    def _handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message
        try:
            if method == "initialize":
                result = self._handle_initialize(message.get("params") or {})
            elif method == "notifications/initialized":
                return None  # a real notification — no response, ever
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = self._handle_tools_call(message.get("params") or {})
            elif method == "ping":
                result = {}
            else:
                if is_notification:
                    return None  # unknown notification: ignore, don't error a one-way message
                return {
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"unknown method: {method}"},
                }
        except Exception as exc:  # noqa: BLE001 - a handler bug must never crash the server or hang the client
            if is_notification:
                print(f"dourmouse mcp_bridge: error handling notification {method!r}: {exc}", file=self._stderr)
                return None
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32603, "message": f"internal error: {exc}"},
            }
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # Real negotiation, not a blind accept: report OUR version either
        # way (the spec's own documented pattern) — a client on a
        # genuinely incompatible version can see the mismatch and decide,
        # rather than this server silently pretending to speak whatever
        # version was asked for.
        return {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
        }

    def _handle_tools_list(self) -> dict[str, Any]:
        return {"tools": [_tool_to_mcp_schema(t) for t in self._tools]}

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        tool = self._by_name.get(name)
        if tool is None:
            return {
                "content": [{"type": "text", "text": f"ERROR: unknown tool '{name}'"}],
                "isError": True,
            }
        try:
            # confirmation_gate=None is the load-bearing part for the
            # default case: for a REGULAR tool this is a plain pass-through
            # to tool.handler(); for a REQUIRES_CONFIRMATION tool it makes
            # _execute_tool() return an honest "CONFIRMATION REQUIRED ...
            # NOT executed" string and genuinely never call the handler.
            # See the module docstring.
            #
            # Real, live-caught bug (2026-09-14): the user turned on the
            # real "skip confirmations" Settings toggle (auto_approve_
            # enabled(), config.py) specifically to stop gated tools from
            # refusing outright, and it works everywhere the web UI's own
            # WebConfirmationGate is in the loop -- but this bridge never
            # instantiates that gate at all, it hardcodes None, so the
            # toggle was silently never consulted on this path. A live
            # gmail_send through this exact bridge kept refusing with
            # "no confirmation channel attached" even with the toggle on.
            # Fix: read the same setting directly and, when it is on, hand
            # _execute_tool a gate that approves everything -- the same
            # honest bypass WebConfirmationGate itself performs, not a
            # fake auto-click on a prompt nobody sees.
            from dourmouse.config import auto_approve_enabled

            gate = (lambda _prompt: True) if auto_approve_enabled() else None
            result_text = _execute_tool(tool, arguments, confirmation_gate=gate)
        except Exception as exc:  # noqa: BLE001 - Rule 2.2: a real failure is reported, never fabricated
            result_text = f"ERROR: tool '{name}' failed: {exc}"
            self._log_toolcall(name, arguments, result_text)
            return {
                "content": [{"type": "text", "text": result_text}],
                "isError": True,
            }
        self._log_toolcall(name, arguments, result_text)
        return {"content": [{"type": "text", "text": str(result_text)}], "isError": False}

    def _log_toolcall(self, name: str, arguments: dict[str, Any], result_text: str) -> None:
        """Best-effort real-tool-call record, read back by ClaudeCliClient
        (dispatch.py) after its `claude -p` call returns and replayed as
        real transcript "tool_use"/"tool_result" entries -- see that
        class's own comment. Logged unconditionally on EVERY call attempt
        (success, error, or a REQUIRES_CONFIRMATION refusal), matching
        dispatch.py's own convention for its normal tool-calling loop: a
        "tool_use" entry is appended the instant a call is attempted,
        before the outcome is known, because grounded-mode cares whether
        the model tried to ground its answer, not just whether the attempt
        succeeded.

        An observer, same discipline as every other event_sink/chime_fn in
        this codebase: logging must never break the real tool call it is
        recording. Not just "no exception propagates" -- a log_path that is
        set but unwritable (e.g. its directory vanished) is silently
        swallowed too, so a filesystem hiccup on the recording side can
        never turn a real, successful tool call into a failed one.
        """
        log_path = os.environ.get(_TOOLCALL_LOG_ENV_VAR)
        if not log_path:
            return
        try:
            record = {
                "name": name,
                "raw_arguments": json.dumps(arguments, default=str),
                "result_text": result_text,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass


def build_mcp_config_file(path: Any) -> None:
    """Write the --mcp-config JSON a CLI needs to launch this bridge as a
    subprocess. ``path`` is a pathlib.Path (or str); uses THIS process's
    own real python executable (sys.executable) so the launched subprocess
    shares the same venv/interpreter this server itself is running under,
    never a guessed "python3" that might resolve to a different, dourmouse-
    less environment.

    Real, live-reproduced bug (2026-09-14): ``dourmouse`` is not an
    installed package, so ``-m dourmouse.mcp_bridge`` only resolves when
    the subprocess's OWN working directory happens to be this repo's
    root. Claude Code CLI spawns each configured MCP server with the
    same working directory as the ``claude`` process that loaded this
    config, and several real callers (code_backends.py's own orchestrator
    session, run via ``_claude_orchestrator_cwd()``'s dedicated workspace
    subdirectory) deliberately use a DIFFERENT cwd. From there, launching
    this exact command reproduces:
        ModuleNotFoundError: No module named 'dourmouse'
    and the CLI just reports the server as unavailable rather than
    surfacing that error anywhere the user can see it, so every tool this
    bridge would have exposed (mail, tasks, browser, world data) silently
    vanishes with no explanation. Verified live: running this same
    command from workspace/claude_orchestrator failed with that exact
    error; setting PYTHONPATH to the repo root fixed it and a real
    initialize handshake succeeded. Fix: hand the subprocess an explicit
    PYTHONPATH pointing at this repo's root, so the import resolves no
    matter what cwd the calling CLI process used."""
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parent.parent)
    config = {
        "mcpServers": {
            "dourmouse": {
                "command": sys.executable,
                "args": ["-m", "dourmouse.mcp_bridge"],
                "env": {"PYTHONPATH": repo_root},
            }
        }
    }
    Path(path).write_text(json.dumps(config, indent=2), encoding="utf-8")


def ensure_codex_mcp_registered(cli: str) -> None:
    """Idempotently register this bridge as an MCP server Codex CLI can see.

    Codex CLI has NO per-invocation ``--mcp-config`` flag the way Claude
    Code does (confirmed live via ``codex exec --help`` / ``codex --help``:
    no such flag exists). Instead MCP servers are a PERSISTENT registration
    living in ``~/.codex/config.toml``, managed via ``codex mcp add`` /
    ``codex mcp list`` (confirmed live: ``codex mcp add dourmouse -- <cli> -m
    dourmouse.mcp_bridge`` then ``codex mcp list`` showing it ``enabled``).
    So unlike the Claude path (a config file built fresh and passed per
    call), Codex needs this ONE-TIME registration step, done lazily and
    cheaply on first use rather than requiring the user to run it by hand.

    Best-effort by design: a failure to register here must never break the
    coding task itself (Rule 2.1/2.2 concern is about THIS call's own
    output, not the environment). Worst case, Codex proceeds without the
    dourmouse tools available and answers accordingly.
    """
    try:
        listed = subprocess.run(
            [cli, "mcp", "list"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if "dourmouse" in (listed.stdout or ""):
        return  # already registered — codex mcp add would just re-add it
    try:
        subprocess.run(
            [cli, "mcp", "add", "dourmouse", "--", sys.executable, "-m", "dourmouse.mcp_bridge"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def main() -> None:
    McpBridgeServer().serve_forever()


if __name__ == "__main__":
    main()
