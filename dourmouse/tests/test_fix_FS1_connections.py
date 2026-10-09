"""FS1 fixes in dourmouse/connections.py: P3-14 (FREEBUFF API line read a
key nothing sets) and P3-15 (a bad memory-URL port crashed the report)."""

from __future__ import annotations

import dourmouse.connections as conn
from dourmouse.tests.test_connections import no_real_probes  # noqa: F401 - autouse fixture


def test_freebuff_api_line_follows_ok(monkeypatch):
    monkeypatch.setattr(conn, "check_connections", lambda: {})
    monkeypatch.setattr(conn, "freebuff_status", lambda: {"app_running": True, "ok": True, "detail": "connected"})
    assert "FREEBUFF API: ready" in conn.format_connections()
    monkeypatch.setattr(conn, "freebuff_status", lambda: {"app_running": True, "ok": False, "detail": "x"})
    assert "FREEBUFF API: app running · no authed account" in conn.format_connections()
    monkeypatch.setattr(conn, "freebuff_status", lambda: {"app_running": False, "ok": False, "detail": "x"})
    assert "FREEBUFF API: not available (app not running)" in conn.format_connections()


def test_bad_memory_url_port_is_reported_not_raised(monkeypatch):
    for bad in ("host:abc", "http://h:99999"):
        monkeypatch.setenv("DOURMOUSE_MEMORY_REMOTE_URL", bad)
        report = conn.check_connections()
        assert report["memory"]["ok"] is False
        assert "not a valid" in report["memory"]["detail"]
