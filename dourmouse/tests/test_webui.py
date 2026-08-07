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
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read().decode()
        assert "DOURMOUSE // CENTRAL AGENT DISPATCH" in body
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

    def test_chat_without_focus_uses_session_default_model(self, server):
        """No focus_agent -> the session's default config model drives the
        run (no per-agent override injected)."""
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
