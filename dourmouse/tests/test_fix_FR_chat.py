"""FR fixes P4-21, P4-14, P4-15 in chat.py."""

from __future__ import annotations

import json

from dourmouse import chat as chat_mod
from dourmouse.chat import ChatSession, most_recent_session_file
from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec


def _registry_with_a_tool() -> DispatchRegistry:
    reg = DispatchRegistry()
    reg.register_subagent(Subagent(
        name="x", domain="d", description="d",
        tools=(ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {}}, handler=lambda a: "ok"),),
    ))
    return reg


# ---- P4-21 ---------------------------------------------------------------

def test_helper_session_does_not_become_the_most_recent_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    real = ChatSession(_registry_with_a_tool())
    real._persist("hello", {"final_text": "hi", "transcript": []}, 1.0)
    helper = ChatSession(DispatchRegistry(), session_file=None)  # tool-less throwaway helper
    helper._persist("summarise this file text", {"final_text": "sum", "transcript": []}, 1.0)
    import os
    os.utime(real.session_file, (1, 1))  # the helper is newer, as after a background job
    assert helper.session_file != real.session_file
    assert helper.session_file.parent != real.session_file.parent
    assert most_recent_session_file() == real.session_file


def test_two_sessions_started_in_the_same_second_never_share_a_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))

    class _Frozen(chat_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return chat_mod.datetime(2026, 1, 1, 12, 0, 0)

    monkeypatch.setattr(chat_mod, "datetime", _Frozen)
    a = ChatSession(DispatchRegistry())
    b = ChatSession(DispatchRegistry())
    a.messages.append({"role": "user", "content": "FILE CONTENTS A"})
    a._persist("p", {"final_text": "x", "transcript": []}, 1.0)
    assert a.session_file != b.session_file
    c = ChatSession(DispatchRegistry())
    assert not any("FILE CONTENTS A" in str(m) for m in c.messages)


# ---- P4-14 ---------------------------------------------------------------

def _run_turns(tmp_path, monkeypatch, prompts):
    """Drive ask() with a fake dispatch loop that records what it was sent."""
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    seen: list[list[str]] = []

    def fake_dispatch(messages, registry, **kwargs):
        seen.append([str(m.get("content")) for m in messages])
        messages.append({"role": "assistant", "content": "ok"})
        return {"final_text": "ok", "transcript": []}

    monkeypatch.setattr(chat_mod, "run_dispatch_messages", fake_dispatch)
    monkeypatch.setattr(chat_mod, "learn_enabled", lambda: True)
    monkeypatch.setattr(chat_mod, "recall_block", lambda memory, prompt: f"REMEMBERED CONTEXT\n- fact about {prompt}")
    monkeypatch.setattr("dourmouse.skills.skill_context_block", lambda prompt: f"[SKILL: s]\nskill for {prompt}")
    from dourmouse.memory_store import MemoryStore

    session = ChatSession(DispatchRegistry(), session_file=tmp_path / "s.jsonl", memory=MemoryStore(tmp_path / "m.db"))
    for p in prompts:
        session.ask(p)
    return session, seen


def test_previous_turns_recall_and_skill_blocks_are_not_resent(tmp_path, monkeypatch):
    session, seen = _run_turns(tmp_path, monkeypatch, ["topic A", "topic B"])
    second = "\n".join(seen[1])
    assert "fact about topic B" in second and "skill for topic B" in second
    assert "fact about topic A" not in second and "skill for topic A" not in second
    saved = json.dumps(json.loads((tmp_path / "s.messages.json").read_text()))
    assert "topic B" in saved and "fact about topic A" not in saved and "skill for topic A" not in saved


# ---- P4-15 ---------------------------------------------------------------

def test_snapshot_is_written_atomically(tmp_path, monkeypatch):
    session, _ = _run_turns(tmp_path, monkeypatch, ["one"])
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads((tmp_path / "s.messages.json").read_text())[-1]["content"] == "ok"


def test_truncated_snapshot_is_rebuilt_from_the_ledger_not_fatal(tmp_path, monkeypatch):
    _run_turns(tmp_path, monkeypatch, ["first question", "second question"])
    state = tmp_path / "s.messages.json"
    state.write_text(state.read_text()[:25])  # a kill mid-write
    resumed = ChatSession(DispatchRegistry(), session_file=tmp_path / "s.jsonl")
    users = [m["content"] for m in resumed.messages if m["role"] == "user"]
    assert users == ["first question", "second question"]
    assert resumed.state_recovery and "rebuilt 2 turn(s)" in resumed.state_recovery
    assert (tmp_path / "s.messages.json.corrupt").exists()


def test_a_non_serialisable_transcript_value_does_not_raise_from_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    session = ChatSession(DispatchRegistry(), session_file=tmp_path / "s.jsonl")
    session._persist("p", {"final_text": "x", "transcript": [{"type": "t", "value": {1, 2}}]}, 1.0)
    from dourmouse.chat import verify_session_audit

    ok, errors = verify_session_audit(tmp_path / "s.jsonl")
    assert ok, errors
