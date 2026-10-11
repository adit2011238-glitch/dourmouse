"""FD2 fixes for tv_webhook_server: P3-62 (open webhook, quoted secret, timing compare, no timeout)."""

from __future__ import annotations

import inspect
import socket
import threading

import pytest

from dourmouse import tradingview_ops
from dourmouse import tv_webhook_server as tv


class _FakeServer:
    created = []

    def __init__(self, addr, handler):
        _FakeServer.created.append(addr)

    def serve_forever(self):
        raise KeyboardInterrupt


@pytest.fixture(autouse=True)
def _no_real_server(monkeypatch):
    _FakeServer.created = []
    monkeypatch.setattr(tv, "DourmouseHTTPServer", _FakeServer)
    monkeypatch.setattr(tv, "_load_env", lambda *a, **k: None)


def test_refuses_to_start_without_a_secret(monkeypatch, capsys):
    monkeypatch.delenv("TV_WEBHOOK_SECRET", raising=False)
    rc = tv.main(["--port", "0"])
    assert rc != 0
    assert _FakeServer.created == []
    assert "TV_WEBHOOK_SECRET" in capsys.readouterr().err


def test_blank_secret_is_also_refused(monkeypatch):
    monkeypatch.setenv("TV_WEBHOOK_SECRET", "   ")
    assert tv.main(["--port", "0"]) != 0
    assert _FakeServer.created == []


def test_starts_with_a_secret(monkeypatch):
    monkeypatch.setenv("TV_WEBHOOK_SECRET", "s3cret")
    assert tv.main(["--port", "0"]) == 0
    assert _FakeServer.created == [("127.0.0.1", 0)]


def test_explicit_open_flag_starts_with_a_loud_warning(monkeypatch, capsys):
    monkeypatch.delenv("TV_WEBHOOK_SECRET", raising=False)
    assert tv.main(["--port", "0", "--allow-open-webhook"]) == 0
    assert "WARNING" in capsys.readouterr().err
    assert _FakeServer.created == [("127.0.0.1", 0)]


def test_load_env_strips_quotes_like_the_main_server(tmp_path, monkeypatch):
    monkeypatch.undo()  # use the real _load_env here
    env = tmp_path / ".env"
    env.write_text('TV_WEBHOOK_SECRET="abc"\nexport FD2_OTHER=\'xyz\'\n# c\n')
    monkeypatch.delenv("TV_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("FD2_OTHER", raising=False)
    tv._load_env(env)
    import os

    assert os.environ["TV_WEBHOOK_SECRET"] == "abc"
    assert os.environ["FD2_OTHER"] == "xyz"
    os.environ.pop("TV_WEBHOOK_SECRET", None)
    os.environ.pop("FD2_OTHER", None)


def test_secret_comparison_is_constant_time_and_safe_for_non_ascii(monkeypatch):
    monkeypatch.setenv("TV_WEBHOOK_SECRET", "abc")
    ok, reason = tradingview_ops.validate_signal({"secret": "abc", "ticker": "AAPL"})
    assert ok is True
    ok, reason = tradingview_ops.validate_signal({"secret": "abé", "ticker": "AAPL"})
    assert ok is False and reason == "secret mismatch"
    assert "compare_digest" in inspect.getsource(tradingview_ops.validate_signal)


def test_a_slow_client_does_not_hold_a_handler_thread_forever(monkeypatch):
    monkeypatch.undo()
    from dourmouse.http_server import DourmouseHTTPServer

    monkeypatch.setattr(tv, "_REQUEST_TIMEOUT", 0.4)
    srv = DourmouseHTTPServer(("127.0.0.1", 0), tv._Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=4)
        s.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 50\r\n\r\n")
        data = s.recv(4096)  # server must answer or close well inside 4s; otherwise socket.timeout
        assert data == b"" or b"408" in data or b"HTTP/1" in data
        s.close()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)
