"""FIX-W2 H-FD2-6: POST /api/tv-webhook on the main server is closed unless TV_WEBHOOK_SECRET is set, the same
rule the standalone listener enforces at start-up. A tunnel that forwards to loopback would otherwise let
anyone on the internet inject signals into the paper log and the agent bus."""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import tradingview_ops
from dourmouse.dispatch import DispatchRegistry
from dourmouse.webui import run_server


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("TV_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("TV_WEBHOOK_ALLOW_OPEN", raising=False)
    srv = run_server(DispatchRegistry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _post(port, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/tv-webhook", body=json.dumps(body),
                 headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


def _signals(tmp_path):
    p = tradingview_ops._signals_path()
    return p.read_text().splitlines() if p.exists() else []


def test_with_no_secret_configured_the_route_refuses_and_records_nothing(server, tmp_path):
    status, data = _post(server, {"ticker": "EURUSD", "side": "buy"})
    assert status == 403 and data["ok"] is False
    assert "TV_WEBHOOK_SECRET" in data["error"]
    assert _signals(tmp_path) == []


def test_with_a_secret_the_right_one_is_accepted(server, tmp_path, monkeypatch):
    monkeypatch.setenv("TV_WEBHOOK_SECRET", "s3cret")
    status, data = _post(server, {"ticker": "EURUSD", "side": "buy", "secret": "s3cret"})
    assert status == 200 and data["ok"] is True
    assert len(_signals(tmp_path)) == 1


def test_with_a_secret_a_wrong_one_is_still_refused(server, tmp_path, monkeypatch):
    monkeypatch.setenv("TV_WEBHOOK_SECRET", "s3cret")
    status, data = _post(server, {"ticker": "EURUSD", "side": "buy", "secret": "nope"})
    assert status == 400 and data["error"] == "secret mismatch"
    assert _signals(tmp_path) == []


def test_an_explicit_open_webhook_flag_restores_the_old_behaviour(server, tmp_path, monkeypatch):
    monkeypatch.setenv("TV_WEBHOOK_ALLOW_OPEN", "1")
    status, data = _post(server, {"ticker": "EURUSD", "side": "buy"})
    assert status == 200 and data["ok"] is True


def test_the_open_flag_must_be_exactly_one(server, monkeypatch):
    monkeypatch.setenv("TV_WEBHOOK_ALLOW_OPEN", "true")
    assert _post(server, {"ticker": "EURUSD"})[0] == 403
