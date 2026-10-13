"""Fix agent FD1: P4-4, the backend probe must hit an endpoint that exists and count a 4xx as reachable."""

from __future__ import annotations

import http.server
import threading

import pytest

from dourmouse import backend_fallback as bf


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []
    answers = {"/v1/models": 200}

    def do_GET(self):  # noqa: N802
        type(self).seen.append(self.path)
        code = type(self).answers.get(self.path, 404)
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Handler.seen = []
    _Handler.answers = {"/v1/models": 200}
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_bare_root_is_probed_at_v1_models(server):
    root = f"http://127.0.0.1:{server.server_port}"
    assert bf._probe_backend(root) is True
    assert _Handler.seen == ["/v1/models"]


def test_v1_base_url_is_not_doubled(server):
    base = f"http://127.0.0.1:{server.server_port}/v1"
    assert bf._probe_backend(base) is True
    assert _Handler.seen == ["/v1/models"]


def test_auth_error_means_reachable(server):
    _Handler.answers = {"/v1/models": 401}
    assert bf._probe_backend(f"http://127.0.0.1:{server.server_port}/v1") is True


def test_server_error_means_unreachable(server):
    _Handler.answers = {"/v1/models": 503}
    assert bf._probe_backend(f"http://127.0.0.1:{server.server_port}/v1") is False


def test_nothing_listening_is_unreachable():
    assert bf._probe_backend("http://127.0.0.1:1/v1", timeout=0.5) is False


def test_configured_ollama_at_default_local_is_not_reprobed(monkeypatch):
    """A configured base url that IS the local default (with /v1) must not trigger a second probe of the same host."""
    monkeypatch.delenv("DOURMOUSE_FALLBACK_DISABLED", raising=False)
    calls = []
    monkeypatch.setattr(bf, "llm_backend", lambda: "ollama")
    monkeypatch.setattr(bf, "_probe_backend", lambda url, timeout=3.0: calls.append(url) or False)

    class Cfg:
        base_url = "http://127.0.0.1:11434/v1"
        model = "m"

    monkeypatch.setattr(bf, "load_llm_config", lambda: Cfg())
    assert bf.load_llm_config_with_fallback() is not None
    assert len(calls) == 1
