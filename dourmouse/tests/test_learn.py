"""Store & Learn loop tests (v2.9, dourmouse/learn.py + chat/webui wiring).

The system stores data (every completed session auto-ingests into the
long-term SQLite FTS5 store) and learns from it (relevant stored knowledge is
recalled into each new prompt; operator 👍/👎 feedback steers later recall).
All tests are hermetic — tmp stores, fake clients, real HTTP only against a
local ephemeral-port server, zero network (Rule 2.1). Honest degradation is
asserted, not fabricated success (Rule 2.2).
"""

from __future__ import annotations

import json
import threading

import pytest

from dourmouse import learn
from dourmouse.chat import ChatSession
from dourmouse.dispatch import system_message
from dourmouse.memory_store import MemoryStore
from dourmouse.tests.test_chat import FakeClient, _FakeMessage, _FakeResponse, _registry
from dourmouse.tests.test_webui import _echo_registry
from dourmouse.webui import run_server


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem" / "test.db")
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# learn_enabled env gate
# --------------------------------------------------------------------------- #

class TestLearnEnabled:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("TRUE", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("off", False),
            ("", False),
        ],
    )
    def test_values(self, value, expected):
        assert learn.learn_enabled(value) is expected

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        assert learn.learn_enabled() is False
        monkeypatch.setenv("DOURMOUSE_LEARN", "1")
        assert learn.learn_enabled() is True
        monkeypatch.delenv("DOURMOUSE_LEARN", raising=False)
        assert learn.learn_enabled() is True  # default ON


# --------------------------------------------------------------------------- #
# default store path + open_default_store
# --------------------------------------------------------------------------- #

class TestDefaultStore:
    def test_path_uses_memory_db_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "custom.db"))
        assert learn.default_store_path() == tmp_path / "custom.db"

    def test_path_uses_workspace_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DOURMOUSE_MEMORY_DB", raising=False)
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        assert learn.default_store_path() == tmp_path / "memory" / "atlas_memory.db"

    def test_open_default_store_respects_learn_gate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "x.db"))
        assert learn.open_default_store() is None  # gate off -> no store at all

    def test_open_default_store_returns_none_when_fts5_missing(self, monkeypatch, tmp_path):
        """Honest degradation (Rule 2.2): no FTS5 -> None, never a fake."""
        monkeypatch.setenv("DOURMOUSE_LEARN", "1")
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "nofs.db"))
        import sqlite3

        monkeypatch.setattr(
            MemoryStore, "_init_schema",
            lambda self: (_ for _ in ()).throw(
                sqlite3.OperationalError("no such module: fts5")
            ),
        )
        assert learn.open_default_store() is None

    def test_open_default_store_returns_real_store(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_LEARN", "1")
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "real.db"))
        s = learn.open_default_store()
        assert s is not None
        s.close()


# --------------------------------------------------------------------------- #
# recall_block — deterministic FTS5 recall
# --------------------------------------------------------------------------- #

class TestRecallBlock:
    def test_no_matches_returns_empty(self, store):
        store.remember("agent", "fact", "the argon engines of project nebula")
        assert learn.recall_block(store, "weather forecast") == ""

    def test_no_distinctive_terms_returns_empty(self, store):
        store.remember("agent", "fact", "anything at all")
        assert learn.recall_block(store, "the and of a to is") == ""

    def test_matches_return_formatted_block(self, store):
        store.remember("session:s1", "fact one", "project nebula uses argon engines")
        block = learn.recall_block(store, "tell me about project nebula")
        assert "REMEMBERED CONTEXT" in block
        assert "session:s1" in block
        assert "argon" in block

    def test_prompt_distillation_drops_stopwords(self):
        assert learn.distill_query("tell me about project nebula") == "project nebula"
        assert learn.distill_query("what is the weather like") == "weather"
        assert learn.distill_query("the and of") == ""

    def test_feedback_facts_are_recalled(self, store, tmp_path):
        """A rated turn is learnable: recall surfaces the operator's rating."""
        session_file = tmp_path / "s.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "turn": 1,
                    "user": "draft the quarterly report",
                    "final_text": "here is the draft",
                }
            )
            + "\n"
        )
        learn.record_feedback(store, session_file, "good")
        block = learn.recall_block(store, "quarterly report draft")
        assert "rated good" in block  # the fact's title (the rating signal)
        # The fact body is stored verbatim — verify at the store level
        # (the FTS5 snippet() brackets matched terms, so no phrase assert).
        body_hits = store.search("rated good")
        assert body_hits
        assert "RATED: good by operator" in body_hits[0]["snippet"].replace("[", "").replace("]", "")


# --------------------------------------------------------------------------- #
# ChatSession — auto-ingest + recall injection
# --------------------------------------------------------------------------- #

class TestChatLearning:
    def test_completed_turn_is_auto_ingested(self, tmp_path, store):
        """The moment a turn completes, it lands in the long-term store —
        the 'stores data' half of the loop."""
        client = FakeClient([_FakeResponse(_FakeMessage(content="Answer one."))])
        session = ChatSession(
            _registry(), client=client, session_file=tmp_path / "s1.jsonl", memory=store
        )
        session.ask("project nebula uses argon engines")
        assert store.count() >= 1
        hits = store.search("nebula")
        assert hits
        # snippet() brackets matched terms, so assert on the terms, not the
        # exact contiguous phrase.
        assert "nebula" in hits[0]["snippet"]
        assert "argon" in hits[0]["snippet"]

    def test_relevant_memory_is_recalled_into_next_prompt(self, tmp_path, store):
        """The 'learns from it' half: stored knowledge is injected into the
        system message of the NEXT relevant prompt."""
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="Stored.")),
                _FakeResponse(_FakeMessage(content="Recalled.")),
            ]
        )
        session = ChatSession(
            _registry(), client=client, session_file=tmp_path / "s2.jsonl", memory=store
        )
        session.ask("remember project nebula uses argon engines")
        session.ask("tell me about project nebula")

        sent = client.chat.completions.calls[1]["messages"]
        # v4.2: recall is injected as its OWN trailing system message so the
        # base prompt stays immutable (KV-cache stability) — search ALL
        # system messages, not just messages[0].
        system_msgs = [m.get("content", "") for m in sent if m.get("role") == "system"]
        assert any("REMEMBERED CONTEXT" in s for s in system_msgs)
        assert any("nebula" in s for s in system_msgs)
        assert any("argon" in s for s in system_msgs)
        assert sent[0]["content"] == system_message(_registry())

    def test_no_match_leaves_system_message_unchanged(self, tmp_path, store):
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="Stored.")),
                _FakeResponse(_FakeMessage(content="Unrelated.")),
            ]
        )
        session = ChatSession(
            _registry(), client=client, session_file=tmp_path / "s3.jsonl", memory=store
        )
        session.ask("remember project nebula uses argon engines")
        session.ask("check the weather forecast")

        sent = client.chat.completions.calls[1]["messages"]
        assert "REMEMBERED CONTEXT" not in sent[0]["content"]
        assert sent[0]["content"] == system_message(_registry())

    def test_no_memory_is_backward_compatible(self, tmp_path):
        """memory=None (the default) behaves exactly like v2.8: no recall, no
        ingest, system message is the plain base."""
        client = FakeClient([_FakeResponse(_FakeMessage(content="Fine."))])
        session = ChatSession(_registry(), client=client, session_file=tmp_path / "s4.jsonl")
        session.ask("anything")
        assert session.messages[0]["content"] == system_message(_registry())

    def test_learn_gate_disables_ingest_and_recall(self, tmp_path, store, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="Stored.")),
                _FakeResponse(_FakeMessage(content="Recalled.")),
            ]
        )
        session = ChatSession(
            _registry(), client=client, session_file=tmp_path / "s5.jsonl", memory=store
        )
        session.ask("remember project nebula uses argon engines")
        assert store.count() == 0  # nothing ingested
        session.ask("tell me about project nebula")
        sent = client.chat.completions.calls[1]["messages"]
        assert "REMEMBERED CONTEXT" not in sent[0]["content"]

    def test_failed_turn_is_not_ingested(self, tmp_path, store):
        """Never learn from a failed turn (empty answer) — honest, not noise."""

        class _RaisingCompletions:
            def create(self, **kwargs):
                raise RuntimeError("nvidia api down")

        class _RaisingChat:
            def __init__(self):
                self.completions = _RaisingCompletions()

        class _RaisingClient:
            def __init__(self):
                self.chat = _RaisingChat()

        session = ChatSession(
            _registry(), client=_RaisingClient(), session_file=tmp_path / "s6.jsonl", memory=store
        )
        with pytest.raises(RuntimeError, match="nvidia api down"):
            session.ask("do the thing")
        assert store.count() == 0

    def test_resume_keeps_learning_wired(self, tmp_path, store):
        """A resumed session recalls with the CURRENT roster base system."""
        client = FakeClient(
            [
                _FakeResponse(_FakeMessage(content="Stored.")),
                _FakeResponse(_FakeMessage(content="Recalled.")),
            ]
        )
        session_file = tmp_path / "s7.jsonl"
        session = ChatSession(_registry(), client=client, session_file=session_file, memory=store)
        session.ask("remember project nebula uses argon engines")

        resumed = ChatSession(_registry(), client=client, session_file=session_file, memory=store)
        client.chat.completions._responses = [_FakeResponse(_FakeMessage(content="R."))]
        resumed.ask("tell me about project nebula")
        # v4.2: recall is injected as its OWN trailing system message so the
        # base prompt stays immutable (KV-cache stability). The base itself
        # no longer carries the block — it is a separate message in history.
        assert resumed.messages[0]["content"] == system_message(_registry())
        # Exactly ONE recall block: stale blocks from earlier turns are
        # stripped at resume, and this ask injects the fresh one.
        recall_blocks = [
            m
            for m in resumed.messages
            if m.get("role") == "system" and "REMEMBERED CONTEXT" in m.get("content", "")
        ]
        assert len(recall_blocks) == 1
        assert "nebula" in recall_blocks[0]["content"]


# --------------------------------------------------------------------------- #
# record_feedback
# --------------------------------------------------------------------------- #

class TestRecordFeedback:
    def test_valid_ratings_store_a_fact(self, store, tmp_path):
        session_file = tmp_path / "s.jsonl"
        session_file.write_text(
            json.dumps({"turn": 3, "user": "u", "final_text": "a"}) + "\n"
        )
        msg = learn.record_feedback(store, session_file, "good")
        assert "MEMORY STORED" in msg
        assert store.count() == 1

    def test_invalid_rating_raises(self, store, tmp_path):
        with pytest.raises(ValueError, match="rating"):
            learn.record_feedback(store, tmp_path / "s.jsonl", "meh")

    def test_no_session_returns_honest_error(self, store, tmp_path):
        msg = learn.record_feedback(store, tmp_path / "missing.jsonl", "bad")
        assert msg.startswith("ERROR")


# --------------------------------------------------------------------------- #
# HTTP — /api/memory stats + /api/feedback
# --------------------------------------------------------------------------- #

class TestMemoryApi:
    def test_memory_stats_active_with_count(self, tmp_path):
        store = MemoryStore(tmp_path / "mem" / "http.db")
        store.remember("agent", "fact", "project nebula")
        srv = run_server(_echo_registry(), port=0, client=None, config=None, memory=store)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/api/memory")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 200
            assert data["active"] is True
            assert data["count"] == 1  # real count, not a stub
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
            store.close()

    def test_memory_stats_inactive_without_store(self, tmp_path):
        srv = run_server(_echo_registry(), port=0, client=None, config=None, memory=None)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/api/memory")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 200
            assert data["active"] is False
            assert data["count"] == 0
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_feedback_ok_stores_fact(self, tmp_path):
        store = MemoryStore(tmp_path / "mem" / "fb.db")
        srv = run_server(_echo_registry(), port=0, client=None, config=None, memory=store)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            # A real completed turn must exist to rate: run one through the
            # session with a fake client (no network, Rule 2.1).
            srv.session.client = FakeClient(
                [_FakeResponse(_FakeMessage(content="Here is the draft."))]
            )
            srv.session.ask("draft the quarterly report")

            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request(
                "POST", "/api/feedback",
                body=json.dumps({"rating": "good"}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 200
            assert data["ok"] is True
            # The turn auto-ingested (1) + the feedback fact (1).
            assert store.count() == 2
            fb_hits = store.search("rated good")
            assert fb_hits  # the feedback fact is genuinely learnable
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
            store.close()

    def test_feedback_without_store_is_honest_409(self, tmp_path):
        srv = run_server(_echo_registry(), port=0, client=None, config=None, memory=None)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request(
                "POST", "/api/feedback",
                body=json.dumps({"rating": "good"}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 409
            assert data["ok"] is False
            assert "memory disabled" in data["error"]
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_feedback_invalid_rating_400(self, tmp_path):
        store = MemoryStore(tmp_path / "mem" / "fb.db")
        srv = run_server(_echo_registry(), port=0, client=None, config=None, memory=store)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request(
                "POST", "/api/feedback",
                body=json.dumps({"rating": "meh"}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert resp.status == 400
            assert data["ok"] is False
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
            store.close()


# --------------------------------------------------------------------------- #
# UI wiring — the dashboard surfaces the learning loop
# --------------------------------------------------------------------------- #

class TestUiWiring:
    def _read(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / rel).read_text()

    def test_dashboard_shows_memory_line_and_polls_api(self):
        html = self._read("ui/index.html")
        assert "MEMORY:" in html
        assert "pollMemory" in html
        assert "/api/memory" in html

    def test_dashboard_ships_feedback_buttons(self):
        html = self._read("ui/index.html")
        assert "feedbackRow" in html
        assert "/api/feedback" in html
        assert "👍 GOOD" in html

    def test_learn_module_ships_recall_and_feedback(self):
        src = self._read("dourmouse/learn.py")
        assert "def recall_block" in src
        assert "def record_feedback" in src
        assert "def open_default_store" in src

    def test_chat_ships_memory_loop(self):
        src = self._read("dourmouse/chat.py")
        assert "recall_block" in src
        assert "ingest_session_file" in src

    def test_webui_ships_memory_routes(self):
        src = self._read("dourmouse/webui.py")
        assert "/api/memory" in src
        assert "/api/feedback" in src
        assert "record_feedback" in src
