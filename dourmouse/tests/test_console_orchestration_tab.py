"""ui/console.html — the ORCHESTRATION tab (v14, user-directed,
2026-09-12): a real, live view of orchestrator<->subagent activity.

Real gap this closes: dourmouse/webui.py's ActivityTracker (per-agent
status, last activity, bounded feed — genuinely SSE-pushed over
/api/events as "agent_activity", server.tracker.set_broadcast already
wired at server-construction time) had NO screen anywhere in
console.html at all — the same orphaned-backend pattern the Study tab
fix found earlier this session. This tab is that missing UI half: one
GET /api/activity snapshot on load, then live deltas over the SAME
shared EventSource this console already holds open for News — no new
connection, no polling loop.

No headless browser here (none available in this suite, matching every
other test_console_*.py file's own stated convention) — source-level
coverage that the real wiring is present and correct.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"


def _extract_inline_script() -> str:
    html = _CONSOLE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


class TestOrchestrationScreenRegistered:
    def test_orchestration_is_a_real_screen(self):
        script = _extract_inline_script()
        assert '"ORCHESTRATION"' in script

    def test_the_pane_markup_exists(self):
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        assert 'id="pane-orchestration"' in html
        assert 'id="orchestrationBody"' in html

    def test_show_dispatches_to_paintOrchestration(self):
        script = _extract_inline_script()
        assert 'if(name==="ORCHESTRATION") paintOrchestration();' in script


class TestOrchestrationDataWiring:
    def test_initial_snapshot_hits_the_real_endpoint(self):
        script = _extract_inline_script()
        idx = script.index("function loadOrchestrationSnapshot")
        block = script[idx: idx + 400]
        assert '"/api/activity"' in block

    def test_live_deltas_reuse_the_shared_event_source_not_a_new_one(self):
        """The real point of this design — no second EventSource, no
        polling loop, just a new branch on the connection News already
        holds open."""
        script = _extract_inline_script()
        idx = script.index('data.type === "agent_activity"')
        block = script[max(0, idx - 400): idx + 100]
        assert "_newsEventSource" in block or "startNewsStream" in script

    def test_agent_activity_events_are_routed_to_orchApplyDelta(self):
        script = _extract_inline_script()
        assert 'if(data.type === "agent_activity"){ orchApplyDelta(data.agents); return; }' in script


class TestOrchestrationRendering:
    def test_paint_shows_a_real_agent_and_active_count(self):
        script = _extract_inline_script()
        idx = script.index("function paintOrchestration")
        block = script[idx: idx + 1600]
        assert "AGENTS WIRED" in block
        assert "ACTIVE RIGHT NOW" in block

    def test_active_agents_render_before_idle_ones(self):
        script = _extract_inline_script()
        idx = script.index("function paintOrchestration")
        # Phase 4 (live orchestration view) widened paintOrchestration's
        # body (the new ACTIVE FAN-OUT section) enough to push "IDLE ("
        # just past the old 1600-char window — bumped with real headroom
        # rather than the exact new distance, so the next small addition
        # here doesn't retrigger this same window-too-small failure.
        block = script[idx: idx + 2400]
        active_idx = block.index("ACTIVE")
        idle_idx = block.index("IDLE (")
        assert active_idx < idle_idx

    def test_computing_status_gets_a_visibly_distinct_live_indicator(self):
        script = _extract_inline_script()
        idx = script.index("function orchStatusDot")
        block = script[idx: idx + 400]
        assert '"computing"' in block
        assert "animation:pulse" in block


class TestDelegateFanoutRendering:
    """Phase 4: delegate_parallel's own per-branch detail (which branch,
    which agent, which model), a genuinely different event type than
    agent_activity above — see ActivityTracker._record_fanout/
    _broadcast_fanout in dourmouse/webui.py for the backend half."""

    def test_delegate_fanout_events_are_routed_to_orchApplyFanout(self):
        script = _extract_inline_script()
        assert 'if(data.type === "delegate_fanout"){ orchApplyFanout(data); return; }' in script

    def test_initial_snapshot_also_loads_fanouts(self):
        """A client opening ORCHESTRATION mid-fan-out must see it
        immediately, not only once the NEXT branch event happens to
        arrive — same real-state contract the per-agent snapshot has."""
        script = _extract_inline_script()
        idx = script.index("function loadOrchestrationSnapshot")
        block = script[idx: idx + 400]
        assert "j.fanouts" in block

    def test_finished_run_is_removed_from_local_state(self):
        script = _extract_inline_script()
        idx = script.index("function orchApplyFanout")
        block = script[idx: idx + 400]
        assert "delete _orchFanouts" in block

    def test_fanout_section_renders_before_the_active_section(self):
        """The whole point of a live fan-out board: it should be the
        first thing you see, not buried below the per-agent list."""
        script = _extract_inline_script()
        idx = script.index("function paintOrchestration")
        block = script[idx: idx + 2400]
        fanout_idx = block.index("ACTIVE FAN-OUT")
        active_idx = block.index('class="sec" style="margin-top:${fanoutIds.length')
        assert fanout_idx < active_idx

    def test_branch_row_shows_index_agent_and_model(self):
        script = _extract_inline_script()
        idx = script.index("function orchFanoutRowHtml")
        block = script[idx: idx + 800]
        assert "b.agent" in block
        assert "modelShown" in block
        assert "localCloudSuffix(b)" in block
