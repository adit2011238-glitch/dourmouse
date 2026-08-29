"""AttentionQueue (dourmouse/webui.py) — the v13 cross-screen "needs
attention" feed.

Real gap this closes, found live: a fabricated no-tool RESEARCH answer, a
NOT CONFIGURED tool, or a timed-out CLI call each land quietly inside one
turn's own reply text with nothing surfacing them anywhere else — a caveat
on turn 4 of a scrolled-past CODE conversation is invisible unless the user
happens to scroll back and read that exact reply. AttentionQueue is a pure
observer fed from the same event_sink every chat turn already emits.

Two layers of coverage, matching this project's established pattern for a
tracker class: unit tests against the class directly (deterministic, no
server), then a handful of real-HTTP integration tests proving a genuine
/api/chat turn populates /api/attention end to end.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec
from dourmouse.tests.test_webui import FakeClient, _FakeMessage, _FakeResponse, _FakeToolCall
from dourmouse.webui import AttentionQueue, run_server


class TestAttentionQueueUnit:
    def test_starts_empty(self):
        q = AttentionQueue()
        assert q.snapshot() == []

    def test_not_configured_tool_result_recorded(self):
        q = AttentionQueue()
        q.on_event(
            {"type": "tool_result", "name": "qwen_code", "text": "NOT CONFIGURED: needs QWEN_API_KEY"},
            screen="CODE",
        )
        items = q.snapshot()
        assert len(items) == 1
        assert items[0]["kind"] == "not_configured"
        assert items[0]["screen"] == "CODE"
        assert "qwen_code" in items[0]["summary"]

    def test_error_tool_result_recorded(self):
        q = AttentionQueue()
        q.on_event({"type": "tool_result", "name": "write_file", "text": "ERROR: permission denied"}, screen="CODE")
        items = q.snapshot()
        assert items[0]["kind"] == "tool_error"
        assert "write_file" in items[0]["summary"]

    def test_timeout_tool_result_recorded(self):
        q = AttentionQueue()
        q.on_event(
            {"type": "tool_result", "name": "claude_code", "text": "ERROR: claude_code timed out after 20s (task still running)."},
            screen="CODE",
        )
        items = q.snapshot()
        # Starts with "ERROR:" -> classified tool_error, not timeout (the
        # first matching branch wins) — real behavior, asserted explicitly
        # so a future reordering of the classifier is a deliberate choice.
        assert items[0]["kind"] == "tool_error"

    def test_bare_timeout_text_without_error_prefix_recorded_as_timeout(self):
        q = AttentionQueue()
        q.on_event({"type": "tool_result", "name": "some_tool", "text": "operation timed out waiting for response"}, screen="RESEARCH")
        items = q.snapshot()
        assert items[0]["kind"] == "timeout"

    def test_ordinary_tool_result_not_recorded(self):
        q = AttentionQueue()
        q.on_event({"type": "tool_result", "name": "echo", "text": "ECHOED: hi"}, screen="HOME")
        assert q.snapshot() == []

    def test_error_event_recorded(self):
        q = AttentionQueue()
        q.on_event({"type": "error", "message": "API request failed"}, screen="HOME")
        items = q.snapshot()
        assert items[0]["kind"] == "error"
        assert items[0]["summary"] == "API request failed"

    def test_budget_exhausted_recorded(self):
        q = AttentionQueue()
        q.on_event({"type": "budget_exhausted", "reason": "max_turns exceeded"}, screen="HOME")
        items = q.snapshot()
        assert items[0]["kind"] == "budget_exhausted"

    def test_grounded_mode_caveat_in_final_text_recorded(self):
        q = AttentionQueue()
        q.on_event(
            {
                "type": "done",
                "final_text": "3.11.0\n\n[DOURMOUSE: Grounded Mode was on and this answer used zero tool calls despite real tools being available — treat it as unverified, not as a live lookup result]",
            },
            screen="RESEARCH",
        )
        items = q.snapshot()
        assert items[0]["kind"] == "ungrounded_answer"
        assert items[0]["screen"] == "RESEARCH"

    def test_incomplete_plan_caveat_in_final_text_recorded(self):
        q = AttentionQueue()
        q.on_event(
            {"type": "done", "final_text": "done\n\n[DOURMOUSE: plan step(s) not executed via tools — STEP 2/2 (mail): send it]"},
            screen="COMMS",
        )
        items = q.snapshot()
        assert items[0]["kind"] == "incomplete_plan"

    def test_ordinary_done_event_not_recorded(self):
        q = AttentionQueue()
        q.on_event({"type": "done", "final_text": "all good, no caveats here"}, screen="HOME")
        assert q.snapshot() == []

    def test_newest_first(self):
        q = AttentionQueue()
        q.on_event({"type": "error", "message": "first"}, screen="HOME")
        q.on_event({"type": "error", "message": "second"}, screen="HOME")
        items = q.snapshot()
        assert [i["summary"] for i in items] == ["second", "first"]

    def test_bounded_at_fifty(self):
        q = AttentionQueue()
        for i in range(60):
            q.on_event({"type": "error", "message": f"err {i}"}, screen="HOME")
        assert len(q.snapshot()) == 50
        # The oldest 10 were dropped; the newest (59) survives.
        assert q.snapshot()[0]["summary"] == "err 59"

    def test_dismiss_hides_from_default_snapshot(self):
        q = AttentionQueue()
        q.on_event({"type": "error", "message": "boom"}, screen="HOME")
        item_id = q.snapshot()[0]["id"]
        assert q.dismiss(item_id) is True
        assert q.snapshot() == []
        assert len(q.snapshot(include_dismissed=True)) == 1

    def test_dismiss_unknown_id_returns_false(self):
        q = AttentionQueue()
        assert q.dismiss(999) is False

    def test_none_text_field_handled_without_raising(self):
        q = AttentionQueue()
        q.on_event({"type": "tool_result", "name": "x", "text": None}, screen="HOME")
        assert q.snapshot() == []

    def test_a_malformed_non_dict_entry_never_breaks_the_caller(self):
        """Observer discipline (Rule: an observer must never affect
        execution) — on_event's own try/except must swallow a completely
        malformed entry (not even a dict) rather than letting it raise up
        into the real chat turn that's feeding it."""
        q = AttentionQueue()
        q.on_event(None, screen="HOME")  # entry.get(...) on None -> AttributeError, caught
        assert q.snapshot() == []


def _echo_and_not_configured_registry() -> DispatchRegistry:
    r = DispatchRegistry()
    r.register_subagent(
        Subagent(
            name="broken_agent",
            domain="Test",
            description="a tool that reports NOT CONFIGURED",
            tools=(
                ToolSpec(
                    name="broken_tool",
                    description="always unconfigured",
                    parameters={"type": "object", "properties": {}, "required": []},
                    handler=lambda a: "NOT CONFIGURED: needs FAKE_API_KEY",
                ),
            ),
        )
    )
    return r


@pytest.fixture
def server():
    srv = run_server(_echo_and_not_configured_registry(), port=0, client=None, config=None)
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
    body = resp.read().decode()
    conn.close()
    return resp.status, json.loads(body)


def _post(port, path, payload: dict):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(payload).encode()
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, json.loads(data)


class TestAttentionEndpointsOverRealHttp:
    def test_empty_before_any_turn(self, server):
        srv, port = server
        status, data = _get(port, "/api/attention")
        assert status == 200
        assert data == {"items": [], "count": 0}

    def test_a_real_not_configured_tool_call_populates_the_queue(self, server):
        srv, port = server
        srv.session.client = FakeClient(
            [
                _FakeResponse(
                    _FakeMessage(
                        tool_calls=[_FakeToolCall("1", "broken_tool", "{}")]
                    )
                ),
                _FakeResponse(_FakeMessage(content="it's not configured")),
            ]
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST", "/api/chat",
            body=json.dumps({"prompt": "use the broken tool", "focus_agent": "broken_agent", "screen": "CODE"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        while resp.readline():
            pass
        conn.close()
        assert resp.status == 200

        status, data = _get(port, "/api/attention")
        assert status == 200
        assert data["count"] == 1
        assert data["items"][0]["kind"] == "not_configured"
        assert data["items"][0]["screen"] == "CODE"

        # Dismiss it, then confirm it drops out of the default listing.
        item_id = data["items"][0]["id"]
        status, dismiss_result = _post(port, "/api/attention/dismiss", {"id": item_id})
        assert status == 200
        assert dismiss_result["ok"] is True
        status, data = _get(port, "/api/attention")
        assert data == {"items": [], "count": 0}

    def test_dismiss_unknown_id_reports_ok_false(self, server):
        srv, port = server
        status, data = _post(port, "/api/attention/dismiss", {"id": 999})
        assert status == 200
        assert data["ok"] is False

    def test_dismiss_non_integer_id_is_a_clean_error_not_a_500(self, server):
        srv, port = server
        status, data = _post(port, "/api/attention/dismiss", {"id": "not-a-number"})
        assert status == 200
        assert data["ok"] is False
        assert "integer" in data["detail"]
