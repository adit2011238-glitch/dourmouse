"""v5.0 feature tests — upload, setup panel, Codex backend, Gmail, fast dispatch.

Covers the new user-facing capabilities: the sandboxed /api/upload flow, the
/api/setup capability checklist, the Codex coding backend resolution, the
Gmail (IMAP/SMTP) module's honest NOT CONFIGURED contract, the fast-dispatch
orchestrator model default, and the read_upload tool's sandbox.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse.tests.test_webui import _echo_registry


@pytest.fixture
def server(monkeypatch, tmp_path):
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
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


def _post(port, path, body: bytes, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


# --------------------------------------------------------------------------- #
# A2 — file upload
# --------------------------------------------------------------------------- #

class TestUpload:
    def test_upload_round_trip(self, server):
        _, port = server
        status, body = _post(
            port,
            "/api/upload?name=notes.txt",
            b"hello world upload",
            {"Content-Type": "application/octet-stream"},
        )
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True and data["name"] == "notes.txt"
        assert data["size"] == len(b"hello world upload")
        # listed
        status, body = _get(port, "/api/files")
        assert status == 200
        files = json.loads(body)["files"]
        assert any(f["name"] == "notes.txt" for f in files)
        # served back
        status, body = _get(port, "/uploads/notes.txt")
        assert status == 200 and body == "hello world upload"

    def test_upload_rejects_path_escape(self, server):
        _, port = server
        status, body = _post(
            port, "/api/upload?name=..%2F..%2Fevil.txt", b"x"
        )
        assert status == 400
        assert b"filename must be" in body or b"bad" in body

    def test_upload_rejects_bad_name(self, server):
        _, port = server
        status, _body = _post(port, "/api/upload?name=a%2Fb.txt", b"x")
        assert status == 400

    def test_upload_requires_body(self, server):
        _, port = server
        status, body = _post(port, "/api/upload?name=empty.txt", b"")
        assert status == 400
        assert b"empty" in body

    def test_upload_path_traversal_in_get_404(self, server):
        _, port = server
        status, _body = _get(port, "/uploads/..%2F..%2Fetc%2Fpasswd")
        assert status in (400, 404)


# --------------------------------------------------------------------------- #
# A7 — setup status panel
# --------------------------------------------------------------------------- #

class TestSetupPanel:
    def test_setup_returns_capability_checklist(self, server):
        _, port = server
        status, body = _get(port, "/api/setup")
        assert status == 200
        items = json.loads(body)["items"]
        for key in (
            "llm_backend",
            "voice",
            "codex",
            "deepseek",
            "claude",
            "gmail",
            "upload",
            "memory",
            "live",
        ):
            assert key in items, f"setup item missing: {key}"
            assert "configured" in items[key]
            assert "hint" in items[key]


# --------------------------------------------------------------------------- #
# A5 — Codex backend
# --------------------------------------------------------------------------- #

class TestCodexBackend:
    def test_codex_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from dourmouse.code_backends import load_backend

        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            load_backend("codex")

    def test_codex_resolves_with_key(self, monkeypatch):
        monkeypatch.setenv("CODEX_API_KEY", "sk-test-1234567890abcdef")
        from dourmouse.code_backends import load_backend

        base, key, model = load_backend("codex")
        assert base == "https://api.openai.com/v1"
        assert key == "sk-test-1234567890abcdef"
        assert model == "gpt-5-codex"

    def test_codex_accepts_openai_key(self, monkeypatch):
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-alt-1234567890abcdef")
        from dourmouse.code_backends import load_backend

        _base, key, _model = load_backend("codex")
        assert key == "sk-alt-1234567890abcdef"


# --------------------------------------------------------------------------- #
# A6 — Gmail module (honest NOT CONFIGURED)
# --------------------------------------------------------------------------- #

class TestGoogleServices:
    def test_not_configured_without_env(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_GMAIL_USER", raising=False)
        monkeypatch.delenv("GOOGLE_GMAIL_APP_PASSWORD", raising=False)
        from dourmouse import google_services as gs

        # Hermetic: pin the local_secrets fallback to empty too, so the test
        # never depends on what a user has typed into that gitignored file.
        monkeypatch.setattr(gs, "_local_secrets", dict)
        assert gs.gmail_configured() is False
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            gs.gmail_search("test")
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            gs.gmail_send("a@b.com", "hi", "body")

    def test_configured_when_env_present(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "me@gmail.com")
        monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "abcdefghijklmnop")
        from dourmouse import google_services as gs

        assert gs.gmail_configured() is True
        assert gs.status()["configured"] is True
        assert "(via env)" in gs.status()["detail"]

    def test_configured_via_local_secrets(self, monkeypatch):
        """v5.1: single-user source-tree credentials work with NO env vars."""
        monkeypatch.delenv("GOOGLE_GMAIL_USER", raising=False)
        monkeypatch.delenv("GOOGLE_GMAIL_APP_PASSWORD", raising=False)
        from dourmouse import google_services as gs

        monkeypatch.setattr(
            gs, "_local_secrets", lambda: {"user": "local@gmail.com", "password": "1234567890abcdef"}
        )
        assert gs.gmail_configured() is True
        assert "local@gmail.com" in gs.status()["detail"]
        assert "(via local_secrets.py)" in gs.status()["detail"]

    def test_env_wins_over_local_secrets(self, monkeypatch):
        """v5.1: env vars always beat the local_secrets fallback."""
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "env@gmail.com")
        monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "abcdefghijklmnop")
        from dourmouse import google_services as gs

        monkeypatch.setattr(
            gs, "_local_secrets", lambda: {"user": "local@gmail.com", "password": "deadbeef12345678"}
        )
        assert gs._user() == "env@gmail.com"
        assert gs._app_password() == "abcdefghijklmnop"

    def test_missing_local_secrets_module_is_empty(self, monkeypatch):
        """v5.1: an absent/broken local_secrets file degrades to {}."""
        from dourmouse import google_services as gs

        monkeypatch.setattr(gs, "_local_secrets", dict)
        assert gs._user() == ""
        assert gs._app_password() == ""
        assert gs.gmail_configured() is False

    def test_local_secrets_absent_module_guard(self, monkeypatch):
        """v5.1: the REAL import guard — a machine without the gitignored
        file (fresh checkout / any-device install) must degrade to NOT
        CONFIGURED, never crash gmail_configured (reviewer-caught)."""
        import sys

        monkeypatch.delenv("GOOGLE_GMAIL_USER", raising=False)
        monkeypatch.delenv("GOOGLE_GMAIL_APP_PASSWORD", raising=False)
        # Simulate the file being absent: block the module import entirely.
        monkeypatch.setitem(sys.modules, "dourmouse.local_secrets", None)
        from dourmouse import google_services as gs

        assert gs._local_secrets() == {}
        assert gs.gmail_configured() is False

    def test_send_validates_input_before_network(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "me@gmail.com")
        monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "abcdefghijklmnop")
        from dourmouse import google_services as gs

        assert "ERROR" in gs.gmail_send("", "subj", "body")
        assert "ERROR" in gs.gmail_send("a@b.com", "", "body")

    def test_calendar_honest_not_configured(self):
        from dourmouse import google_services as gs

        assert "NOT CONFIGURED" in gs.calendar_events()


# --------------------------------------------------------------------------- #
# A4 — fast dispatch model
# --------------------------------------------------------------------------- #

class TestFastDispatchModel:
    def test_orchestrator_defaults_to_fast_model(self, monkeypatch):
        """v5.2: the fast-dispatch default is qwen2.5:7b (answers directly;
        qwen3:4b ignored think=False and rambled — measured live)."""
        monkeypatch.delenv("DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR", raising=False)
        from dourmouse.config import load_ollama_config

        cfg = load_ollama_config()
        assert cfg.model_for_agent("orchestrator") == "qwen2.5:7b"
        # heavy agents stay on the big model
        assert cfg.model_for_agent("research_info") == cfg.model
        # unknown agents fall back to the default model
        assert cfg.model_for_agent("nope") == cfg.model

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR", "qwen3:8b")
        from dourmouse.config import load_ollama_config

        cfg = load_ollama_config()
        assert cfg.model_for_agent("orchestrator") == "qwen3:8b"


# --------------------------------------------------------------------------- #
# A2 — read_upload tool (sandbox)
# --------------------------------------------------------------------------- #

class TestReadUploadTool:
    def test_upload_readable_by_system_agent(self, monkeypatch, tmp_path):
        from dourmouse.system_access import _uploads_root, build_system_subagent

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        root = _uploads_root()
        (root / "data.csv").write_text("a,b,c\n1,2,3\n")
        sub = build_system_subagent()
        spec = next(t for t in sub.tools if t.name == "read_upload")
        out = spec.handler({"name": "data.csv"})
        assert "a,b,c" in out

    def test_upload_rejects_path_escape(self, monkeypatch, tmp_path):
        from dourmouse.system_access import build_system_subagent

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        sub = build_system_subagent()
        spec = next(t for t in sub.tools if t.name == "read_upload")
        # A path-like name is refused before it can resolve anywhere (the
        # first guard rejects any path separators — honest refusal either way).
        out = spec.handler({"name": "../.env"})
        assert "ERROR" in out or "REFUSED" in out
        assert "ERROR" in spec.handler({"name": "missing.txt"})
