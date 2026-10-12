"""FIX-W2 P3-35: a Codex database that exists but cannot be read is shown to the owner (CLI line, API
payload, console note), not read as 'Codex is not installed'."""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading

import pytest

from dourmouse import history_import, history_sync_cli
from dourmouse.dispatch import DispatchRegistry
from dourmouse.memory_store import MemoryStore
from dourmouse.webui import run_server


def _broken_codex_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE threads (id TEXT, title TEXT)")  # the columns Codex used to have are gone
    con.commit()
    con.close()
    return path


def test_the_sync_cli_prints_the_codex_error(monkeypatch, capsys, tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    monkeypatch.setattr(history_sync_cli, "MemoryStore", lambda path: store)
    monkeypatch.setattr(history_sync_cli, "default_store_path", lambda: tmp_path / "m.db")
    monkeypatch.setattr(history_sync_cli, "sync_and_import", lambda s: {
        "ok": True, "pulled": 0,
        "claude": {"imported": 0, "scanned": 0},
        "codex": {"imported": 0, "scanned": 0, "configured": True, "error": "could not read Codex threads: no such column"},
    })
    assert history_sync_cli.main() == 0
    out = capsys.readouterr().out
    assert "history sync: ok" in out
    assert "codex" in out.lower() and "could not read Codex threads: no such column" in out


def test_the_sync_cli_prints_the_codex_error_also_when_the_sync_itself_failed(monkeypatch, capsys, tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    monkeypatch.setattr(history_sync_cli, "MemoryStore", lambda path: store)
    monkeypatch.setattr(history_sync_cli, "default_store_path", lambda: tmp_path / "m.db")
    monkeypatch.setattr(history_sync_cli, "sync_and_import", lambda s: {
        "ok": False, "reason": "remote_unreachable",
        "claude": {}, "codex": {"configured": True, "error": "could not open the Codex state database: locked"},
    })
    assert history_sync_cli.main() == 0
    out = capsys.readouterr().out
    assert "remote_unreachable" in out and "could not open the Codex state database: locked" in out


def test_a_clean_sync_prints_no_codex_error_line(monkeypatch, capsys, tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    monkeypatch.setattr(history_sync_cli, "MemoryStore", lambda path: store)
    monkeypatch.setattr(history_sync_cli, "default_store_path", lambda: tmp_path / "m.db")
    monkeypatch.setattr(history_sync_cli, "sync_and_import", lambda s: {
        "ok": True, "pulled": 1, "claude": {"imported": 1, "scanned": 1}, "codex": {"imported": 0, "scanned": 0},
    })
    history_sync_cli.main()
    assert "error" not in capsys.readouterr().out.lower()


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(DispatchRegistry(), port=0, client=None, config=None)
    srv.memory = MemoryStore(tmp_path / "mem.db")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def test_the_import_route_reports_a_codex_error_in_a_top_level_warnings_list(server, tmp_path, monkeypatch):
    db = _broken_codex_db(tmp_path / "state_5.sqlite")
    monkeypatch.setattr(history_import, "_codex_state_db", lambda: db)
    monkeypatch.setattr(history_import, "_claude_projects_root", lambda: tmp_path / "no-claude", raising=False)
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    conn.request("POST", "/api/history/import", body=b"{}",
                 headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server}"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 200 and data["ok"] is True
    assert data["codex"]["error"].startswith("could not read Codex threads")
    assert any("Codex" in w and "could not read" in w for w in data["warnings"])


def test_the_console_note_shows_the_error(tmp_path):
    from pathlib import Path

    html = (Path(__file__).resolve().parents[2] / "ui" / "console.html").read_text(encoding="utf-8")
    start = html.index('const r = await fetch("/api/history/import"')
    block = html[start:start + 1500]
    assert "x.error" in block
