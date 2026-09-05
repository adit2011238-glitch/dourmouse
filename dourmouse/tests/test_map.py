"""Agent Map feature tests (map window, live activity, task->agent search).

Covers the pieces the dashboard's AGENT MAP window depends on:

- GET  /map            -> serves the standalone map window (ui/map.html)
- GET  /api/activity   -> live per-subagent status/feed snapshot
- GET  /api/find_agent -> deterministic task->agent ranking (Rule 2.8: pure
                          string scoring, no LLM in the lookup path)
- POST /api/chat       -> focus_agent option routes a directive at ONE agent
                          and rejects unknown agent names

Real ThreadingHTTPServer on an ephemeral port with the REAL general roster
(integration discipline), plus pure unit tests for the tracker and the
rank scorer.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse.webui import ActivityTracker, find_agents_for_query, run_server


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    status = resp.status
    conn.close()
    return status, body


def _post(port, path, payload):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(
        "POST",
        path,
        body=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    body = resp.read()
    status = resp.status
    conn.close()
    return status, body


class TestMapWindow:
    def test_map_page_served(self, server):
        _, port = server
        status, body = _get(port, "/map")
        assert status == 200
        text = body.decode()
        assert "AGENT ORCHESTRATION MAP" in text
        assert "find_agent" in text  # the search wires to the API
        assert "focus_agent" in text  # per-agent dispatch wires to /api/chat

    def test_map_page_is_not_dashboard(self, server):
        _, port = server
        status, body = _get(port, "/map")
        assert b"CENTRAL AGENT DISPATCH" not in body


class TestFindAgents:
    def test_web_search_ranks_research_info_first(self):
        registry = build_general_registry()
        # "news" routes to the v2.3 news agent; use an unambiguous web
        # research query to assert research_info routing.
        matches = find_agents_for_query(registry, "search the web for facts", limit=3)
        assert matches, "expected at least one match"
        assert matches[0]["name"] == "research_info"
        assert matches[0]["score"] >= 1
        assert "web_search" in matches[0]["tools"]

    def test_am_i_free_tomorrow_ranks_scheduling_first(self):
        """v13.8, live-reproduced: "am I free tomorrow afternoon" has no
        domain word at all ("calendar"/"schedule"/"meeting" all absent), so
        it scored 0 for scheduling and the model honestly-but-wrongly
        answered "I don't have access to your calendar" even though
        list_calendar_events/propose_time_slots are real, working tools --
        confirmed live: "check my calendar for tomorrow afternoon", one
        word different, routed and worked correctly."""
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "am I free tomorrow afternoon", limit=3)
        assert matches, "expected at least one match"
        assert matches[0]["name"] == "scheduling"
        assert "list_calendar_events" in matches[0]["tools"]

    def test_bare_free_does_not_misroute_unrelated_queries(self):
        """The compound trigger requires "free" PLUS a temporal word,
        specifically because a bare "free" domain word was checked against
        the live registry and found to be a real, literal word in
        code_deepseek's ("free DeepSeek backend"), t212's, mt5's, and
        design_3d's own descriptions/tools -- these must not get shoved
        toward scheduling."""
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "is deepseek free", limit=3)
        assert matches, "expected at least one match"
        assert matches[0]["name"] != "scheduling"

    def test_draft_email_ranks_comms_first(self):
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "draft an email to the team", limit=3)
        assert matches[0]["name"] == "comms"
        assert "draft_message" in matches[0]["tools"]

    def test_delete_file_ranks_admin_ops_first(self):
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "delete a file", limit=3)
        assert matches[0]["name"] == "admin_ops"
        assert "delete_file" in matches[0]["tools"]

    def test_run_terminal_ranks_system_first(self):
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "run a terminal command", limit=3)
        assert matches[0]["name"] == "system"
        assert "run_command" in matches[0]["tools"]

    def test_empty_query_returns_no_matches(self):
        registry = build_general_registry()
        assert find_agents_for_query(registry, "") == []
        assert find_agents_for_query(registry, "   ") == []

    def test_stopwords_only_returns_no_matches(self):
        registry = build_general_registry()
        assert find_agents_for_query(registry, "please help me with this") == []

    def test_limit_is_respected(self):
        registry = build_general_registry()
        matches = find_agents_for_query(registry, "file", limit=1)
        assert len(matches) == 1

    def test_scoring_is_deterministic(self):
        registry = build_general_registry()
        a = find_agents_for_query(registry, "search the web", limit=5)
        b = find_agents_for_query(registry, "search the web", limit=5)
        assert a == b

    def test_find_agent_http_endpoint(self, server):
        _, port = server
        status, body = _get(port, "/api/find_agent?q=search+the+web")
        assert status == 200
        data = json.loads(body)
        assert data["query"] == "search the web"
        assert data["matches"][0]["name"] == "research_info"

    def test_find_agent_http_bad_limit_falls_back(self, server):
        _, port = server
        status, body = _get(port, "/api/find_agent?q=file&limit=not-a-number")
        assert status == 200
        data = json.loads(body)
        assert 1 <= len(data["matches"]) <= 3


class TestActivityTracker:
    @pytest.fixture
    def tracker(self):
        return ActivityTracker(build_general_registry())

    def test_snapshot_lists_all_agents_idle(self, tracker):
        snap = tracker.snapshot()
        assert set(snap["agents"]) == set(tracker._status)
        for name, state in snap["agents"].items():
            assert state["status"] == "idle"
            assert state["last"] is None
            assert state["feed"] == []

    def test_tool_use_marks_computing_and_feeds(self, tracker):
        tracker.on_event(
            {
                "type": "tool_use",
                "name": "web_search",
                "raw_arguments": '{"query": "nvidia"}',
            }
        )
        state = tracker.snapshot()["agents"]["research_info"]
        assert state["status"] == "computing"
        assert state["last"]["tool"] == "web_search"
        assert state["feed"][-1]["type"] == "tool_use"

    def test_tool_result_populates_last_result(self, tracker):
        tracker.on_event(
            {"type": "tool_use", "name": "web_search", "raw_arguments": "{}"}
        )
        tracker.on_event(
            {"type": "tool_result", "name": "web_search", "text": "REAL RESULTS"}
        )
        state = tracker.snapshot()["agents"]["research_info"]
        assert state["last"]["result"] == "REAL RESULTS"
        assert state["feed"][-1]["type"] == "tool_result"

    def test_confirmation_marks_auth(self, tracker):
        tracker.on_event(
            {"type": "tool_use", "name": "delete_file", "raw_arguments": "{}"}
        )
        tracker.on_event(
            {"type": "confirmation_requested", "id": "c1", "prompt": "Delete it?"}
        )
        state = tracker.snapshot()["agents"]["admin_ops"]
        assert state["status"] == "auth"
        assert state["feed"][-1]["type"] == "auth"

    def test_terminal_event_returns_all_to_idle(self, tracker):
        tracker.on_event(
            {"type": "tool_use", "name": "run_command", "raw_arguments": "{}"}
        )
        tracker.on_event(
            {"type": "done", "final_text": "ok"}
        )
        snap = tracker.snapshot()
        assert all(s["status"] == "idle" for s in snap["agents"].values())

    def test_unknown_tool_is_ignored(self, tracker):
        tracker.on_event(
            {"type": "tool_use", "name": "no_such_tool", "raw_arguments": "{}"}
        )
        snap = tracker.snapshot()
        assert all(s["status"] == "idle" for s in snap["agents"].values())
        assert all(s["feed"] == [] for s in snap["agents"].values())

    def test_feed_is_bounded(self, tracker):
        for _ in range(40):
            tracker.on_event(
                {"type": "tool_use", "name": "web_search", "raw_arguments": "{}"}
            )
        feed = tracker.snapshot()["agents"]["research_info"]["feed"]
        assert len(feed) == tracker._MAX_FEED

    def test_activity_http_endpoint(self, server):
        _, port = server
        status, body = _get(port, "/api/activity")
        assert status == 200
        data = json.loads(body)
        assert len(data["agents"]) == len(build_general_registry().subagent_names)


class TestFocusAgent:
    def test_focus_agent_rejects_unknown_name(self, server):
        _, port = server
        status, body = _post(
            port, "/api/chat", {"prompt": "do a thing", "focus_agent": "nope"}
        )
        assert status == 400
        assert "unknown subagent" in json.loads(body)["error"]

    def test_focus_agent_wraps_the_directive(self, server, monkeypatch):
        srv, port = server
        captured: dict = {}

        def fake_ask(prompt, **kwargs):
            captured["prompt"] = prompt
            return {"final_text": "routed to one agent", "transcript": []}

        monkeypatch.setattr(srv.session, "ask", fake_ask)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/chat",
            body=json.dumps({"prompt": "search the web", "focus_agent": "research_info"}),
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

        assert captured["prompt"].startswith("[ROUTING DIRECTIVE]")
        assert "'research_info'" in captured["prompt"]
        assert "search the web" in captured["prompt"]
        assert events[-1]["type"] == "done"
