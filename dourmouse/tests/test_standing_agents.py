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
