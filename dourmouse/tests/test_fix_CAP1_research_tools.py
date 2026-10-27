"""CAP1-1 (2026-10-27): the RESEARCH FIRST rule (finding #184) was inert for ordinary questions
because tools were only attached for agents scoring 3 or more, and a plain factual question
scores nothing. A prompt that needs evidence, or carries a link, now always gets the research
agent's tools, unless it is an operational request or a live-fetching agent already matched."""

from __future__ import annotations

import pytest

from dourmouse.dispatch import _URL_IN_PROMPT, run_dispatch_messages, system_message
from dourmouse.general_roster import build_general_registry


class _Msg:
    def __init__(self):
        self.content = "ok"
        self.tool_calls = None
        self.role = "assistant"


class _Resp:
    def __init__(self):
        self.choices = [type("C", (), {"message": _Msg(), "finish_reason": "stop"})()]
        self.usage = None


class _Client:
    def __init__(self):
        self.seen: list[list[str]] = []
        self.chat = type("X", (), {})()
        self.chat.completions = self

    def create(self, **kw):
        self.seen.append([t["function"]["name"] for t in (kw.get("tools") or [])])
        return _Resp()


def _tools_for(prompt):
    reg = build_general_registry()
    client = _Client()
    run_dispatch_messages([{"role": "system", "content": system_message(reg)}, {"role": "user", "content": prompt}], reg, client=client)
    return client.seen[0]


@pytest.mark.parametrize("prompt", [
    "Who is the current UK prime minister?",
    "how many people live in Japan",
    "Is it true that Python 3.15 removes the GIL?",
    "what did the Fed decide yesterday",
    "summarise https://example.com/post for me",
])
def test_a_question_about_the_world_is_given_web_search_and_fetch(prompt):
    names = _tools_for(prompt)
    assert "web_search" in names and "fetch_url" in names


@pytest.mark.parametrize("prompt", ["what is 2+2", "explain how dns works", "define ephemeral", "check my inbox"])
def test_stable_chat_and_operational_requests_do_not_get_the_research_tools_added(prompt):
    names = _tools_for(prompt)
    assert "web_search" not in names


def test_an_operational_question_is_not_loaded_with_research_tools():
    assert "web_search" not in _tools_for("how much free disk space do I have")


def test_the_url_pattern_finds_links_only():
    assert _URL_IN_PROMPT.search("read http://a.example/x please")
    assert _URL_IN_PROMPT.search("see HTTPS://A.EXAMPLE")
    assert not _URL_IN_PROMPT.search("example.com without a scheme")
