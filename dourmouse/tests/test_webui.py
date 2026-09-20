"""Web UI server tests (dourmouse/webui.py + ui/index.html serving).

Runs a real ThreadingHTTPServer on an ephemeral port with a fake
OpenAI-shaped client so the SSE chat, confirmation gate, and API endpoints
are exercised over actual HTTP (Integration Rule 7.3 discipline). The fake
client only shapes the LLM side; tool behavior is the real roster/engine.
"""

from __future__ import annotations

import http.client
import json
import pathlib
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
    _SSEStream,
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


class TestGoalRuntimeWiring:
    """run_server's goal_runtime wiring (webui.py, just above the
    `all_hands`/`atlas_lab` hub binding) had zero direct test coverage
    before 2026-09-18's opt-in-to-opt-out flip -- worth real coverage
    now given the stakes of that default change. The `server` fixture
    above never exercises this path directly since the autouse
    `_goal_runtime_off` conftest fixture keeps it off there by design;
    these two tests explicitly opt back in/out themselves, the same
    override-the-fixture convention every other isolation fixture in
    this suite uses."""

    def test_enabled_by_default_a_real_worker_thread_starts(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setenv("DOURMOUSE_GOAL_RUNTIME", "1")
        srv = run_server(_echo_registry(), port=0, client=None, config=None)
        try:
            assert srv.goal_runtime is not None
            assert srv.goal_runtime._thread is not None
            assert srv.goal_runtime._thread.is_alive()
        finally:
            srv.goal_runtime.stop()
            srv.server_close()

    def test_explicit_opt_out_starts_no_worker(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setenv("DOURMOUSE_GOAL_RUNTIME", "0")
        srv = run_server(_echo_registry(), port=0, client=None, config=None)
        try:
            assert srv.goal_runtime is None
        finally:
            srv.server_close()


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

    def test_auto_approve_setting_bypasses_the_gate_entirely(self, monkeypatch):
        """2026-09-14, live-caught: "approval keeps failing, remove the
        need for approval, make this a toggle in settings". When on, the
        gate returns True immediately and never emits confirmation_
        requested at all — a real bypass, not a fake auto-click on an
        event nothing ever renders."""
        monkeypatch.setattr("dourmouse.config.auto_approve_enabled", lambda: True)
        events = []
        gate = WebConfirmationGate(events.append)
        assert gate("delete everything?") is True
        assert events == []

    def test_auto_approve_off_still_blocks_as_before(self, monkeypatch):
        monkeypatch.setattr("dourmouse.config.auto_approve_enabled", lambda: False)
        monkeypatch.setattr(webui_module, "_CONFIRM_TIMEOUT_SECONDS", 0.05)
        gate = WebConfirmationGate(lambda e: None)
        assert gate("anything?") is False

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


class TestSecurityEndpoint:
    """GET /api/security — real network/host telemetry (Phase 4,
    docs/GODSPEED_ROADMAP.md). Runs against the REAL platform adapter (no
    monkeypatching) since it only ever reads this test machine's own,
    already-real network state — the same thing test_security_platform_adapter.py
    already proves is honest about failures, so this just proves the
    route is wired."""

    def test_returns_the_real_expected_shape(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/security")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert set(data.keys()) == {
            "interfaces", "default_gateway", "dns", "arp_neighbors",
            "listening_ports", "established_connections", "firewall",
        }
        for section in data.values():
            assert "available" in section
        conn.close()


class TestSecurityDashboardEndpoint:
    """GET /api/security_dashboard -- Domain I's own dashboard UI
    (docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md section 11, item 6, the
    last-scoped piece after the sentry itself was proven real). Isolated
    via a real, per-test SQLite file for the sentry store, and a directly-
    assigned `server.security_sentry` so tests never race the server's
    own real background SentryRuntime thread."""

    @pytest.fixture(autouse=True)
    def _isolated_sentry_store(self, tmp_path, monkeypatch):
        import dourmouse.security.sentry as sentry_module

        monkeypatch.setattr(sentry_module, "DEFAULT_DB", tmp_path / "sentry.db")

    def test_no_scan_yet_is_honest(self, server):
        srv, port = server
        srv.security_sentry = None
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/security_dashboard")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["scanned"] is False
        assert data["risk_score"] == 0.0
        assert data["findings"] == []
        conn.close()

    def test_a_real_scan_result_is_reported(self, server):
        from dourmouse.security.sentry import SentryFinding, SentryScanResult

        finding = SentryFinding(
            fingerprint="fp1", kind="firewall_disabled", severity="high",
            title="Application Firewall is disabled", detail="d", recommended_action="a",
        )
        result = SentryScanResult(
            all_findings=[finding], new_findings=[finding], suppressed_false_positives=[],
            risk_score=9.0, telemetry_available={"firewall": True}, alerts_written=1,
        )
        srv, port = server
        srv.security_sentry = type("R", (), {"last_result": result, "last_scan_at": 1234.0, "tick_count": 3})()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/security_dashboard")
        data = json.loads(conn.getresponse().read())
        assert data["scanned"] is True
        assert data["risk_score"] == 9.0
        assert data["tick_count"] == 3
        assert data["findings_by_severity"]["high"] == 1
        assert len(data["findings"]) == 1
        assert data["findings"][0]["is_new"] is True
        conn.close()

    def test_known_device_count_is_reported(self, server):
        from dourmouse.security.sentry import DEFAULT_DB, SentryStore

        SentryStore(DEFAULT_DB).record_devices(
            [{"hostname": "a", "ip": "1.2.3.4", "mac": "aa:aa:aa:aa:aa:aa", "interface": "en0"}], now=1000.0,
        )
        srv, port = server
        srv.security_sentry = None
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/security_dashboard")
        data = json.loads(conn.getresponse().read())
        assert data["known_device_count"] == 1
        conn.close()

    def test_incidents_by_status_counts_real_incidents(self, server):
        from dourmouse.security.sentry import DEFAULT_DB, SentryFinding, SentryStore

        store = SentryStore(DEFAULT_DB)
        finding = SentryFinding(fingerprint="fp1", kind="k", severity="med", title="t", detail="d", recommended_action="a")
        store.record_and_classify(finding, now=1000.0)
        store.open_incident("fp1", "", now=1000.0)
        srv, port = server
        srv.security_sentry = None
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/security_dashboard")
        data = json.loads(conn.getresponse().read())
        assert data["incidents_by_status"]["OPEN"] == 1
        conn.close()


class TestGoalsEndpoint:
    """GET /api/goals — read-only inspection of the Phase 2 autonomous
    Goal/Task runtime (docs/GODSPEED_ROADMAP.md). Isolated from whatever
    the process-wide goal-store singleton otherwise holds, same pattern
    as test_goal_tools.py's own isolation fixture."""

    @pytest.fixture(autouse=True)
    def _isolated_goal_store(self):
        from dourmouse.goals import GoalStore, set_goal_store

        set_goal_store(GoalStore(None))
        yield
        set_goal_store(None)

    def test_no_goals_yet_is_an_honest_empty_list(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/goals")
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read()) == {"goals": []}
        conn.close()

    def test_lists_a_real_goal(self, server):
        from dourmouse.goals import get_goal_store

        get_goal_store().create_goal("Do the real thing")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/goals")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert len(data["goals"]) == 1
        assert data["goals"][0]["objective"] == "Do the real thing"
        conn.close()

    def test_id_returns_the_full_snapshot_including_tasks_and_events(self, server):
        from dourmouse.goals import get_goal_store

        goal = get_goal_store().create_goal("Snapshot me")
        get_goal_store().create_task(goal["id"], "one step")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/api/goals?id={goal['id']}")
        resp = conn.getresponse()
        data = json.loads(resp.read())["goal"]
        assert len(data["tasks"]) == 1
        assert any(e["type"] == "goal_created" for e in data["events"])
        conn.close()

    def test_unknown_id_is_a_real_404_not_a_fabricated_empty_goal(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/goals?id=goal_doesnotexist")
        resp = conn.getresponse()
        assert resp.status == 404
        conn.close()

    def test_status_filter_is_applied(self, server):
        from dourmouse.goals import get_goal_store

        store = get_goal_store()
        keep = store.create_goal("Keep")
        store.update_goal_status(keep["id"], "EXECUTING")
        store.create_goal("Drop")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/goals?status=executing")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert [g["objective"] for g in data["goals"]] == ["Keep"]
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
        assert "<title>DOURMOUSE</title>" in body
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


class TestDeviceWikiEndpoint:
    """GET /api/device_wiki -- Domain E's own read-only inspection route
    over the device wiki's real persisted state (docs/GODSPEED_ROADMAP.md
    Domain E, step 6). Isolated via a real, per-test SQLite file."""

    @pytest.fixture(autouse=True)
    def _isolated_wiki_store(self, tmp_path, monkeypatch):
        import dourmouse.device_wiki.store as wiki_store_module

        monkeypatch.setattr(wiki_store_module, "DEFAULT_DB", tmp_path / "wiki.db")

    def test_no_entries_yet_is_an_honest_empty_list(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/device_wiki")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["entries"] == []
        conn.close()

    def test_lists_a_real_entry(self, server, tmp_path):
        from dourmouse.device_wiki.core import with_new_entry, with_summary
        from dourmouse.device_wiki.store import DEFAULT_DB, WikiStore

        WikiStore(DEFAULT_DB).save_entry(
            with_summary(with_new_entry("/a.txt", "h1", 10, now=1000.0), "a real summary", now=1000.0)
        )
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/device_wiki")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert len(data["entries"]) == 1
        assert data["entries"][0]["path"] == "/a.txt"
        assert data["entries"][0]["summary"] == "a real summary"
        conn.close()

    def test_path_returns_one_real_entry(self, server):
        from dourmouse.device_wiki.core import with_new_entry
        from dourmouse.device_wiki.store import DEFAULT_DB, WikiStore

        WikiStore(DEFAULT_DB).save_entry(with_new_entry("/a.txt", "h1", 10, now=1000.0))
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/device_wiki?path=/a.txt")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert data["entry"]["path"] == "/a.txt"
        conn.close()

    def test_unknown_path_is_a_real_404(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/device_wiki?path=/never-scanned.txt")
        resp = conn.getresponse()
        assert resp.status == 404
        conn.close()

    def test_status_filter_is_applied(self, server):
        from dourmouse.device_wiki.core import with_new_entry, with_summary
        from dourmouse.device_wiki.store import DEFAULT_DB, WikiStore

        store = WikiStore(DEFAULT_DB)
        store.save_entry(with_summary(with_new_entry("/keep.txt", "h1", 1, now=1000.0), "s", now=1000.0))
        store.save_entry(with_new_entry("/drop.txt", "h2", 2, now=1000.0))
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/device_wiki?status=summarized")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert [e["path"] for e in data["entries"]] == ["/keep.txt"]
        conn.close()

    def test_configured_roots_are_reported(self, server, tmp_path, monkeypatch):
        from dourmouse.device_wiki.walker import ROOTS_ENV

        monkeypatch.setenv(ROOTS_ENV, str(tmp_path))
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/device_wiki")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert data["configured_roots"] == [str(tmp_path)]
        conn.close()


class TestAuditEndpoint:
    """GET /api/audit (2026-09-18) — the cross-goal audit trail, the same
    real event data goal_snapshot() already exposes per-goal, answering
    the founding spec's own global question ("what did the assistant
    actually do") rather than a per-goal one. See
    docs/ENGINEERING_AUDIT.md finding #022."""

    @pytest.fixture(autouse=True)
    def _isolated_goal_store(self):
        from dourmouse.goals import GoalStore, set_goal_store

        set_goal_store(GoalStore(None))
        yield
        set_goal_store(None)

    def test_no_goals_yet_is_an_honest_empty_list(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/audit")
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read()) == {"events": []}
        conn.close()

    def test_spans_every_goal_by_default(self, server):
        from dourmouse.goals import get_goal_store

        store = get_goal_store()
        first = store.create_goal("First goal")
        second = store.create_goal("Second goal")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/audit")
        resp = conn.getresponse()
        goal_ids = {e["goal_id"] for e in json.loads(resp.read())["events"]}
        assert goal_ids == {first["id"], second["id"]}
        conn.close()

    def test_goal_id_scopes_to_one_goal(self, server):
        from dourmouse.goals import get_goal_store

        store = get_goal_store()
        keep = store.create_goal("Keep")
        store.create_goal("Drop")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/api/audit?goal_id={keep['id']}")
        resp = conn.getresponse()
        events = json.loads(resp.read())["events"]
        assert all(e["goal_id"] == keep["id"] for e in events)
        assert len(events) >= 1
        conn.close()

    def test_format_markdown_returns_a_real_readable_report(self, server):
        from dourmouse.goals import get_goal_store

        get_goal_store().create_goal("Research competitors")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/audit?format=markdown")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert "# Dourmouse Audit Trail" in data["markdown"]
        assert "goal_created" in data["markdown"]
        conn.close()


class TestGoalsCancelEndpoint:
    """POST /api/goals/cancel (2026-09-18) -- the GOALS screen's one write
    action (ui/console.html), a thin route over goals.GoalStore.cancel_goal,
    already tested in test_goals.py. See docs/ENGINEERING_AUDIT.md finding
    #026."""

    @pytest.fixture(autouse=True)
    def _isolated_goal_store(self):
        from dourmouse.goals import GoalStore, set_goal_store

        set_goal_store(GoalStore(None))
        yield
        set_goal_store(None)

    def test_cancels_a_real_goal(self, server):
        from dourmouse.goals import get_goal_store

        store = get_goal_store()
        goal = store.create_goal("Do the thing")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/goals/cancel",
            body=json.dumps({"id": goal["id"]}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data == {"ok": True}
        assert store.get_goal(goal["id"])["status"] == "CANCELLED"

    def test_missing_id_is_a_real_400_not_a_silent_no_op(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/goals/cancel",
            body=json.dumps({}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_unknown_goal_id_reports_false_not_an_error(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/goals/cancel",
            body=json.dumps({"id": "no-such-goal"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert data == {"ok": False}

    def test_an_already_completed_goal_cannot_be_uncancelled_by_this_route(self, server):
        from dourmouse.goals import get_goal_store

        store = get_goal_store()
        goal = store.create_goal("Already done")
        store.update_goal_status(goal["id"], "COMPLETED")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/goals/cancel",
            body=json.dumps({"id": goal["id"]}),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data == {"ok": False}
        assert store.get_goal(goal["id"])["status"] == "COMPLETED"


class TestGoalsTaskApprovalEndpoint:
    """POST /api/goals/tasks/approve (2026-09-18) -- the resumable
    per-task approval ticket, acceptance test 7. See
    docs/ENGINEERING_AUDIT.md finding #029. No chat tool reaches
    GoalStore.resolve_task_approval; this route is the only way in,
    same human-only posture as self-extension approval."""

    @pytest.fixture(autouse=True)
    def _isolated_goal_store(self):
        from dourmouse.goals import GoalStore, set_goal_store

        set_goal_store(GoalStore(None))
        yield
        set_goal_store(None)

    def _waiting_task(self):
        from dourmouse.goals import get_goal_store

        store = get_goal_store()
        goal = store.create_goal("Risky goal")
        task = store.create_task(goal["id"], "delete an important file")
        store.update_task_status(task["id"], "WAITING_FOR_APPROVAL", error="needs approval")
        store.update_goal_status(goal["id"], "WAITING_FOR_APPROVAL", blocked_reason="needs approval")
        return goal, task

    def test_approving_over_real_http_resumes_the_task(self, server):
        from dourmouse.goals import get_goal_store

        goal, task = self._waiting_task()
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/goals/tasks/approve",
            body=json.dumps({"task_id": task["id"], "approved": True}),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data == {"ok": True}
        store = get_goal_store()
        assert store.get_task(task["id"])["status"] == "READY"
        assert store.get_goal(goal["id"])["status"] == "EXECUTING"

    def test_declining_over_real_http_blocks_the_goal_with_the_real_reason(self, server):
        from dourmouse.goals import get_goal_store

        goal, task = self._waiting_task()
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/goals/tasks/approve",
            body=json.dumps({"task_id": task["id"], "approved": False, "reason": "too risky"}),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data == {"ok": True}
        store = get_goal_store()
        assert store.get_task(task["id"])["status"] == "FAILED"
        assert "too risky" in store.get_goal(goal["id"])["blocked_reason"]

    def test_missing_task_id_is_a_real_400(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/goals/tasks/approve",
            body=json.dumps({"approved": True}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_a_task_not_actually_waiting_reports_false_not_an_error(self, server):
        from dourmouse.goals import get_goal_store

        store = get_goal_store()
        goal = store.create_goal("Normal goal")
        task = store.create_task(goal["id"], "ordinary task")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/goals/tasks/approve",
            body=json.dumps({"task_id": task["id"], "approved": True}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert data == {"ok": False}


class TestSchedulesEndpoints:
    """GET /api/schedules + POST /api/schedules/toggle + .../remove
    (2026-09-18) -- the TIMETABLE screen's HTTP surface, the first one
    this store (schedules.py) has ever had; the store and the chat-facing
    tools (schedule_recurring/list_schedules/cancel_schedule) already
    existed and were already tested. See docs/ENGINEERING_AUDIT.md
    finding #027."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))

    def test_list_is_honestly_empty_with_nothing_scheduled(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/schedules")
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read()) == {"schedules": []}
        conn.close()

    def test_list_includes_human_readable_fields(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.schedules import Schedules

        Schedules().add("list_tasks", {}, {
            "kind": "weekday", "time": "09:00", "weekday": 0,
        }, "every Monday at 9:00")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/schedules")
        entry = json.loads(conn.getresponse().read())["schedules"][0]
        conn.close()
        assert entry["tool"] == "list_tasks"
        assert "schedule_description" in entry and entry["schedule_description"]
        assert "next_run" in entry and entry["next_run"]

    def test_toggle_pauses_a_real_schedule(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.schedules import Schedules

        store = Schedules()
        entry = store.add("list_tasks", {}, {"kind": "interval", "interval_seconds": 60}, "every 60 minutes")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/schedules/toggle",
            body=json.dumps({"id": entry["id"], "enabled": False}),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data == {"ok": True}
        assert store.list()[0]["enabled"] is False

    def test_toggle_missing_id_is_a_real_400(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/schedules/toggle",
            body=json.dumps({"enabled": False}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_update_over_real_http_reschedules_the_real_job(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.schedules import Schedules

        store = Schedules()
        entry = store.add("gmail_search", {"query": "receipt"}, {
            "kind": "weekday", "time": "09:00", "weekday": 0,
        }, "every Monday at 9:00")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/schedules/update",
            body=json.dumps({"id": entry["id"], "schedule_text": "every Friday at 17:00"}),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data["ok"] is True
        assert data["schedule"]["schedule_text"] == "every Friday at 17:00"
        assert store.list()[0]["schedule_text"] == "every Friday at 17:00"
        assert store.list()[0]["tool"] == "gmail_search"  # untouched

    def test_update_an_unparseable_schedule_is_a_real_400_not_a_silent_no_op(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.schedules import Schedules

        store = Schedules()
        entry = store.add("list_tasks", {}, {"kind": "interval", "interval_seconds": 60}, "every 60 minutes")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/schedules/update",
            body=json.dumps({"id": entry["id"], "schedule_text": "sometimes, whenever"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert resp.status == 400
        assert data["ok"] is False
        assert store.list()[0]["schedule_text"] == "every 60 minutes"  # untouched by the rejected edit

    def test_update_missing_fields_is_a_real_400(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/schedules/update",
            body=json.dumps({"schedule_text": "daily at 9:00"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_remove_deletes_a_real_schedule(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.schedules import Schedules

        store = Schedules()
        entry = store.add("list_tasks", {}, {"kind": "interval", "interval_seconds": 60}, "every 60 minutes")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/schedules/remove",
            body=json.dumps({"id": entry["id"]}),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data == {"ok": True}
        assert store.list() == []

    def test_remove_unknown_id_reports_false_not_an_error(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/schedules/remove",
            body=json.dumps({"id": "no-such-schedule"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert data == {"ok": False}


class TestSelfExtensionsEndpoints:
    """GET /api/self_extensions (+?id=) + POST .../approve + .../reject --
    Domain D's HTTP surface. The approve route is THE review gate: no
    chat tool anywhere in this codebase can reach it (see
    TestAgentSmith.test_no_approve_or_reject_tool_exists_anywhere_in_the_roster,
    test_general_roster.py). See docs/ENGINEERING_AUDIT.md finding #028."""

    _HANDLER = (
        "def handle(arguments: dict) -> str:\n"
        "    text = arguments.get(\"text\", \"\")\n"
        "    if not isinstance(text, str) or not text:\n"
        "        return \"ERROR: 'text' must be a non-empty string.\"\n"
        "    return text[::-1]\n"
    )
    _TEST_OK = (
        "from dourmouse.self_extensions import load_approved\n\n\n"
        "def test_reverses_text():\n"
        "    mod = load_approved(\"reverse_text_http\")\n"
        "    assert mod.handle({\"text\": \"abc\"}) == \"cba\"\n"
    )
    _PARAMS = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))

    def test_list_is_honestly_empty_with_nothing_drafted(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/self_extensions")
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read()) == {"drafts": []}
        conn.close()

    def test_detail_returns_the_real_full_source_for_human_review(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.self_extensions import SelfExtensions

        entry = SelfExtensions().add_draft(
            capability_gap="no way to reverse text", tool_name="reverse_text_http",
            description="Reverses text.", parameters_schema=self._PARAMS,
            handler_source=self._HANDLER, test_source=self._TEST_OK,
        )
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/api/self_extensions?id={entry['id']}")
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data["draft"]["handler_source"] == self._HANDLER
        assert data["draft"]["test_source"] == self._TEST_OK

    def test_detail_unknown_id_is_a_real_404(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/self_extensions?id=no-such-draft")
        resp = conn.getresponse()
        assert resp.status == 404
        conn.close()

    def test_approve_over_real_http_makes_a_genuinely_working_extension(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.self_extensions import SelfExtensions, load_approved

        entry = SelfExtensions().add_draft(
            capability_gap="no way to reverse text", tool_name="reverse_text_http",
            description="Reverses text.", parameters_schema=self._PARAMS,
            handler_source=self._HANDLER, test_source=self._TEST_OK,
        )
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/self_extensions/approve",
            body=json.dumps({"id": entry["id"]}),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data["ok"] is True, data.get("error")
        mod = load_approved("reverse_text_http")
        assert mod.handle({"text": "abc"}) == "cba"

    def test_approve_missing_id_is_a_real_400(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/self_extensions/approve",
            body=json.dumps({}), headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_reject_over_real_http(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        from dourmouse.self_extensions import SelfExtensions

        store = SelfExtensions()
        entry = store.add_draft(
            capability_gap="x", tool_name="reverse_text_http", description="d",
            parameters_schema=self._PARAMS, handler_source=self._HANDLER, test_source=self._TEST_OK,
        )
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/self_extensions/reject",
            body=json.dumps({"id": entry["id"], "reason": "not needed"}),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data["ok"] is True
        assert store.get(entry["id"])["status"] == "REJECTED"


class TestSpotifyWidgetInjection:
    """v13.x backlog item 8: the floating widget is injected at serve time
    (dourmouse/webui.py::_serve_static) onto every screen EXCEPT the
    pre-auth login/setup pages, which the designer lane owns this cycle.

    The widget files (ui/spotify_widget.css/.js) are the designer lane's
    deliverable, not this test's — created here only to exercise the
    file-exists-guarded injection, and removed afterwards so a real
    worktree state without them is unaffected.
    """

    _WIDGET_TAGS = (
        b'<link rel="stylesheet" href="/ui/spotify_widget.css">'
        b'<script defer src="/ui/spotify_widget.js"></script>'
    )

    @pytest.fixture
    def with_widget_files(self):
        css = webui_module._UI_DIR / "spotify_widget.css"
        js = webui_module._UI_DIR / "spotify_widget.js"
        pre_existing = css.exists() or js.exists()
        if not pre_existing:
            css.write_text("/* test widget */", encoding="utf-8")
            js.write_text("// test widget", encoding="utf-8")
        yield
        if not pre_existing:
            css.unlink(missing_ok=True)
            js.unlink(missing_ok=True)

    def test_injected_into_a_non_login_setup_page(self, server, monkeypatch, with_widget_files):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        # NOT "/" — that serves console.html (the default since v8.7),
        # which is correctly EXCLUDED (it ships its own inline Spotify
        # controls, per _serve_static's own exclusion-list comment).
        # os.html has no inline controls of its own, so it's a real
        # "should get the floating widget" page.
        conn.request("GET", "/os.html")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read()
        conn.close()
        assert self._WIDGET_TAGS in body

    def test_absent_from_login_and_setup_pages(self, server, with_widget_files):
        srv, port = server
        for path in ("/login", "/setup"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            assert resp.status == 200, path
            body = resp.read()
            conn.close()
            assert self._WIDGET_TAGS not in body, path

    def test_injection_silently_skipped_when_widget_files_absent(self, server, monkeypatch):
        """Simulate a worktree state without the designer lane's widget
        files — regardless of whether ui/spotify_widget.css/.js actually
        exist on disk in THIS checkout (post-6cf7a26 they do) — by forcing
        just those two exists() lookups to False. Injection must no-op, not
        error, and every other Path.exists() call (serving the real
        index.html, etc.) is left untouched."""
        real_exists = pathlib.Path.exists
        widget_names = {"spotify_widget.css", "spotify_widget.js"}

        def fake_exists(self, *args, **kwargs):
            if self.name in widget_names:
                return False
            return real_exists(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "exists", fake_exists)
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read()
        conn.close()
        assert self._WIDGET_TAGS not in body


class TestStartupCheckInjection:
    """backlog #6: startup animation + real claude/codex/Google sign-in
    check, injected on the default landing page only (console.html) —
    login.html/setup.html already have their own real sign-in UI."""

    _STARTUP_TAG = b'<script defer src="/assets/startup_check.js"></script>'

    def test_injected_on_the_default_landing_page(self, server, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read()
        conn.close()
        assert self._STARTUP_TAG in body

    def test_absent_from_login_and_setup_pages(self, server):
        srv, port = server
        for path in ("/login", "/setup"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            assert resp.status == 200, path
            body = resp.read()
            conn.close()
            assert self._STARTUP_TAG not in body, path

    def test_injection_silently_skipped_when_script_absent(self, server, monkeypatch):
        real_exists = pathlib.Path.exists

        def fake_exists(self, *args, **kwargs):
            if self.name == "startup_check.js":
                return False
            return real_exists(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "exists", fake_exists)
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read()
        conn.close()
        assert self._STARTUP_TAG not in body


class TestStudyTab:
    """backlog #9: the Study tab — a real page served at /study, plus the
    honest folder-status endpoint it polls on load."""

    def test_study_page_is_served(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/study")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read()
        conn.close()
        assert b"Study" in body
        assert b"focus_agent" in body

    def test_study_html_alias_also_works(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/study.html")
        resp = conn.getresponse()
        assert resp.status == 200
        conn.close()

    def test_study_status_endpoint_is_honest_about_a_missing_folder(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(tmp_path / "not-there"))
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/study/status")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["exists"] is False

    def test_study_status_endpoint_reports_a_real_folder(self, server, monkeypatch, tmp_path):
        real_dir = tmp_path / "study"
        real_dir.mkdir()
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(real_dir))
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/study/status")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["exists"] is True
        assert data["path"] == str(real_dir)


class TestStudyFilesAndReadEndpoints:
    """v14 (user-directed, 2026-09-08): backlog #9's real /study.html page
    existed and worked, but was never linked from console.html's nav — an
    orphaned page (see the Study nav-link fix in console.html itself). Its
    bare chat box had no real way to browse the folder without asking the
    model and hoping it called study_list_files; these two direct HTTP
    endpoints (no LLM round trip) give it a real file browser."""

    def test_files_lists_the_real_study_folder(self, server, monkeypatch, tmp_path):
        real_dir = tmp_path / "study"
        real_dir.mkdir()
        (real_dir / "notes.txt").write_text("hello")
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(real_dir))
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/study/files")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["ok"] is True
        names = {e["name"] for e in data["entries"]}
        assert "notes.txt" in names

    def test_files_honestly_reports_a_missing_folder(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(tmp_path / "not-there"))
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/study/files")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["ok"] is False
        assert "error" in data

    def test_read_returns_real_file_content(self, server, monkeypatch, tmp_path):
        real_dir = tmp_path / "study"
        real_dir.mkdir()
        (real_dir / "notes.txt").write_text("real study content here")
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(real_dir))
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/study/read?" + urllib.parse.urlencode({"path": "notes.txt"}))
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["ok"] is True
        assert "real study content here" in data["content"]

    def test_read_requires_a_path(self, server):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/study/read")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["ok"] is False

    def test_read_refuses_a_path_traversal_attempt(self, server, monkeypatch, tmp_path):
        real_dir = tmp_path / "study"
        real_dir.mkdir()
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(real_dir))
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET", "/api/study/read?" + urllib.parse.urlencode({"path": "../../../etc/passwd"})
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["ok"] is False


class TestClaudeFrontModeEndpoint:
    """The Settings UI's backend half for the Claude-front-mode toggle —
    ON by default, changeable, mirroring TestStudyTab's real-HTTP pattern."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_get_reports_on_by_default(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/settings/claude-front-mode")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["enabled"] is True

    def test_post_false_then_get_reflects_it(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/settings/claude-front-mode",
            body=json.dumps({"enabled": False}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        post_data = json.loads(resp.read())
        conn.close()
        assert post_data["ok"] is True

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/settings/claude-front-mode")
        resp = conn.getresponse()
        get_data = json.loads(resp.read())
        conn.close()
        assert get_data["enabled"] is False


class TestGoogleFullScopesEndpoint:
    """The Settings UI's backend half for the Google full-scopes toggle —
    OFF by default (opt-in), changeable, mirroring
    TestClaudeFrontModeEndpoint's real-HTTP pattern."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )
        monkeypatch.delenv("GOOGLE_OAUTH_FULL_SCOPES", raising=False)

    def test_get_reports_off_by_default(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/settings/google-full-scopes")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["enabled"] is False

    def test_post_true_then_get_reflects_it(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/settings/google-full-scopes",
            body=json.dumps({"enabled": True}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        post_data = json.loads(resp.read())
        conn.close()
        assert post_data["ok"] is True

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/settings/google-full-scopes")
        resp = conn.getresponse()
        get_data = json.loads(resp.read())
        conn.close()
        assert get_data["enabled"] is True


class TestAppControlDryRunEndpoint:
    """v14 (user-directed, 2026-09-08): "Consider adding a 'dry run'
    mode where it shows what would be clicked without actually
    clicking." The Settings UI's backend half — OFF by default
    (opt-in), mirroring TestClaudeFrontModeEndpoint's real-HTTP
    pattern."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_get_reports_off_by_default(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/settings/app-control-dry-run")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["enabled"] is False

    def test_post_true_then_get_reflects_it(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/settings/app-control-dry-run",
            body=json.dumps({"enabled": True}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        post_data = json.loads(resp.read())
        conn.close()
        assert post_data["ok"] is True

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/settings/app-control-dry-run")
        resp = conn.getresponse()
        get_data = json.loads(resp.read())
        conn.close()
        assert get_data["enabled"] is True


class TestByokApiKeysEndpoint:
    """v14 (user-directed, 2026-09-12): "commercial, for other people to
    use" — the real Settings-UI backend for BYOK (bring your own key).
    GET never echoes the key VALUE back (Rule 2.6) — only whether one is
    configured, matching this app's own NOT CONFIGURED honesty pattern."""

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dourmouse.config.user_env_path", lambda: tmp_path / "dourmouse" / ".env"
        )
        monkeypatch.setattr(
            "dourmouse.config.user_config_dir", lambda: tmp_path / "dourmouse"
        )

    def test_get_reports_both_keys_unconfigured_by_default(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/settings/api-keys")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data == {"OLLAMA_API_KEY": False, "GEMINI_API_KEY": False}

    def test_post_a_real_key_then_get_reflects_configured_true(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/settings/api-keys",
            body=json.dumps({"name": "OLLAMA_API_KEY", "value": "a-real-test-key"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        post_data = json.loads(resp.read())
        conn.close()
        assert post_data["ok"] is True

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/settings/api-keys")
        resp = conn.getresponse()
        get_data = json.loads(resp.read())
        conn.close()
        assert get_data["OLLAMA_API_KEY"] is True
        assert get_data["GEMINI_API_KEY"] is False  # untouched, still unconfigured

    def test_the_get_response_never_contains_the_real_key_value(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        secret = "super-secret-value-must-never-leak-back-out"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/settings/api-keys",
            body=json.dumps({"name": "GEMINI_API_KEY", "value": secret}),
            headers={"Content-Type": "application/json"},
        )
        conn.getresponse().read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/settings/api-keys")
        resp = conn.getresponse()
        raw_body = resp.read()
        conn.close()
        assert secret.encode() not in raw_body

    def test_posting_an_empty_value_clears_a_previously_set_key(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/settings/api-keys",
            body=json.dumps({"name": "OLLAMA_API_KEY", "value": "will-be-cleared"}),
            headers={"Content-Type": "application/json"},
        )
        conn.getresponse().read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/settings/api-keys",
            body=json.dumps({"name": "OLLAMA_API_KEY", "value": ""}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["ok"] is True
        assert data["configured"] is False

    def test_a_name_outside_the_allowlist_is_refused_over_http(self, server, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/api/settings/api-keys",
            body=json.dumps({"name": "NVIDIA_API_KEY", "value": "sneaky"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data["ok"] is False


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


class TestAutonomousMode:
    """Phase 5 (bounded autonomous multi-step execution): body.autonomous
    controls two real things at once — the turn ceiling (_handle_chat_authed
    passes max_turns=_AUTONOMOUS_MAX_TURNS instead of 8) and
    force_plain_dispatch (skips Claude Front Mode's client resolution for
    this one call so a REQUIRES_CONFIRMATION tool stays pausable). Both
    checked here independently, hermetically — no real Claude CLI/Ollama
    Cloud process is ever invoked."""

    def _post_and_drain(self, port, body):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat", body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        while True:
            line = resp.readline()
            if not line:
                break
        conn.close()

    def test_default_still_caps_at_eight_turns(self, server):
        srv, port = server
        tool_call = _FakeToolCall("c", "echo", json.dumps({"text": "x"}))
        looping = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        srv.session.client = FakeClient([looping] * 20)
        self._post_and_drain(port, {"prompt": "loop forever"})
        # 8 turns to exhaust the ordinary ceiling, +1 forced tools=[]
        # synthesis call — same accounting as test_dispatch.py's own
        # test_max_turns_bounds_looping_model.
        assert len(srv.session.client.chat.completions.calls) == 9

    def test_autonomous_flag_raises_the_real_turn_ceiling(self, server):
        srv, port = server
        tool_call = _FakeToolCall("c", "echo", json.dumps({"text": "x"}))
        looping = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        srv.session.client = FakeClient([looping] * 40)
        self._post_and_drain(port, {"prompt": "loop forever", "autonomous": True})
        assert len(srv.session.client.chat.completions.calls) == webui_module._AUTONOMOUS_MAX_TURNS + 1

    def test_autonomous_flag_forces_plain_dispatch_through_to_build_client(self, server, monkeypatch):
        """The other real half: force_plain_dispatch=True must actually
        reach dispatch._build_client for this top-level turn (client=None
        so run_dispatch_messages really calls _build_client instead of
        skipping straight to whatever client is already sitting there).

        config is set explicitly to a real OllamaConfig() (still no fake
        network client — _build_client itself is patched below) rather
        than left at the session's own None default: the root conftest.py
        deliberately sets DOURMOUSE_LLM_BACKEND=none for every test as a
        real-network-call safety net, and `config is None` is exactly the
        one condition that makes run_dispatch_messages call
        load_llm_config_with_fallback(), which honors that env var and
        raises. A real, valid config sidesteps that fallback entirely
        without weakening the safety net itself."""
        import dourmouse.dispatch as dispatch_module
        from dourmouse.config import OllamaConfig

        srv, port = server
        srv.session.config = OllamaConfig()
        seen_force_flags = []

        def spy_build_client(config, forced_agent=None, session_stem=None, force_plain_dispatch=False):
            seen_force_flags.append(force_plain_dispatch)
            return FakeClient([_FakeResponse(_FakeMessage(content="ok"))])

        monkeypatch.setattr(dispatch_module, "_build_client", spy_build_client)
        assert srv.session.client is None  # the real default this test relies on
        self._post_and_drain(port, {"prompt": "hello", "autonomous": True})
        assert seen_force_flags == [True]

    def test_non_autonomous_turn_does_not_force_plain_dispatch(self, server, monkeypatch):
        import dourmouse.dispatch as dispatch_module
        from dourmouse.config import OllamaConfig

        srv, port = server
        srv.session.config = OllamaConfig()
        seen_force_flags = []

        def spy_build_client(config, forced_agent=None, session_stem=None, force_plain_dispatch=False):
            seen_force_flags.append(force_plain_dispatch)
            return FakeClient([_FakeResponse(_FakeMessage(content="ok"))])

        monkeypatch.setattr(dispatch_module, "_build_client", spy_build_client)
        assert srv.session.client is None
        self._post_and_drain(port, {"prompt": "hello"})
        assert seen_force_flags == [False]

    def test_gate_carries_the_autonomous_flag_for_the_ui_copy_change(self, server):
        """console.html's addApproval reads evt.autonomous to show the
        extra "keeps going automatically" line — proves the real gated tool
        call during an autonomous turn actually emits it, not just that the
        gate object has the attribute."""
        srv, port = server
        tool_call = _FakeToolCall("c", "gated_echo", json.dumps({"text": "x"}))
        srv.session.client = FakeClient(
            [_FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))]
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat",
            body=json.dumps({"prompt": "echo x", "autonomous": True}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        events = []
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                e = json.loads(line[6:])
                events.append(e)
                if e.get("type") == "confirmation_requested":
                    # Decline immediately so the request can finish and this
                    # test doesn't block on the real 300s auto-decline path.
                    fetch_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    fetch_conn.request(
                        "POST", "/api/confirm",
                        body=json.dumps({"id": e["id"], "approved": False}),
                        headers={"Content-Type": "application/json"},
                    )
                    fetch_conn.getresponse().read()
                    fetch_conn.close()
        conn.close()
        gate_events = [e for e in events if e.get("type") == "confirmation_requested"]
        assert gate_events and gate_events[0]["autonomous"] is True


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

    def test_an_unhandled_backend_exception_still_ends_with_a_real_done_event(self, server):
        """Real, live-reproduced bug (full-day feature sweep, 2026-09-12):
        an exception escaping run_dispatch_messages (observed live: a raw
        urllib.error.HTTPError, "429 Too Many Requests", from a cloud
        backend under concurrent load) used to end the SSE stream with
        ONLY an "error" event — no "done", no "assistant_text" at all.
        console.html's own lastError fallback (see its "error" case
        comment) exists precisely to turn a raw error into a real visible
        reply instead of a bare "No reply." — but it only runs on "done",
        which never arrived, so the result was a permanently blank/stuck
        bubble instead. Every real client (this test included) must see a
        real, honest, non-empty final answer regardless of which layer the
        failure came from."""
        srv, port = server
        srv.session.client = FakeClient([])  # first call raises immediately
        events = self._stream_events(port, "hello")
        error_events = [e for e in events if e["type"] == "error"]
        done_events = [e for e in events if e["type"] == "done"]
        text_events = [e for e in events if e["type"] == "assistant_text"]
        assert error_events, f"expected a real error event, got {events}"
        assert done_events, f"a real backend failure must still end with done, got {events}"
        assert done_events[0]["final_text"].strip(), "done event must carry real text, not blank"
        assert text_events and text_events[0]["text"].strip()
        # History stays well-formed: the dangling user turn ChatSession.ask()
        # appends before the network call gets a real assistant reply too,
        # so the NEXT turn never sends two consecutive user messages.
        assert srv.session.messages[-1]["role"] == "assistant"
        assert srv.session.messages[-1]["content"].strip()

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

    def test_focus_agent_privacy_pinned_model_matches_the_forced_local_client(self, server, monkeypatch):
        """2026-09-14, real live-caught bug (STUDY tab, "HTTP Error 404:
        Not Found" on every question): _build_client() already swaps a
        privacy-pinned agent (model_delegation._LOCAL_ONLY_AGENTS) onto a
        genuinely local Ollama client even when a real Ollama Cloud key
        is configured, but the webui.py handler's own model_override
        computation used to call model_for_agent() on the AMBIENT
        (cloud) config regardless — handing the local client a real
        cloud-only model name it had never pulled, which the local
        daemon then genuinely 404'd on. This asserts the model actually
        used agrees with what a forced-local OllamaConfig resolves to,
        not the ambient cloud config's own default model."""
        from dourmouse.config import OllamaConfig

        monkeypatch.setattr(
            "dourmouse.model_delegation._LOCAL_ONLY_AGENTS", frozenset({"echo_agent"})
        )
        srv, port = server
        srv.session.config = OllamaConfig(
            api_key="real-cloud-key",
            base_url="https://ollama.com/v1",
            model="gpt-oss:20b",
            is_cloud=True,
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
        models = [c["model"] for c in srv.session.client.chat.completions.calls]
        assert models and all(m == "qwen2.5:7b" for m in models), models

    def test_force_backend_freellmapi_swaps_client_for_this_turn_then_restores(self, server, monkeypatch):
        """2026-09-14, user-directed: a real third DIRECTIVE VIA option,
        FreeLLMAPI (github.com/tashfeenahmed/freellmapi) -- a self-hosted
        OpenAI-compatible router. force_backend:"freellmapi" must swap in
        a real client built against FreeLLMAPIConfig's own base_url/key
        for exactly this turn, then restore the session's original
        client/config afterward so the NEXT ordinary turn is unaffected."""
        import openai

        srv, port = server
        original_client = srv.session.client
        original_config = srv.session.config
        captured_init = {}

        class _FakeOpenAI:
            def __init__(self, api_key=None, base_url=None):
                captured_init["api_key"] = api_key
                captured_init["base_url"] = base_url
                self.chat = _FakeChat(
                    _FakeCompletions([_FakeResponse(_FakeMessage(content="via freellmapi"))])
                )

        monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
        monkeypatch.setenv("FREELLMAPI_API_KEY", "freellmapi-test-key")
        monkeypatch.setenv("FREELLMAPI_BASE_URL", "http://localhost:3001/v1")

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/chat",
            body=json.dumps({"prompt": "hi", "force_backend": "freellmapi"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        while True:
            line = resp.readline()
            if not line:
                break
        conn.close()
        assert resp.status == 200
        assert captured_init["api_key"] == "freellmapi-test-key"
        assert captured_init["base_url"] == "http://localhost:3001/v1"
        # Restored — the swap must not leak into the session's normal state.
        assert srv.session.client is original_client
        assert srv.session.config is original_config

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


class TestPerTabSessions:
    """backlog #7: each browser tab gets its own real ChatSession — the
    user's own explicit ask. Additive: no tab_id sent (every test above
    this class) is completely unaffected — proven by this whole file's
    pre-existing suite passing unchanged. This class proves the NEW,
    opted-in behavior actually isolates two tabs from each other, both
    conversation history and (the part a half-measure would get wrong)
    the confirmation gate itself.
    """

    def _stream_until_done_or_confirm(self, resp):
        events = []
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event["type"] in ("confirmation_requested", "done"):
                    break
        return events

    def test_two_tabs_get_two_distinct_sessions(self, server):
        srv, port = server
        srv.client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat",
            body=json.dumps({"prompt": "hi", "tab_id": "tabA"}),
            headers={"Content-Type": "application/json"},
        )
        self._stream_until_done_or_confirm(conn.getresponse())
        conn.close()
        assert "tabA" in srv.sessions_by_tab
        assert srv.sessions_by_tab["tabA"] is not srv.session

    def test_same_tab_id_reuses_the_same_session(self, server):
        srv, port = server
        srv.client = FakeClient([
            _FakeResponse(_FakeMessage(content="one")),
            _FakeResponse(_FakeMessage(content="two")),
        ])
        for _ in range(2):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(
                "POST", "/api/chat",
                body=json.dumps({"prompt": "hi", "tab_id": "tabA"}),
                headers={"Content-Type": "application/json"},
            )
            self._stream_until_done_or_confirm(conn.getresponse())
            conn.close()
        session = srv.sessions_by_tab["tabA"]
        # Both turns landed on the SAME session's own history, not two
        # separate ones — real conversation continuity per tab.
        assert len(session.messages) >= 4  # 2 user + 2 assistant turns minimum

    def test_a_confirmation_pending_in_one_tab_is_invisible_to_another(self, server):
        """The real point of a per-tab GATE, not just a per-tab session:
        a half-measure (separate ChatSession, same shared gate) would let
        tab B's "send it" resolve tab A's pending confirmation. This
        proves it can't."""
        srv, port = server
        srv.client = FakeClient([
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c1", "gated_echo", json.dumps({"text": "secret-a"}))],
                )
            ),
            _FakeResponse(_FakeMessage(content="ok-b")),
        ])
        connA = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connA.request(
            "POST", "/api/chat",
            body=json.dumps({"prompt": "gated", "tab_id": "tabA"}),
            headers={"Content-Type": "application/json"},
        )
        events_a = self._stream_until_done_or_confirm(connA.getresponse())
        assert events_a[-1]["type"] == "confirmation_requested"

        # tabB has never sent a message, so it has NO pending confirmation
        # of its own — resolving tabA's real id but tagged as tabB's must
        # fail (there's no confirm_resolvers_by_tab["tabB"] entry at all).
        connB = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connB.request(
            "POST", "/api/confirm",
            body=json.dumps({"id": events_a[-1]["id"], "approved": True, "tab_id": "tabB"}),
            headers={"Content-Type": "application/json"},
        )
        resp_b = connB.getresponse()
        data_b = json.loads(resp_b.read())
        connB.close()
        assert data_b["ok"] is False
        assert resp_b.status == 409

        # The REAL owner (tabA) can still resolve its own confirmation —
        # tabB's failed attempt didn't corrupt tabA's real pending state.
        connA2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connA2.request(
            "POST", "/api/confirm",
            body=json.dumps({"id": events_a[-1]["id"], "approved": True, "tab_id": "tabA"}),
            headers={"Content-Type": "application/json"},
        )
        data_a = json.loads(connA2.getresponse().read())
        connA2.close()
        assert data_a["ok"] is True
        connA.close()


class TestProjectScopedSessions:
    """world-monitor-expansion ("make projects open their own chat like
    Claude Desktop's Projects"): opening a project's real tab_id (see
    project_bookkeeper.project_tab_id/open_project) must seed that tab's
    session with the project's real path + context BEFORE its first turn
    — the actual mechanism that makes "the model knows exactly what to
    do" true for project-scoped work, not just a routing detail."""

    def _stream_until_done(self, resp):
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                event = json.loads(line[6:])
                if event["type"] == "done":
                    return

    def test_opening_a_project_seeds_its_session_with_real_path_and_context(self, server, tmp_path):
        from dourmouse.project_bookkeeper import create_project, project_tab_id

        srv, port = server
        srv.client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        project_dir = tmp_path / "my-real-project"
        project_dir.mkdir()
        create_project("My Real Project", str(project_dir), description="building the widget API")
        tab_id = project_tab_id(str(project_dir))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat",
            body=json.dumps({"prompt": "hi", "tab_id": tab_id}),
            headers={"Content-Type": "application/json"},
        )
        self._stream_until_done(conn.getresponse())
        conn.close()

        session = srv.sessions_by_tab[tab_id]
        seeded = [m for m in session.messages if m["role"] == "system"]
        assert len(seeded) == 2, "the base system prompt plus exactly one project-seed message"
        seed_text = seeded[-1]["content"]
        assert str(project_dir) in seed_text
        assert "My Real Project" in seed_text
        assert "building the widget API" in seed_text
        # Live-caught regression (2026-09-08): asking "what project am I
        # in?" got routed to the freebuff_projects/freebuff_status tools
        # instead of answered from this seed directly — freebuff_bridge's
        # own tool descriptions are saturated with the word "project",
        # which out-scores everything else in planner.py's routing for a
        # bare "project" query. The seed now heads that off explicitly.
        assert "freebuff" in seed_text.lower()
        assert "no tool call needed" in seed_text
        # v14, live-caught: a real, correct zero-tool answer to "what
        # project is this" (answered straight from this exact seed) still
        # got flagged "unverified" by Grounded Mode — see dispatch.py's
        # own grounded_exempt for the deterministic fix this marker drives.
        assert "[GROUNDED MODE EXEMPT]" in seed_text

    def test_an_ordinary_tab_id_gets_no_project_seeding(self, server):
        """The honest negative: a ROOT ordinary browser tab_id (not
        derived from any real project) must never accidentally match a
        project and get seeded with the wrong context."""
        srv, port = server
        srv.client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat",
            body=json.dumps({"prompt": "hi", "tab_id": "ordinary-browser-tab-uuid"}),
            headers={"Content-Type": "application/json"},
        )
        self._stream_until_done(conn.getresponse())
        conn.close()
        session = srv.sessions_by_tab["ordinary-browser-tab-uuid"]
        seeded = [m for m in session.messages if m["role"] == "system"]
        assert len(seeded) == 1, "only the base system prompt — no project was ever opened for this tab"


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
        import dourmouse.webui as webui_module
        from dourmouse import google_auth

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
        # Phase 6: button copy sentence-cased and its decorative arrow
        # glyph dropped, same pass that fixed showManualLink's colors and
        # its dead claimNote element id below.
        assert "Open in Chrome" in html
        assert "showManualLink" in html
        assert "startClaimPoll" in html
        # The plain-browser path (no webview) must still redirect directly.
        assert "IN_WEBVIEW" in html


class TestGoogleStartPromptChoice:
    """Real UX friction, live-caught (2026-09-13): authorization_url's own
    default (prompt='consent') forces Google's full permission-grant
    screen on EVERY login, even the hundredth, for an account that
    already granted everything -- most "Sign in with X" flows only do
    that once. Can't know which identity is signing in before Google
    says so, but CAN check whether this install has ever completed a
    real Google connection before: fresh install -> force full consent
    (guarantees the refresh_token the "stay signed in" feature depends
    on); at least one real connection already exists -> ask for
    account-picker only and let Google's own normal behavior decide
    whether re-consent is actually needed."""

    def _capture_prompt(self, monkeypatch):
        from dourmouse import google_auth

        seen = {}

        def fake_authorization_url(redirect_uri, state, challenge, **kwargs):
            seen["prompt"] = kwargs.get("prompt")
            return "https://accounts.google.com/o/oauth2/v2/auth?fake=1"

        monkeypatch.setattr(google_auth, "google_configured", lambda: True)
        monkeypatch.setattr(google_auth, "authorization_url", fake_authorization_url)
        return seen

    def _get(self, server, path: str):
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        conn.close()

    def test_fresh_install_forces_full_consent(self, server, monkeypatch):
        from dourmouse import google_auth

        seen = self._capture_prompt(monkeypatch)
        store = google_auth.auth_store()
        assert store is not None and store.all_user_emails() == []
        self._get(server, "/api/auth/google/start")
        assert seen["prompt"] == "consent"

    def test_returning_install_only_asks_for_account_picker(self, server, monkeypatch):
        from dourmouse import google_auth

        seen = self._capture_prompt(monkeypatch)
        store = google_auth.auth_store()
        assert store is not None
        store.upsert_user("someone@example.com", {"access_token": "a", "refresh_token": "r"})
        self._get(server, "/api/auth/google/start")
        assert seen["prompt"] == "select_account"

    def test_no_mounted_store_falls_back_to_the_safer_full_consent(self, server, monkeypatch):
        """If the store can't be read for any reason, default to the
        option that guarantees a refresh_token rather than guessing."""
        from dourmouse import google_auth

        seen = self._capture_prompt(monkeypatch)
        monkeypatch.setattr(google_auth, "auth_store", lambda: None)
        self._get(server, "/api/auth/google/start")
        assert seen["prompt"] == "consent"


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

    def test_setup_status_reports_when_a_cloud_key_would_override_local(self, monkeypatch):
        """User-caught live bug (2026-09-17, see docs/ENGINEERING_AUDIT.md
        finding #020): detect_ollama() only ever probes the LOCAL server,
        so it has no way to know that config.load_ollama_config() always
        prefers Ollama Cloud the moment OLLAMA_API_KEY is set. Without this
        field, setup.html showed "Local · Ollama, works now, no key
        needed" and picked it as the default on an install that was never
        going to run local Ollama at all, key or no key."""
        from dourmouse.firstrun import setup_status

        monkeypatch.setenv("OLLAMA_API_KEY", "real-key-value")
        assert setup_status()["ollama_cloud_key_configured"] is True

    def test_setup_status_reports_false_with_no_cloud_key(self, monkeypatch):
        from dourmouse.firstrun import setup_status

        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        assert setup_status()["ollama_cloud_key_configured"] is False


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
        import dourmouse.webui as webui_module
        from dourmouse import google_auth

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
        def fake_stream_claude(task, *, cwd, timeout, on_delta, tab=None,
                                on_thinking=None, on_tool_use=None, on_tool_result=None, on_usage=None):
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

    def test_real_usage_is_recorded_and_emitted_live(self, code_claude_server, monkeypatch, tmp_path):
        """v13.6, real usage bar: the CLI's own real usage fields (see
        code_backends.py's own docstring for the exact real field names,
        transcribed from a live `claude -p` call) reach BOTH the
        persisted usage_tracker AND a live "usage" SSE event, not just
        one or the other."""
        monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))

        def fake_stream_claude(task, *, cwd, timeout, on_delta, tab=None, on_thinking=None,
                                on_tool_use=None, on_tool_result=None, on_usage=None):
            on_delta("Hello.")
            if on_usage:
                on_usage({"cost_usd": 0.05, "input_tokens": 10, "output_tokens": 5})
            return "Hello."

        monkeypatch.setattr("dourmouse.code_backends.stream_claude", fake_stream_claude)
        status, events = self._stream(code_claude_server[1], "say hello")
        assert status == 200
        usage_events = [e for e in events if e["type"] == "usage"]
        assert usage_events == [{"type": "usage", "backend": "claude", "cost_usd": 0.05, "input_tokens": 10, "output_tokens": 5}]

        from dourmouse.usage_tracker import get_totals

        totals = get_totals()
        assert totals["claude"]["requests"] == 1
        assert totals["claude"]["cost_usd"] == 0.05

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

    def test_claude_turn_is_appended_to_session_messages_for_a_later_backend_switch(
        self, code_claude_server, monkeypatch
    ):
        """2026-09-15, real live-reported bug: "when models switch then
        the model it switches to doesn't have the memory of the chat, it
        continues with its own thread" -- a Claude CLI turn used to only
        ever reach the separate audit ledger (record_slash), never
        session.messages, which is the ONLY thing a later Ollama/
        FreeLLMAPI-answered turn reads for context. This is the half of
        the fix that closes "switch AWAY from Claude"."""
        monkeypatch.setattr(
            "dourmouse.code_backends.stream_claude", lambda task, **kwargs: "def foo(): pass"
        )
        srv, port = code_claude_server
        self._stream(port, "write a function called foo")
        roles_and_content = [(m["role"], m["content"]) for m in srv.session.messages]
        assert ("user", "write a function called foo") in roles_and_content
        assert ("assistant", "def foo(): pass") in roles_and_content

    def test_a_later_claude_turn_recaps_prior_session_messages(self, code_claude_server, monkeypatch):
        """The reverse direction: a turn ALREADY in session.messages
        (e.g. answered by Ollama/FreeLLMAPI before the user switched
        DIRECTIVE VIA back to CLAUDE) must reach Claude CLI somehow --
        Claude's own --resume session continuity never saw it, since it
        was never a Claude turn. Prefixing a recap onto what Claude CLI
        actually receives is the one real mechanism available."""
        srv, port = code_claude_server
        # Simulate a turn a DIFFERENT backend already answered on this
        # same session, before Claude CLI is asked anything.
        srv.session.messages.append({"role": "user", "content": "what is the capital of France"})
        srv.session.messages.append({"role": "assistant", "content": "Paris"})

        seen = {}

        def fake_stream_claude(task, **kwargs):
            seen["task"] = task
            return "ok"

        monkeypatch.setattr("dourmouse.code_backends.stream_claude", fake_stream_claude)
        self._stream(port, "what did I just ask about")
        assert "capital of France" in seen["task"]
        assert "Paris" in seen["task"]
        assert "[Current request]: what did I just ask about" in seen["task"]

    def test_recap_never_pollutes_what_gets_stored_or_audited(self, code_claude_server, monkeypatch):
        """The recap must only ever reach Claude CLI's own input -- the
        real record (session.messages, the audit ledger) keeps the
        user's actual, clean words, or the recap would compound on
        itself turn after turn."""
        srv, port = code_claude_server
        srv.session.messages.append({"role": "user", "content": "earlier question"})
        srv.session.messages.append({"role": "assistant", "content": "earlier answer"})
        monkeypatch.setattr(
            "dourmouse.code_backends.stream_claude", lambda task, **kwargs: "fresh answer"
        )
        self._stream(port, "a new question")
        stored = [m["content"] for m in srv.session.messages if m["role"] == "user"]
        assert "a new question" in stored
        assert not any("[Current request]" in c for c in stored)
        lines = srv.session.session_file.read_text(encoding="utf-8").strip().splitlines()
        last = json.loads(lines[-1])
        assert last["user"] == "a new question"


class TestSSEStreamShouldStop:
    """v13.5 "stop/directive bug" fix: _SSEStream used to silently
    discard a dead-client write failure ("client went away; loop
    continues harmlessly") instead of recording it anywhere — see
    dispatch.run_dispatch_messages' should_stop docstring paragraph for
    why that mattered (nothing upstream could ever learn the user had
    clicked STOP outside the one already-fixed confirmation-deadlock
    case). Covered directly against the real class, no server needed."""

    class _DeadWfile:
        def write(self, _data: bytes) -> None:
            raise BrokenPipeError("client gone")

        def flush(self) -> None:
            pass

    class _LiveWfile:
        def __init__(self) -> None:
            self.written: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.written.append(data)

        def flush(self) -> None:
            pass

    def test_should_stop_false_while_the_client_is_still_connected(self):
        stream = _SSEStream(self._LiveWfile())
        assert stream.should_stop() is False
        stream.emit({"type": "assistant_delta", "text": "hi"})
        assert stream.should_stop() is False

    def test_should_stop_true_once_a_write_hits_a_dead_socket(self):
        stream = _SSEStream(self._DeadWfile())
        assert stream.should_stop() is False  # nothing written yet
        stream.emit({"type": "assistant_delta", "text": "hi"})  # BrokenPipeError, swallowed
        assert stream.should_stop() is True
        # A dead client must never resurrect itself on a later emit — the
        # flag is sticky for the rest of this request's lifetime.
        stream.emit({"type": "assistant_delta", "text": "more"})
        assert stream.should_stop() is True

    def test_connection_reset_error_also_flips_it(self):
        class _ResetWfile:
            def write(self, _data: bytes) -> None:
                raise ConnectionResetError("reset")

            def flush(self) -> None:
                pass

        stream = _SSEStream(_ResetWfile())
        stream.emit({"type": "done", "final_text": "x"})
        assert stream.should_stop() is True


class TestBrowserScreenshotEndpoint:
    """GET /api/browser/screenshot — real, live-reproduced bug (Electron
    migration Stage E testing, 2026-09-13): the honest 404's reason phrase
    contained an em-dash, which BaseHTTPRequestHandler.send_error's
    stdlib implementation encodes as latin-1 (RFC 7230's status-line
    convention) — raising a real UnicodeEncodeError that crashed the
    request's own handler thread every time this 404 fired, instead of
    the client ever seeing a real 404 response at all."""

    def test_missing_screenshot_returns_a_real_404_not_a_crash(self, server, monkeypatch):
        srv, port = server
        # No screenshot has ever been taken in this test's fresh workspace
        # — latest_screenshot() honestly returns None, the real path this
        # bug lived on.
        monkeypatch.setattr("dourmouse.browser_agent.latest_screenshot", lambda name="latest": None)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/browser/screenshot")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 404

    def test_reason_phrase_is_real_ascii_not_a_repeat_of_the_encoding_bug(self, server, monkeypatch):
        """The actual bug: send_error's message must stay within latin-1
        (ideally plain ASCII) — any future edit to this string must not
        reintroduce a curly quote, em/en-dash, or other char outside it."""
        monkeypatch.setattr("dourmouse.browser_agent.latest_screenshot", lambda name="latest": None)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/browser/screenshot")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.reason.isascii(), f"non-ASCII reason phrase would crash send_error: {resp.reason!r}"


class TestGeneratedImageEndpoint:
    """GET /api/images/generated — 2026-09-14, user-directed: "give it
    the ability to ... generate ... images." Same shape as the
    screenshot endpoint right above, backed by image_gen.py's own
    sandboxed resolve_generated_image() instead of a fixed-.png lookup
    (a generated file's real extension varies with its mime type)."""

    def test_serves_a_real_generated_image(self, server, monkeypatch, tmp_path):
        from dourmouse import image_gen

        images_dir = tmp_path / "generated"
        images_dir.mkdir()
        (images_dir / "a-red-circle.png").write_bytes(b"fake-png-bytes")
        monkeypatch.setattr(image_gen, "IMAGES_DIR", images_dir)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/images/generated?name=a-red-circle.png")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "image/png"
        assert body == b"fake-png-bytes"

    def test_jpeg_extension_gets_the_real_jpeg_mime_type(self, server, monkeypatch, tmp_path):
        from dourmouse import image_gen

        images_dir = tmp_path / "generated"
        images_dir.mkdir()
        (images_dir / "a-photo.jpg").write_bytes(b"fake-jpeg-bytes")
        monkeypatch.setattr(image_gen, "IMAGES_DIR", images_dir)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/images/generated?name=a-photo.jpg")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.getheader("Content-Type") == "image/jpeg"

    def test_missing_image_returns_a_real_404_not_a_crash(self, server, monkeypatch, tmp_path):
        from dourmouse import image_gen

        monkeypatch.setattr(image_gen, "IMAGES_DIR", tmp_path / "empty")
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/images/generated?name=nope.png")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 404
        assert resp.reason.isascii()

    def test_path_traversal_is_rejected_not_served(self, server, monkeypatch, tmp_path):
        from dourmouse import image_gen

        images_dir = tmp_path / "generated"
        images_dir.mkdir()
        (tmp_path / "secret.png").write_bytes(b"not yours")
        monkeypatch.setattr(image_gen, "IMAGES_DIR", images_dir)
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/images/generated?name=../secret.png")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 404
