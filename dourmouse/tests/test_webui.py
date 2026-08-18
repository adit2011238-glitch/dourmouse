"""Web UI server tests (dourmouse/webui.py + ui/index.html serving).

Runs a real ThreadingHTTPServer on an ephemeral port with a fake
OpenAI-shaped client so the SSE chat, confirmation gate, and API endpoints
are exercised over actual HTTP (Integration Rule 7.3 discipline). The fake
client only shapes the LLM side; tool behavior is the real roster/engine.
"""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

import dourmouse.webui as webui_module
from dourmouse.dispatch import (
    DispatchRegistry,
    Permission,
    Subagent,
    ToolSpec,
)
from dourmouse.webui import (
    WebConfirmationGate,
    build_roster_payload,
    run_server,
)


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []  # v3.1: record each LLM call's kwargs

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("fake client exhausted")
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class FakeClient:
    def __init__(self, responses: list[_FakeResponse]):
        self.chat = _FakeChat(_FakeCompletions(responses))


def _echo_registry() -> DispatchRegistry:
    r = DispatchRegistry()
    r.register_subagent(
        Subagent(
            name="echo_agent",
            domain="Test",
            description="echoes text",
            tools=(
                ToolSpec(
                    name="echo",
                    description="echo the text back",
                    parameters={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    handler=lambda a: f"ECHOED: {a['text']}",
                ),
                ToolSpec(
                    name="gated_echo",
                    description="gated echo",
                    parameters={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    handler=lambda a: f"GATED-EXECUTED: {a['text']}",
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: f"Echo {a['text']!r}?",
                ),
            ),
        )
    )
    return r


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr(webui_module, "_CONFIRM_TIMEOUT_SECONDS", 5.0)
    srv = run_server(_echo_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


class TestConfirmationGate:
    def test_block_then_resolve_approves(self):
        events: list[dict] = []
        gate = WebConfirmationGate(events.append)
        result: dict = {}

        def run():
            result["approved"] = gate("Send this?")

        t = threading.Thread(target=run)
        t.start()
        # Let the gate emit and block.
        for _ in range(50):
            if events:
                break
            time.sleep(0.02)
        assert events[0]["type"] == "confirmation_requested"
        confirm_id = events[0]["id"]
        assert gate.resolve(confirm_id, True) is True
        t.join(timeout=2)
        assert result["approved"] is True

    def test_resolve_unknown_id_returns_false(self):
        gate = WebConfirmationGate(lambda e: None)
        assert gate.resolve("does-not-exist", True) is False

    def test_timeout_declines(self, monkeypatch):
        monkeypatch.setattr(webui_module, "_CONFIRM_TIMEOUT_SECONDS", 0.05)
        gate = WebConfirmationGate(lambda e: None)
        assert gate("anything?") is False  # never resolved -> auto-decline


class TestRosterPayload:
    def test_payload_lists_subagents_and_permissions(self):
        payload = build_roster_payload(_echo_registry())
        assert [s["name"] for s in payload["subagents"]] == ["echo_agent"]
        tools = payload["subagents"][0]["tools"]
        perm = {t["name"]: t["permission"] for t in tools}
        assert perm == {"echo": "regular", "gated_echo": "requires_confirmation"}

    def test_payload_includes_per_agent_model(self):
        from dourmouse.config import NvidiaConfig

        config = NvidiaConfig(
            api_key="k", base_url="u", model="nvidia/base-120b",
            agent_models={"ECHO_AGENT": "nvidia/echo-70b"},
        )
        payload = build_roster_payload(_echo_registry(), config=config)
        sub = payload["subagents"][0]
        assert sub["model"] == "nvidia/echo-70b"  # agent's own model

    def test_payload_model_defaults_without_config(self):
        payload = build_roster_payload(_echo_registry())
        assert payload["subagents"][0]["model"] == "default"

    def test_resolve_server_config_returns_explicit(self):
        from dourmouse.config import NvidiaConfig

        cfg = NvidiaConfig(api_key="k", base_url="u", model="m")
        assert webui_module._resolve_server_config(cfg) is cfg

    def test_resolve_server_config_loads_from_env(self, monkeypatch):
        # v4.0: force the NVIDIA backend explicitly so this test exercises
        # the per-agent model resolution deterministically (the default is
        # now "auto" → Ollama when the local server answers).
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-test-key")
        monkeypatch.setenv("DOURMOUSE_MODEL_ECHO_AGENT", "nvidia/echo-70b")
        cfg = webui_module._resolve_server_config(None)
        assert cfg is not None
        assert cfg.model_for_agent("echo_agent") == "nvidia/echo-70b"

    def test_resolve_server_config_none_without_key(self, monkeypatch):
        # v4.0: with no backend configured and no Ollama server, resolution
        # honestly returns None (chat still fails loudly per-call). Force
        # NVIDIA + no key AND no Ollama to pin the honest-None path.
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "nvidia")
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        import dourmouse.config as _cfg

        monkeypatch.setattr(_cfg, "ollama_available", lambda: False)
        assert webui_module._resolve_server_config(None) is None


class TestHttpEndpoints:
    def test_roster_endpoint(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/roster")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert [s["name"] for s in data["subagents"]] == ["echo_agent"]
        conn.close()

    def test_sessions_endpoint(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/sessions")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "sessions" in data
        conn.close()

    def test_sessions_recent_endpoint_with_real_summaries(self, server, tmp_path):
        """Phase 2.3: /api/sessions/recent surfaces REAL data already on disk
        (first user message + last answer per session)."""
        ws = tmp_path / "ws"
        sessions_dir = ws / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "session_20260801_000000.jsonl").write_text(
            json.dumps({"user": "first question", "final_text": "first answer"}) + "\n"
            + json.dumps({"user": "second question", "final_text": "second answer"}) + "\n"
        )
        (sessions_dir / "session_20260731_000000.jsonl").write_text(
            json.dumps({"user": "older session", "final_text": "old answer"}) + "\n"
        )
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/sessions/recent")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        sessions = data["sessions"]
        assert sessions, "expected real sessions from disk"
        newest = sessions[0]  # newest first
        assert newest["first_user"] == "first question"
        assert newest["last_answer"] == "second answer"
        assert newest["turns"] == 2
        conn.close()

    def test_ui_html_served(self, server):
        """v8.7: "/" serves the CONSOLE (the new default surface)."""
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read().decode()
        assert "DOURMOUSE // CONSOLE" in body
        conn.close()

    def test_hud_still_served_at_index_html(self, server):
        """The HUD was NOT removed — only demoted from the default route.

        /index.html must keep serving it, because the deeplink redirect
        targets that exact path and the #/atlas, #/world, #/portfolio hash
        router lives only in the HUD.
        """
        srv, port = server
        for path in ("/index.html", "/dispatch"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            assert resp.status == 200, path
            body = resp.read().decode()
            assert "DOURMOUSE // CENTRAL AGENT DISPATCH" in body, path
            conn.close()

    def test_traversal_is_blocked(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/assets/../../etc/passwd")
        resp = conn.getresponse()
        assert resp.status == 404
        conn.close()


class TestSseChat:
    def _stream_events(self, port, prompt):
        """POST /api/chat and read the SSE stream into a list of events."""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/chat",
            body=json.dumps({"prompt": prompt}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 200
        events = []
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))
        conn.close()
        return events

    def test_focus_agent_uses_that_agents_model(self, server):
        """v3.1: a focus_agent chat route runs on THAT agent's configured
        NVIDIA model (DOURMOUSE_MODEL_<AGENT>), not the session default."""
        from dourmouse.config import NvidiaConfig

        srv, port = server
        srv.session.config = NvidiaConfig(
            api_key="k", base_url="u", model="nvidia/base-120b",
            agent_models={"ECHO_AGENT": "nvidia/echo-70b"},
        )
        srv.config = srv.session.config
        srv.session.client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="Focused echo answer.")),
            ]
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/chat",
            body=json.dumps({"prompt": "echo hi", "focus_agent": "echo_agent"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        while True:
            line = resp.readline()
            if not line:
                break
        conn.close()
        assert resp.status == 200
        # The run's LLM calls used the AGENT's model, not the session default.
        models = [c["model"] for c in srv.session.client.chat.completions.calls]
        assert models and all(m == "nvidia/echo-70b" for m in models)

    def test_chat_without_focus_uses_session_default_model(self, server, monkeypatch):
        """No focus_agent -> the session's default config model drives the
        run (no per-agent override injected). Fast lane pinned off — this
        asserts session-default plumbing, not the simple-response lane."""
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.config import NvidiaConfig

        srv, port = server
        srv.session.config = NvidiaConfig(
            api_key="k", base_url="u", model="nvidia/base-120b",
            agent_models={"ECHO_AGENT": "nvidia/echo-70b"},
        )
        srv.session.client = FakeClient(
            [_FakeResponse(_FakeMessage(content="plain answer."))]
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/chat",
            body=json.dumps({"prompt": "plain question"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        while True:
            line = resp.readline()
            if not line:
                break
        conn.close()
        models = [c["model"] for c in srv.session.client.chat.completions.calls]
        assert models and all(m == "nvidia/base-120b" for m in models)

    def test_regular_tool_streams_events(self, server):
        srv, port = server
        # Replace the server's session client with a fake.
        srv.session.client = FakeClient(
            [
                _FakeResponse(
                    _FakeMessage(
                        content=None,
                        tool_calls=[_FakeToolCall("c1", "echo", json.dumps({"text": "hi"}))],
                    )
                ),
                _FakeResponse(_FakeMessage(content="It said hi.")),
            ]
        )
        events = self._stream_events(port, "echo hi")
        types = [e["type"] for e in events]
        assert "tool_use" in types
        assert "tool_result" in types
        assert types[-1] == "done"
        # v2.8 regression guard: the terminal event must be emitted EXACTLY
        # once (a former double-emit through the sink duplicated the RESPONSE
        # line in the dashboard feed — caught only by review, not by tests).
        assert types.count("done") == 1
        done = events[-1]
        assert done["final_text"] == "It said hi."
        tool_result = next(e for e in events if e["type"] == "tool_result")
        assert "ECHOED: hi" in tool_result["text"]

    def test_confirmation_requires_approval_over_http(self, server):
        srv, port = server
        srv.session.client = FakeClient(
            [
                _FakeResponse(
                    _FakeMessage(
                        content=None,
                        tool_calls=[_FakeToolCall("c1", "gated_echo", json.dumps({"text": "secret"}))],
                    )
                ),
                _FakeResponse(_FakeMessage(content="Approved and done.")),
            ]
        )

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/chat",
            body=json.dumps({"prompt": "do the gated thing"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 200

        # Read until we hit the confirmation_requested event.
        confirm_id = None
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                event = json.loads(line[6:])
                if event["type"] == "confirmation_requested":
                    confirm_id = event["id"]
                    break

        assert confirm_id is not None, "expected a confirmation_requested event"

        # Approve it from a second connection.
        conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn2.request(
            "POST",
            "/api/confirm",
            body=json.dumps({"id": confirm_id, "approved": True}),
            headers={"Content-Type": "application/json"},
        )
        resp2 = conn2.getresponse()
        assert resp2.status == 200
        assert json.loads(resp2.read())["ok"] is True
        conn2.close()

        # Now the stream should continue and finish with the tool result.
        remaining = []
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                remaining.append(json.loads(line[6:]))
        conn.close()

        types = [e["type"] for e in remaining]
        assert "tool_result" in types
        assert "done" in types
        tool_result = next(e for e in remaining if e["type"] == "tool_result")
        assert "GATED-EXECUTED: secret" in tool_result["text"]

    def test_declined_confirmation_not_executed(self, server):
        srv, port = server
        srv.session.client = FakeClient(
            [
                _FakeResponse(
                    _FakeMessage(
                        content=None,
                        tool_calls=[_FakeToolCall("c1", "gated_echo", json.dumps({"text": "no"}))],
                    )
                ),
                _FakeResponse(_FakeMessage(content="Skipped.")),
            ]
        )

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/chat",
            body=json.dumps({"prompt": "gated"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        confirm_id = None
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                event = json.loads(line[6:])
                if event["type"] == "confirmation_requested":
                    confirm_id = event["id"]
                    break
        assert confirm_id

        conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn2.request(
            "POST",
            "/api/confirm",
            body=json.dumps({"id": confirm_id, "approved": False}),
            headers={"Content-Type": "application/json"},
        )
        conn2.getresponse().read()
        conn2.close()

        remaining = []
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                remaining.append(json.loads(line[6:]))
        conn.close()

        tool_result = next(e for e in remaining if e["type"] == "tool_result")
        assert "DECLINED" in tool_result["text"]
        assert "GATED-EXECUTED" not in tool_result["text"]


class TestSpotifyPlayEndpoints:
    """v5.21 HUD music section: the play-anything POST endpoints. The
    spotify_services functions are stubbed at the module level (the handlers
    import them lazily per request), so these assert the HTTP contract —
    payload parsing, ok/error shape — without touching the Spotify API.
    """

    @staticmethod
    def _stub_spotify(monkeypatch) -> None:
        from dourmouse import spotify_services as ss

        monkeypatch.setattr(
            ss, "search_tracks_data",
            lambda query, limit=8: [
                {"name": "Around the World", "artists": "Daft Punk", "uri": "spotify:track:1"}
            ],
        )
        monkeypatch.setattr(ss, "play_uri", lambda uri: "SPOTIFY: playback started.")
        monkeypatch.setattr(
            ss, "playlists_data",
            lambda limit=20: [
                {"name": "Chill", "uri": "spotify:playlist:9", "tracks": 42}
            ],
        )
        monkeypatch.setattr(
            ss, "recently_played_data",
            lambda limit=8: [
                {"name": "Get Lucky", "artists": "Daft Punk", "uri": "spotify:track:7"}
            ],
        )
        monkeypatch.setattr(
            ss, "top_tracks_data",
            lambda time_range="medium_term", limit=8: [
                {"name": "One More Time", "artists": "Daft Punk", "uri": "spotify:track:8"}
            ],
        )
        monkeypatch.setattr(
            ss, "playback_control", lambda action: f"SPOTIFY: {action} — done."
        )

    def _post(self, port, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", path,
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_search_returns_structured_results(self, server, monkeypatch):
        self._stub_spotify(monkeypatch)
        srv, port = server
        status, data = self._post(port, "/api/spotify/search", {"query": "daft punk", "limit": 8})
        assert status == 200
        assert data["ok"] is True
        assert data["results"][0]["uri"] == "spotify:track:1"

    def test_play_returns_message(self, server, monkeypatch):
        self._stub_spotify(monkeypatch)
        srv, port = server
        status, data = self._post(port, "/api/spotify/play", {"uri": "spotify:playlist:9"})
        assert status == 200
        assert data["ok"] is True
        assert "playback started" in data["message"]

    def test_playlists_returns_rows(self, server, monkeypatch):
        self._stub_spotify(monkeypatch)
        srv, port = server
        status, data = self._post(port, "/api/spotify/playlists", {})
        assert status == 200
        assert data["ok"] is True
        assert data["playlists"][0]["uri"] == "spotify:playlist:9"

    def test_control_returns_message(self, server, monkeypatch):
        self._stub_spotify(monkeypatch)
        srv, port = server
        status, data = self._post(port, "/api/spotify/control", {"action": "next"})
        assert status == 200
        assert data["ok"] is True
        assert "next" in data["message"]

    def test_play_error_is_honest_ok_false(self, server, monkeypatch):
        from dourmouse import spotify_services as ss

        monkeypatch.setattr(ss, "play_uri", lambda uri: "ERROR: no active device")
        srv, port = server
        status, data = self._post(port, "/api/spotify/play", {"uri": "spotify:track:1"})
        assert status == 200
        assert data["ok"] is False
        assert "no active device" in data["message"]

    def test_bad_limit_does_not_crash(self, server, monkeypatch):
        """Regression: a non-numeric limit previously 500'd the handler."""
        self._stub_spotify(monkeypatch)
        srv, port = server
        status, data = self._post(port, "/api/spotify/search", {"query": "x", "limit": "abc"})
        assert status == 200
        assert data["ok"] is True

    def test_recent_returns_playable_rows(self, server, monkeypatch):
        self._stub_spotify(monkeypatch)
        srv, port = server
        status, data = self._post(port, "/api/spotify/recent", {})
        assert status == 200
        assert data["ok"] is True
        assert data["recent"][0]["uri"] == "spotify:track:7"

    def test_top_returns_playable_rows(self, server, monkeypatch):
        self._stub_spotify(monkeypatch)
        srv, port = server
        status, data = self._post(port, "/api/spotify/top", {"time_range": "medium_term"})
        assert status == 200
        assert data["ok"] is True
        assert data["top"][0]["uri"] == "spotify:track:8"

    def test_non_dict_payload_does_not_crash(self, server, monkeypatch):
        self._stub_spotify(monkeypatch)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/spotify/search",
            body="[1, 2, 3]",
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert data["ok"] is True  # treated as an empty payload, not a 500


class TestSpotifyHudLinkedState:
    """v5.7: the HUD Spotify panel (/api/spotify) and /api/connections report
    'linked' honestly once a spotify_tokens.json exists in the workspace, and
    'not linked' when it doesn't (Rule 2.2 honest contract). The token file is
    written to the fixture's hermetic workspace — the endpoints read REAL
    disk state; only the network probes (now_playing, connections checks) are
    stubbed so the test never touches the Spotify API or local services.
    """

    @staticmethod
    def _write_tokens(tmp_path, display_name: str = "Adit") -> None:
        ws = tmp_path / "ws"  # the server fixture's DOURMOUSE_WORKSPACE
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "spotify_tokens.json").write_text(
            json.dumps(
                {
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                    "expires_at": time.time() + 3600,
                    "scope": "user-read-currently-playing",
                    "display_name": display_name,
                    "user_id": "test-user",
                }
            )
        )

    @staticmethod
    def _stub_network(monkeypatch: pytest.MonkeyPatch) -> None:
        # /api/spotify calls now_playing() (a REAL Spotify API call) when
        # linked, and /api/connections probes local services/subprocesses.
        # Both are stubbed so these tests assert on disk state alone.
        import dourmouse.connections as conn_mod
        from dourmouse import spotify_services as ss

        monkeypatch.setattr(conn_mod, "_tcp_reachable", lambda *a, **k: False)
        monkeypatch.setattr(conn_mod, "_cli_version", lambda name: None)
        monkeypatch.setattr(conn_mod, "_codex_auth_mode", lambda: "none")
        monkeypatch.setattr(
            conn_mod, "_gmail_status", lambda: {"ok": "missing", "detail": "test"}
        )
        monkeypatch.setattr(
            ss, "now_playing",
            lambda: "SPOTIFY NOW PLAYING: ▶ Test Track — Test Artist (0:30 / 3:00)",
        )

    def test_spotify_panel_reports_linked_once_token_exists(
        self, server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
        self._write_tokens(tmp_path)
        self._stub_network(monkeypatch)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/spotify")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        conn.close()
        assert data["configured"] is True
        assert data["linked"] is True
        assert data["detail"] == "linked as Adit"
        assert "Test Track" in data["now_playing"]

    def test_connections_reports_spotify_linked_once_token_exists(
        self, server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
        self._write_tokens(tmp_path)
        self._stub_network(monkeypatch)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/connections")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        conn.close()
        spot = data["spotify"]
        assert spot["ok"] is True
        assert spot["detail"] == "linked · linked as Adit"

    def test_both_report_not_linked_without_token(self, server, tmp_path, monkeypatch):
        """The honest counterpart: no token file -> NOT linked, even with a
        client ID configured (a vacuous always-true test proves nothing)."""
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
        self._stub_network(monkeypatch)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/spotify")
        panel = json.loads(conn.getresponse().read())
        conn.request("GET", "/api/connections")
        conns = json.loads(conn.getresponse().read())
        conn.close()
        assert panel["configured"] is True
        assert panel["linked"] is False
        assert conns["spotify"]["ok"] is False
        assert "not linked" in conns["spotify"]["detail"]


class TestPwaEndpoints:
    """v5.22.3: the installable-app surface — manifest, service worker, icons.

    These are the files the phone browser fetches to install DourMouse as a
    standalone app (own icon, full screen). The /assets/ route previously
    stripped the directory and 404'd EVERY asset — locked by this test.
    """

    def _get(self, server, path: str):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        ctype = resp.getheader("Content-Type", "")
        status = resp.status
        conn.close()
        return status, ctype, body

    def test_manifest_serves_standalone_json(self, server):
        status, ctype, body = self._get(server, "/manifest.json")
        assert status == 200
        assert "application/json" in ctype
        manifest = json.loads(body)
        assert manifest["display"] == "standalone"
        assert manifest["short_name"] == "DourMouse"
        assert any(i["sizes"] == "512x512" for i in manifest["icons"])

    def test_service_worker_serves(self, server):
        status, ctype, body = self._get(server, "/sw.js")
        assert status == 200
        assert "javascript" in ctype
        assert b"manifest.json" in body  # PWA assets joined the shell cache

    def test_assets_route_serves_icons(self, server):
        # Regression: /assets/<file> used to serve from the UI root and 404.
        for icon in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
            status, ctype, body = self._get(server, f"/assets/{icon}")
            assert status == 200, f"{icon} should serve"
            assert "image/png" in ctype
            assert body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_index_has_pwa_head_tags(self, server):
        """Whatever "/" serves must be installable.

        v8.7: this caught a real regression — promoting the console to the
        default route dropped the manifest/apple-touch tags with it, which
        would have silently killed "Add to Home Screen" and the standalone
        window. Asserted on "/" (not a fixed file) so the NEXT change of
        default surface has to carry the install metadata too.
        """
        status, ctype, body = self._get(server, "/")
        assert status == 200
        html = body.decode("utf-8", errors="replace")
        assert 'rel="manifest"' in html
        assert "apple-touch-icon" in html
        assert "apple-mobile-web-app-capable" in html
        assert "/sw.js" in html, "the default surface must register the SW"


class TestSystemBrowserClaimFlow:
    """v5.22.11: the system-browser sign-in bridge. Google refuses consent
    inside embedded WebKit webviews, so the flow is: webview asks
    /api/auth/google/start?claim=CODE → consent opens in the REAL browser →
    the callback parks the session under the claim code → the webview
    adopts it via /api/auth/claim?code=CODE. Hermetic: google_auth's network
    calls are monkeypatched; only the server wiring is exercised."""

    #: The identity Google's verified id_token would report.
    _IDENTITY = {"email": "bridge@example.com", "name": "Bridge User",
                 "picture": "", "sub": "sub-123"}

    def _patch_google(self, monkeypatch):
        """Patch google_auth's network surface so the flow is hermetic."""
        from dourmouse import google_auth
        import dourmouse.webui as webui_module

        monkeypatch.setattr(google_auth, "google_configured", lambda: True)
        monkeypatch.setattr(
            google_auth, "authorization_url",
            lambda *a, **k: "https://accounts.google.com/o/oauth2/v2/auth?fake=1")
        monkeypatch.setattr(
            google_auth, "exchange_code",
            lambda *a, **k: {"id_token": "tok", "access_token": "a",
                             "refresh_token": "r"})
        monkeypatch.setattr(
            google_auth, "verify_id_token", lambda *a, **k: dict(self._IDENTITY))
        # Parked entries are never stale inside the test (now = far future).
        monkeypatch.setattr(webui_module, "_pending_created_ts", lambda p: 4_100_000_000)

    def _seed_pending(self, srv, state: str, claim: str):
        with srv.oauth_lock:
            srv.oauth_pending[state] = {
                "verifier": "v", "redirect_uri": "http://127.0.0.1/cb",
                "redirect_to": "/", "claim": claim,
                "created": "2026-01-01T00:00:00",
            }

    def _get(self, server, path: str, cookie: str | None = None):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Cookie": cookie} if cookie else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        location = resp.getheader("Location")
        set_cookie = resp.getheader("Set-Cookie")
        body = resp.read()
        conn.close()
        return status, location, set_cookie, body

    def _complete_callback(self, server, state: str) -> str:
        """Simulate Google redirecting back with ?code=..&state=.."""
        status, location, _, _ = self._get(
            server, f"/api/auth/google/callback?code=abc&state={state}")
        assert status == 302, f"callback should 302, got {status}"
        return location

    def _claim(self, server, code: str):
        status, location, set_cookie, raw = self._get(server, f"/api/auth/claim?code={code}")
        body = json.loads(raw.decode() or "{}")
        return status, body, set_cookie

    def test_claim_flow_parks_session_and_adopts(self, server, monkeypatch):
        self._patch_google(monkeypatch)
        srv, _ = server
        state, claim = "st1", "claim-abc123"
        self._seed_pending(srv, state, claim)
        # 1) Google redirects back; the session is parked, NOT cookie-set.
        location = self._complete_callback(server, state)
        assert location == "/login?claimed=1"
        # 2) The webview polls /api/auth/claim and adopts the session.
        status, body, set_cookie = self._claim(server, claim)
        assert status == 200 and body["ok"] is True
        assert body["me"]["email"] == self._IDENTITY["email"]
        assert set_cookie and "dourmouse_user_session=" in set_cookie
        # 3) The adopted cookie is a real, working session — /api/auth/me
        # recognizes it (the whole point of the bridge).
        sid = set_cookie.split("dourmouse_user_session=")[1].split(";")[0]
        status, _, _, raw = self._get(server, "/api/auth/me",
                                      cookie=f"dourmouse_user_session={sid}")
        assert status == 200
        assert json.loads(raw)["me"]["email"] == self._IDENTITY["email"]

    def test_claim_code_single_use(self, server, monkeypatch):
        self._patch_google(monkeypatch)
        srv, _ = server
        state, claim = "st2", "claim-single-use"
        self._seed_pending(srv, state, claim)
        self._complete_callback(server, state)
        status1, body1, _ = self._claim(server, claim)
        assert status1 == 200 and body1["ok"] is True
        # Second redemption of the same code must fail — single-use, ever.
        status2, body2, _ = self._claim(server, claim)
        assert status2 == 404 and body2["ok"] is False

    def test_claim_requires_code(self, server, monkeypatch):
        self._patch_google(monkeypatch)
        status, body, _ = self._claim(server, "")
        assert status == 400 and body["ok"] is False

    def test_unknown_claim_is_404(self, server, monkeypatch):
        self._patch_google(monkeypatch)
        status, body, _ = self._claim(server, "never-issued")
        assert status == 404 and body["ok"] is False

    def test_claim_flow_start_accepts_claim_param(self, server, monkeypatch):
        # The start endpoint must accept ?claim= (the webview's entry point)
        # and still 302 to Google consent.
        self._patch_google(monkeypatch)
        status, location, _, _ = self._get(
            server, "/api/auth/google/start?claim=claim-from-webview")
        assert status == 302
        assert location and location.startswith("https://accounts.google.com")
        # The claim code was recorded against the pending state.
        srv, _ = server
        pending = srv.oauth_pending.values()
        assert any(p.get("claim") == "claim-from-webview" for p in pending)

    def test_login_page_ships_chrome_bridge_and_fallback(self, server):
        """v5.22.12: the login page must open consent via the Chrome-first
        bridge (window.pywebview.api.open_external) and, when the browser
        cannot be opened, show a copyable link instead of silently
        navigating the blocked webview."""
        status, _, _, raw = self._get(server, "/login")
        assert status == 200
        html = raw.decode("utf-8", errors="replace")
        assert "open_external" in html
        assert "OPEN IN CHROME" in html
        assert "showManualLink" in html
        assert "startClaimPoll" in html
        # The plain-browser path (no webview) must still redirect directly.
        assert "IN_WEBVIEW" in html


class TestDeeplinkTargetsHashRouter:
    """v8.7: the deeplink 302 must land on the UI that has a hash router.

    /api/deeplink?to=atlas returns a 302 to an SPA hash route. The router
    that resolves #/atlas, #/world, #/portfolio etc lives ONLY in the HUD
    (ui/index.html); ui/console.html has no hash routing at all. When the
    console became the default at "/", a Location of "/" + "#/atlas" would
    still have returned 200 and simply dropped the user on the console home
    screen — a SILENT failure with no error anywhere. These pin the target.
    """

    def _get_no_redirect(self, server, path):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        status = resp.status
        location = resp.getheader("Location")
        resp.read()
        conn.close()
        return status, location

    def test_deeplink_redirects_to_the_hud_not_root(self, server):
        status, location = self._get_no_redirect(server, "/api/deeplink?to=atlas")
        assert status == 302
        assert location is not None
        assert location.startswith("/index.html#"), location
        assert not location.startswith("/#"), (
            "a bare '/' now serves the console, which has no hash router — "
            "this deeplink would silently do nothing"
        )

    def test_deeplink_hash_is_preserved(self, server):
        status, location = self._get_no_redirect(server, "/api/deeplink?to=atlas")
        assert status == 302
        assert location.endswith("#/atlas"), location


class TestFirstRunSetup:
    """v8.9: setup must never report a broken configuration as working.

    The original validator hit GET /v1/models, which NVIDIA serves WITHOUT
    checking auth — an obviously fake key returned HTTP 200 and setup told
    the user "key works". These pin the honest behaviour.
    """

    def test_malformed_key_rejected_without_network(self):
        from dourmouse.firstrun import validate_nvidia_key

        r = validate_nvidia_key("not-a-key")
        assert r["ok"] is False
        assert "nvapi-" in (r.get("hint") or "")

    def test_empty_key_rejected(self):
        from dourmouse.firstrun import validate_nvidia_key

        assert validate_nvidia_key("")["ok"] is False

    def test_validation_uses_an_authenticating_endpoint(self):
        """Guard the regression directly: /v1/models does NOT check keys."""
        from dourmouse import firstrun

        assert "chat/completions" in firstrun._NVIDIA_CHAT
        assert not hasattr(firstrun, "_NVIDIA_MODELS"), (
            "validating against /v1/models reports fake keys as valid"
        )

    def test_save_config_rejects_unknown_keys(self):
        """An allowlist stops a malformed payload writing arbitrary env vars."""
        from dourmouse.firstrun import save_config

        r = save_config({"EVIL_VAR": "x", "PATH": "/tmp"})
        assert r["ok"] is False

    def test_item_config_dir_is_outside_the_bundle(self):
        """Config must survive reinstall — never live beside the package."""
        from pathlib import Path

        from dourmouse.config import user_config_dir

        pkg = Path(__file__).resolve().parent.parent
        assert pkg not in user_config_dir().parents
