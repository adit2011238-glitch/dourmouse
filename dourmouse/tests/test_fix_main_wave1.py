"""Wave 1 integration fixes by the main thread (finding #173)."""
from __future__ import annotations

import pytest

from dourmouse.dispatch import _argument_gate
from dourmouse.general_roster import build_general_registry


def _spec(name):
    registry = build_general_registry()
    subs = [registry.get_subagent(n) for n in registry.subagent_names]
    return next(t for sub in subs if sub is not None for t in sub.tools if t.name == name)


@pytest.mark.parametrize("key", ["Space", " ", "Control+Space", "spacebar", "Enter"])
def test_space_and_enter_presses_ask_first(key):
    decision = _argument_gate(_spec("browser_press"), {"key": key}, "orchestrator")
    assert decision is not None and decision[0] == "confirm"


def test_other_keys_do_not_ask():
    assert _argument_gate(_spec("browser_press"), {"key": "Tab"}, "orchestrator") is None


def test_browser_click_is_routed_through_the_click_gate(monkeypatch):
    from dourmouse import browser_agent

    seen = {}

    def fake(arguments):
        seen["args"] = arguments
        return ("confirm", "Click Send on example.com?")

    monkeypatch.setattr(browser_agent, "click_gate", fake)
    decision = _argument_gate(_spec("browser_click"), {"target": "e12"}, "orchestrator")
    assert decision == ("confirm", "Click Send on example.com?")
    assert seen["args"]["target"] == "e12"


def test_a_plain_click_passes_the_gate(monkeypatch):
    from dourmouse import browser_agent

    monkeypatch.setattr(browser_agent, "click_gate", lambda a: None)
    assert _argument_gate(_spec("browser_click"), {"target": "e3"}, "orchestrator") is None


def test_quarantine_refuses_system_paths_in_any_case(tmp_path):
    from pathlib import Path

    from dourmouse.security.response import ResponseRefused, _check_quarantinable

    for spelled in ("/System/Library/CoreServices", "/SYSTEM/Library/CoreServices", "/system/library"):
        p = Path(spelled)
        if not p.exists():
            continue  # a case-sensitive volume has no such alias; the real spelling is covered below
        with pytest.raises(ResponseRefused):
            _check_quarantinable(p)
    with pytest.raises(ResponseRefused):
        _check_quarantinable(Path("/System/Library"))
