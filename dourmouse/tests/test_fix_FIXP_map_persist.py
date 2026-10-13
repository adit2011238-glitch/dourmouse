"""FIX-P R5-13: a chat's placeholder map survives a restart and eviction, and a full map refuses the value."""

from __future__ import annotations

import os
import stat

import pytest

from dourmouse import privacy


def _forget_in_memory(chat: str) -> None:
    with privacy._maps_lock:
        privacy._MAPS.pop(chat, None)


def test_numbering_and_values_survive_a_restart():
    m = privacy.placeholder_map("tab-persist")
    assert m.placeholder("EMAIL", "alice@example.org") == "[EMAIL_1]"
    _forget_in_memory("tab-persist")  # a restart: the in-memory map is gone
    again = privacy.placeholder_map("tab-persist")
    assert again.restore_text("[EMAIL_1]") == "alice@example.org"
    assert again.placeholder("EMAIL", "bob@example.org") == "[EMAIL_2]"  # never reuses [EMAIL_1] for Bob


def test_eviction_of_the_65th_chat_does_not_lose_a_chat(monkeypatch):
    first = privacy.placeholder_map("tab-evict-0")
    first.placeholder("PHONE", "+44 20 7946 0958")
    for i in range(1, privacy._MAPS_LIMIT + 3):
        privacy.placeholder_map(f"tab-evict-{i}")
    assert "tab-evict-0" not in privacy._MAPS
    back = privacy.placeholder_map("tab-evict-0")
    assert back.restore_text("[PHONE_1]") == "+44 20 7946 0958"


def test_the_map_file_is_private_and_a_different_chat_has_its_own():
    privacy.placeholder_map("tab-own-a").placeholder("EMAIL", "alice@example.org")
    privacy.placeholder_map("tab-own-b").placeholder("EMAIL", "bob@example.org")
    folder = privacy._map_file("x").parent
    files = list(folder.glob("*.json"))
    assert len(files) >= 2
    for f in files:
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    _forget_in_memory("tab-own-a")
    assert privacy.placeholder_map("tab-own-a").restore_text("[EMAIL_1]") == "alice@example.org"


def test_a_full_map_refuses_the_value_instead_of_sending_a_bare_placeholder(monkeypatch):
    monkeypatch.setattr(privacy.PlaceholderMap, "MAX_VALUES", 2)
    m = privacy.PlaceholderMap()
    m.placeholder("EMAIL", "a@example.org")
    m.placeholder("EMAIL", "b@example.org")
    with pytest.raises(privacy.PlaceholderMapFull):
        m.placeholder("EMAIL", "c@example.org")
    assert m.placeholder("EMAIL", "a@example.org") == "[EMAIL_1]"  # a known value still works
    turn = privacy.TurnPrivacy(chat="t", private=False, placeholders=m)
    out = privacy.gate_tool_result("web_search", {}, "mail c@example.org", turn=turn)
    assert out.startswith("NOT SENT") and "c@example.org" not in out
