"""Finding #114 (INFRA-1): standing agents run without prompting, wake on
their inbox, stay on the leash, and never take the runtime down."""

from __future__ import annotations

import threading
import time

import pytest

from dourmouse.message_bus import MessageBus
from dourmouse.standing_agents import StandingRuntime


class Echo:
    name = "echo"
    interval_s = 3600.0
    capabilities = frozenset({"read"})

    def __init__(self):
        self.ticks = 0
        self.answered = threading.Event()

    def tick(self):
        self.ticks += 1
        return f"tick {self.ticks}"

    def handle_message(self, subject, body, sender):
        self.answered.set()
        return None if subject == "ignore" else f"echo: {body}"


def test_a_message_wakes_the_agent_and_gets_an_answer():
    bus = MessageBus()
    rt = StandingRuntime(bus)
    agent = Echo()
    rt.register(agent)
    rt.start()
    try:
        time.sleep(0.2)  # first tick runs at once; the next is an hour away
        bus.post("research", "echo", "where", "is my thesis")
        assert agent.answered.wait(3), "the bus post must wake the agent, not its hourly timer"
        deadline = time.time() + 3
        while time.time() < deadline and not bus.inbox("research"):
            time.sleep(0.02)
        reply = bus.inbox("research")[0]
        assert reply["from"] == "echo" and reply["subject"] == "Re: where" and reply["body"] == "echo: is my thesis"
        st = rt.status()[0]
        assert st["ticks"] == 1 and st["answered"] == 1
        assert [a["kind"] for a in st["activity"]][:2] == ["answered", "tick"]
    finally:
        rt.stop()


def test_messages_are_answered_once_and_ignored_ones_left_unanswered():
    bus = MessageBus()
    rt = StandingRuntime(bus)
    rt.register(Echo())
    bus.post("a", "echo", "ignore", "x")
    bus.post("b", "echo", "q", "y")
    assert rt.drain_inbox("echo") == 1
    assert rt.drain_inbox("echo") == 0  # already read
    assert [m["body"] for m in bus.inbox("b")] == ["echo: y"] and bus.inbox("a") == []


def test_the_leash_refuses_anything_beyond_read_and_propose():
    class Mover(Echo):
        name = "mover"
        capabilities = frozenset({"read", "modify"})

    with pytest.raises(ValueError, match="may only read and propose"):
        StandingRuntime(MessageBus()).register(Mover())


def test_a_broken_tick_is_recorded_and_the_agent_keeps_going():
    class Broken(Echo):
        name = "broken"

        def tick(self):
            raise RuntimeError("disk vanished")

    rt = StandingRuntime(MessageBus())
    rt.register(Broken())
    assert rt.run_tick("broken") == ""
    st = rt.status()[0]
    assert st["errors"] == 1 and "disk vanished" in st["last_error"] and st["activity"][0]["kind"] == "error"
    assert rt.run_tick("broken") == "" and rt.status()[0]["ticks"] == 2


def test_broadcasts_are_not_answered():
    """Found live (finding #121): the live feeds broadcast every poll to "*",
    and the librarian answered each one as if it were a search."""
    bus = MessageBus()
    rt = StandingRuntime(bus)
    agent = Echo()
    rt.register(agent)
    bus.post("live", "*", "live:read_inbox", "3 new emails")
    assert rt.drain_inbox("echo") == 0 and not agent.answered.is_set()
    assert bus.outbox("echo") == []


def test_event_log_wakes_agents_that_asked_for_those_kinds(tmp_path):
    """R6 (finding #128): an agent with wake_on prefixes is handed matching
    events from the append-only log; others are not."""
    from dourmouse.office_logger import OfficeLogger

    class Watcher(Echo):
        name = "watcher"
        wake_on = ("graph.put",)

        def __init__(self):
            super().__init__()
            self.seen = []

        def handle_event(self, event):
            self.seen.append(event["kind"])
            return f"saw {event['subject_type']}"

    log = OfficeLogger(tmp_path / "o.db")
    rt = StandingRuntime(MessageBus())
    w = Watcher()
    rt.register(w)
    rt.attach_event_log(log)
    log.append_event("graph.put", "claim", "c1", "t")
    log.append_event("security.scan", "scan", "x", "sentry")
    assert rt.drain_events("watcher") == 1 and w.seen == ["graph.put"]
    assert rt.status()[0]["activity"][0]["text"] == "saw claim"


def _wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_event_queue_is_bounded_and_drops_are_counted(monkeypatch, caplog):
    from dourmouse import standing_agents as sa

    class Watcher(Echo):
        name = "watcher"
        wake_on = ("graph.",)

    monkeypatch.setattr(sa, "MAX_PENDING_EVENTS", 5)
    rt = StandingRuntime(MessageBus())
    rt.register(Watcher())
    with caplog.at_level("WARNING"):
        for i in range(12):
            rt._on_event({"kind": "graph.put", "n": i})
    assert len(rt._events["watcher"]) == 5
    assert [e["n"] for e in rt._events["watcher"]] == [7, 8, 9, 10, 11]  # oldest dropped
    assert rt.status()[0]["dropped_events"] == 7
    assert "dropped oldest" in caplog.text


def test_loop_survives_a_failure_outside_the_guarded_calls():
    """S29: an exception from inside the loop's own bookkeeping (here the
    inbox drain) used to end the thread silently."""
    rt = StandingRuntime(MessageBus())
    agent = Echo()
    rt.register(agent)
    calls = {"n": 0}
    real = rt.drain_inbox

    def flaky(name):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("bus exploded")
        return real(name)

    rt.drain_inbox = flaky
    rt.start()
    try:
        assert _wait_for(lambda: agent.ticks >= 1)
        st = rt.status()[0]
        assert st["errors"] >= 2
        assert any("bus exploded" in a["text"] for a in st["activity"] if a["kind"] == "error")
        assert st["health"] == "ok"
    finally:
        rt.stop()


def test_a_dead_loop_is_restarted_and_reported():
    class Fatal(Echo):
        name = "fatal"

        def __init__(self):
            super().__init__()
            self.boom = True

        def tick(self):
            if self.boom:
                self.boom = False
                raise SystemExit("agent tried to exit the thread")
            return super().tick()

    rt = StandingRuntime(MessageBus())
    agent = Fatal()
    rt.register(agent)
    assert rt.status()[0]["health"] == "not_started"
    rt.start()
    try:
        assert _wait_for(lambda: agent.ticks >= 1)
        st = rt.status()[0]
        assert st["restarts"] == 1
        assert any("SystemExit" in a["text"] for a in st["activity"] if a["kind"] == "error")
        assert st["health"] == "ok"
        assert rt.health() == {"fatal": "ok"}
    finally:
        rt.stop()


def test_stalled_loop_is_reported(monkeypatch):
    rt = StandingRuntime(MessageBus())
    rt.register(Echo())
    rt.start()
    try:
        assert _wait_for(lambda: rt.status()[0]["last_alive_at"] is not None)
        rt._state["echo"].last_alive_at = time.time() - 100000
        assert rt.status()[0]["health"] == "stalled"
    finally:
        rt.stop()
