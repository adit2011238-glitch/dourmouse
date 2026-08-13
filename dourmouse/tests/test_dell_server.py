"""Hermetic tests for the Dell compute-node server (dell/dell_server.py).

No Ollama and no network: the server's ``_ollama_post`` / ``_get`` seams are
replaced with fakes, and the app is exercised through FastAPI's in-process
TestClient. This is the exact file that ships to the Dell (LAN-only).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_DELL = Path(__file__).resolve().parents[2] / "dell" / "dell_server.py"
_spec = importlib.util.spec_from_file_location("dell_server_test", _DELL)
dell = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(dell)


@pytest.fixture
def fake_ollama(monkeypatch):
    """Replace the server's network seams with deterministic fakes."""

    class FakeOllama:
        def __init__(self):
            self.chat_calls = []
            self.reply = "Hello from Qwen3!"
            self.tags_ok = True
            self.fail_chat = False

        def post(self, url, payload, timeout):
            self.chat_calls.append((url, payload))
            if self.fail_chat:
                raise RuntimeError("connection refused")
            return 200, {"message": {"role": "assistant", "content": self.reply}}

        def get(self, url, timeout):
            if not self.tags_ok:
                raise RuntimeError("connection refused")
            return 200, {"models": []}

    fake = FakeOllama()
    monkeypatch.setattr(dell, "_ollama_post", fake.post)
    monkeypatch.setattr(dell, "_get", fake.get)
    return fake


@pytest.fixture
def client():
    return TestClient(dell.app)


@pytest.fixture(autouse=True)
def _pin_env(monkeypatch):
    """The dell module reads env at CALL time — pin it so a leaked
    OLLAMA_MODEL / DOURMOUSE_SERVER_MODEL (from other suites or the project
    .env) can never change what the server reports."""
    monkeypatch.setenv("DOURMOUSE_SERVER_MODEL", "qwen3:1.7b")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("DOURMOUSE_SERVER_API_KEY", raising=False)
    yield


@pytest.fixture
def no_auth():
    yield


class TestLegacyStatus:
    def test_status_shape(self, client, no_auth):
        r = client.get("/status")
        assert r.status_code == 200
        data = r.json()
        assert data["system"] == "DOURMOUSE"
        assert data["status"] == "online"
        assert data["node"] == "Node-01"
        assert data["model"] == "qwen3:1.7b"


class TestV1Status:
    def test_online_with_ollama(self, client, fake_ollama, no_auth):
        r = client.get("/v1/status")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "online"
        assert data["node"] == "Node-01"
        assert data["model"] == "qwen3:1.7b"
        assert data["ollama"] is True
        assert isinstance(data["latency_ms"], int)

    def test_ollama_down_reported_honestly(self, client, fake_ollama, no_auth):
        fake_ollama.tags_ok = False
        r = client.get("/v1/status")
        data = r.json()
        assert data["status"] == "online"  # the NODE is up even if Ollama is not
        assert data["ollama"] is False


class TestV1Generate:
    def test_happy_path(self, client, fake_ollama, no_auth):
        r = client.post("/v1/generate", json={"prompt": "Explain Brownian motion", "system": "You are a mathematical assistant.", "temperature": 0.2})
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["node"] == "Node-01"
        assert data["model"] == "qwen3:1.7b"
        assert data["response"] == "Hello from Qwen3!"
        assert isinstance(data["latency_ms"], int)
        # The system prompt and temperature reached Ollama
        url, payload = fake_ollama.chat_calls[-1]
        assert payload["messages"][0] == {"role": "system", "content": "You are a mathematical assistant."}
        assert payload["options"]["temperature"] == 0.2

    def test_prompt_required(self, client, no_auth):
        r = client.post("/v1/generate", json={"system": "x"})
        assert r.status_code == 400
        assert r.json()["success"] is False

    def test_ollama_down_is_502_not_fabricated(self, client, fake_ollama, no_auth):
        fake_ollama.fail_chat = True
        r = client.post("/v1/generate", json={"prompt": "hi"})
        assert r.status_code == 502
        data = r.json()
        assert data["success"] is False
        assert "failed" in data["error"]

    def test_bad_json(self, client, no_auth):
        r = client.post("/v1/generate", content=b"{not json", headers={"Content-Type": "application/json"})
        assert r.status_code == 400


class TestV1Chat:
    def test_happy_path(self, client, fake_ollama, no_auth):
        r = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "Hello"}]})
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["response"] == "Hello from Qwen3!"

    def test_empty_messages_rejected(self, client, no_auth):
        r = client.post("/v1/chat", json={"messages": []})
        assert r.status_code == 400
        assert r.json()["success"] is False

    def test_system_turn_preserved(self, client, fake_ollama, no_auth):
        client.post("/v1/chat", json={"messages": [{"role": "system", "content": "You are an assistant."}, {"role": "user", "content": "Hello"}]})
        url, payload = fake_ollama.chat_calls[-1]
        assert payload["messages"][0] == {"role": "system", "content": "You are an assistant."}


class TestAuth:
    def test_key_required_when_set(self, client, fake_ollama, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_API_KEY", "sekret")
        assert client.get("/v1/status").status_code == 401
        assert client.get("/v1/status", headers={"X-API-Key": "sekret"}).status_code == 200
        assert client.get("/v1/status", headers={"Authorization": "Bearer sekret"}).status_code == 200
        assert client.post("/v1/generate", json={"prompt": "hi"}).status_code == 401

    def test_no_key_no_auth(self, client, no_auth):
        assert client.get("/v1/status").status_code == 200


class TestLoggingNeverLeaks:
    def test_errors_are_bounded(self, client, fake_ollama, no_auth, caplog):
        fake_ollama.fail_chat = True
        client.post("/v1/chat", json={"messages": [{"role": "user", "content": "secret body"}]})
        for record in caplog.records:
            assert "secret body" not in record.getMessage()
