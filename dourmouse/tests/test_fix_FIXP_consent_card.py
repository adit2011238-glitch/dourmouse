"""FIX-P R5-20: a typed yes never answers a privacy consent card."""

from __future__ import annotations

import threading
from typing import Any

from dourmouse import webui


def test_consent_cards_are_not_pending_items_but_actions_are():
    events: list[dict[str, Any]] = []
    gate = webui.WebConfirmationGate(events.append)
    result: dict[str, Any] = {}

    t = threading.Thread(target=lambda: result.update(a=gate.ask_owner("Send your Mail?")))
    t.start()
    for _ in range(200):
        if events:
            break
        threading.Event().wait(0.01)
    assert gate.pending_items() == []  # the affirm shortcut cannot see it
    assert gate.consent_pending() is True
    gate.resolve(events[-1]["id"], True)  # a click
    t.join(timeout=2)
    assert result["a"] is True and gate.consent_pending() is False


def test_action_cards_stay_visible_to_the_affirm_shortcut():
    events: list[dict[str, Any]] = []
    gate = webui.WebConfirmationGate(events.append)
    t = threading.Thread(target=lambda: gate("Send the email?"))
    t.start()
    for _ in range(200):
        if events:
            break
        threading.Event().wait(0.01)
    assert [p for _, p in gate.pending_items()] == ["Send the email?"] and gate.consent_pending() is False
    gate.resolve(events[-1]["id"], False)
    t.join(timeout=2)
