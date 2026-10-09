"""FIX-R R-11: the owner is told when a damaged chat snapshot was rebuilt, and helper ledgers are pruned."""

from __future__ import annotations

import os
import time

from dourmouse import chat as chat_mod
from dourmouse.chat import ChatSession
from dourmouse.dispatch import DispatchRegistry


def _fake_dispatch(messages, registry, **kwargs):
    messages.append({"role": "assistant", "content": "the answer"})
    return {"final_text": "the answer", "transcript": []}


def _damaged_session(tmp_path, monkeypatch) -> ChatSession:
    monkeypatch.setattr(chat_mod, "run_dispatch_messages", _fake_dispatch)
    first = ChatSession(DispatchRegistry(), session_file=tmp_path / "s.jsonl")
    first.ask("first question")
    state = tmp_path / "s.messages.json"
    state.write_text(state.read_text()[:25])  # a kill mid-write
    return ChatSession(DispatchRegistry(), session_file=tmp_path / "s.jsonl")


def test_the_first_reply_after_a_rebuild_says_so_in_plain_words_once(tmp_path, monkeypatch):
    resumed = _damaged_session(tmp_path, monkeypatch)
    assert resumed.state_recovery
    first = resumed.ask("second question")
    assert "saved state of this conversation was damaged" in first["final_text"]
    assert "rebuilt" in first["final_text"] and "tool" in first["final_text"]
    assert first["final_text"].rstrip().endswith("the answer")
    assert first["state_recovery"] == resumed.state_recovery
    second = resumed.ask("third question")
    assert second["final_text"] == "the answer" and "state_recovery" not in second


def test_what_is_saved_and_remembered_is_the_models_own_text_not_the_notice(tmp_path, monkeypatch):
    resumed = _damaged_session(tmp_path, monkeypatch)
    resumed.ask("second question")
    assert not any("damaged" in str(m.get("content")) for m in resumed.messages)
    ledger = (tmp_path / "s.jsonl").read_text()
    assert "damaged" not in ledger.split("second question")[-1]


def test_an_undamaged_session_has_no_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_mod, "run_dispatch_messages", _fake_dispatch)
    s = ChatSession(DispatchRegistry(), session_file=tmp_path / "ok.jsonl")
    out = s.ask("hello")
    assert out["final_text"] == "the answer" and "state_recovery" not in out


# ---- helper ledgers ------------------------------------------------------

def _make_helper_files(folder, n, age_days):
    old = time.time() - age_days * 86400
    for i in range(n):
        for suffix in (".jsonl", ".messages.json"):
            f = folder / f"session_20200101_00000{i}_{i:08x}{suffix}"
            f.write_text("x")
            os.utime(f, (old, old))


def test_old_helper_ledgers_and_their_snapshots_are_pruned_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(chat_mod, "_HELPER_PRUNE_AT", 0.0)
    helpers = chat_mod._helper_sessions_dir()
    _make_helper_files(helpers, 5, age_days=30)
    keeper = helpers / "notes.txt"
    keeper.write_text("not a session file")
    real = tmp_path / "sessions" / "session_20200101_000000_deadbeef.jsonl"
    real.write_text("the owner's conversation")
    os.utime(real, (1, 1))
    ChatSession(DispatchRegistry())  # a new throwaway helper triggers the pruning
    left = sorted(p.name for p in helpers.iterdir())
    assert not [n for n in left if n.startswith("session_2020")], left
    assert keeper.exists(), "only session files are touched"
    assert real.exists(), "the owner's own conversation ledgers are never touched, however old"
    assert len([n for n in left if n.endswith(".jsonl")]) == 0  # the new helper writes its ledger only on its first turn


def test_recent_helper_ledgers_are_kept_up_to_a_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(chat_mod, "_HELPER_PRUNE_AT", 0.0)
    monkeypatch.setattr(chat_mod, "_HELPER_KEEP_MAX", 3)
    helpers = chat_mod._helper_sessions_dir()
    _make_helper_files(helpers, 6, age_days=0)
    for i, f in enumerate(sorted(helpers.glob("session_*.jsonl"))):
        os.utime(f, (time.time() - 100 + i, time.time() - 100 + i))  # a known newest-last order
    ChatSession(DispatchRegistry())
    kept = sorted(p.name for p in helpers.glob("session_*.jsonl"))
    assert len(kept) == 3, kept
    assert kept == sorted(kept) and kept[-1].startswith("session_20200101_000005")
    assert len(list(helpers.glob("session_*.messages.json"))) == 3


def test_pruning_runs_at_most_once_in_a_while(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(chat_mod, "_HELPER_PRUNE_AT", 0.0)
    helpers = chat_mod._helper_sessions_dir()
    ChatSession(DispatchRegistry())
    _make_helper_files(helpers, 2, age_days=30)
    ChatSession(DispatchRegistry())  # within the interval: no second scan
    assert len(list(helpers.glob("session_2020*.jsonl"))) == 2
