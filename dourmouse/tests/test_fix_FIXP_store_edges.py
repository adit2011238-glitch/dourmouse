"""FIX-P R5-22: the consent store fails closed."""

from __future__ import annotations

import json

import pytest

from dourmouse import privacy


def _write(obj) -> None:
    path = privacy.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def test_corrupt_file_keeps_private_mode_on_and_says_why():
    privacy.PrivacyStore().set_private_mode(True)
    privacy.PrivacyStore().set_chat_private("tab-1", True)
    _write("{not json")
    store = privacy.PrivacyStore()
    assert store.consent("mail") is None  # every source asks
    assert store.private_mode() is True and store.private_for("tab-9") is True
    # still on after the file was set aside and the store read again
    assert privacy.PrivacyStore().private_mode() is True
    assert "kept as" in str(privacy.PrivacyStore().snapshot().get("recovery_note"))
    privacy.PrivacyStore().set_private_mode(False)  # the owner decides
    assert privacy.PrivacyStore().private_mode() is False
    assert "recovery_note" not in privacy.PrivacyStore().snapshot()


def test_wrong_types_never_raise_in_the_gate():
    _write({"consent": ["mail"], "private_chats": ["tab-1"], "providers": 3})
    store = privacy.PrivacyStore()
    assert store.consent("mail") is None
    assert store.private_mode() is True  # an unreadable list of Private chats: Private for all
    turn = privacy.TurnPrivacy(chat="t", private=False, placeholders=privacy.PlaceholderMap())
    out = privacy.gate_tool_result("gmail_read", {}, "From: a", turn=turn, agent="mail", store=store)
    assert out.startswith("NOT SENT")
    _write({"consent": "allow"})
    assert privacy.PrivacyStore().consent("mail") is None


def test_private_chats_are_never_evicted_to_make_room(monkeypatch):
    monkeypatch.setattr(privacy, "PRIVATE_CHATS_LIMIT", 3)
    store = privacy.PrivacyStore()
    for i in range(3):
        store.set_chat_private(f"tab-{i}", True)
    with pytest.raises(ValueError, match="already Private"):
        store.set_chat_private("tab-new", True)
    assert all(store.chat_private(f"tab-{i}") for i in range(3))
    store.set_chat_private("tab-0", True)  # an existing one can be re-set
    store.set_chat_private("tab-0", False)
    store.set_chat_private("tab-new", True)
