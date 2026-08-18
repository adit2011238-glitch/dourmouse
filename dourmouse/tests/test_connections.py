"""Connection-status tests (v5.3) — dourmouse/connections.py + /api/connections.

The report is deterministic (Rule 2.8) and honest (Rule 2.2): every probe is
monkeypatched here so the suite never touches sockets, subprocesses, or the
real Gmail module. A missing credential is reported as not configured, never
assumed present.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

import dourmouse.connections as conn


@pytest.fixture(autouse=True)
def no_real_probes(monkeypatch):
    """Hermetic: no sockets, no subprocesses, no real auth files."""
    monkeypatch.setattr(conn, "_tcp_reachable", lambda host, port, timeout=0.6: False)
    monkeypatch.setattr(conn, "_cli_version", lambda name: None)
    monkeypatch.setattr(conn, "_codex_auth_mode", lambda: "none")
    monkeypatch.setattr(conn, "_gmail_status", lambda: {"ok": "missing", "detail": "test"})
    for name in (
        "NVIDIA_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ATLAS_REPO_PATH",
        "ATLAS_VENV_PATH",
        "FREEBUFF_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


class TestReportShape:
    def test_all_services_present(self):
        report = conn.check_connections()
        for key in (
            "ollama",
            "nvidia",
            "claude",
            "codex",
            "gmail",
            "freebuff",
            "slack",
            "alpaca",
            "atlas",
        ):
            assert key in report, f"missing service: {key}"
            item = report[key]
            assert set(item) == {"ok", "detail", "hint"}, f"bad shape for {key}"
            assert isinstance(item["ok"], bool)

    def test_everything_off_when_nothing_configured(self):
        report = conn.check_connections()
        for key in ("ollama", "nvidia", "claude", "codex", "gmail", "slack", "alpaca"):
            assert report[key]["ok"] is False, f"{key} should be off"


class TestEnvGates:
    def test_nvidia_tracks_env(self, monkeypatch):
        assert conn.check_connections()["nvidia"]["ok"] is False
        monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
        assert conn.check_connections()["nvidia"]["ok"] is True
        assert "present" in conn.check_connections()["nvidia"]["detail"]
        # the key itself is never echoed back
        assert "nv-test" not in json.dumps(conn.check_connections())

    def test_slack_and_alpaca_track_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("APCA_API_KEY_ID", "ak")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "sk")
        report = conn.check_connections()
        assert report["slack"]["ok"] is True
        assert report["alpaca"]["ok"] is True

    def test_atlas_requires_real_repo_dir(self, monkeypatch, tmp_path):
        repo = tmp_path / "atlas"
        repo.mkdir()
        monkeypatch.setenv("ATLAS_REPO_PATH", str(repo))
        monkeypatch.setenv("ATLAS_VENV_PATH", "/tmp/nope-venv")
        assert conn.check_connections()["atlas"]["ok"] is True
        monkeypatch.setenv("ATLAS_REPO_PATH", str(tmp_path / "missing"))
        assert conn.check_connections()["atlas"]["ok"] is False


class TestClaudeCodexDiscovery:
    def test_claude_requires_cli_and_confirmed_signin(self, monkeypatch):
        """Presence alone used to report ok=True — and the CLI would then
        answer every request with "Not logged in · Please run /login".
        A green tick the operator only discovers is wrong mid-task is worse
        than an amber one, so an unconfirmed sign-in is not ok."""
        monkeypatch.setattr(conn, "_cli_version", lambda name: "2.1.220" if name == "claude" else None)

        monkeypatch.setattr(conn, "_claude_signin", lambda: "unknown")
        report = conn.check_connections()
        assert report["claude"]["ok"] is False
        assert "2.1.220" in report["claude"]["detail"]
        assert "not verified" in report["claude"]["detail"]
        assert "/login" in report["claude"]["hint"]

        monkeypatch.setattr(conn, "_claude_signin", lambda: "yes")
        report = conn.check_connections()
        assert report["claude"]["ok"] is True
        assert "signed in" in report["claude"]["detail"]

    def test_claude_missing_cli_says_so(self, monkeypatch):
        monkeypatch.setattr(conn, "_cli_version", lambda name: None)
        report = conn.check_connections()
        assert report["claude"]["ok"] is False
        assert "not on PATH" in report["claude"]["detail"]
        assert "npm i -g" in report["claude"]["hint"]

    def test_claude_signin_never_guesses_from_projects_dir(self, tmp_path, monkeypatch):
        """A populated ~/.claude/projects is NOT evidence of a session — an
        earlier fix used it and went straight back to a false green."""
        home = tmp_path / ".claude"
        (home / "projects" / "some-project").mkdir(parents=True)
        monkeypatch.setattr(conn.Path, "expanduser", lambda self: home)
        assert conn._claude_signin() == "unknown"

    def test_codex_requires_cli_and_login(self, monkeypatch):
        monkeypatch.setattr(
            conn, "_cli_version", lambda name: "0.144.6" if name == "codex" else None
        )
        # CLI present but no auth -> not ok
        report = conn.check_connections()
        assert report["codex"]["ok"] is False
        # CLI + chatgpt login -> ok
        monkeypatch.setattr(conn, "_codex_auth_mode", lambda: "chatgpt")
        report = conn.check_connections()
        assert report["codex"]["ok"] is True
        assert "chatgpt" in report["codex"]["detail"]


class TestFreebuff:
    def test_app_not_running(self):
        report = conn.check_connections()
        assert report["freebuff"]["ok"] is False
        assert "not running" in report["freebuff"]["detail"]

    def test_app_running_not_authed_not_usable(self, monkeypatch):
        """v5.5: ok means USABLE — the app's own renderer API (51819, no
        token) must answer auth/status as authed. A running app that is
        not authed is honestly ○ with the fix hint."""
        monkeypatch.setattr(
            conn, "_tcp_reachable",
            lambda host, port, timeout=0.6: port == conn._FREEBUFF_UI_PORT,
        )
        monkeypatch.setattr(
            conn, "freebuff_status",
            lambda: {"ok": False, "detail": "app running · not authed", "hint": "start the Freebuff app and sign in"},
        )
        report = conn.check_connections()
        assert report["freebuff"]["ok"] is False
        assert "not authed" in report["freebuff"]["detail"]

    def test_app_authed_ready(self, monkeypatch):
        """v5.5: an authed 51819 API is USABLE with NO token (the 51820
        bridge's per-launch random token is the debugger API, not the read
        path — the old token check was misleading)."""
        monkeypatch.setattr(
            conn, "_tcp_reachable",
            lambda host, port, timeout=0.6: port == conn._FREEBUFF_UI_PORT,
        )
        monkeypatch.setattr(
            conn, "freebuff_status",
            lambda: {"ok": True, "detail": "app running · user", "account": {"email": "user@example.com"}},
        )
        report = conn.check_connections()
        assert report["freebuff"]["ok"] is True
        assert "app running" in report["freebuff"]["detail"]
        # the account email never leaks into the report
        assert "user@example.com" not in json.dumps(report)

    def test_ollama_probe_used(self, monkeypatch):
        seen: list[tuple] = []
        def fake(host, port, timeout=0.6):
            seen.append((host, port))
            return False
        monkeypatch.setattr(conn, "_tcp_reachable", fake)
        conn.check_connections()
        assert any(port == 11434 for _host, port in seen)


class TestFormat:
    def test_format_mentions_services_and_fixes(self):
        text = conn.format_connections()
        for name in ("ollama", "nvidia", "claude", "codex", "gmail", "freebuff", "atlas"):
            assert name in text, f"{name} missing from report text"
        assert "FREEBUFF APP" in text


# --------------------------------------------------------------------------- #
# Web endpoint
# --------------------------------------------------------------------------- #

@pytest.fixture
def server(monkeypatch, tmp_path):
    from dourmouse.tests.test_webui import _echo_registry
    from dourmouse.webui import run_server

    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(_echo_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(port, path):
    conn_ = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn_.request("GET", path)
    resp = conn_.getresponse()
    body = resp.read().decode()
    conn_.close()
    return resp.status, body


class TestConnectionsEndpoint:
    def test_api_connections_returns_report(self, server):
        _, port = server
        status, body = _get(port, "/api/connections")
        assert status == 200
        report = json.loads(body)
        assert "claude" in report and "codex" in report and "freebuff" in report

    def test_setup_includes_new_entries(self, server):
        _, port = server
        status, body = _get(port, "/api/setup")
        assert status == 200
        items = json.loads(body)["items"]
        for key in ("codex_cli", "freebuff"):
            assert key in items, f"setup item missing: {key}"
            assert "configured" in items[key]
            assert "hint" in items[key]
