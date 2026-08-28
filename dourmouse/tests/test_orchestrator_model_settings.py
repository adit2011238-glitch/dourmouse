"""world-monitor-expansion: /api/settings/orchestrator-model (webui.py) +
its config.py persistence layer, exercised over real HTTP against a real
ThreadingHTTPServer (Integration Rule 7.3 discipline) — same pattern as
test_webui.py / test_v50_features.py.

Hermetic: DOURMOUSE_ORCHESTRATOR_MODEL is stored under user_env_path(),
which every test here redirects to a tmp_path via monkeypatch — nothing
touches the real per-user Dourmouse config directory. The Ollama/OmniRoute
probes are monkeypatched too, so nothing here depends on (or is slowed by)
what's actually running on this machine.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import config as config_module
from dourmouse.tests.test_webui import _echo_registry
from dourmouse.webui import run_server


def _isolate_user_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "user_env_path", lambda: tmp_path / "dourmouse" / ".env")
    monkeypatch.setattr(config_module, "user_config_dir", lambda: tmp_path / "dourmouse")


@pytest.fixture
def server(monkeypatch, tmp_path):
    _isolate_user_config(monkeypatch, tmp_path)
    # Deterministic, no real network: neither local backend answers unless
    # a test explicitly says otherwise.
    monkeypatch.setattr(config_module, "ollama_available", lambda **_: False)
    monkeypatch.setattr(config_module, "omniroute_available", lambda **_: False)
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    for name in ("NVIDIA_API_KEY", "NVIDIA_MODEL", "DOURMOUSE_MODEL_ORCHESTRATOR"):
        monkeypatch.delenv(name, raising=False)
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
    return resp.status, json.loads(body)


def _post(port, path, payload: dict):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(payload).encode()
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, json.loads(data)


class TestOrchestratorModelGet:
    def test_no_key_no_persisted_setting_reports_honest_defaults(self, server, monkeypatch):
        srv, port = server
        status, data = _get(port, "/api/settings/orchestrator-model")
        assert status == 200
        assert data["source"] == "default_active_backend"
        assert isinstance(data["backends"], list)
        names = {b["name"] for b in data["backends"]}
        # The 3 core system backends + the coding-family menu documented
        # in code_backends.py / cn_backends.py.
        assert names == {
            "nvidia", "ollama", "omniroute",
            "deepseek", "qwen", "glm", "kimi", "codex", "claude",
        }
        nvidia_entry = next(b for b in data["backends"] if b["name"] == "nvidia")
        assert nvidia_entry["configured"] is False
        assert "not set" in nvidia_entry["detail"]

    def test_nvidia_key_present_defaults_current_to_nvidia(self, server, monkeypatch):
        srv, port = server
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        status, data = _get(port, "/api/settings/orchestrator-model")
        assert status == 200
        assert data["source"] == "default_nvidia"
        assert data["current"] == config_module.NVIDIA_DEFAULT_MODEL
        nvidia_entry = next(b for b in data["backends"] if b["name"] == "nvidia")
        assert nvidia_entry["configured"] is True

    def test_persisted_setting_reported_as_current(self, server, monkeypatch):
        srv, port = server
        config_module.save_orchestrator_model_setting("nvidia/my-picked-model")
        status, data = _get(port, "/api/settings/orchestrator-model")
        assert status == 200
        assert data["current"] == "nvidia/my-picked-model"
        assert data["source"] == "persisted"
        assert data["persisted"] == "nvidia/my-picked-model"

    def test_env_override_wins_over_persisted_setting(self, server, monkeypatch):
        srv, port = server
        config_module.save_orchestrator_model_setting("nvidia/persisted")
        monkeypatch.setenv("DOURMOUSE_MODEL_ORCHESTRATOR", "nvidia/env-wins")
        status, data = _get(port, "/api/settings/orchestrator-model")
        assert status == 200
        assert data["current"] == "nvidia/env-wins"
        assert data["source"] == "env_override"


class TestOrchestratorModelPost:
    def test_post_with_explicit_model_persists_it(self, server):
        srv, port = server
        status, data = _post(port, "/api/settings/orchestrator-model", {"model": "custom/model-id"})
        assert status == 200
        assert data["ok"] is True
        assert data["model"] == "custom/model-id"
        assert config_module.orchestrator_model_setting() == "custom/model-id"

    def test_post_with_backend_name_resolves_and_persists_its_model(self, server, monkeypatch):
        srv, port = server
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("NVIDIA_MODEL", "nvidia/chosen-by-backend")
        status, data = _post(port, "/api/settings/orchestrator-model", {"backend": "nvidia"})
        assert status == 200
        assert data["ok"] is True
        assert data["model"] == "nvidia/chosen-by-backend"
        assert data["configured"] is True
        assert config_module.orchestrator_model_setting() == "nvidia/chosen-by-backend"

    def test_post_with_unconfigured_backend_still_saves_honestly(self, server):
        """Saving is allowed even when the backend isn't configured yet —
        the response says so honestly via 'configured' rather than
        blocking the save (the user may be about to add the key)."""
        srv, port = server
        status, data = _post(port, "/api/settings/orchestrator-model", {"backend": "ollama"})
        assert status == 200
        assert data["ok"] is True
        assert data["configured"] is False
        assert config_module.orchestrator_model_setting() == config_module.OLLAMA_DEFAULT_MODEL

    def test_post_with_neither_field_is_rejected(self, server):
        srv, port = server
        status, data = _post(port, "/api/settings/orchestrator-model", {})
        assert status == 200
        assert data["ok"] is False

    def test_post_with_unknown_backend_is_rejected(self, server):
        srv, port = server
        status, data = _post(port, "/api/settings/orchestrator-model", {"backend": "not-a-real-backend"})
        assert status == 200
        assert data["ok"] is False
        assert "unknown backend" in data["detail"]

    def test_post_then_get_reflects_the_new_choice(self, server):
        srv, port = server
        _post(port, "/api/settings/orchestrator-model", {"model": "nvidia/round-trip"})
        status, data = _get(port, "/api/settings/orchestrator-model")
        assert status == 200
        assert data["current"] == "nvidia/round-trip"
        assert data["source"] == "persisted"
