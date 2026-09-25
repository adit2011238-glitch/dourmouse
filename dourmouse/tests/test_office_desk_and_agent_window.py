"""Findings #125 (A2) and #126 (A3): the agent window renders the live
stream (thinking, tools, answer, approvals), and the live activity delta
says how many runs an agent has at once, for its office desk."""

from __future__ import annotations

from pathlib import Path

from dourmouse.general_roster import build_general_registry
from dourmouse.webui import ActivityTracker

UI = Path(__file__).resolve().parents[2] / "ui"


def test_the_live_delta_carries_how_many_runs_an_agent_has():
    tracker = ActivityTracker(build_general_registry())
    sent = []
    tracker.set_broadcast(sent.append)
    tracker.on_event({"type": "tool_use", "name": "web_search", "raw_arguments": "{}", "call_id": "call-A"})
    tracker.on_event({"type": "tool_use", "name": "web_search", "raw_arguments": "{}", "call_id": "call-B"})
    assert sent[-1]["agents"]["research_info"]["concurrent"] == 2


def test_the_office_desk_shows_the_count():
    src = (UI / "console.html").read_text(encoding="utf-8")
    assert "concurrent: patch.concurrent ?? prev.concurrent ?? 0" in src
    assert '` x${live}`' in src


def test_the_agent_window_renders_the_whole_stream_and_asks_for_approval():
    src = (UI / "agent.html").read_text(encoding="utf-8")
    for event in ("thinking_delta", "assistant_delta", "tool_use", "tool_result", "confirmation_requested"):
        assert f"evt.type === '{event}'" in src, event
    assert "fetch('/api/confirm'" in src and 'id="convo"' in src
