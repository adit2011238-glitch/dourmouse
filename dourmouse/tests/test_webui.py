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
import urllib.parse

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
    _is_imperative_affirm,
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

    def test_pending_items_empty_when_nothing_pending(self):
        gate = WebConfirmationGate(lambda e: None)
        assert gate.pending_items() == []

    def test_pending_items_lists_each_confirmation_awaiting_response(self):
        # Two independent gate() calls, each blocking in its own thread, can
        # coexist in _pending — pending_items() must surface both with their
        # ids and prompt text, for the "just say send" ambiguity check.
        gate = WebConfirmationGate(lambda e: None)
        results: dict[str, bool] = {}

        def run(key: str, prompt: str) -> None:
            results[key] = gate(prompt)

        t1 = threading.Thread(target=run, args=("a", "Send email to bob?"))
        t2 = threading.Thread(target=run, args=("b", "Delete the file?"))
        t1.start()
        t2.start()
        for _ in range(50):
            if len(gate.pending_items()) == 2:
                break
            time.sleep(0.02)
        items = gate.pending_items()
        assert len(items) == 2
        prompts = {text for _cid, text in items}
        assert prompts == {"Send email to bob?", "Delete the file?"}
        # Clean up both blocked threads.
        for cid, _text in items:
            gate.resolve(cid, True)
        t1.join(timeout=2)
        t2.join(timeout=2)
        assert results == {"a": True, "b": True}


class TestImperativeAffirmMatching:
    """Unit coverage for the "just say send" phrase matcher — exact,
    trimmed, case-insensitive whole-message matches only, never a substring
    hit inside ordinary conversation."""

    @pytest.mark.parametrize(
        "text",
        [
            "send it",
            "Send It",
            "  send it  ",
            "send it.",
            "send it!",
            "SEND IT!!",
            "yes",
            "go ahead",
            "do it",
            "confirm",
            "confirmed",
        ],
    )
    def test_matches_known_affirm_phrases(self, text):
        assert _is_imperative_affirm(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "let's go ahead and refactor this",
            "can you send it to the team tomorrow",
            "I don't want to do it",
            "sending the file now",
            "yes, but only after you check the logs",
            "please go ahead with caution",
        ],
    )
    def test_does_not_match_embedded_or_unrelated_text(self, text):
        assert _is_imperative_affirm(text) is False

    def test_bare_send_is_a_match(self):
        assert _is_imperative_affirm("send") is True


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

    def test_ui_html_served(self, server, monkeypatch):
        """v8.7: "/" serves the CONSOLE (the new default surface).

        v13: "/" redirects to /setup when config.is_configured() is False
        — root conftest.py's hermetic isolation deliberately makes that the
        DEFAULT test state (no real backend selected). This test is about
        what an already-configured install serves, so it opts into that
        state explicitly rather than relying on whatever real credentials
        happen to be sitting in a developer's own .env (the exact
        leakage the isolation fixture exists to prevent — see its own
        docstring for the real incident this test itself turned out to be
        quietly depending on).
        """
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")
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


class TestSessionTranscriptEndpoint:
    """GET /api/session/current and /api/session/<id> — reload-survival
    groundwork: the live ChatSession already writes one hash-chained JSONL
    record per turn (chat.py's _persist); these endpoints are the first
    thing that ever reads it back for a UI to rebuild a thread with."""

    def _get(self, port: int, path: str) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_current_reflects_a_real_turn_just_run(self, server):
        """Drive one real turn through /api/chat, then confirm
        /api/session/current returns it — proving this reads the SAME
        ledger _persist() writes, not a second/fabricated store."""
        srv, port = server
        srv.session.client = FakeClient(
            [_FakeResponse(_FakeMessage(content="hello back"))]
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat",
            body=json.dumps({"prompt": "hello there"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        while resp.readline():
            pass
        conn.close()
        assert resp.status == 200

        status, data = self._get(port, "/api/session/current")
        assert status == 200
        assert data["ok"] is True
        assert data["id"] == srv.session.session_file.stem
        assert len(data["turns"]) == 1
        turn = data["turns"][0]
        assert turn["user"] == "hello there"
        assert turn["final_text"] == "hello back"

    def test_current_before_any_turn_is_empty_not_missing(self, server):
        """A brand-new session file doesn't exist on disk until the first
        turn persists — current must say so honestly (ok:false, not a
        fabricated empty transcript), matching _persist()'s real timing."""
        srv, port = server
        status, data = self._get(port, "/api/session/current")
        assert status == 404
        assert data["ok"] is False

    def test_by_id_reads_a_past_sessions_full_turn_shape(self, server, tmp_path):
        """A concrete id (the same 'name' minus .jsonl that /api/sessions
        already lists) returns that file's real records, tool transcript
        included — not just the first/last-line summary /api/sessions/recent
        gives."""
        sessions_dir = tmp_path / "ws" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "session_20260801_000000.jsonl").write_text(
            json.dumps(
                {
                    "turn": 0,
                    "user": "run the echo tool",
                    "final_text": "done",
                    "transcript": [{"type": "tool_use", "name": "echo", "raw_arguments": "{}"}],
                }
            )
            + "\n"
        )
        srv, port = server
        status, data = self._get(port, "/api/session/session_20260801_000000")
        assert status == 200
        assert data["ok"] is True
        assert data["id"] == "session_20260801_000000"
        assert len(data["turns"]) == 1
        assert data["turns"][0]["user"] == "run the echo tool"
        assert data["turns"][0]["transcript"] == [
            {"type": "tool_use", "name": "echo", "raw_arguments": "{}"}
        ]
        # A record written before v13 (display_text/screen didn't exist yet)
        # must still resolve both — falling back to "user" and "HOME",
        # matching this exact record's only real behavior before the fields
        # existed.
        assert data["turns"][0]["display_text"] == "run the echo tool"
        assert data["turns"][0]["screen"] == "HOME"

    def test_focus_agent_turn_persists_raw_text_and_screen_separately(self, server):
        """v13: a real bug fixed — a focus_agent turn's `user` field is the
        internal "[ROUTING DIRECTIVE] ..." wrapper webui.py builds before
        calling session.ask(); `display_text` must stay the raw text the
        user actually typed, and `screen` must be whatever the request
        said, so the console's session-restore can show/re-file it
        correctly instead of leaking the wrapper onto the wrong thread."""
        srv, port = server
        srv.session.client = FakeClient(
            [_FakeResponse(_FakeMessage(content="OK-CLAUDE"))]
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat",
            body=json.dumps({
                "prompt": "reply with the exact text OK-CLAUDE",
                "focus_agent": "echo_agent",
                "screen": "CODE",
            }),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        while resp.readline():
            pass
        conn.close()
        assert resp.status == 200

        status, data = self._get(port, "/api/session/current")
        assert status == 200
        turn = data["turns"][0]
        assert turn["user"].startswith("[ROUTING DIRECTIVE]")
        assert "reply with the exact text OK-CLAUDE" in turn["user"]
        assert turn["display_text"] == "reply with the exact text OK-CLAUDE"
        assert turn["screen"] == "CODE"

    def test_unknown_id_is_404_not_500(self, server):
        srv, port = server
        status, data = self._get(port, "/api/session/session_no_such_file")
        assert status == 404
        assert data["ok"] is False

    def test_path_traversal_id_is_rejected(self, server):
        """session_id reaches straight into a filesystem path — anything
        outside [A-Za-z0-9_-] must be refused before it ever touches disk,
        the same discipline as the static-asset traversal guard above."""
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/session/" + urllib.parse.quote("../../../etc/passwd"))
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert resp.status == 404
        assert data["ok"] is False
        assert "invalid" in data["error"]


class TestVisionStatusEndpoint:
    """GET /api/vision/status — honest status roll-up for overlay.py,
    tray.py, wakeword.py, vision_bridge.py, proactive.py."""

    def _get(self, port: int) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/vision/status")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        conn.close()
        return data

    def test_returns_all_five_sections(self, server):
        srv, port = server
        data = self._get(port)
        for key in ("kill_switch", "overlay", "tray", "wakeword", "vision_bridge", "proactive"):
            assert key in data, key

    def test_kill_switch_defaults_both_armed_on_fresh_workspace(self, server):
        """No privacy_state.json written yet -> honest defaults (both
        enabled), matching tray.load_state()'s own documented default."""
        srv, port = server
        data = self._get(port)
        assert data["kill_switch"]["mic_enabled"] is True
        assert data["kill_switch"]["camera_enabled"] is True
        # tray.py's section carries the SAME real state, not a second copy.
        assert data["tray"]["kill_switch"]["mic_enabled"] is True
        assert data["tray"]["kill_switch"]["camera_enabled"] is True

    def test_kill_switch_reflects_real_persisted_state(self, server, tmp_path):
        """Writing the real on-disk flag (the same file tray.py/overlay.py
        share) must change what this endpoint reports — this is a real
        disk read, not a fabricated status."""
        import dourmouse.tray as tray_module

        srv, port = server
        state_path = tmp_path / "ws" / "privacy_state.json"
        tray_module.save_state(
            tray_module.KillSwitchState(mic_enabled=False, camera_enabled=True, updated_at="x"),
            state_path,
        )
        data = self._get(port)
        assert data["kill_switch"]["mic_enabled"] is False
        assert data["kill_switch"]["camera_enabled"] is True

    def test_overlay_and_tray_running_state_is_honestly_unknown(self, server):
        """Neither is started by this web server or the native launcher —
        the endpoint must say "unknown", never fabricate on/off."""
        srv, port = server
        data = self._get(port)
        assert data["overlay"]["running"] == "unknown"
        assert data["tray"]["running"] == "unknown"

    def test_wakeword_disabled_by_default_and_listening_unknown(self, server, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_WAKEWORD", raising=False)
        srv, port = server
        data = self._get(port)
        assert data["wakeword"]["enabled"] is False
        assert data["wakeword"]["listening"] == "unknown"
        # honesty caveat about unverified live mic capture must be surfaced
        assert "microphone" in data["wakeword"]["note"].lower()

    def test_vision_bridge_unreachable_reports_honestly(self, server, monkeypatch):
        """No dourmouse.tray process is running in this test, so nothing is
        actually listening on the configured bridge port -> reachable must
        be False with a real error, never a fabricated True."""
        monkeypatch.setenv("DOURMOUSE_VISION_BRIDGE_PORT", "18766")
        srv, port = server
        data = self._get(port)
        assert data["vision_bridge"]["configured_port"] == 18766
        assert data["vision_bridge"]["reachable"] is False
        assert data["vision_bridge"]["state"] is None
        assert data["vision_bridge"]["error"]

    def test_vision_bridge_reachable_reports_real_live_state(self, server, monkeypatch):
        """Start a REAL VisionBridgeServer and confirm the endpoint's probe
        genuinely reaches it and relays its real state, not a canned one."""
        from dourmouse.tray import KillSwitchState
        from dourmouse.vision_bridge import VisionBridgeServer

        bridge = VisionBridgeServer(
            state_reader=lambda: KillSwitchState(mic_enabled=False, camera_enabled=False, updated_at="t"),
            port=0,
        )
        ok, detail = bridge.start()
        assert ok, detail
        try:
            monkeypatch.setenv("DOURMOUSE_VISION_BRIDGE_PORT", str(bridge.port))
            srv, port = server
            data = self._get(port)
            assert data["vision_bridge"]["reachable"] is True
            assert data["vision_bridge"]["state"]["mic_enabled"] is False
            assert data["vision_bridge"]["state"]["camera_enabled"] is False
        finally:
            bridge.stop()

    def test_proactive_allowlist_and_wiring_flags(self, server, monkeypatch):
        srv, port = server
        data = self._get(port)
        assert set(data["proactive"]["allowed_alert_kinds"]) == {"system", "world", "atlas"}
        assert "market" not in data["proactive"]["allowed_alert_kinds"]
        assert isinstance(data["proactive"]["events_hub_present"], bool)
        assert isinstance(data["proactive"]["env_enabled"], bool)

    def test_proactive_env_disabled_is_reflected(self, server, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_PROACTIVE_SURFACE", "0")
        srv, port = server
        data = self._get(port)
        assert data["proactive"]["env_enabled"] is False


class TestVisionKillSwitchEndpoint:
    """POST /api/vision/kill-switch — a real toggle onto the SAME shared
    state file dourmouse/tray.py's KillSwitch owns."""

    def _post(self, port: int, body: dict) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/vision/kill-switch",
            body=json.dumps(body), headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        status = resp.status
        conn.close()
        return status, data

    def _get_status(self, port: int) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/vision/status")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return data

    def test_kill_all_flips_both_off_and_persists(self, server, tmp_path):
        srv, port = server
        status, data = self._post(port, {"action": "kill_all"})
        assert status == 200
        assert data["ok"] is True
        assert data["kill_switch"]["mic_enabled"] is False
        assert data["kill_switch"]["camera_enabled"] is False
        # a fresh GET /api/vision/status must see the SAME real write, not a
        # second copy of the truth.
        follow_up = self._get_status(port)
        assert follow_up["kill_switch"]["mic_enabled"] is False
        assert follow_up["kill_switch"]["camera_enabled"] is False

    def test_set_mic_toggles_independently(self, server):
        srv, port = server
        status, data = self._post(port, {"action": "set_mic", "enabled": False})
        assert status == 200
        assert data["kill_switch"]["mic_enabled"] is False
        assert data["kill_switch"]["camera_enabled"] is True

    def test_set_camera_toggles_independently(self, server):
        srv, port = server
        status, data = self._post(port, {"action": "set_camera", "enabled": False})
        assert status == 200
        assert data["kill_switch"]["camera_enabled"] is False
        assert data["kill_switch"]["mic_enabled"] is True

    def test_unknown_action_is_rejected_400(self, server):
        srv, port = server
        status, data = self._post(port, {"action": "bogus"})
        assert status == 400
        assert data["ok"] is False


class TestWorkspaceRoute:
    """GET /workspace, /workspace.html — serves ui/workspace.html, the
    Vision floating multi-window workspace (world-monitor-expansion)."""

    def _get(self, port: int, path: str) -> tuple[int, str, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        ctype = resp.getheader("Content-Type", "")
        body = resp.read()
        status = resp.status
        conn.close()
        return status, ctype, body

    def test_workspace_serves_html(self, server):
        srv, port = server
        status, ctype, body = self._get(port, "/workspace")
        assert status == 200
        assert "html" in ctype
        assert b"DOURMOUSE" in body.upper() or b"dourmouse" in body.lower()

    def test_workspace_html_alias_serves_same_file(self, server):
        srv, port = server
        status, ctype, body = self._get(port, "/workspace.html")
        assert status == 200
        assert "html" in ctype


class TestVoiceCommandEndpoint:
    """POST /api/voice/command — the one real place
    dourmouse.voice_commands.parse_voice_command runs server-side."""

    def _post(self, port: int, text: str) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/voice/command",
            body=json.dumps({"text": text}), headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        status = resp.status
        conn.close()
        return status, data

    def test_recognized_email_command(self, server):
        srv, port = server
        status, data = self._post(port, "email sam saying running late")
        assert status == 200
        assert data["ok"] is True
        assert data["recognized"] is True
        assert data["command"]["action"] == "email"
        assert data["command"]["args"] == {"person": "sam", "message": "running late"}

    def test_recognized_open_panel_command(self, server):
        srv, port = server
        status, data = self._post(port, "open the mail panel")
        assert status == 200
        assert data["recognized"] is True
        assert data["command"]["action"] == "open_panel"
        assert data["command"]["args"] == {"panel": "mail"}

    def test_unrecognized_text_reports_not_recognized_not_an_error(self, server):
        srv, port = server
        status, data = self._post(port, "what's the weather like")
        assert status == 200
        assert data["ok"] is True
        assert data["recognized"] is False
        assert "command" not in data

    def test_empty_text_reports_not_recognized(self, server):
        srv, port = server
        status, data = self._post(port, "")
        assert status == 200
        assert data["recognized"] is False


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

    def test_focus_agent_with_commas_never_gets_split_into_a_multi_agent_plan(self, server):
        """v13: a real bug fixed here, live-caught through an actual
        directive against the CODE screen's "docs" toolchain — a request
        with real commas in it ("make a slideshow explaining X, Y, and Z.
        Create it in my Drive.") used to get cut by build_plan()'s
        comma-splitting fallback into multiple fragments routed to
        DIFFERENT WRONG agents, because focus_agent only ever wrapped the
        prompt in a "[ROUTING DIRECTIVE]..." sentence and never set the
        real forced_agent dispatch.py already built to bypass exactly this
        (see run_dispatch_messages' own forced_agent docstring). A real
        forced_agent turn must produce ZERO "plan" transcript events —
        build_plan() must never run at all when an agent is pinned."""
        srv, port = server
        srv.session.client = FakeClient(
            [_FakeResponse(_FakeMessage(content="done"))]
        )
        # This test's server fixture registers "echo_agent" — any real
        # subagent name works identically for this assertion.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat",
            body=json.dumps({
                "prompt": "make a slideshow explaining what this is, how it "
                          "works, and what it does. Create it in my drive.",
                "focus_agent": "echo_agent",
            }),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        events = []
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))
        conn.close()
        assert resp.status == 200
        assert not any(e.get("type") == "plan" for e in events), (
            "focus_agent turn produced a 'plan' event — build_plan() ran "
            "despite an agent being pinned, meaning forced_agent never "
            "reached dispatch.py"
        )

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

    def test_just_say_send_resolves_the_one_pending_confirmation(self, server):
        """"send it" on a second chat request approves the single pending
        confirmation through the SAME resolver POST /api/confirm uses,
        instead of starting a normal chat turn."""
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
        assert confirm_id is not None

        # "send it" on a second connection, instead of a UI-click POST to
        # /api/confirm with the exact id.
        conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn2.request(
            "POST",
            "/api/chat",
            body=json.dumps({"prompt": "send it"}),
            headers={"Content-Type": "application/json"},
        )
        resp2 = conn2.getresponse()
        assert resp2.status == 200
        events2 = []
        while True:
            line = resp2.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                events2.append(json.loads(line[6:]))
        conn2.close()

        resolved = next(e for e in events2 if e["type"] == "confirmation_resolved")
        assert resolved["id"] == confirm_id
        assert resolved["approved"] is True
        assert resolved["ok"] is True
        assert events2[-1]["type"] == "done"

        # The original stream continues and completes with the tool result —
        # proof the intercept resolved the SAME pending confirmation via the
        # real resolver, not a fake/parallel approval.
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

    def test_just_say_send_falls_through_to_chat_when_nothing_pending(self, server):
        """With zero confirmations pending, an affirm-shaped message like
        "send it" is NOT special-cased — it goes through as an ordinary chat
        turn, so legitimate chat content is never false-triggered."""
        srv, port = server
        srv.session.client = FakeClient(
            [_FakeResponse(_FakeMessage(content="Sent what, exactly?"))]
        )
        events = self._stream_events(port, "send it")
        types = [e["type"] for e in events]
        assert "confirmation_resolved" not in types
        assert types[-1] == "done"
        assert events[-1]["final_text"] == "Sent what, exactly?"

    def test_just_say_send_asks_which_one_when_multiple_pending(self, server):
        """More than one confirmation pending -> never guess; list them and
        let the user pick, without resolving either."""
        srv, port = server
        results: dict[str, bool] = {}

        def run(key: str, prompt_text: str) -> None:
            results[key] = srv.gate(prompt_text)

        t1 = threading.Thread(target=run, args=("a", "Send email to bob?"))
        t2 = threading.Thread(target=run, args=("b", "Delete the file?"))
        t1.start()
        t2.start()
        try:
            for _ in range(50):
                if len(srv.gate.pending_items()) == 2:
                    break
                time.sleep(0.02)
            assert len(srv.gate.pending_items()) == 2

            events = self._stream_events(port, "send it")
            types = [e["type"] for e in events]
            assert "confirmation_resolved" not in types
            assert types[-1] == "done"
            final_text = events[-1]["final_text"]
            assert "Send email to bob?" in final_text
            assert "Delete the file?" in final_text

            # Neither pending confirmation was resolved by the ambiguous
            # "send it" — both are still awaiting a real answer.
            assert len(srv.gate.pending_items()) == 2
        finally:
            for cid, _text in srv.gate.pending_items():
                srv.gate.resolve(cid, False)
            t1.join(timeout=2)
            t2.join(timeout=2)


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

    def test_index_has_pwa_head_tags(self, server, monkeypatch):
        """Whatever "/" serves must be installable.

        v8.7: this caught a real regression — promoting the console to the
        default route dropped the manifest/apple-touch tags with it, which
        would have silently killed "Add to Home Screen" and the standalone
        window. Asserted on "/" (not a fixed file) so the NEXT change of
        default surface has to carry the install metadata too.

        v13: opts into a configured backend explicitly — see
        test_ui_html_served's own docstring for why "/" needs this now.
        """
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")
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


class TestSetupWizardGoogleStep:
    """v8.19: Google sign-in moved into the first-run setup wizard, as a
    skippable step (ui/setup.html). UI wiring only — the OAuth flow itself
    (google_auth.py, the claim bridge in webui.py) is unchanged and its
    core mechanics are already covered by TestSystemBrowserClaimFlow. These
    pin two things the new caller depends on: (1) the setup page actually
    SHIPS the step wired to the real endpoints, not a decorative stub, and
    (2) the callback's cancel/deny path — previously untested anywhere in
    this suite — degrades gracefully instead of leaving a parked state or
    a hung poll (Rule 2.2: honest failure, never silent).
    """

    _IDENTITY = {"email": "wizard@example.com", "name": "Wizard User",
                 "picture": "", "sub": "sub-999"}

    def _patch_google(self, monkeypatch):
        """Same hermetic patch as TestSystemBrowserClaimFlow — no network."""
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
        monkeypatch.setattr(webui_module, "_pending_created_ts", lambda p: 4_100_000_000)

    def _seed_pending(self, srv, state: str, claim: str):
        with srv.oauth_lock:
            srv.oauth_pending[state] = {
                "verifier": "v", "redirect_uri": "http://127.0.0.1/cb",
                "redirect_to": "/", "claim": claim,
                "created": "2026-01-01T00:00:00",
            }

    def _get(self, server, path: str):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        status = resp.status
        location = resp.getheader("Location")
        body = resp.read()
        conn.close()
        return status, location, body

    def test_setup_page_ships_google_step_wired_to_real_endpoints(self, server):
        """The step must call the real, already-working OAuth surface —
        not a placeholder button with no handler (the exact regression the
        login page's claim bridge was built to catch, v5.22.11/12)."""
        status, _, raw = self._get(server, "/setup")
        assert status == 200
        html = raw.decode("utf-8", errors="replace")
        assert "/api/auth/google/start?claim=" in html
        assert "/api/auth/claim?code=" in html
        assert "/api/auth/status" in html
        assert "/api/auth/me" in html
        assert "Connect Google" in html
        # the desktop app's webview bridge is honored, same as /login
        assert "pywebview" in html and "open_external" in html

    def test_setup_google_step_skip_button_is_never_gated(self, server):
        """Requirement: this integration must remain entirely optional.
        SKIP is a bare onclick with no disabled attribute anywhere near
        it — unlike CONTINUE on the brain-choice step, which IS gated."""
        status, _, raw = self._get(server, "/setup")
        html = raw.decode("utf-8", errors="replace")
        assert 'id="gSkipBtn" onclick="gAdvance()">SKIP<' in html

    def test_claim_flow_works_when_initiated_from_the_setup_wizard(self, server, monkeypatch):
        """The exact sequence the new step performs: start with a claim
        code, Google redirects back, the wizard polls /api/auth/claim and
        adopts the session — the same server wiring the login page's
        webview bridge already uses, now exercised for this new caller."""
        self._patch_google(monkeypatch)
        srv, _ = server
        state, claim = "wizard-state-1", "wizard-claim-1"
        self._seed_pending(srv, state, claim)

        status, location, _ = self._get(
            server, f"/api/auth/google/callback?code=abc&state={state}")
        assert status == 302
        assert location == "/login?claimed=1"

        status, _, raw = self._get(server, f"/api/auth/claim?code={claim}")
        body = json.loads(raw.decode() or "{}")
        assert status == 200 and body["ok"] is True
        assert body["me"]["email"] == self._IDENTITY["email"]
        assert body["me"]["name"] == self._IDENTITY["name"]

    def test_denied_consent_never_parks_a_claim(self, server, monkeypatch):
        """Regression, previously untested anywhere in this suite: if the
        user cancels on Google's consent screen, the callback returns
        error=access_denied and must NOT park anything under the claim
        code. The wizard's poll must keep getting an honest 404 forever —
        never a phantom success, never a crash — which is what lets it
        time out to a friendly retry instead of hanging (requirement 4)."""
        self._patch_google(monkeypatch)
        srv, _ = server
        state, claim = "wizard-state-denied", "wizard-claim-denied"
        self._seed_pending(srv, state, claim)

        status, location, raw = self._get(
            server, f"/api/auth/google/callback?code=&state={state}&error=access_denied")
        assert status == 302
        assert location == "/login?reason=denied"
        assert raw == b""  # no traceback, no body leaked on the honest path

        status2, _, raw2 = self._get(server, f"/api/auth/claim?code={claim}")
        body2 = json.loads(raw2.decode() or "{}")
        assert status2 == 404
        assert body2["ok"] is False

        # The consumed state cannot be replayed either — single-use even
        # on a denial, so a retried/duplicated Google redirect can't revive it.
        status3, _, _ = self._get(
            server, f"/api/auth/google/callback?code=abc&state={state}")
        assert status3 == 400


class TestGmailInboxWarmCache:
    """world-monitor-expansion (UX pass item 5): the "" query 'recent
    inbox' listing reads a warm server-side cache instead of paying a live
    IMAP round trip on every COMMS open; a real search always bypasses it.
    """

    def _get(self, server, path: str):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        status = resp.status
        conn.close()
        return status, body

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        """The warm cache is real module-level state (by design — it must
        survive across requests) — reset it around every test so none of
        these leak into each other or into unrelated suites."""
        webui_module._gmail_inbox_cache["at"] = 0.0
        webui_module._gmail_inbox_cache["max_results"] = None
        webui_module._gmail_inbox_cache["payload"] = None
        yield
        webui_module._gmail_inbox_cache["at"] = 0.0
        webui_module._gmail_inbox_cache["max_results"] = None
        webui_module._gmail_inbox_cache["payload"] = None

    def test_empty_query_cold_start_falls_back_to_live_fetch_and_warms_cache(self, server, monkeypatch):
        calls = []

        def fake_search(query, max_results):
            calls.append((query, max_results))
            return "- [2026-01-01 00:00] from a@b.com | Subject one (uid 1)"

        monkeypatch.setattr(
            "dourmouse.google_services.gmail_search", fake_search
        )
        status, raw = self._get(server, "/api/gmail/search?q=&max_results=25")
        body = json.loads(raw.decode())
        assert status == 200
        assert body["ok"] is True
        assert body["rows"][0]["subject"] == "Subject one"
        assert calls == [("", 25)]  # ONE live fetch — the genuine cold start
        # and it's now cached: a second request must NOT call gmail_search again
        status2, raw2 = self._get(server, "/api/gmail/search?q=&max_results=25")
        body2 = json.loads(raw2.decode())
        assert status2 == 200
        assert body2 == body
        assert calls == [("", 25)]  # still just the one call — served from cache

    def test_nonempty_query_never_uses_the_cache(self, server, monkeypatch):
        calls = []

        def fake_search(query, max_results):
            calls.append((query, max_results))
            return "- [2026-01-01 00:00] from a@b.com | Subject one (uid 1)"

        monkeypatch.setattr(
            "dourmouse.google_services.gmail_search", fake_search
        )
        self._get(server, "/api/gmail/search?q=invoice&max_results=25")
        self._get(server, "/api/gmail/search?q=invoice&max_results=25")
        # a real search hits gmail_search live EVERY time — never cached
        assert calls == [("invoice", 25), ("invoice", 25)]

    def test_cache_get_respects_ttl_and_max_results(self, monkeypatch):
        """Unit-level: _gmail_inbox_cache_get is a pure cache read — never
        fetches — and only counts as a hit for the SAME max_results,
        within TTL."""
        monkeypatch.setenv("DOURMOUSE_GMAIL_INBOX_TTL", "60")
        assert webui_module._gmail_inbox_cache_get(25) is None  # nothing warmed yet
        webui_module._gmail_inbox_cache["at"] = time.monotonic()
        webui_module._gmail_inbox_cache["max_results"] = 25
        webui_module._gmail_inbox_cache["payload"] = {"ok": True, "rows": []}
        assert webui_module._gmail_inbox_cache_get(25) == {"ok": True, "rows": []}
        assert webui_module._gmail_inbox_cache_get(50) is None  # different max_results
        webui_module._gmail_inbox_cache["at"] = time.monotonic() - 999  # long expired
        assert webui_module._gmail_inbox_cache_get(25) is None


class TestWarmCacheWarmers:
    """Both background warmer threads (item 5): opt-out env vars, and the
    Gmail one's own self-gate on whether Gmail is even configured."""

    def test_gmail_warmer_noop_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(
            "dourmouse.google_services.gmail_configured", lambda: False
        )
        assert webui_module.start_gmail_inbox_warmer() is False

    def test_gmail_warmer_noop_when_env_disabled(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GMAIL_WARMER", "0")
        assert webui_module.start_gmail_inbox_warmer() is False

    def test_world_pulse_warmer_noop_when_env_disabled(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORLD_PULSE_WARMER", "0")
        assert webui_module.start_world_pulse_warmer() is False

    def test_world_pulse_warmer_refreshes_the_real_cache(self, monkeypatch):
        """Integration: the warmer thread really calls
        world_pulse_snapshot(force=True) on its loop — proven by pointing
        the TTL very low and watching a REAL (monkeypatched) snapshot
        counter tick past 1 within a couple of intervals, then stopping
        the thread cleanly."""
        monkeypatch.setenv("DOURMOUSE_WORLD_PULSE_WARMER", "1")
        monkeypatch.setenv("DOURMOUSE_WORLD_PULSE_TTL", "0.2")  # -> refresh every ~0.1s (floored at 1.0s by the warmer)
        calls = []

        def fake_snapshot(force=False):
            calls.append(force)
            return {}

        monkeypatch.setattr(
            "dourmouse.world_pulse.world_pulse_snapshot", fake_snapshot
        )
        try:
            assert webui_module.start_world_pulse_warmer() is True
            deadline = time.time() + 3
            while time.time() < deadline and len(calls) < 1:
                time.sleep(0.05)
        finally:
            webui_module.stop_world_pulse_warmer()
        assert calls, "warmer never called world_pulse_snapshot"
        assert all(c is True for c in calls)  # always a forced refresh, never a lazy read

    def test_world_pulse_warmer_is_idempotent(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORLD_PULSE_TTL", "60")
        monkeypatch.setattr(
            "dourmouse.world_pulse.world_pulse_snapshot", lambda force=False: {}
        )
        try:
            assert webui_module.start_world_pulse_warmer() is True
            assert webui_module.start_world_pulse_warmer() is True  # already running -> no second thread
        finally:
            webui_module.stop_world_pulse_warmer()


def _registry_with_code_claude() -> DispatchRegistry:
    """echo_agent (real tools, for the existing focus_agent tests) plus a
    real "code_claude" subagent name — just enough to pass the focus_agent
    validation check in _handle_chat_authed; the CLAUDE CODE passthrough
    path never actually calls any of its tools (it bypasses the whole
    orchestrator/tool loop, see _handle_code_claude_passthrough)."""
    r = _echo_registry()
    r.register_subagent(
        Subagent(name="code_claude", domain="Code", description="claude cli", tools=())
    )
    return r


@pytest.fixture
def code_claude_server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr(webui_module, "_CONFIRM_TIMEOUT_SECONDS", 5.0)
    srv = run_server(_registry_with_code_claude(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


class TestCodeClaudePassthrough:
    """v13.2: focus_agent == "code_claude" talks DIRECTLY to
    code_backends.stream_claude, live, bypassing run_dispatch_messages and
    the ROUTING DIRECTIVE wrapper entirely — explicit user request ("I
    only want to be talking to claude directly")."""

    def _stream(self, port, prompt):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat",
            body=json.dumps({"prompt": prompt, "focus_agent": "code_claude", "screen": "CODE"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        events = []
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))
        conn.close()
        return resp.status, events

    def test_raw_prompt_reaches_stream_claude_unwrapped(self, code_claude_server, monkeypatch):
        seen = {}

        def fake_stream_claude(task, **kwargs):
            seen["task"] = task
            return "ok"

        monkeypatch.setattr(
            "dourmouse.code_backends.stream_claude", fake_stream_claude
        )
        status, events = self._stream(code_claude_server[1], "list the files here")
        assert status == 200
        # No "[ROUTING DIRECTIVE]" wrapper — Claude gets the user's exact words.
        assert seen["task"] == "list the files here"

    def test_deltas_and_thinking_and_tool_use_reach_the_sse_stream(self, code_claude_server, monkeypatch):
        def fake_stream_claude(task, *, cwd, timeout, on_delta,
                                on_thinking=None, on_tool_use=None, on_tool_result=None):
            on_thinking("reasoning...")
            on_delta("Hel")
            on_delta("lo.")
            on_tool_use("Bash", "")
            on_tool_use("Bash", '{"command":"ls"}')
            on_tool_result("file1.py")
            return "Hello."

        monkeypatch.setattr(
            "dourmouse.code_backends.stream_claude", fake_stream_claude
        )
        status, events = self._stream(code_claude_server[1], "say hello")
        assert status == 200
        types = [e["type"] for e in events]
        assert "thinking_delta" in types
        assert types.count("assistant_delta") == 2
        assert any(e["type"] == "tool_use" and e["name"] == "Bash" for e in events)
        assert any(e["type"] == "tool_result" and e["text"] == "file1.py" for e in events)
        done = next(e for e in events if e["type"] == "done")
        assert done["final_text"] == "Hello."

    def test_real_error_surfaces_as_an_error_event_not_fabricated_success(
        self, code_claude_server, monkeypatch
    ):
        def fake_stream_claude(task, **kwargs):
            raise RuntimeError("NOT CONFIGURED: the Claude Code CLI ('claude') was not found on PATH.")

        monkeypatch.setattr(
            "dourmouse.code_backends.stream_claude", fake_stream_claude
        )
        status, events = self._stream(code_claude_server[1], "do something")
        assert status == 200
        assert any(
            e["type"] == "error" and "NOT CONFIGURED" in e["message"] for e in events
        )
        assert not any(e["type"] == "done" for e in events)

    def test_turn_is_recorded_on_the_code_screen_not_home(self, code_claude_server, monkeypatch):
        monkeypatch.setattr(
            "dourmouse.code_backends.stream_claude", lambda task, **kwargs: "result text"
        )
        srv, port = code_claude_server
        self._stream(port, "write a function")
        lines = srv.session.session_file.read_text(encoding="utf-8").strip().splitlines()
        last = json.loads(lines[-1])
        assert last["screen"] == "CODE"
        assert last["final_text"] == "result text"
        assert last["user"] == "write a function"
