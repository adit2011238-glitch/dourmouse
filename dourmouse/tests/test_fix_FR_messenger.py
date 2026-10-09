"""FR fixes P2-22 (send_message from a plan-routed messenger turn) and P2-18
(read_agent_inbox scoped to the real caller)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dourmouse.dispatch import run_dispatch_messages
from dourmouse.general_roster import build_general_registry
from dourmouse.message_bus import MessageBus, set_message_bus


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _Tc:
    def __init__(self, cid, name, arguments):
        self.id, self.function = cid, _Fn(name, arguments)


class _Resp:
    def __init__(self, content=None, tool_calls=None):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]


class _Client:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]


@pytest.fixture()
def bus():
    b = MessageBus()
    set_message_bus(b)
    yield b
    set_message_bus(None)


def _turn(prompt, *responses, **kw):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": prompt}]
    return run_dispatch_messages(msgs, build_general_registry(), client=_Client(*responses), **kw)


def test_a_plan_routed_messenger_turn_can_send_a_message(bus):
    args = json.dumps({"to_agent": "markets", "subject": "hi", "body": "hello from the user"})
    report = _turn(
        "send a message to the markets agent saying hello from the user",
        _Resp(tool_calls=[_Tc("c1", "send_message", args)]), _Resp(content="sent"),
    )
    results = [e["text"] for e in report["transcript"] if e["type"] == "tool_result"]
    assert results and results[0].startswith("MESSAGE SENT: messenger -> markets"), results
    assert bus.snapshot()[-1]["from"] == "messenger"


def test_the_top_level_orchestrator_turn_is_still_refused(bus):
    args = json.dumps({"to_agent": "markets", "body": "x"})
    report = _turn(
        "tell me a joke about cats and then something else entirely",
        _Resp(tool_calls=[_Tc("c1", "send_message", args)]), _Resp(content="done"),
    )
    results = [e["text"] for e in report["transcript"] if e["type"] == "tool_result"]
    assert results and (results[0].startswith("REFUSED") or "unknown tool" in results[0])
    assert bus.snapshot() == []


def test_a_routed_messenger_still_cannot_send_as_another_agent(bus):
    args = json.dumps({"from_agent": "security", "to_agent": "markets", "body": "x"})
    report = _turn(
        "send a message to the markets agent saying hi",
        _Resp(tool_calls=[_Tc("c1", "send_message", args)]), _Resp(content="done"),
    )
    results = [e["text"] for e in report["transcript"] if e["type"] == "tool_result"]
    assert results and results[0].startswith("REFUSED") and "cannot send as" in results[0]


# ---- P2-18 -----------------------------------------------------------------

def test_a_scoped_run_reads_only_its_own_inbox(bus):
    bus.post(from_agent="dourmouse", to_agent="security", subject="alert", body="private alert")
    args = json.dumps({"agent": "security"})
    report = _turn(
        "check the inbox", _Resp(tool_calls=[_Tc("c1", "read_agent_inbox", args)]), _Resp(content="x"),
        forced_agent="messenger",
    )
    results = [e["text"] for e in report["transcript"] if e["type"] == "tool_result"]
    assert results and results[0].startswith("REFUSED") and "only its own" in results[0]
    assert bus.unread_count("security") == 1  # its badge was not cleared


def test_a_scoped_run_can_read_its_own_inbox(bus):
    bus.post(from_agent="dourmouse", to_agent="messenger", subject="hello", body="for you")
    args = json.dumps({"agent": "messenger"})
    report = _turn(
        "check the inbox", _Resp(tool_calls=[_Tc("c1", "read_agent_inbox", args)]), _Resp(content="x"),
        forced_agent="messenger",
    )
    results = [e["text"] for e in report["transcript"] if e["type"] == "tool_result"]
    assert results and results[0].startswith("INBOX (messenger)")
