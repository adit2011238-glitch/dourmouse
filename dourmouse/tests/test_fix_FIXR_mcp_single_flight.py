"""FIX-R R-1: concurrent registry builds start the external MCP servers once, not once per thread."""

from __future__ import annotations

import sys
import textwrap
import threading
import time

from dourmouse import mcp_client
from dourmouse.mcp_client import build_external_mcp_subagent


def _run_four(results: list) -> None:
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(build_external_mcp_subagent())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)


def test_concurrent_builds_with_fake_clients_start_one_set(monkeypatch):
    mcp_client.close_all_external_mcp()
    made: list = []
    lock = threading.Lock()

    class Fake:
        def __init__(self, name, command, args=None, env=None, **kw):
            self.name, self.tools, self.closed = name, [{"name": "t"}], False
            with lock:
                made.append(self)

        def start(self, timeout=15.0):
            time.sleep(0.3)

        def close(self):
            self.closed = True

        def alive(self):
            return not self.closed

    monkeypatch.setattr(mcp_client, "McpClient", Fake)
    monkeypatch.setattr(mcp_client, "load_external_mcp_servers", lambda path=None: {"s": {"command": "c"}})
    results: list = []
    _run_four(results)
    try:
        assert len(results) == 4
        assert len(made) == 1, f"{len(made)} server sets were started"
        assert all(r[0] is results[0][0] for r in results)
        assert sum(1 for m in made if not m.closed) == 1
    finally:
        mcp_client.close_all_external_mcp()
    assert all(m.closed for m in made)


def test_concurrent_builds_with_real_server_processes_spawn_one_process(tmp_path, monkeypatch):
    """Real subprocesses and real threads: count the server processes that were actually spawned."""
    mcp_client.close_all_external_mcp()
    spawn_log = tmp_path / "spawns.txt"
    script = tmp_path / "server.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, sys
        with open({str(spawn_log)!r}, "a") as f:
            f.write(str(os.getpid()) + "\\n")
        for line in sys.stdin:
            m = json.loads(line)
            if "id" not in m:
                continue
            if m["method"] == "initialize":
                r = {{}}
            elif m["method"] == "tools/list":
                r = {{"tools": [{{"name": "t", "inputSchema": {{}}}}]}}
            else:
                r = {{"content": [{{"type": "text", "text": "ok"}}]}}
            print(json.dumps({{"jsonrpc": "2.0", "id": m["id"], "result": r}}), flush=True)
    """), encoding="utf-8")
    cfg = {"real": {"command": sys.executable, "args": [str(script)]}}
    monkeypatch.setattr(mcp_client, "load_external_mcp_servers", lambda path=None: cfg)
    results: list = []
    _run_four(results)
    procs = []
    try:
        assert len(results) == 4 and all(r[0] is not None for r in results)
        pids = [int(x) for x in spawn_log.read_text().split()]
        assert len(pids) == 1, f"{len(pids)} server processes were spawned"
        assert all(r[1][0] is results[0][1][0] for r in results)
        procs = [c._process for c in results[0][1] if c._process is not None]
    finally:
        mcp_client.close_all_external_mcp()
    for p in procs:
        p.wait(timeout=10)
        assert p.poll() is not None
