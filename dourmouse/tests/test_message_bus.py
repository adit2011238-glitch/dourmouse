"""Inter-agent message bus tests (v3.0, dourmouse/message_bus.py).

The agents communicate with each other on two planes: a deterministic data
plane (live agents broadcast their REAL poll results onto the bus) and an
LLM-mediated tool plane (the messenger subagent's send_message / read_inbox).
All tests are hermetic — fresh buses, fake fetchers, real HTTP only on
ephemeral ports, zero network (Rule 2.1). No fabricated message bodies
anywhere (Rule 2.2).
"""

from __future__ import annotations

import json
import threading

import pytest

from dourmouse import message_bus as mb
from dourmouse.general_roster import build_general_registry
from dourmouse.live_runtime import LiveRuntime
from dourmouse.message_bus import BROADCAST, MessageBus
from dourmouse.tests.test_live_runtime import _FakeTracker, _live_registry
from dourmouse.tests.test_webui import _echo_registry
from dourmouse.webui import run_server


@pytest.fixture(autouse=True)
def _fresh_bus():
    """Every test gets a fresh process bus singleton (no cross-test leaks)."""
    mb.set_message_bus(MessageBus())
    yield
    mb.set_message_bus(None)


# --------------------------------------------------------------------------- #
# MessageBus — core semantics
# --------------------------------------------------------------------------- #

class TestMessageBus:
    def test_post_and_inbox(self):
        bus = MessageBus()
        msg = bus.post("news", "markets", "catalyst", "NVDA spiked")
        assert msg["id"].startswith("msg-")
        assert msg["from"] == "news"
        assert msg["to"] == "markets"
        assert msg["read"] is False  # per-recipient: nobody has read it yet
        assert msg["read_by"] == []  # JSON-safe list, never a raw set

        rows = bus.inbox("markets")
        assert len(rows) == 1
        assert rows[0]["body"] == "NVDA spiked"
        # Other agents don't see direct messages.
        assert bus.inbox("rnd") == []

    def test_broadcast_goes_to_everyone(self):
        bus = MessageBus()
        bus.post("news", BROADCAST, "flash", "headline: markets steady")
        for agent in ("markets", "rnd", "tasks", "news"):
            assert any(m["from"] == "news" for m in bus.inbox(agent))

    def test_outbox_and_order_newest_first(self):
        bus = MessageBus()
        bus.post("a", "b", "one", "first")
        bus.post("a", "b", "two", "second")
        bus.post("b", "a", "reply", "third")
        out = bus.outbox("a")
        assert [m["subject"] for m in out] == ["two", "one"]
        # b's inbox: broadcast/direct newest first (no broadcasts here).
        assert [m["subject"] for m in bus.inbox("b")] == ["two", "one"]

    def test_unread_and_mark_read(self):
        bus = MessageBus()
        bus.post("a", "b", "s", "body")
        bus.post("a", BROADCAST, "s2", "body")
        assert bus.unread_count("b") == 2
        rows = bus.inbox("b")
        assert bus.mark_read(rows[0]["id"], "b") is True
        assert bus.unread_count("b") == 1
        assert bus.mark_read("msg-does-not-exist", "b") is False

    def test_read_state_is_per_recipient(self):
        """Reading a broadcast must only clear the READER's badge — other
        agents keep their own unread status (reviewer-caught flaw)."""
        bus = MessageBus()
        bus.post("news", BROADCAST, "flash", "breaking")
        assert bus.unread_count("markets") == 1
        assert bus.unread_count("rnd") == 1
        row = bus.inbox("markets")[0]
        assert row["read"] is False
        assert bus.mark_read(row["id"], "markets") is True
        assert bus.unread_count("markets") == 0  # reader cleared
        assert bus.unread_count("rnd") == 1  # other agent untouched
        assert bus.inbox("rnd")[0]["read"] is False
        # snapshot (no viewer) reports read-by-anyone, JSON-safe read_by list.
        snap = bus.snapshot()[0]
        assert snap["read"] is True
        assert snap["read_by"] == ["markets"]

    def test_bounded_ring_evicts_oldest(self):
        bus = MessageBus(max_messages=5)
        for i in range(8):
            bus.post("a", "b", f"s{i}", f"body{i}")
        assert bus.count() == 5
        subjects = [m["subject"] for m in bus.snapshot()]
        assert subjects == ["s7", "s6", "s5", "s4", "s3"]  # oldest 3 evicted

    def test_body_and_subject_capped(self):
        bus = MessageBus()
        bus.post("a", "b", "x" * 500, "y" * 5000)
        row = bus.inbox("b")[0]
        assert len(row["subject"]) <= 200
        assert len(row["body"]) <= 1200

    def test_snapshot_and_clear(self):
        bus = MessageBus()
        bus.post("a", "b", "s", "body")
        assert len(bus.snapshot()) == 1
        bus.clear()
        assert bus.count() == 0

    def test_observers_fire_and_never_break_post(self):
        bus = MessageBus()
        seen = []
        bus.on_post(lambda m: seen.append(m["id"]))
        bus.on_post(lambda m: (_ for _ in ()).throw(RuntimeError("observer boom")))
        msg = bus.post("a", "b", "s", "body")  # raising observer swallowed
        assert seen == [msg["id"]]

    def test_thread_safety(self):
        bus = MessageBus(max_messages=100)
        errors = []

        def _writer(n):
            try:
                for i in range(50):
                    bus.post("a", BROADCAST, f"s{n}-{i}", "body")
            except Exception as exc:  # pragma: no cover - only on failure
                errors.append(exc)

        threads = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert bus.count() == 100  # bounded under concurrent writes

    def test_singleton_get_set(self):
        b1 = mb.get_message_bus()
        b2 = mb.get_message_bus()
        assert b1 is b2  # stable singleton
        fresh = MessageBus()
        mb.set_message_bus(fresh)
        assert mb.get_message_bus() is fresh


# --------------------------------------------------------------------------- #
# Live runtime — agents broadcast their real poll results
# --------------------------------------------------------------------------- #

class TestLiveBroadcast:
    def test_poll_result_is_broadcast_with_agent_as_sender(self):
        bus = MessageBus()
        rt = LiveRuntime(
            _live_registry(),
            _FakeTracker(),
            fetcher=lambda tool, args: f"REAL {tool} data",
            schedule={"news": [("news_headlines", {}, 60)]},
            bus=bus,
        )
        rt.start()
        try:
            deadline = __import__("time").time() + 2
            while not bus.snapshot() and __import__("time").time() < deadline:
                __import__("time").sleep(0.02)
            msgs = bus.snapshot()
            assert msgs
            assert msgs[0]["from"] == "news"
            assert msgs[0]["to"] == BROADCAST
            assert "news_headlines" in msgs[0]["subject"]
            assert "REAL news_headlines data" in msgs[0]["body"]
        finally:
            rt.stop()

    def test_failed_poll_is_broadcast_honestly(self):
        bus = MessageBus()

        def _boom(tool, args):
            raise RuntimeError("network down")

        rt = LiveRuntime(
            _live_registry(),
            _FakeTracker(),
            fetcher=_boom,
            schedule={"news": [("news_headlines", {}, 60)]},
            bus=bus,
        )
        rt.start()
        try:
            deadline = __import__("time").time() + 2
            while not bus.snapshot() and __import__("time").time() < deadline:
                __import__("time").sleep(0.02)
            assert bus.snapshot()[0]["body"].startswith("LIVE POLL FAILED")
        finally:
            rt.stop()

    def test_no_bus_means_no_posting(self):
        bus = MessageBus()
        rt = LiveRuntime(
            _live_registry(),
            _FakeTracker(),
            fetcher=lambda tool, args: "data",
            schedule={"news": [("news_headlines", {}, 60)]},
            bus=None,  # v3.0 default: hermetic tests post nothing
        )
        rt.start()
        try:
            deadline = __import__("time").time() + 1
            while not bus.snapshot() and __import__("time").time() < deadline:
                __import__("time").sleep(0.02)
            assert bus.snapshot() == []
        finally:
            rt.stop()


# --------------------------------------------------------------------------- #
# Messenger subagent — the tool plane (LLM-mediated agent-to-agent comms)
# --------------------------------------------------------------------------- #

class TestMessengerTools:
    def _registry(self):
        return build_general_registry()

    def _call(self, name: str, arguments: dict):
        spec = self._registry().lookup(name)
        assert spec is not None, f"{name} not registered"
        return spec.handler(arguments)

    def test_send_message_posts_to_bus(self):
        out = self._call(
            "send_message",
            {"from_agent": "research_info", "to_agent": "markets", "subject": "catalyst", "body": "NVDA spiked on news"},
        )
        assert "MESSAGE SENT" in out
        assert "research_info" in out and "markets" in out
        rows = mb.get_message_bus().inbox("markets")
        assert rows and rows[0]["subject"] == "catalyst"

    def test_send_message_broadcast(self):
        out = self._call("send_message", {"from_agent": "news", "to_agent": "*", "subject": "flash", "body": "breaking"})
        assert "broadcast" in out
        assert mb.get_message_bus().inbox("rnd")

    def test_send_message_refuses_unknown_sender(self):
        out = self._call("send_message", {"from_agent": "ghost", "to_agent": "markets", "subject": "s", "body": "b"})
        assert out.startswith("REFUSED")
        assert "unknown sender" in out

    def test_send_message_refuses_unknown_recipient(self):
        out = self._call("send_message", {"from_agent": "news", "to_agent": "ghost", "subject": "s", "body": "b"})
        assert out.startswith("REFUSED")
        assert "unknown recipient" in out

    def test_send_message_requires_body(self):
        out = self._call("send_message", {"from_agent": "news", "to_agent": "markets", "subject": "s"})
        assert "non-empty 'body'" in out

    def test_read_agent_inbox_returns_real_messages(self):
        bus = mb.get_message_bus()
        bus.post("news", "markets", "catalyst", "NVDA spiked")
        bus.post("rnd", "markets", "research", "new paper")
        out = self._call("read_agent_inbox", {"agent": "markets"})
        assert "INBOX (markets)" in out
        assert "2 shown" in out
        assert "NVDA spiked" in out

    def test_read_agent_inbox_honest_empty(self):
        out = self._call("read_agent_inbox", {"agent": "markets"})
        assert "empty" in out

    def test_read_agent_inbox_refuses_unknown_agent(self):
        out = self._call("read_agent_inbox", {"agent": "ghost"})
        assert out.startswith("REFUSED")

    def test_mail_read_inbox_still_imap_not_bus(self):
        """Regression guard (reviewer-caught): the messenger tool is named
        read_agent_inbox precisely so it does NOT collide with the mail
        subagent's IMAP read_inbox — both must exist with their own tools."""
        reg = self._registry()
        mail = reg.get_subagent("mail")
        messenger = reg.get_subagent("messenger")
        assert mail is not None and messenger is not None
        mail_tools = {t.name for t in mail.tools}
        msg_tools = {t.name for t in messenger.tools}
        assert "read_inbox" in mail_tools       # IMAP stays on mail
        assert "read_agent_inbox" in msg_tools  # bus inbox on messenger
        assert "read_inbox" not in msg_tools

    def test_tools_registered_on_messenger_subagent(self):
        reg = self._registry()
        sub = reg.get_subagent("messenger")
        assert sub is not None
        names = {t.name for t in sub.tools}
        # query_shared_memory (shared_rag.py) rides every non-orchestrator
        # subagent — see build_general_registry's own comment. v13.7:
        # query_desktop_vault (desktop_rag.py) rides alongside it now too.
        assert names == {"send_message", "read_agent_inbox", "query_shared_memory", "query_desktop_vault"}
        assert "messenger" in reg.subagent_names


# --------------------------------------------------------------------------- #
# HTTP — /api/messages + /api/agent/<name> inbox
# --------------------------------------------------------------------------- #

class TestMessagesApi:
    def test_messages_endpoint_returns_bus_traffic(self):
        bus = MessageBus()
        srv = run_server(_echo_registry(), port=0, client=None, config=None, bus=bus)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            bus.post("echo_agent", "echo_agent", "hello", "world")
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/api/messages")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 200
            assert data["count"] == 1
            assert data["messages"][0]["body"] == "world"
            assert "echo_agent" in data["unread"]
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_agent_endpoint_includes_inbox_and_unread(self):
        bus = MessageBus()
        srv = run_server(_echo_registry(), port=0, client=None, config=None, bus=bus)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            bus.post("echo_agent", "echo_agent", "hi", "there")
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/api/agent/echo_agent")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 200
            # v3.0: opening the agent's window READS its inbox — unread clears.
            assert data["inbox"][0]["subject"] == "hi"
            assert data["inbox"][0]["read"] is True
            assert data["unread"] == 0
            assert bus.unread_count("echo_agent") == 0
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_agent_endpoint_leaves_other_agents_unread(self):
        """Reading one agent's inbox must NOT clear another agent's unread."""
        bus = MessageBus()
        srv = run_server(_echo_registry(), port=0, client=None, config=None, bus=bus)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            bus.post("echo_agent", "echo_agent", "hi", "there")
            bus.post("echo_agent", BROADCAST, "flash", "breaking")
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/api/agent/echo_agent")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 200
            assert data["unread"] == 0  # echo_agent read its own
            # A different roster agent still has the broadcast unread.
            assert bus.unread_count("other_agent") == 1
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_messages_endpoint_since_filter_returns_only_new(self):
        """v3.2: ?since=msg-<N> returns ONLY messages newer than that id,
        while unread counts stay absolute (whole-bus, never the window)."""
        bus = MessageBus()
        srv = run_server(_echo_registry(), port=0, client=None, config=None, bus=bus)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            bus.post("news", "markets", "first", "one")
            bus.post("news", BROADCAST, "second", "two")
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/api/messages?since=msg-1")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 200
            # Only the message with id > msg-1 is returned.
            assert len(data["messages"]) == 1
            assert data["messages"][0]["subject"] == "second"
            # Unread is still the ABSOLUTE count across the whole bus for a
            # REGISTERED roster agent (echo_agent receives the broadcast).
            assert data["unread"]["echo_agent"] == 1
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_messages_endpoint_malformed_since_falls_back_to_full_window(self):
        """Garbage since= must never error — anchored regex falls back to
        the full window (safe by construction, locked by this test)."""
        bus = MessageBus()
        srv = run_server(_echo_registry(), port=0, client=None, config=None, bus=bus)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            bus.post("news", "markets", "hello", "world")
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/api/messages?since=not-a-msg-id")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 200
            assert len(data["messages"]) == 1  # full window, no error
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_messages_endpoint_since_accepts_bare_number(self):
        bus = MessageBus()
        srv = run_server(_echo_registry(), port=0, client=None, config=None, bus=bus)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            bus.post("news", "markets", "only", "one")
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/api/messages?since=5")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert data["messages"] == []  # msg-1 is not > 5
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_server_defaults_to_process_singleton_bus(self):
        bus = mb.get_message_bus()
        srv = run_server(_echo_registry(), port=0, client=None, config=None)
        try:
            assert srv.bus is bus
        finally:
            srv.server_close()

    def test_bus_mirrors_to_memory_store(self, tmp_path):
        from dourmouse.memory_store import MemoryStore

        store = MemoryStore(tmp_path / "mem.db")
        bus = MessageBus()
        srv = run_server(
            _echo_registry(), port=0, client=None, config=None,
            memory=store, bus=bus,
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            bus.post("news", "markets", "catalyst", "NVDA spiked on earnings")
            deadline = 0
            import time as _t
            while store.count() == 0 and deadline < 100:
                _t.sleep(0.02)
                deadline += 1
            assert store.count() == 1
            hits = store.search("NVDA")
            assert hits and hits[0]["source"] == "bus"
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
            store.close()


# --------------------------------------------------------------------------- #
# UI wiring — the comms surfaces
# --------------------------------------------------------------------------- #

class TestUiWiring:
    def _read(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / rel).read_text(encoding="utf-8")

    def test_dashboard_has_comms_panel(self):
        html = self._read("ui/index.html")
        assert "AGENT COMMS" in html
        assert "pollComms" in html
        assert "/api/messages" in html

    def test_map_has_inbox_and_unread_badges(self):
        html = self._read("ui/map.html")
        assert "INBOX // INTER-AGENT COMMS" in html
        assert "applyInboxBadge" in html
        assert "pollBus" in html  # v3.2: renamed from pollInboxes
        assert "pulseTo" in html  # v3.2: live neural-link pulse animation

    def test_agent_window_has_inbox(self):
        html = self._read("ui/agent.html")
        assert "INBOX // INTER-AGENT COMMS" in html
        assert "renderInbox" in html

    def test_roster_ships_messenger(self):
        src = self._read("dourmouse/general_roster.py")
        assert "_build_messenger_subagent" in src
        assert "send_message" in src and "read_inbox" in src

    def test_live_runtime_ships_bus(self):
        src = self._read("dourmouse/live_runtime.py")
        assert "self._bus.post" in src

    def test_webui_ships_messages_route(self):
        src = self._read("dourmouse/webui.py")
        assert "/api/messages" in src
        assert "MessageBus" in src
