"""FIX-P R5-3: the learn store's automatic recall block (stored notes, imported history summaries, earlier
answers) is Memory content. It is injected into a turn only when Memory is allowed and the chat is not Private."""

from __future__ import annotations

import json

from dourmouse import chat as chat_mod
from dourmouse import privacy
from dourmouse.chat import ChatSession
from dourmouse.dispatch import DispatchRegistry
from dourmouse.memory_store import MemoryStore


def _session(tmp_path, monkeypatch, seen):
    def fake_dispatch(messages, registry, **kwargs):
        seen.append(json.dumps(messages))
        messages.append({"role": "assistant", "content": "ok"})
        return {"final_text": "ok", "transcript": []}

    monkeypatch.setattr(chat_mod, "run_dispatch_messages", fake_dispatch)
    monkeypatch.setattr(chat_mod, "learn_enabled", lambda: True)
    monkeypatch.setattr(chat_mod, "recall_block", lambda memory, prompt: "REMEMBERED CONTEXT\n- the merger closes Friday")
    return ChatSession(DispatchRegistry(), session_file=tmp_path / "s.jsonl", memory=MemoryStore(tmp_path / "m.db"))


def test_recall_not_injected_when_memory_consent_unset(tmp_path, monkeypatch):
    seen: list[str] = []
    session = _session(tmp_path, monkeypatch, seen)
    with privacy.chat_scope("tab-r1"):
        session.ask("what about the merger")
    assert "merger closes" not in seen[0]


def test_recall_not_injected_when_memory_denied(tmp_path, monkeypatch):
    privacy.PrivacyStore().set_consent("memory", "deny")
    seen: list[str] = []
    session = _session(tmp_path, monkeypatch, seen)
    with privacy.chat_scope("tab-r2"):
        session.ask("what about the merger")
    assert "merger closes" not in seen[0]


def test_recall_not_injected_in_private_mode_even_with_consent(tmp_path, monkeypatch):
    privacy.PrivacyStore().set_consent("memory", "allow")
    privacy.PrivacyStore().set_chat_private("tab-r3", True)
    seen: list[str] = []
    session = _session(tmp_path, monkeypatch, seen)
    with privacy.chat_scope("tab-r3"):
        session.ask("what about the merger")
    assert "merger closes" not in seen[0]


def test_recall_injected_when_allowed(tmp_path, monkeypatch):
    privacy.PrivacyStore().set_consent("memory", "allow")
    seen: list[str] = []
    session = _session(tmp_path, monkeypatch, seen)
    with privacy.chat_scope("tab-r4"):
        session.ask("what about the merger")
    assert "merger closes" in seen[0]
