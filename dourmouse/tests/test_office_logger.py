"""Tests for dourmouse/office_logger.py (finding #066) -- the persistent,
append-only log of real message_bus traffic and delegate_parallel fan-out
lifecycle events. Hermetic: every test gets its own on-disk SQLite file
under tmp_path, never default_db().
"""

from __future__ import annotations

from dourmouse.office_logger import OfficeLogger


class TestOfficeLoggerMessages:
    def test_log_message_persists_a_real_row(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.log_message({"id": "msg-1", "from": "research_info", "to": "markets",
                          "subject": "catalyst", "body": "NVDA spiked"})
        rows = log.recent_messages()
        assert len(rows) == 1
        assert rows[0]["from"] == "research_info"
        assert rows[0]["to"] == "markets"
        assert rows[0]["body"] == "NVDA spiked"

    def test_recent_messages_newest_first_and_capped(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        for i in range(5):
            log.log_message({"id": f"msg-{i}", "from": "news", "to": "*", "subject": "", "body": str(i)})
        rows = log.recent_messages(limit=3)
        assert len(rows) == 3
        assert [r["body"] for r in rows] == ["4", "3", "2"]

    def test_log_message_swallows_bad_input(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.log_message(None)  # type: ignore[arg-type]
        assert log.recent_messages() == []

    def test_survives_reopen_same_path(self, tmp_path):
        db = tmp_path / "office.db"
        OfficeLogger(db).log_message({"id": "m", "from": "a", "to": "b", "subject": "s", "body": "hello"})
        reopened = OfficeLogger(db)
        rows = reopened.recent_messages()
        assert len(rows) == 1 and rows[0]["body"] == "hello"


class TestOfficeLoggerAgentEvents:
    """Finding #067: real per-agent/per-call_id dispatch events (tool_use,
    tool_result, thinking_delta, assistant_delta, assistant_text, brain),
    additively tagged by dispatch.py's _emit_event."""

    def test_tagged_tool_use_is_persisted(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "tool_use", "name": "review_read_file", "agent": "reviewer", "call_id": "c1", "depth": 1})
        rows = log.transcript(call_id="c1")
        assert len(rows) == 1
        assert rows[0]["agent"] == "reviewer" and rows[0]["type"] == "tool_use"
        assert rows[0]["name"] == "review_read_file"

    def test_untagged_event_is_skipped_not_fabricated(self, tmp_path):
        # No call_id at all (e.g. an emitter that never passed ctx) -- must
        # never be logged under a made-up identity.
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "tool_use", "name": "review_read_file"})
        assert log.transcript() == []

    def test_unrelated_event_types_are_ignored(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "budget_exhausted", "agent": "reviewer", "call_id": "c1"})
        log.on_event({"type": "stop", "agent": "reviewer", "call_id": "c1"})
        assert log.transcript() == []

    def test_transcript_scoped_by_agent_and_call_id_oldest_first(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "thinking_delta", "text": "step 1", "agent": "reviewer", "call_id": "c1"})
        log.on_event({"type": "thinking_delta", "text": "step 2", "agent": "reviewer", "call_id": "c1"})
        log.on_event({"type": "tool_use", "name": "x", "agent": "reviewer", "call_id": "c2"})
        log.on_event({"type": "tool_use", "name": "y", "agent": "security", "call_id": "c3"})
        by_call = log.transcript(call_id="c1")
        assert [r["text"] for r in by_call] == ["step 1", "step 2"]
        by_agent = log.transcript(agent="reviewer")
        assert len(by_agent) == 3  # both c1 events + the c2 tool_use
        security_rows = log.transcript(agent="security")
        assert len(security_rows) == 1
        assert security_rows[0]["call_id"] == "c3" and security_rows[0]["name"] == "y"

    def test_two_concurrent_calls_to_the_same_agent_stay_separate(self, tmp_path):
        # The exact real gap flaw #4 named: two independent runs against
        # the SAME agent name. call_id (not agent name alone) is what lets
        # this store tell them apart.
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "tool_use", "name": "review_read_file", "agent": "reviewer", "call_id": "call-A"})
        log.on_event({"type": "tool_use", "name": "review_diff", "agent": "reviewer", "call_id": "call-B"})
        assert [r["name"] for r in log.transcript(call_id="call-A")] == ["review_read_file"]
        assert [r["name"] for r in log.transcript(call_id="call-B")] == ["review_diff"]
        assert len(log.transcript(agent="reviewer")) == 2


class TestOfficeLoggerFanoutEvents:
    def test_on_event_persists_delegate_parallel_branch(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({
            "type": "delegate_parallel_branch", "phase": "start", "run_id": "run-1",
            "index": 0, "total": 2, "agent": "reviewer",
        })
        log.on_event({
            "type": "delegate_parallel_branch", "phase": "result", "run_id": "run-1",
            "index": 0, "total": 2, "agent": "reviewer", "ok": True, "error": "", "elapsed_s": 1.23,
        })
        events = log.recent_fanout_events(run_id="run-1")
        assert len(events) == 2
        assert events[0]["phase"] == "result"  # newest first
        assert events[0]["ok"] is True
        assert events[0]["elapsed_s"] == 1.23
        assert events[1]["phase"] == "start"

    def test_on_event_ignores_unrelated_event_types(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "tool_use", "name": "review_read_file"})
        log.on_event({"type": "assistant_delta", "text": "hi"})
        assert log.recent_fanout_events() == []

    def test_recent_fanout_events_scoped_by_run_id(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "delegate_parallel_branch", "phase": "start", "run_id": "run-a", "agent": "x"})
        log.on_event({"type": "delegate_parallel_branch", "phase": "start", "run_id": "run-b", "agent": "y"})
        assert len(log.recent_fanout_events(run_id="run-a")) == 1
        assert len(log.recent_fanout_events()) == 2


class TestMeetings:
    """Finding #123 (Phase 5 A0 + A1): a fan-out branch carries the call_id
    of its own run, so a whole meeting reads as one conversation."""

    def test_a_meeting_reads_as_one_conversation(self, tmp_path):
        from dourmouse.office_logger import OfficeLogger

        log = OfficeLogger(tmp_path / "office.db")
        for i, (agent, cid) in enumerate((("research_info", "aaa111"), ("markets", "bbb222"))):
            log.on_event({"type": "delegate_parallel_branch", "phase": "start", "run_id": "run1", "index": i,
                          "total": 2, "agent": agent, "call_id": cid, "parent_call_id": "root", "task": f"task {i}"})
        # interleaved streaming from both branches, as real concurrency produces
        for agent, cid, text in (("research_info", "aaa111", "Rates "), ("markets", "bbb222", "EUR "),
                                 ("research_info", "aaa111", "are up."), ("markets", "bbb222", "fell.")):
            log.on_event({"type": "assistant_delta", "agent": agent, "call_id": cid, "text": text})
        log.on_event({"type": "tool_use", "agent": "markets", "call_id": "bbb222", "name": "quote", "text": "EURUSD"})
        for i, (agent, cid) in enumerate((("research_info", "aaa111"), ("markets", "bbb222"))):
            log.on_event({"type": "delegate_parallel_branch", "phase": "result", "run_id": "run1", "index": i,
                          "total": 2, "agent": agent, "call_id": cid, "ok": True, "elapsed_s": 1.5})
        m = log.meeting("run1")
        says = {(line["agent"], line["text"]) for line in m["lines"] if line["kind"] == "says"}
        assert says == {("research_info", "Rates are up."), ("markets", "EUR fell.")}
        kinds = [(line["agent"], line["kind"]) for line in m["lines"]]
        assert kinds[:2] == [("research_info", "task"), ("markets", "task")]
        assert ("markets", "uses") in kinds and kinds.count(("markets", "done")) == 1
        assert [b["call_id"] for b in m["branches"]] == ["aaa111", "bbb222"]
        meetings = log.recent_meetings()
        assert meetings[0]["run_id"] == "run1" and meetings[0]["agents"] == ["markets", "research_info"]
        assert meetings[0]["succeeded"] == 2

    def test_an_old_log_gains_the_new_columns(self, tmp_path):
        import sqlite3

        from dourmouse.office_logger import OfficeLogger

        db = tmp_path / "old.db"
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE fanout_events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
                      "phase TEXT NOT NULL, branch_index INTEGER, total INTEGER, agent TEXT NOT NULL DEFAULT '', "
                      "ok INTEGER, error TEXT NOT NULL DEFAULT '', elapsed_s REAL, ts REAL NOT NULL)")
            c.execute("INSERT INTO fanout_events (run_id, phase, agent, ts) VALUES ('old', 'start', 'a', 1)")
        log = OfficeLogger(db)
        assert log.meeting("old")["branches"][0]["call_id"] == ""


def test_delegate_parallel_branches_announce_the_id_their_run_uses(monkeypatch):
    """A0 end to end: the call_id on a branch's fan-out event is the call_id
    every event of that branch's own run carries."""
    import json

    from dourmouse.dispatch import run_dispatch_messages, system_message
    from dourmouse.general_roster import build_general_registry

    class _Fn:
        def __init__(self, n, a):
            self.name, self.arguments = n, a

    class _Call:
        def __init__(self, cid, n, a):
            self.id, self.function = cid, _Fn(n, json.dumps(a))

    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content, self.tool_calls = content, tool_calls

    class _Comp:
        def __init__(self):
            self.n = 0

        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                msg = _Msg(tool_calls=[_Call("c1", "delegate_parallel", {"branches": [
                    {"agent_or_task": "research_info", "instructions": "say hi"}]})])
            else:
                msg = _Msg(content="hi")
            return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

    client = type("Cl", (), {})()
    client.chat = type("Ch", (), {})()
    client.chat.completions = _Comp()
    events = []
    registry = build_general_registry()
    run_dispatch_messages([{"role": "system", "content": system_message(registry)},
                           {"role": "user", "content": "split this across agents in parallel"}],
                          registry, client=client, event_sink=events.append)
    branch = [e for e in events if e.get("type") == "delegate_parallel_branch" and e.get("phase") == "start"]
    assert branch, "no branch ran"
    cid = branch[0]["call_id"]
    nested = [e for e in events if e.get("call_id") == cid and e.get("type") != "delegate_parallel_branch"]
    assert nested, "the branch's own run did not carry the id it announced"


class TestEventLog:
    """R6 (finding #128): the research graph's changes land in the
    append-only event log, only committed ones, and read back by cursor."""

    def test_graph_changes_are_logged_and_a_rollback_logs_nothing(self, tmp_path):
        from dourmouse.office_logger import OfficeLogger
        from dourmouse.research_graph import store as gs

        log = OfficeLogger(tmp_path / "office.db")

        def obs(e):
            log.append_event(e["kind"], e["type"], e["id"], e["by"], e)

        gs.add_observer(obs)
        try:
            g = gs.GraphStore(tmp_path / "graph.db")
            g.put("hypothesis", "h1", {"statement": "s"}, created_by="t")
            g.put("hypothesis", "h1", {"statement": "s"}, created_by="t")  # idempotent: no second event
            g.revise("hypothesis", "h1", {"status": "tested"}, created_by="t")
            g.put("experiment", "e1", {"protocol": "p"}, created_by="t")
            g.link(("hypothesis", "h1"), "tested_by", ("experiment", "e1"), created_by="t")
            g.link(("hypothesis", "h1"), "tested_by", ("experiment", "e1"), created_by="t")  # no duplicate
            try:
                with g.transaction():
                    g.put("hypothesis", "h2", {"statement": "never"}, created_by="t")
                    raise RuntimeError("abort")
            except RuntimeError:
                pass
        finally:
            gs.remove_observer(obs)
        events = log.events_since(0)
        assert [(e["kind"], e["subject_id"]) for e in events] == [
            ("graph.put", "h1"), ("graph.revise", "h1"), ("graph.put", "e1"), ("graph.link", "h1")]
        assert events[1]["payload"]["version"] == 2 and events[3]["payload"]["relation"] == "tested_by"
        assert log.events_since(events[1]["seq"], "graph.put") == [events[2]]
