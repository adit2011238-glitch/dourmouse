"""FIX-P R5-17: the bridge gates its error text, knows the owner secret by hash, and recognises credential paths
built inside a command and .env-shaped content."""

from __future__ import annotations

import io
from typing import Any

import pytest

from dourmouse import privacy
from dourmouse.dispatch import ToolSpec
from dourmouse.mcp_bridge import McpBridgeServer

SECRET = "s3cr3t-owner-token-0123456789-abcdefXYZ"


@pytest.fixture(autouse=True)
def _clean_never_send():
    yield
    with privacy._never_lock:
        privacy._NEVER_SEND.clear()
        privacy._NEVER_HASHES.clear()


def _server(handler) -> McpBridgeServer:
    tool = ToolSpec(name="read_path", description="d", parameters={"type": "object", "properties": {}},
                    handler=handler)
    return McpBridgeServer(tools=[tool, ToolSpec(name="run_command", description="d",
                                                  parameters={"type": "object", "properties": {}},
                                                  handler=handler)],
                           stdin=io.StringIO(""), stdout=io.StringIO(), stderr=io.StringIO())


def _call(server, name="read_path", args=None) -> str:
    return server._handle_tools_call({"name": name, "arguments": args or {}})["content"][0]["text"]


def test_error_text_that_echoes_a_real_value_is_swapped(monkeypatch):
    def boom(a: dict[str, Any]) -> str:
        raise RuntimeError("could not mail alice@example.org")

    out = _call(_server(boom))
    assert "alice@example.org" not in out and "[EMAIL_" in out


def test_error_text_with_the_owner_secret_is_withheld(monkeypatch):
    def boom(a: dict[str, Any]) -> str:
        raise RuntimeError(f"bad header {SECRET}")

    privacy.register_never_send(SECRET)
    out = _call(_server(boom))
    assert SECRET not in out and out.startswith("NOT SENT")


def test_the_bridge_gets_the_secret_hash_not_the_secret(monkeypatch):
    privacy.register_never_send(SECRET)
    with privacy.chat_scope("tab-sec"):
        env, _ = privacy.bridge_handoff("claude_cli")
    assert SECRET not in "".join(env.values()) and privacy.BRIDGE_SECRET_ENV in env
    # a fresh bridge process: no value registered, only the hash from the environment
    with privacy._never_lock:
        privacy._NEVER_SEND.clear()
        privacy._NEVER_HASHES.clear()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    out = _call(_server(lambda a: f"the token is {SECRET} ok"))
    assert SECRET not in out and out.startswith("NOT SENT") and "owner secret" in out


@pytest.mark.parametrize("command", [
    "python -c \"print(open(Path.home()/'.ssh'/'id_rsa').read())\"",
    "python -c \"import pathlib; pathlib.Path.home().joinpath('.aws','credentials')\"",
    "cat \"$HOME\"/.env",
    "grep -r KEY ~/proj/.env.production",
])
def test_credential_paths_built_inside_a_command_are_t3(command):
    assert privacy.t3_reason("run_command", {"command": command}, "output") is not None


def test_ordinary_commands_are_not_t3():
    for command in ("ls -la", "python -c \"print(1)\"", "grep -r todo ./src", "echo environment"):
        assert privacy.t3_reason("run_command", {"command": command}, "output") is None


def test_dotenv_shaped_content_is_t3_whatever_read_it():
    body = "OPENAI_API_KEY=sk-abcdef123456\nDB_PASSWORD=hunter2hunter2\nDEBUG=true\n"
    assert privacy.t3_reason("run_command", {"command": "grep -r KEY ~/proj"}, body) is not None
    assert privacy.t3_reason("run_command", {"command": "ls"}, "API_KEY=abcdefgh\nrest of the page") is None  # one line
