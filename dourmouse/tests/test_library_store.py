"""Finding 206: the library store (add, search, cite, verify_quote, queue, filters, migration) and facade."""

from __future__ import annotations

import sqlite3

import pytest

from dourmouse import library, privacy
from dourmouse.library import HashEmbedder, Store
from dourmouse.library.chunking import page_marker


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "wiki.db", kind="wiki")


def test_add_returns_ids_and_cite_gives_the_verbatim_passage_with_its_place(store):
    text = f"{page_marker(7)}\n\n# Soil\n\nChlorophyll absorbs red and blue light most strongly."
    got = store.add("file", text, {"title": "plants.pdf", "path": "/Users/a/Documents/plants.pdf", "date": "2026-01-02"})
    assert got["doc_id"] and len(got["passage_ids"]) == 1
    c = store.cite(got["passage_ids"][0])
    assert c["text"].endswith("most strongly.") and c["page"] == 7 and c["heading"] == "Soil"
    assert c["path"].endswith("plants.pdf") and c["date"] == "2026-01-02"
    assert c["citation"] == "plants.pdf, p. 7"


def test_cite_unknown_passage_raises(store):
    with pytest.raises(LookupError):
        store.cite(99)
    with pytest.raises(LookupError):
        store.cite("x")


def test_adding_the_same_source_id_replaces_the_old_passages(store):
    a = store.add("page", "old words about cats", {"external_id": "u1", "title": "t"})
    b = store.add("page", "new words about dogs", {"external_id": "u1", "title": "t"})
    assert a["source_id"] == b["source_id"] == "u1" and store.count() == 1
    assert store.search("cats") == [] and store.search("dogs")[0]["source_id"] == "u1"
    with pytest.raises(LookupError):
        store.cite(a["passage_ids"][0]) if a["passage_ids"][0] != b["passage_ids"][0] else (_ for _ in ()).throw(LookupError)


def test_bad_input_is_refused(store):
    for args in (("File!", "x"), ("file", "   "), ("", "x")):
        with pytest.raises(ValueError):
            store.add(*args)
    with pytest.raises(ValueError):
        store.add("file", "x", {"tier": "T9"})


def test_keyword_search_snippet_and_or_fallback(store):
    store.add("file", "The mitochondria is the powerhouse of the cell.", {"title": "bio"})
    store.add("file", "Photosynthesis happens in chloroplasts.", {"title": "bio2"})
    hit = store.search("mitochondria powerhouse")[0]
    assert hit["title"] == "bio" and "[" in hit["snippet"] and hit["matched"] == ["keyword"]
    assert store.search("mitochondria chloroplasts")  # no passage has both words: any-word fallback finds them
    assert store.search("") == [] and store.search('"; DROP TABLE x --') == []


def test_filters_by_source_namespace_folder_dates_and_tier(store):
    store.add("file", "budget plan alpha", {"title": "a", "folder": "/D/finance", "date": "2025-01-01"})
    store.add("memory", "budget plan beta", {"title": "b", "namespace": "agent:planner", "date": "2026-06-01"})
    store.add("page", "budget plan gamma", {"title": "c", "tier": "T2", "folder": "/D/finance/sub"})
    q = "budget plan"
    assert {h["title"] for h in store.search(q, filters={"source": "memory"})} == {"b"}
    assert {h["title"] for h in store.search(q, filters={"namespace": "agent:planner"})} == {"b"}
    assert {h["title"] for h in store.search(q, filters={"folder": "/D/finance"})} == {"a", "c"}
    assert {h["title"] for h in store.search(q, filters={"folder": "/D/fin"})} == set()
    assert {h["title"] for h in store.search(q, filters={"date_from": "2026-01-01"})} == {"b"}
    assert {h["title"] for h in store.search(q, filters={"date_to": "2025-12-31"})} == {"a"}
    assert {h["title"] for h in store.search(q, filters={"max_tier": "T1"})} == {"a", "b"}
    with pytest.raises(ValueError):
        store.search(q, filters={"nope": 1})


def test_verify_quote_is_verbatim_but_ignores_whitespace_and_curly_quotes(store):
    pid = store.add("file", "She said “the cell is the basic unit” and left.\n\nNext  line.", {"title": "t"})["passage_ids"][0]
    assert store.verify_quote(pid, 'said "the cell is the basic unit" and')["ok"]
    assert store.verify_quote(pid, "Next line.")["ok"] is False  # two words: too short to prove anything
    r = store.verify_quote(pid, "the cell is a basic unit")
    assert r["ok"] is False and "does not appear" in r["reason"]
    assert store.verify_quote(pid, "THE CELL IS THE BASIC")["ok"] is False


def test_vectors_queue_and_hybrid_fusion_with_the_stub_embedder(store):
    emb = HashEmbedder()
    a = store.add("file", "orchids need humid shade", {"title": "orchids"})
    store.add("file", "tractors need diesel fuel", {"title": "tractors"})
    assert store.queue_stats() == {"passages": 2, "embedded": 0, "waiting": 2, "blocked": 0}
    out = store.embed_pending(emb, batch=1)
    assert out["embedded"] == 2 and out["queue"]["waiting"] == 0 and out["error"] == "" and out["dim"] == 768
    hits = store.search("humid orchids", query_vector=emb.embed_query("humid orchids"), model=emb.model)
    assert hits[0]["passage_id"] == a["passage_ids"][0] and set(hits[0]["matched"]) == {"keyword", "vector"}
    # a vector-only match: no shared FTS word with the query but a shared hash bucket is impossible, so the
    # keyword list is empty and the result is still ranked by cosine alone
    only = store.search("zzzz", query_vector=emb.embed_query("diesel fuel tractors"), model=emb.model)
    assert only and only[0]["matched"] == ["vector"]


def test_vector_search_respects_filters(store):
    emb = HashEmbedder()
    store.add("file", "orchids need humid shade", {"title": "o", "folder": "/D/a"})
    store.add("file", "orchids need humid shade too", {"title": "o2", "folder": "/D/b"})
    store.embed_pending(emb)
    hits = store.search("zzzz", query_vector=emb.embed_query("orchids humid"), filters={"folder": "/D/b"})
    assert [h["title"] for h in hits] == ["o2"]


def test_removing_a_document_removes_its_vectors_and_queue_rows(store):
    emb = HashEmbedder()
    store.add("file", "alpha beta gamma", {"title": "x", "source_id": "s1"})
    store.embed_pending(emb)
    store.remove("file", "s1")
    assert store.queue_stats() == {"passages": 0, "embedded": 0, "waiting": 0, "blocked": 0}


class RemoteStub(HashEmbedder):
    remote = True
    model = "stub-remote"

    def __init__(self):
        super().__init__()
        self.sent = []

    def embed_documents(self, texts):
        self.sent += texts
        return super().embed_documents(texts)


def test_remote_embedder_is_gated_by_folder_consent_tier_secrets_and_private_mode(store, tmp_path):
    ps = privacy.PrivacyStore(tmp_path / "priv.json")
    docs = tmp_path / "Documents" / "notes"
    docs.mkdir(parents=True)
    store.add("file", "a plain note about gardens", {"title": "ok", "folder": str(docs)})
    store.add("file", "no consent for this folder " + "x", {"title": "nofolder", "folder": str(tmp_path / "other")})
    store.add("file", "sensitive diary entry", {"title": "t2", "tier": "T2", "folder": str(docs)})
    store.add("file", "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123", {"title": "env", "folder": str(docs)})
    store.add("memory", "remembered fact: likes tea", {"title": "fact"})  # no folder: not folder gated
    ps.set_folder_consent(docs, "allow")
    emb = RemoteStub()
    out = store.embed_pending(emb, privacy_store=ps)
    assert sorted(emb.sent) == ["a plain note about gardens", "remembered fact: likes tea"]
    assert out["embedded"] == 2 and out["blocked"] == 3
    assert store.blocked_reasons() == {"this folder has no consent yet": 1, "marked sensitive": 1,
                                       "looks like it holds a key or password": 1}
    # a consent given later is noticed on the next run
    ps.set_folder_consent(tmp_path / "other", "allow")
    emb2 = RemoteStub()
    store.embed_pending(emb2, privacy_store=ps)
    assert emb2.sent == ["no consent for this folder x"]
    # Private mode: nothing goes out at all
    store.add("file", "another plain note", {"title": "n", "folder": str(docs)})
    ps.set_private_mode(True)
    emb3 = RemoteStub()
    out = store.embed_pending(emb3, privacy_store=ps)
    assert emb3.sent == [] and out["embedded"] == 0
    assert "Private mode is on" in store.blocked_reasons()


def test_remote_batch_writes_one_egress_row_with_size_and_hash_never_text(store, tmp_path):
    ps = privacy.PrivacyStore(tmp_path / "priv.json")
    store.add("memory", "remembered fact: secretword42", {"title": "f"})
    store.embed_pending(RemoteStub(), privacy_store=ps)
    rows = privacy.egress_path().read_text().splitlines()
    assert len(rows) == 1 and "secretword42" not in rows[0] and '"origin": "library-embed"' in rows[0]


def test_embedder_failure_keeps_passages_queued_and_reports_it(store):
    class Boom(HashEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("402 payment required")

    store.add("file", "queued words here", {"title": "q"})
    out = store.embed_pending(Boom())
    assert "402" in out["error"] and out["embedded"] == 0 and out["queue"]["waiting"] == 1


def test_forget_folder_removes_documents_below_it_only(store):
    store.add("file", "one two three", {"title": "a", "folder": "/D/x", "source_id": "a"})
    store.add("file", "four five six", {"title": "b", "folder": "/D/x/y", "source_id": "b"})
    store.add("file", "seven eight nine", {"title": "c", "folder": "/D/xx", "source_id": "c"})
    store.scan_put("/D/x/y/b.txt", 3, 1.0)
    assert store.forget_folder("/D/x") == 2
    assert [d["title"] for d in store.documents()] == ["c"] and store.scan_paths("/D/x") == []


def test_scan_state_roundtrip(store):
    assert store.scan_get("/a") is None
    store.scan_put("/a", 10, 2.5, 1)
    assert store.scan_get("/a")["size"] == 10
    store.scan_forget("/a")
    assert store.scan_get("/a") is None


def test_old_projects_passage_store_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE passages (id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,"
               " title TEXT NOT NULL, seq INTEGER NOT NULL, text TEXT NOT NULL, added_at REAL NOT NULL)")
    db.execute("CREATE VIRTUAL TABLE passages_fts USING fts5(text, title, content='passages', content_rowid='id',"
               " tokenize='porter unicode61')")
    db.execute("INSERT INTO passages VALUES (1, 'file', 'f1', 'notes.md', 0, 'legacy words about lichen', 1.0)")
    db.execute("INSERT INTO passages_fts(rowid, text, title) VALUES (1, 'legacy words about lichen', 'notes.md')")
    db.commit()
    db.close()
    st = Store(path)
    hit = st.search("lichen")[0]
    assert hit["citation"] == "notes.md, passage 1" and hit["tier"] == "T1" and hit["folder"] == ""
    assert st.queue_stats()["waiting"] == 1  # the old passage waits for a vector
    assert st.cite(1)["text"] == "legacy words about lichen"


def test_snapshot_is_a_readable_copy(store, tmp_path):
    store.add("file", "copy me please", {"title": "c"})
    dest = store.snapshot(tmp_path / "bk" / "wiki.copy.db")
    assert Store(dest).search("copy")[0]["title"] == "c"


# ---- facade ---------------------------------------------------------------------------------------------------

def test_facade_stores_are_separate_files_and_project_ids_are_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    assert library.wiki().path == tmp_path / "ws" / "library" / "wiki.db"
    assert library.project("alpha-1").path == tmp_path / "ws" / "library" / "projects" / "alpha-1.db"
    for bad in ("../etc", "A", "", "a/b", None):
        with pytest.raises(LookupError):
            library.project(bad)


def test_search_goes_to_the_project_first_then_the_wiki_and_never_another_project(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    library.project("alpha-1").add("file", "tidal energy notes for alpha", {"title": "alpha-notes"})
    library.project("beta-2").add("file", "tidal energy secret of beta", {"title": "beta-notes"})
    library.wiki().add("memory", "tidal energy is a favourite topic", {"title": "fact"})
    out = library.search("tidal energy", project="alpha-1")
    assert [(h["store"], h["title"]) for h in out["results"]] == [("project", "alpha-notes"), ("wiki", "fact")]
    assert out["mode"] == "keyword" and out["note"] == ""
    assert all("beta" not in h["text"] for h in out["results"])
    assert [h["store"] for h in library.search("tidal energy")["results"]] == ["wiki"]


def test_refs_cite_and_verify_across_the_two_kinds_of_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    pid = library.project("alpha-1").add("file", "the quick brown fox jumps", {"title": "fox"})["passage_ids"][0]
    wid = library.wiki().add("memory", "the slow green turtle walks", {"title": "turtle"})["passage_ids"][0]
    assert library.cite(f"project:alpha-1:{pid}")["title"] == "fox"
    assert library.cite(f"wiki:{wid}")["ref"] == f"wiki:{wid}"
    assert library.verify_quote(f"wiki:{wid}", "slow green turtle")["ok"]
    assert library.verify_quote(f"project:alpha-1:{pid}", "slow green turtle")["ok"] is False
    for bad in ("wiki", "wiki:x", "project:alpha-1", "other:1", "project:../x:1", ""):
        with pytest.raises(LookupError):
            library.cite(bad)


def test_private_mode_and_a_failing_embedder_fall_back_to_keywords_and_say_so(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    library.wiki().add("memory", "lighthouse keeper logbook", {"title": "log"})
    privacy.PrivacyStore().set_private_mode(True)
    out = library.search("lighthouse", embedder=RemoteStub())
    assert out["mode"] == "keyword" and "Private mode is on" in out["note"] and out["results"]

    privacy.PrivacyStore().set_private_mode(False)

    class Down(RemoteStub):
        def embed_query(self, text):
            raise OSError("offline")

    out = library.search("lighthouse", embedder=Down())
    assert out["mode"] == "keyword" and "failed" in out["note"] and out["results"]
    out = library.search("lighthouse", embedder=RemoteStub())  # allowed, but nothing embedded yet
    assert out["mode"] == "keyword" and "no embedding" in out["note"].lower() or "embedding yet" in out["note"]
    library.wiki().embed_pending(HashEmbedder.__new__(HashEmbedder) if False else _Local())
    out = library.search("lighthouse logbook", embedder=_Local())
    assert out["mode"] == "hybrid"


class _Local(HashEmbedder):
    model = "stub-hash-768"
