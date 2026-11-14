"""SEC2-1 (high): tool scope was advisory. A turn was shown only its agents'
tools, but the execution loop looked the model's chosen name up in the
whole registry, so a mail or web turn steered by what it read could run any
tool by naming it (run_command, write_path, gmail_send ...).
"""

from __future__ import annotations

import json

import pytest

from dourmouse.dispatch import (
    DispatchRegistry,
    Subagent,
    ToolSpec,
    run_dispatch_messages,
    system_message,
)
from dourmouse.general_roster import _build_delegate_tool
from dourmouse.peer_tools import build_peer_tools
from dourmouse.tests.test_dispatch import FakeClient, _FakeMessage, _FakeResponse, _FakeToolCall


@pytest.fixture(autouse=True)
def _no_fast_lane(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")


def _registry(counter):
    def tool(name):
        def handler(_args, n=name):
            counter.append(n)
            return f"{n} ran"
        return ToolSpec(name=name, description=f"{name} tool",
                        parameters={"type": "object", "properties": {}, "required": []}, handler=handler)

    registry = DispatchRegistry()
    registry.register_subagent(Subagent(name="news", domain="Test", description="news agent",
                                        tools=(tool("news_headlines"),)))
    registry.register_subagent(Subagent(name="system", domain="Test", description="system agent",
                                        tools=(tool("list_path"), tool("write_path_like"))))
    registry.register_subagent(Subagent(name="fanout", domain="Test", description="fans out",
                                        tools=(tool("delegate_parallel"),)))
    registry.register_subagent(Subagent(name="messenger", domain="Test", description="agents talk", tools=()))
    delegate = _build_delegate_tool(registry)
    for t in build_peer_tools(registry, delegate):
        registry.extend_subagent("messenger", t)
    return registry


def _call(name, **arguments):
    return _FakeResponse(_FakeMessage(tool_calls=[_FakeToolCall("c-" + name, name, json.dumps(arguments))]))


def _text(text):
    return _FakeResponse(_FakeMessage(content=text))


def _run(registry, client, agent):
    messages = [{"role": "system", "content": system_message(registry)},
                {"role": "user", "content": "summarise this mail"}]
    return run_dispatch_messages(messages, registry, client=client, forced_agent=agent)


def _results(report, name):
    return [e["text"] for e in report["transcript"] if e["type"] == "tool_result" and e["name"] == name]


def test_a_tool_of_another_agent_is_refused_and_never_runs():
    ran = []
    registry = _registry(ran)
    report = _run(registry, FakeClient([_call("list_path", path="/Users"), _text("done")]), "news")
    assert ran == []
    [result] = _results(report, "list_path")
    assert result.startswith("ERROR") and "not available" in result
    assert "system" in result  # names the agent it belongs to


def test_the_scoped_tools_still_run():
    ran = []
    registry = _registry(ran)
    _run(registry, FakeClient([_call("news_headlines"), _text("done")]), "news")
    assert ran == ["news_headlines"]


def test_unknown_names_keep_their_did_you_mean_help():
    ran = []
    registry = _registry(ran)
    report = _run(registry, FakeClient([_call("news_headline"), _text("done")]), "news")
    [result] = _results(report, "news_headline")
    assert "unknown tool" in result and "news_headlines" in result


def test_a_fanout_branch_cannot_fan_out_by_naming_the_tool():
    ran = []
    registry = _registry(ran)
    messages = [{"role": "system", "content": system_message(registry)}, {"role": "user", "content": "x"}]
    report = run_dispatch_messages(messages, registry, client=FakeClient([_call("delegate_parallel"), _text("done")]),
                                   forced_agent="news", fanout_branch=True)
    assert ran == []
    [result] = _results(report, "delegate_parallel")
    assert result.startswith("ERROR")
