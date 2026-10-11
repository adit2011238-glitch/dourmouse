"""Fix agent FD1: P4-37, P4-38, P4-39, P4-40 (memory_store) and P3-36 (memory_embed)."""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from dourmouse import memory_embed
from dourmouse.memory_store import (
    LocalFallbackMemoryStore,
    MemoryStore,
    RemoteMemoryStore,
    RemoteMemoryStoreUnavailable,
    _fts_query,
)


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    yield s
    s.close()


# P4-37 ---------------------------------------------------------------------- #

def test_remember_of_unchanged_content_does_not_rewrite_or_drop_the_embedding(store):
    store.remember("session:s", "turn 1", "USER: a\nANSWER: b")
    fid = store.get("session:s", "turn 1")["id"]
    store.save_embedding(fid, "m", [1.0, 0.0])
    before = store.get("session:s", "turn 1")["updated_at"]
    out = store.remember("session:s", "turn 1", "USER: a\nANSWER: b")
    assert out.startswith("MEMORY STORED")
    assert store.get_embeddings() == {fid: [1.0, 0.0]}, "an unchanged fact keeps its cached vector"
    assert store.get("session:s", "turn 1")["updated_at"] == before


def test_changed_content_still_updates_and_drops_the_embedding(store):
    store.remember("s", "t", "one")
    fid = store.get("s", "t")["id"]
    store.save_embedding(fid, "m", [1.0])
    store.remember("s", "t", "two")
    assert store.get("s", "t")["body"] == "two"
    assert store.get_embeddings() == {}


def test_ingest_session_file_only_writes_new_or_changed_turns(store, tmp_path):
    p = tmp_path / "sess.jsonl"
    p.write_text("\n".join(json.dumps({"turn": i, "user": f"u{i}", "final_text": f"a{i}"}) for i in range(1, 4)))
    assert store.ingest_session_file(p) == 3
    for fact in store.all_facts():
        store.save_embedding(fact["id"], "m", [1.0])
    with p.open("a") as fh:
        fh.write("\n" + json.dumps({"turn": 4, "user": "u4", "final_text": "a4"}))
    writes = []
    real = store.remember
    store.remember = lambda *a, **k: (writes.append(a[1]), real(*a, **k))[1]
    store.ingest_session_file(p)
    assert writes == ["turn 4"], writes
    assert len(store.get_embeddings()) == 3, "the old turns keep their vectors"


# P4-38 ---------------------------------------------------------------------- #

def test_fts_query_keeps_unicode_words():
    assert _fts_query("日本語") != ""
    assert '"café"' in _fts_query("café au lait")


def test_accented_and_non_latin_notes_are_found(store):
    store.remember("n", "cafe note", "we met at the café on Rue Cler")
    store.remember("n", "jp", "これは 日本語 のメモです")
    assert store.search("café")
    assert store.search("日本語")


# P4-39 ---------------------------------------------------------------------- #

class _Handler(http.server.BaseHTTPRequestHandler):
    mode = "stall"

    def do_GET(self):  # noqa: N802
        m = type(self).mode
        if m == "stall":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"{")
            self.wfile.flush()
            import time
            time.sleep(1.5)
        elif m == "html":
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif m == "401":
            body = json.dumps({"error": "unauthorized"}).encode()
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif m == "400ok":
            body = json.dumps({"ok": False, "error": "bad"}).encode()
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("mode", ["stall", "html", "401"])
def test_remote_failures_become_unavailable(server, mode):
    _Handler.mode = mode
    r = RemoteMemoryStore(server, timeout=0.5)
    with pytest.raises(RemoteMemoryStoreUnavailable):
        r.count()


def test_remote_validation_error_body_with_ok_key_is_still_returned(server):
    _Handler.mode = "400ok"
    r = RemoteMemoryStore(server, timeout=0.5)
    assert r._get("/x") == {"ok": False, "error": "bad"}


def test_fallback_store_falls_back_on_a_stalled_remote(server, tmp_path):
    _Handler.mode = "stall"
    local = MemoryStore(tmp_path / "l.db")
    fb = LocalFallbackMemoryStore(RemoteMemoryStore(server, timeout=0.5), lambda: local)
    local.remember("s", "t", "hello fallback")
    assert fb.count() == 1
    assert fb.last_used_local_fallback is True
    local.close()


# P4-40 ---------------------------------------------------------------------- #

def test_fallback_store_has_every_public_method_of_the_local_store():
    def public(cls):
        return {n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n, None))}

    assert public(MemoryStore) - public(LocalFallbackMemoryStore) == set()


def test_fallback_store_ingests_deletes_and_caches_embeddings_locally(tmp_path):
    local = MemoryStore(tmp_path / "l.db")
    fb = LocalFallbackMemoryStore(RemoteMemoryStore("http://127.0.0.1:1", timeout=0.2), lambda: local)
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"turn": 1, "user": "hi", "final_text": "yo"}))
    assert fb.ingest_session_file(p) == 1
    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "n.md").write_text("a note")
    assert fb.ingest_vault(vault) == 1
    fid = local.get("session:s", "turn 1")["id"]
    fb.save_embedding(fid, "m", [1.0])
    assert fb.get_embeddings() == {fid: [1.0]}
    assert fb.delete("session:s", "turn 1") is True
    local.close()


# P3-36 ---------------------------------------------------------------------- #

def test_semantic_search_on_a_remote_store_falls_back_to_fts(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_EMBED", "1")

    class R:
        def all_facts(self):
            raise RemoteMemoryStoreUnavailable("remote")

        def search(self, q, limit=10, source=None):
            return [{"source": "s", "title": "t", "snippet": "x", "score": 1.0}]

    out = memory_embed.semantic_search(R(), "risk parameters")
    assert out["method"] == "fts5" and out["hits"]


def test_each_vector_is_saved_as_produced_so_a_failure_keeps_the_progress(store, monkeypatch):
    for i in range(4):
        store.remember("s", f"t{i}", f"body {i}")
    calls = {"n": 0}

    def fake(texts, timeout=10.0):
        calls["n"] += 1
        if calls["n"] == 3:
            return None
        return [[1.0, float(calls["n"])] for _ in texts]

    monkeypatch.setattr(memory_embed, "embed_texts", fake)
    assert memory_embed.ensure_embeddings(store, store.all_facts()) is None
    assert len(store.get_embeddings()) == 2, "vectors computed before the failure are kept"
    calls["n"] = 100
    assert len(memory_embed.ensure_embeddings(store, store.all_facts())) == 4
    # the second pass only embeds what is missing
    assert calls["n"] == 102


def test_cached_vectors_of_another_model_are_not_used(store, monkeypatch):
    store.remember("s", "t", "body")
    fid = store.get("s", "t")["id"]
    store.save_embedding(fid, "old-model", [1.0, 0.0, 0.0])
    monkeypatch.setenv("DOURMOUSE_EMBED_MODEL", "new-model")
    monkeypatch.setattr(memory_embed, "embed_texts", lambda texts, timeout=10.0: [[0.5, 0.5] for _ in texts])
    vecs = memory_embed.ensure_embeddings(store, store.all_facts())
    assert vecs[fid] == [0.5, 0.5]
    assert store.get_embeddings("new-model") == {fid: [0.5, 0.5]}


def test_non_numeric_embedding_element_does_not_raise(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"embedding": [1.0, "x", None]}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: Resp())
    assert memory_embed.embed_texts(["hello"]) is None
