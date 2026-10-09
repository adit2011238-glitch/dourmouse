"""FS2 P4-35 / P4-36: MCP client matches replies by id, enforces a deadline, and reuses servers."""

from __future__ import annotations

import io
import json
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from dourmouse import mcp_client
from dourmouse.mcp_client import McpClient, McpClientError, build_external_mcp_subagent


class _Transport:
    def __init__(self, lines: list[dict]):
        self.sent: list[dict] = []
        self._stdout = io.StringIO("".join(json.dumps(r) + "\n" for r in lines))

    def launcher(self):
        cap = self

        class _In:
            def write(self, text):
                cap.sent.append(json.loads(text))

            def flush(self):
                pass

            def close(self):
                pass

        return SimpleNamespace(stdin=_In(), stdout=self._stdout)


def _hs(tools=()):
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": list(tools)}},
    ]


def test_notification_before_reply_does_not_desync():
    note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}
    t = _Transport([note] + _hs() + [
        note,
        {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "first"}]}},
        note,
        {"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text": "second"}]}},
    ])
    c = McpClient("srv", "x", launcher=t.launcher)
    c.start()
    assert c.call_tool("a", {}) == "first"
    assert c.call_tool("b", {}) == "second"


def test_stale_reply_with_other_id_is_skipped():
    t = _Transport(_hs() + [
        {"jsonrpc": "2.0", "id": 99, "result": {"content": [{"type": "text", "text": "stale"}]}},
        {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "mine"}]}},
    ])
    c = McpClient("srv", "x", launcher=t.launcher)
    c.start()
    assert c.call_tool("a", {}) == "mine"


def test_server_request_is_answered_not_taken_as_the_reply():
    t = _Transport(_hs() + [
        {"jsonrpc": "2.0", "id": "p1", "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "done"}]}},
    ])
    c = McpClient("srv", "x", launcher=t.launcher)
    c.start()
    assert c.call_tool("a", {}) == "done"
    answers = [m for m in t.sent if m.get("id") == "p1"]
    assert answers and "result" in answers[0]


def _script(tmp_path, body: str):
    p = tmp_path / "srv.py"
    p.write_text(textwrap.dedent(body))
    return p


def test_hung_server_times_out_and_is_terminated(tmp_path):
    p = _script(tmp_path, """
        import time
        time.sleep(60)
    """)
    c = McpClient("hang", sys.executable, [str(p)])
    t0 = time.monotonic()
    with pytest.raises(McpClientError, match="timed out"):
        c.start(timeout=1.0)
    assert time.monotonic() - t0 < 10
    assert c._process is not None
    c._process.wait(timeout=5)
    assert c._process.poll() is not None


def test_real_server_that_logs_before_replying(tmp_path):
    p = _script(tmp_path, """
        import json, sys
        for line in sys.stdin:
            m = json.loads(line)
            if "id" not in m:
                continue
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}), flush=True)
            if m["method"] == "initialize":
                r = {}
            elif m["method"] == "tools/list":
                r = {"tools": [{"name": "t", "inputSchema": {}}]}
            else:
                r = {"content": [{"type": "text", "text": "ran " + m["params"]["name"]}]}
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
    """)
    c = McpClient("real", sys.executable, [str(p)])
    try:
        c.start(timeout=10)
        assert c.call_tool("t", {}, timeout=10) == "ran t"
        assert c.call_tool("t", {}, timeout=10) == "ran t"
    finally:
        c.close()


def test_call_tool_timeout_is_an_error_string_and_next_call_recovers(tmp_path):
    p = _script(tmp_path, """
        import json, sys, time
        for line in sys.stdin:
            m = json.loads(line)
            if "id" not in m:
                continue
            if m["method"] == "tools/call" and m["params"]["name"] == "slow":
                time.sleep(2)
            r = {"tools": []} if m["method"] == "tools/list" else ({} if m["method"] == "initialize" else {"content": [{"type": "text", "text": m["params"]["name"]}]})
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
    """)
    c = McpClient("slow", sys.executable, [str(p)])
    try:
        c.start(timeout=10)
        assert c.call_tool("slow", {}, timeout=0.5).startswith("ERROR:")
        assert c.call_tool("fast", {}, timeout=10) == "fast"
    finally:
        c.close()


def test_build_reuses_started_servers_and_close_all_stops_them(monkeypatch):
    mcp_client.close_all_external_mcp()
    made: list = []

    class Fake:
        def __init__(self, name, command, args=None, env=None, **kw):
            self.name, self.tools, self.closed = name, [{"name": "t"}], False
            made.append(self)

        def start(self, timeout=15.0):
            pass

        def close(self):
            self.closed = True

        def alive(self):
            return not self.closed

    monkeypatch.setattr(mcp_client, "McpClient", Fake)
    monkeypatch.setattr(mcp_client, "load_external_mcp_servers", lambda path=None: {"s": {"command": "c"}})
    a, ca = build_external_mcp_subagent()
    b, cb = build_external_mcp_subagent()
    assert len(made) == 1 and a is b and ca == cb
    mcp_client.close_all_external_mcp()
    assert made[0].closed
    build_external_mcp_subagent()
    assert len(made) == 2
    mcp_client.close_all_external_mcp()
