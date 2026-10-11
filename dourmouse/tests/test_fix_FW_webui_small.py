"""FW: small webui.py fixes (A-18, A-19, A-22)."""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import config as config_module
from dourmouse.dispatch import DispatchRegistry
from dourmouse.webui import _login_cookie_ok, _login_cookie_value, run_server


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "user_env_path", lambda: tmp_path / "dm" / ".env")
    monkeypatch.setattr(config_module, "user_config_dir", lambda: tmp_path / "dm")
    monkeypatch.setattr(config_module, "ollama_available", lambda **_: False)
    monkeypatch.setattr(config_module, "omniroute_available", lambda **_: False)
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    for name in ("NVIDIA_API_KEY", "NVIDIA_MODEL", "DOURMOUSE_MODEL_ORCHESTRATOR",
                 "OLLAMA_MODEL", "OMNIROUTE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    srv = run_server(DispatchRegistry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _req(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw or b"{}")


# -- A-18 ---------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["v1.abc.x.y", "v1..x.y", "v1.12a.x.y", "v1.-5.x.y", "v1.1.2.3.4", "v1.٢.n.abc"])
def test_malformed_login_cookie_expiry_is_false_not_an_exception(bad):
    assert _login_cookie_ok("t" * 40, bad) is False


def test_valid_login_cookie_still_accepted():
    token = "t" * 40
    assert _login_cookie_ok(token, _login_cookie_value(token)) is True


# -- A-19 ---------------------------------------------------------------- #

def test_get_security_report_fresh_is_read_only(server, monkeypatch):
    from dourmouse.security import report as report_mod

    calls = []
    monkeypatch.setattr(report_mod, "build_report", lambda *a, **k: calls.append("build") or {})
    monkeypatch.setattr(report_mod, "save_report", lambda *a, **k: calls.append("save"))
    monkeypatch.setattr(report_mod, "latest_saved", lambda: {"saved": True})
    _, port = server
    status, body = _req(port, "GET", "/api/security/report?fresh=1")
    assert status == 200 and body["report"] == {"saved": True}
    assert calls == []


def test_fresh_report_is_a_post_action(server, monkeypatch):
    """The supported way to build one (owner-only POST /api/security/*)."""
    from dourmouse import request_guard

    assert request_guard.is_owner_route("POST", "/api/security/action")


# -- A-22 ---------------------------------------------------------------- #

def test_orchestrator_model_follows_the_active_backend_not_a_stray_nvidia_key(server, monkeypatch):
    srv, port = server
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-unused")
    srv.config = config_module.OllamaConfig(model="ollama-model-x")
    status, body = _req(port, "GET", "/api/settings/orchestrator-model")
    assert status == 200
    assert body["current"] == srv.config.model_for_agent("orchestrator")
    assert body["current"] != config_module.NVIDIA_DEFAULT_MODEL
    assert body["source"] == "default_active_backend"


def test_orchestrator_model_nvidia_default_kept_when_no_backend_is_active(server, monkeypatch):
    _, port = server
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake")
    status, body = _req(port, "GET", "/api/settings/orchestrator-model")
    assert body["source"] == "default_nvidia"
    assert body["current"] == config_module.NVIDIA_DEFAULT_MODEL


# -- A-17 ---------------------------------------------------------------- #

def test_events_stream_survives_the_idle_socket_timeout(server, monkeypatch):
    """A client that sends nothing after its request must keep receiving
    events past the handler's idle socket timeout; the server keeps the
    stream alive with comment pings instead of dropping it."""
    import time

    import dourmouse.webui as webui_module

    monkeypatch.setattr(webui_module._Handler, "timeout", 1)
    monkeypatch.setattr(webui_module, "_SSE_KEEPALIVE_SECONDS", 0.2)
    srv, port = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    lines: list[bytes] = []
    reader = threading.Thread(
        target=lambda: [lines.append(ln) for ln in iter(resp.readline, b"")], daemon=True)
    reader.start()
    time.sleep(2.6)  # well past the 1 s idle timeout
    assert resp.fp is not None
    srv.events_broadcast.broadcast({"type": "late_alert", "n": 1})
    deadline = time.time() + 3
    while time.time() < deadline and not any(b"late_alert" in ln for ln in lines):
        time.sleep(0.05)
    conn.close()
    reader.join(timeout=2)
    assert any(b"late_alert" in ln for ln in lines), lines
    assert any(ln.startswith(b": ping") for ln in lines), "no keep-alive comment was sent"


def test_events_stream_unregisters_when_the_client_leaves(server, monkeypatch):
    import time

    import dourmouse.webui as webui_module

    monkeypatch.setattr(webui_module, "_SSE_KEEPALIVE_SECONDS", 0.2)
    srv, port = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()  # keep the response alive or its socket closes
    time.sleep(0.3)
    assert len(srv.events_broadcast._clients) == 1
    resp.close()
    conn.close()
    deadline = time.time() + 3
    while time.time() < deadline and srv.events_broadcast._clients:
        time.sleep(0.05)
    assert srv.events_broadcast._clients == []


# -- A-16 ---------------------------------------------------------------- #

class _FakeOpenAI:
    def __init__(self, api_key=None, base_url=None):
        from dourmouse.tests.test_webui import (
            _FakeChat,
            _FakeCompletions,
            _FakeMessage,
            _FakeResponse,
        )

        self.chat = _FakeChat(_FakeCompletions([_FakeResponse(_FakeMessage(content="via freellmapi"))]))


@pytest.fixture
def chat_server(monkeypatch, tmp_path):
    from dourmouse.tests.test_webui import _echo_registry

    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("FREELLMAPI_API_KEY", "k")
    monkeypatch.setenv("FREELLMAPI_BASE_URL", "http://localhost:3001/v1")
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    srv = run_server(_echo_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _chat(port, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/chat", body=json.dumps(body), headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_freellmapi_slash_command_early_return_leaves_the_session_untouched(chat_server, monkeypatch):
    import dourmouse.webui as webui_module

    monkeypatch.setattr(
        webui_module._Handler, "_handle_slash_chat",
        lambda self, prompt, slash, stream, sink: sink({"type": "done", "final_text": "x"}))
    srv, port = chat_server
    original_client, original_config = srv.session.client, srv.session.config
    status, _ = _chat(port, {"prompt": "/claude hello", "force_backend": "freellmapi"})
    assert status == 200
    assert srv.session.client is original_client
    assert srv.session.config is original_config


def test_freellmapi_swap_happens_only_under_the_session_lock(chat_server):
    """While another turn holds the session lock, a freellmapi request must
    not have replaced the shared client yet."""
    import time

    srv, port = chat_server
    original_client = srv.session.client
    seen = {}
    done = threading.Event()

    def _request():
        seen["resp"] = _chat(port, {"prompt": "hi", "force_backend": "freellmapi"})
        done.set()

    srv.session_lock.acquire()
    worker = threading.Thread(target=_request, daemon=True)
    worker.start()
    try:
        time.sleep(0.8)  # the request has arrived and is waiting on the lock
        assert not done.is_set()
        assert srv.session.client is original_client
    finally:
        srv.session_lock.release()
    assert done.wait(10)
    assert seen["resp"][0] == 200
    assert srv.session.client is original_client


# -- A-23 ---------------------------------------------------------------- #

def test_unattended_gate_declines_at_once_instead_of_waiting_for_the_timeout(monkeypatch):
    """Hands-free has no window to show an approval card. The gate must
    answer 'declined' immediately rather than block 300 s under the lock."""
    import time

    import dourmouse.webui as webui_module

    monkeypatch.setattr(webui_module, "_CONFIRM_TIMEOUT_SECONDS", 30.0)
    gate = webui_module.WebConfirmationGate(lambda _e: None)
    gate.set_emit(webui_module._decline_confirmations(gate))
    start = time.monotonic()
    assert gate("Send the email to Bob?") is False
    assert time.monotonic() - start < 2.0
    assert gate.pending_items() == []


def test_unattended_decline_ignores_other_events():
    import dourmouse.webui as webui_module

    gate = webui_module.WebConfirmationGate(lambda _e: None)
    emit = webui_module._decline_confirmations(gate)
    emit({"type": "assistant_text", "text": "hi"})  # must not raise or resolve anything
    assert gate.pending_items() == []
