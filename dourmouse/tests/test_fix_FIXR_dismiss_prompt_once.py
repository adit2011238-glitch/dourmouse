"""FIX-R R-10: dismissing a sentry finding shows ONE question, the spec's own, not two stacked."""

from __future__ import annotations

from dourmouse import dispatch
from dourmouse.general_roster import build_general_registry


def _tool(name):
    registry = build_general_registry()
    return next(t for s in registry.all_subagents() for t in s.tools if t.name == name)


def test_the_registered_dismiss_tool_asks_one_question(monkeypatch):
    prompts: list[str] = []
    out = dispatch._execute_tool(_tool("security_sentry_dismiss"), {"fingerprint": "fp-none"}, lambda p: prompts.append(p) or False)
    assert out.startswith("DECLINED")
    assert len(prompts) == 1
    assert prompts[0].count("false positive") == 1, prompts[0]
    assert prompts[0].count("Dismiss") == 1, prompts[0]


def test_the_argument_gate_leaves_a_tool_that_asks_for_itself_alone():
    assert dispatch._argument_gate(_tool("security_sentry_dismiss"), {"fingerprint": "fp"}, "orchestrator") is None


def test_the_argument_gate_still_covers_a_copy_of_the_tool_that_lost_its_own_gate():
    from dataclasses import replace

    from dourmouse.dispatch import Permission

    bare = replace(_tool("security_sentry_dismiss"), permission=Permission.REGULAR, confirm_prompt=None)
    decision = dispatch._argument_gate(bare, {"fingerprint": "fp"}, "orchestrator")
    assert decision is not None and decision[0] == "confirm" and "fp" in decision[1]
