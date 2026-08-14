"""Hermetic tests for DourmouseServerClient (dourmouse/remote_server.py).

The client is exercised against a tiny in-process HTTP server that emulates
the Dell /v1/* API — no real LAN traffic. The failover seam is tested with a
fake local callable. The overriding contract: a dead node NEVER raises out
of status()/generate()/chat(), and generate_with_fallback() always returns
an honest result from one of the two paths. The master kill switch
(DOURMOUSE_SERVER_ENABLED=0) must block ALL traffic without ever probing.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dourmouse import remote_server as rs


class _FakeDell(BaseHTTPRequestHandler):
    """Emulates the Dell: /status, /v1/status, /v1/generate, /v1/chat."""

    reply = "Dell answer"
    require_key = ""
    model = "qwen3:1.7b"

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        if not self.require_key:
            return True
        return self.headers.get("X-API-Key") == self.require_key

    def do_GET(self):  # noqa: N802
        if not self._auth_ok():
            return self._send(401, {"detail": "invalid or missing API key"})
        if self.path.startswith("/v1/status"):
            return self._send(200, {"status": "online", "node": "Node-01", "model": self.model, "ollama": True, "version": "1.1.0", "latency_ms": 12})
        if self.path.startswith("/status"):
            return self._send(200, {"system": "DOURMOUSE", "status": "online", "version": "1.1.0", "node": "Node-01", "model": self.model})
        return self._send(404, {"detail": "Not Found"})

    def do_POST(self):  # noqa: N802
        if not self._auth_ok():
            return self._send(401, {"detail": "invalid or missing API key"})
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            return self._send(400, {"success": False, "error": "bad json"})
        if self.path.startswith("/v1/generate"):
            if not (body.get("prompt") or "").strip():
                return self._send(400, {"success": False, "error": "'prompt' is required"})
            return self._send(200, {"success": True, "node": "Node-01", "model": self.model, "response": self.reply, "latency_ms": 42})
        if self.path.startswith("/v1/chat"):
            messages = body.get("messages") or []
            if not messages:
                return self._send(400, {"success": False, "error": "no valid messages"})
            return self._send(200, {"success": True, "node": "Node-01", "model": self.model, "response": self.reply, "latency_ms": 42})
        return self._send(404, {"detail": "Not Found"})


@pytest.fixture
def dell_server(monkeypatch):
    """A real in-process HTTP server emulating the Dell + env pointing at it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDell)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("DOURMOUSE_SERVER_URL", url)
    monkeypatch.delenv("DOURMOUSE_SERVER_API_KEY", raising=False)
    monkeypatch.delenv("DOURMOUSE_SERVER_ENABLED", raising=False)
    # Reset the client's health cache between tests.
    with rs._health_lock:
        rs._health_cache.update({"at": 0.0, "online": False, "payload": None})
    yield url
    server.shutdown()


class TestConfig:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SERVER_URL", raising=False)
        monkeypatch.delenv("DOURMOUSE_SERVER_MODEL", raising=False)
        assert rs.server_url() == "http://192.168.1.108:8000"
        assert rs.server_model() == "qwen3:1.7b"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://10.0.0.5:9000/")
        monkeypatch.setenv("DOURMOUSE_SERVER_MODEL", "qwen3:4b")
        assert rs.server_url() == "http://10.0.0.5:9000"
        assert rs.server_model() == "qwen3:4b"

    def test_server_enabled_default_true(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SERVER_ENABLED", raising=False)
        assert rs.server_enabled() is True

    def test_server_enabled_off(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_ENABLED", "0")
        assert rs.server_enabled() is False

    def test_server_connect_timeout_default(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SERVER_CONNECT_TIMEOUT", raising=False)
        assert rs.server_connect_timeout() == 2.0

    def test_server_connect_timeout_override(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_CONNECT_TIMEOUT", "5")
        assert rs.server_connect_timeout() == 5.0


class TestEnabledSwitch:
    """v5.32 — the master kill switch. Disabled must mean: zero probes,
    zero requests, honest status, honest client failures, and the fast lane
    stays local (server_online_cached() -> False)."""

    def test_disabled_blocks_all_traffic_without_probing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            rs, "_request", lambda *a, **k: calls.append(a) or (200, {"status": "online"})
        )
        monkeypatch.setenv("DOURMOUSE_SERVER_ENABLED", "0")
        with rs._health_lock:
            rs._health_cache.update({"at": 0.0, "online": False, "payload": None})
        assert rs.server_online_cached() is False
        assert rs.server_available(force=True) is False
        assert calls == []  # the kill switch must never touch the network

    def test_disabled_status_reports_honestly(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_ENABLED", "0")
        s = rs.server_status()
        assert s["enabled"] is False
        assert s["online"] is False
        assert "disabled" in (s["error"] or "")

    def test_disabled_client_returns_honest_failure(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_ENABLED", "0")
        client = rs.DourmouseServerClient()
        out = client.generate("hi")
        assert out["success"] is False
        assert "disabled" in out["error"]
        out2 = client.chat([{"role": "user", "content": "hi"}])
        assert out2["success"] is False
        assert client.status()["online"] is False

    def test_disabled_fallback_uses_local(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_ENABLED", "0")
        out = rs.generate_with_fallback("hi", local_fallback=lambda p: "LOCAL")
        assert out["success"] is True
        assert out["via"] == "local"
        assert out["response"] == "LOCAL"

    def test_enabled_status_has_flag(self, dell_server):
        s = rs.server_status()
        assert s["enabled"] is True
        assert s["online"] is True


class TestClient:
    def test_status_online(self, dell_server):
        client = rs.DourmouseServerClient()
        s = client.status()
        assert s["online"] is True
        assert s["node"] == "Node-01"
        assert s["model"] == "qwen3:1.7b"
        assert s["latency_ms"] is not None

    def test_generate_success(self, dell_server):
        client = rs.DourmouseServerClient()
        out = client.generate("Explain Brownian motion", system="You are a mathematical assistant.", temperature=0.2)
        assert out["success"] is True
        assert out["response"] == "Dell answer"
        assert out["node"] == "Node-01"
        assert isinstance(out["latency_ms"], int)

    def test_chat_success(self, dell_server):
        client = rs.DourmouseServerClient()
        out = client.chat([{"role": "user", "content": "Hello"}])
        assert out["success"] is True
        assert out["response"] == "Dell answer"

    def test_dead_node_never_raises(self, monkeypatch):
        """A dead/absent node returns an honest failure, never an exception."""
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://127.0.0.1:1")
        client = rs.DourmouseServerClient(timeout=1.0)
        assert client.status()["online"] is False
        out = client.generate("hi")
        assert out["success"] is False
        assert "unreachable" in out["error"]
        out2 = client.chat([{"role": "user", "content": "hi"}])
        assert out2["success"] is False

    def test_api_key_sent_when_configured(self, dell_server, monkeypatch):
        monkeypatch.setattr(_FakeDell, "require_key", "sekret")
        monkeypatch.setenv("DOURMOUSE_SERVER_API_KEY", "sekret")
        client = rs.DourmouseServerClient()
        assert client.status()["online"] is True
        assert client.generate("hi")["success"] is True

    def test_wrong_key_honest_401(self, dell_server, monkeypatch):
        monkeypatch.setattr(_FakeDell, "require_key", "sekret")
        monkeypatch.setenv("DOURMOUSE_SERVER_API_KEY", "wrong")
        client = rs.DourmouseServerClient()
        out = client.generate("hi")
        assert out["success"] is False
        assert "API_KEY" in out["error"]

    def test_openai_surface_raises_when_down(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://127.0.0.1:1")
        client = rs.DourmouseServerClient(timeout=1.0)
        with pytest.raises(rs.ServerUnavailable):
            client.chat_completions_create(model="x", messages=[{"role": "user", "content": "hi"}])

    def test_openai_surface_success(self, dell_server):
        client = rs.DourmouseServerClient()
        resp = client.chat_completions_create(messages=[{"role": "user", "content": "hi"}])
        assert resp.choices[0].message.content == "Dell answer"


class TestFailover:
    def test_dell_up_uses_server(self, dell_server):
        out = rs.generate_with_fallback("hi", local_fallback=lambda p: "LOCAL")
        assert out["success"] is True
        assert out["via"] == "server"
        assert out["response"] == "Dell answer"

    def test_dell_down_falls_back_to_local(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://127.0.0.1:1")
        out = rs.generate_with_fallback("hi", local_fallback=lambda p: "LOCAL")
        assert out["success"] is True
        assert out["via"] == "local"
        assert out["response"] == "LOCAL"

    def test_http_500_falls_back_to_local(self, monkeypatch):
        """A reachable Dell that errors (HTTP 500) must fall back too."""
        monkeypatch.setattr(rs, "_request", lambda *a, **k: (500, {"detail": "boom"}))
        out = rs.generate_with_fallback("hi", local_fallback=lambda p: "LOCAL")
        assert out["success"] is True
        assert out["via"] == "local"
        assert out["response"] == "LOCAL"

    def test_malformed_200_falls_back_to_local(self, monkeypatch):
        """A 200 with a malformed body (success true but no response) must
        be treated as a failure — never a fabricated or empty answer."""
        monkeypatch.setattr(rs, "_request", lambda *a, **k: (200, {"success": True}))
        out = rs.generate_with_fallback("hi", local_fallback=lambda p: "LOCAL")
        assert out["success"] is True
        assert out["via"] == "local"
        assert out["response"] == "LOCAL"

    def test_both_paths_fail_is_honest(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://127.0.0.1:1")

        def boom(_p):
            raise RuntimeError("local ollama down")

        out = rs.generate_with_fallback("hi", local_fallback=boom)
        assert out["success"] is False
        assert "fallback failed" in out["error"]

    def test_fallback_never_raises(self, dell_server, monkeypatch):
        monkeypatch.setattr(_FakeDell, "require_key", "x")  # server now 401s
        monkeypatch.setenv("DOURMOUSE_SERVER_API_KEY", "wrong")
        out = rs.generate_with_fallback("hi", local_fallback=lambda p: "LOCAL")
        assert out["success"] is True
        assert out["via"] == "local"
        assert out["response"] == "LOCAL"


class TestProbe:
    def test_server_available_returns_bool(self, dell_server):
        assert rs.server_available(force=True) is True
        assert isinstance(rs.server_available(), bool)

    def test_server_available_false_when_down(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://127.0.0.1:1")
        with rs._health_lock:
            rs._health_cache.update({"at": 0.0, "online": False, "payload": None})
        assert rs.server_available(force=True) is False


class TestFastLaneHelpers:
    """v5.30 — the server fast lane's gate helpers. The rule: a dead or
    unconfigured node must NEVER add probe latency to a reply, so the lane
    only engages on an EXPLICITLY configured URL + a FRESH cached probe."""

    def test_server_url_configured_only_when_explicit(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SERVER_URL", raising=False)
        assert rs.server_url_configured() is False
        monkeypatch.setenv("DOURMOUSE_SERVER_URL", "http://192.168.1.108:8000")
        assert rs.server_url_configured() is True

    def test_server_online_cached_never_probes_when_stale(self, monkeypatch):
        """A stale cache returns False instantly without touching the network
        — the lane stays local, costing zero probe latency."""
        calls = []
        monkeypatch.setattr(rs, "_request", lambda *a, **k: calls.append(a) or (200, {}))
        with rs._health_lock:
            rs._health_cache["at"] = 0  # ancient -> stale
            rs._health_cache["online"] = True
        assert rs.server_online_cached() is False
        assert calls == []

    def test_server_online_cached_reads_fresh_online(self, monkeypatch):
        import time

        with rs._health_lock:
            rs._health_cache["at"] = time.monotonic()  # fresh
            rs._health_cache["online"] = True
        assert rs.server_online_cached() is True
        with rs._health_lock:
            rs._health_cache["online"] = False
        assert rs.server_online_cached() is False


class TestHealthWarmer:
    """The warmer is what makes the compute-node lane usable at all.

    ``server_online_cached()`` never probes, so without something refreshing
    the cache the lane can only engage while a probe happens to be fresh. The
    UI's /api/connections call runs once at page load (it lives in a view
    renderer, not a timer), so before the warmer existed the Dell served only
    the chats sent within the 30s TTL of a page load and was silently unused
    afterwards.
    """

    def _reset(self):
        # stop_health_warmer joins the thread — a bare `_warmer_thread = None`
        # would orphan a live prober that keeps hitting a fixture server the
        # next test has already shut down.
        rs.stop_health_warmer()
        with rs._health_lock:
            rs._health_cache.update({"at": 0.0, "online": False, "payload": None})

    def test_warmer_makes_the_lane_engage_and_keeps_it_engaged(
        self, dell_server, monkeypatch
    ):
        self._reset()
        # Short TTL so the refresh loop cycles within the test.
        monkeypatch.setenv("DOURMOUSE_SERVER_HEALTH_TTL", "1")
        assert rs.server_online_cached() is False  # cold: lane stays local
        assert rs.start_health_warmer() is True

        import time

        deadline = time.time() + 5
        while time.time() < deadline and not rs.server_online_cached():
            time.sleep(0.05)
        assert rs.server_online_cached() is True

        # Past a full TTL the cache must STILL be fresh — this is the exact
        # window where the old behaviour went permanently dead.
        time.sleep(1.6)
        assert rs.server_online_cached() is True
        self._reset()

    def test_warmer_is_idempotent(self, dell_server):
        self._reset()
        assert rs.start_health_warmer() is True
        assert rs.start_health_warmer() is True  # no second thread
        self._reset()

    def test_warmer_declines_when_no_node_configured(self, monkeypatch):
        """An unconfigured node must never be probed — the warmer must not
        resurrect the very cost server_url_configured() exists to avoid."""
        self._reset()
        monkeypatch.delenv("DOURMOUSE_SERVER_URL", raising=False)
        assert rs.start_health_warmer() is False

    def test_warmer_respects_the_kill_switch(self, dell_server, monkeypatch):
        self._reset()
        monkeypatch.setenv("DOURMOUSE_SERVER_ENABLED", "0")
        assert rs.start_health_warmer() is False

    def test_warmer_can_be_opted_out(self, dell_server, monkeypatch):
        self._reset()
        monkeypatch.setenv("DOURMOUSE_SERVER_WARMER", "0")
        assert rs.start_health_warmer() is False
        self._reset()
