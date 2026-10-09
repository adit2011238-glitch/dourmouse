"""FS1 fixes in dourmouse/mcp_bridge.py: P4-33 (Codex registration carries
PYTHONPATH and a stale registration is repaired) and P4-34 (a non-object
JSON line no longer kills the stdio server)."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dourmouse import mcp_bridge
from dourmouse.mcp_bridge import McpBridgeServer

REPO_ROOT = str(Path(mcp_bridge.__file__).resolve().parent.parent)


# -- P4-34 ------------------------------------------------------------------- #

def _serve(lines):
    out = io.StringIO()
    err = io.StringIO()
    server = McpBridgeServer(tools=[], stdin=io.StringIO("".join(line + "\n" for line in lines)), stdout=out, stderr=err)
    server.serve_forever()
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_a_non_object_line_is_answered_and_the_server_keeps_going():
    replies = _serve(["[1]", "42", '"x"', json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})])
    assert replies[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    errors = list(replies[:-1])
    assert errors and all(r.get("error", {}).get("code") == -32600 for r in errors if isinstance(r, dict))


def test_a_batch_array_gets_a_batch_reply():
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    replies = _serve([json.dumps(batch)])
    assert replies == [[{"jsonrpc": "2.0", "id": 1, "result": {}}, {"jsonrpc": "2.0", "id": 2, "result": {}}]]


# -- P4-33 ------------------------------------------------------------------- #

class _P:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _registration(command, pythonpath):
    env = {"PYTHONPATH": pythonpath} if pythonpath is not None else {}
    return json.dumps({"name": "dourmouse", "transport": {
        "type": "stdio", "command": command, "args": ["-m", "dourmouse.mcp_bridge"], "env": env,
    }})


@pytest.mark.parametrize("current", [
    None,                                   # not registered
    _registration(sys.executable, None),    # the old registration: no PYTHONPATH
    _registration("/old/venv/bin/python", REPO_ROOT),  # stale interpreter
])
def test_registers_with_pythonpath_when_missing_or_stale(monkeypatch, current):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1:3] == ["mcp", "get"]:
            return _P(1, "") if current is None else _P(0, current)
        return _P(0, "Added")

    monkeypatch.setattr(mcp_bridge.subprocess, "run", fake_run)
    mcp_bridge.ensure_codex_mcp_registered("/usr/bin/codex")
    add = calls[-1]
    assert add[:4] == ["/usr/bin/codex", "mcp", "add", "dourmouse"]
    assert add[add.index("--env") + 1] == f"PYTHONPATH={REPO_ROOT}"
    assert add[add.index("--") + 1:] == [sys.executable, "-m", "dourmouse.mcp_bridge"]


def test_a_current_registration_is_left_alone(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _P(0, _registration(sys.executable, REPO_ROOT))

    monkeypatch.setattr(mcp_bridge.subprocess, "run", fake_run)
    mcp_bridge.ensure_codex_mcp_registered("/usr/bin/codex")
    assert len(calls) == 1 and calls[0][1:3] == ["mcp", "get"]


@pytest.mark.skipif(shutil.which("codex") is None, reason="needs the real Codex CLI")
def test_real_codex_cli_accepts_the_registration(tmp_path, monkeypatch):
    """Runs the real codex binary against an isolated CODEX_HOME (never the
    owner's ~/.codex): register, then read the registration back."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    cli = shutil.which("codex")
    mcp_bridge.ensure_codex_mcp_registered(cli)
    got = subprocess.run([cli, "mcp", "get", "dourmouse", "--json"], capture_output=True, text=True, timeout=30, check=True)
    transport = json.loads(got.stdout)["transport"]
    assert transport["command"] == sys.executable
    assert transport["env"]["PYTHONPATH"] == REPO_ROOT
