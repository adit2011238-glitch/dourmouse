"""Finding #095 (R1 + R2): the research object graph, its versioning, and the
migration off the one-JSON-blob store."""

from __future__ import annotations

import sqlite3

import pytest

from dourmouse.research_graph.model import OBJECT_TYPES, RELATIONS, GraphError, ImmutableObject
from dourmouse.research_graph.store import GraphStore
from dourmouse.research_graph.sync import _h, claim_fingerprint, ids_for, migrate_legacy, sync_record
from dourmouse.research_pipeline.core import Claim, Contradiction, ResearchRecord
from dourmouse.research_pipeline.store import ResearchStore


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


@pytest.fixture
def store(tmp_path):
    return GraphStore(tmp_path / "research.db", clock=_Clock())


class TestModel:
    def test_the_twenty_one_objects_of_spec_item_35(self):
        assert set(OBJECT_TYPES) == {
            "project", "research_question", "research_objective", "hypothesis", "claim", "source",
            "document", "passage", "evidence", "experiment", "experiment_run", "dataset", "metric",
            "result", "contradiction", "agent", "task", "message", "decision", "artifact", "event",
        }

    def test_the_evidence_chain_is_immutable(self):
        for t in ("source", "document", "passage", "evidence"):
            assert OBJECT_TYPES[t].mutability.value == "immutable"
        for t in ("claim", "hypothesis", "task"):
            assert OBJECT_TYPES[t].mutability.value == "versioned"

    def test_an_undeclared_field_is_refused_not_silently_stored(self, store):
        with pytest.raises(GraphError, match="no field"):
            store.put("claim", "c1", {"text": "x", "confidnce": 0.9}, created_by="t")

    def test_a_missing_required_field_is_refused(self, store):
        with pytest.raises(GraphError, match="missing required"):
            store.put("hypothesis", "h1", {"status": "open"}, created_by="t")

    def test_a_document_needs_a_raw_or_a_legacy_hash(self, store):
        with pytest.raises(GraphError, match="at least one of"):
            store.put("document", "d1", {"final_url": "https://x"}, created_by="t")
        store.put("document", "d2", {"legacy_text_hash": "abc"}, created_by="t")


class TestStore:
    def test_put_is_idempotent_and_conflicts_are_refused(self, store):
        a = store.put("source", "s1", {"url": "https://a"}, created_by="t")
        assert store.put("source", "s1", {"url": "https://a"}, created_by="t") == a
        with pytest.raises(GraphError, match="immutable"):
            store.put("source", "s1", {"url": "https://b"}, created_by="t")

    def test_a_revision_is_a_new_version_and_the_old_one_stays(self, store):
        store.put("claim", "c1", {"text": "water boils at 100C", "status": "ACTIVE"}, created_by="t")
        v2 = store.revise("claim", "c1", {"status": "REJECTED"}, created_by="critic")
        assert v2.version == 2 and v2.body["status"] == "REJECTED"
        v1 = store.get("claim", "c1", version=1)
        assert v1.body["status"] == "ACTIVE" and v1.superseded_at is not None
        assert [v.version for v in store.history("claim", "c1")] == [1, 2]

    def test_what_did_we_know_when(self, store):
        store.put("hypothesis", "h1", {"statement": "A causes B"}, created_by="t")  # t=101
        store.revise("hypothesis", "h1", {"statement": "A correlates with B"}, created_by="t")  # t=102
        assert store.as_of("hypothesis", "h1", 101.5).body["statement"] == "A causes B"
        assert store.as_of("hypothesis", "h1", 200).body["statement"] == "A correlates with B"
        assert store.as_of("hypothesis", "h1", 50) is None

    def test_immutable_objects_refuse_revision(self, store):
        store.put("passage", "p1", {"text": "the original words"}, created_by="t")
        with pytest.raises(ImmutableObject):
            store.revise("passage", "p1", {"text": "edited words"}, created_by="t")

    def test_a_re_put_after_a_revision_is_still_idempotent(self, store):
        store.put("task", "t1", {"title": "x", "stage": "PLANNED"}, created_by="t")
        store.revise("task", "t1", {"stage": "SYNTHESIZED"}, created_by="t")
        assert store.put("task", "t1", {"title": "x", "stage": "PLANNED"}, created_by="t").version == 2

    def test_find_matches_current_versions_only(self, store):
        store.put("claim", "c1", {"text": "x", "status": "ACTIVE"}, created_by="t")
        store.put("claim", "c2", {"text": "y", "status": "ACTIVE"}, created_by="t")
        store.revise("claim", "c2", {"status": "REJECTED"}, created_by="t")
        assert [o.id for o in store.find("claim", status="ACTIVE")] == ["c1"]
        with pytest.raises(GraphError):
            store.find("claim", nonsense=1)

    def test_edges_are_typed_and_need_both_ends(self, store):
        store.put("source", "s1", {"url": "https://a"}, created_by="t")
        with pytest.raises(KeyError):
            store.link(("document", "missing"), "derived_from", ("source", "s1"), created_by="t")
        store.put("document", "d1", {"raw_sha256": "f" * 64}, created_by="t")
        with pytest.raises(GraphError, match="unknown relation"):
            store.link(("document", "d1"), "vaguely_related", ("source", "s1"), created_by="t")
        store.link(("document", "d1"), "derived_from", ("source", "s1"), created_by="t")
        store.link(("document", "d1"), "derived_from", ("source", "s1"), created_by="t")  # idempotent
        assert len(store.edges(src=("document", "d1"))) == 1

    def test_the_specs_own_worked_example_is_a_query(self, store):
        """Spec: Hypothesis H1 supported by E1 and E4, contradicted by E9,
        tested by Experiment X3, revised by Decision D7."""
        store.put("hypothesis", "H1", {"statement": "caching halves latency"}, created_by="t")
        for e in ("E1", "E4", "E9"):
            store.put("evidence", e, {"stance": "contradicts" if e == "E9" else "supports"}, created_by="t")
        store.put("experiment", "X3", {"protocol": "A/B latency test"}, created_by="t")
        store.put("decision", "D7", {"summary": "narrow to read-heavy paths"}, created_by="t")
        h = ("hypothesis", "H1")
        store.link(h, "supported_by", ("evidence", "E1"), created_by="t")
        store.link(h, "supported_by", ("evidence", "E4"), created_by="t")
        store.link(h, "contradicted_by", ("evidence", "E9"), created_by="t")
        store.link(h, "tested_by", ("experiment", "X3"), created_by="t")
        store.link(h, "revised_by", ("decision", "D7"), created_by="t")
        assert [o.id for o in store.related(h, "supported_by")] == ["E1", "E4"]
        assert [o.id for o in store.related(h, "contradicted_by")] == ["E9"]
        assert [o.id for o in store.related(h, "tested_by")] == ["X3"]
        assert [o.id for o in store.related(h, "revised_by")] == ["D7"]

    def test_relation_vocabulary_covers_the_backward_edge(self):
        assert {"spawned", "contradicted_by", "revised_by"} <= RELATIONS


def _legacy_record() -> ResearchRecord:
    r = ResearchRecord(question="Is MCP transport-agnostic?")
    r.set_plan(["What transports does MCP define?", "Can it run over HTTP?"])
    r.add_sources(["https://spec.example/mcp", "https://blog.example/mcp-http"])
    old = Claim(  # made before #089: no final_url, hash of stripped text
        claim="MCP defines stdio and HTTP transports.", source_id="aaaa1111", url="https://spec.example/mcp",
        document_hash="1" * 64, location="Transports, paragraph 1",
        passage="MCP defines two standard transports: stdio and Streamable HTTP.",
        retrieved_at=1_790_000_000.0, agent="research_info", sub_question="What transports does MCP define?",
    )
    new = Claim(  # made after #089: raw-bytes hash and final URL
        claim="MCP runs over HTTP with SSE.", source_id="bbbb2222", url="https://blog.example/mcp-http",
        document_hash="2" * 64, location="Setup, paragraph 2", passage="It streams over HTTP using server-sent events.",
        retrieved_at=1_790_000_100.0, agent="research_info", sub_question="Can it run over HTTP?",
        final_url="https://blog.example/posts/mcp-http",
    )
    r.add_claim(old)
    r.add_claim(new)
    r.add_contradiction(Contradiction(claim_fingerprint(old), claim_fingerprint(new), "Can it run over HTTP?", "one says SSE only"))
    r.set_synthesis("MCP is transport-agnostic in practice: stdio locally, HTTP remotely.")
    return r


class TestMigration:
    def _legacy_db(self, tmp_path):
        path = tmp_path / "research.db"
        ResearchStore(path).save(_legacy_record(), now=5.0)  # the REAL current store writes the blob
        return path

    def test_every_claim_field_survives_the_migration(self, tmp_path):
        path = self._legacy_db(tmp_path)
        assert migrate_legacy(path) == {"records": 1}
        g = GraphStore(path)
        record = _legacy_record()
        for c in record.claims:
            obj = g.get("claim", "clm-" + _h(claim_fingerprint(c)))
            assert obj.body["text"] == c.claim
            assert obj.body["status"] == c.status
            assert obj.body["agent"] == c.agent
            assert obj.body["retrieved_at"] == c.retrieved_at
            assert obj.body["location"] == c.location
            assert obj.body["legacy_source_id"] == c.source_id
            assert obj.body["sub_question"] == c.sub_question
            (ev,) = g.related(obj.ref, "supported_by")
            (pas,) = g.related(ev.ref, "extracted_from")
            assert pas.body["text"] == c.passage
            (doc,) = g.related(pas.ref, "part_of")
            urls = [s.body["url"] for s in g.related(doc.ref, "derived_from")]
            if c.final_url:
                assert doc.body == {"raw_sha256": c.document_hash}
                assert urls == [c.url, c.final_url]
            else:
                assert doc.body == {"legacy_text_hash": c.document_hash}
                assert urls == [c.url]

    def test_plan_contradiction_synthesis_and_stage_survive(self, tmp_path):
        path = self._legacy_db(tmp_path)
        migrate_legacy(path)
        g = GraphStore(path)
        ids = ids_for("Is MCP transport-agnostic?")
        subs = [o.body["text"] for o in g.related(("research_question", ids["question"]), "decomposes_into")]
        assert subs == ["What transports does MCP define?", "Can it run over HTTP?"]
        (con,) = g.find("contradiction")
        assert con.body["note"] == "one says SSE only"
        assert len(g.related(con.ref, "about")) == 2
        (res,) = g.find("result")
        assert res.body["summary"].startswith("MCP is transport-agnostic")
        assert g.get("task", ids["task"]).body["stage"] == "SYNTHESIZED"
        assert g.count("source") == 3  # two discovered sources + the redirect's final URL

    def test_migration_is_idempotent(self, tmp_path):
        path = self._legacy_db(tmp_path)
        migrate_legacy(path)
        g = GraphStore(path)
        before = {t: g.count(t) for t in OBJECT_TYPES}
        edges_before = len(g.edges())
        migrate_legacy(path)
        assert {t: g.count(t) for t in OBJECT_TYPES} == before
        assert len(g.edges()) == edges_before
        assert all(len(g.history("claim", o.id)) == 1 for o in g.find("claim"))

    def test_the_legacy_table_is_left_exactly_as_it_was(self, tmp_path):
        path = self._legacy_db(tmp_path)
        conn = sqlite3.connect(path)
        before = conn.execute("SELECT question, body, stage, updated_at FROM research_records").fetchall()
        conn.close()
        migrate_legacy(path)
        conn = sqlite3.connect(path)
        after = conn.execute("SELECT question, body, stage, updated_at FROM research_records").fetchall()
        conn.close()
        assert after == before

    def test_a_later_rejection_is_a_new_claim_version(self, tmp_path):
        g = GraphStore(tmp_path / "g.db")
        record = _legacy_record()
        sync_record(g, record)
        record.reject_claim(0, "superseded")
        sync_record(g, record)
        clm = "clm-" + _h(claim_fingerprint(record.claims[0]))
        hist = g.history("claim", clm)
        assert [v.body["status"] for v in hist] == ["ACTIVE", "REJECTED"]

    def test_the_same_bytes_reached_through_two_urls_are_one_document(self, tmp_path):
        g = GraphStore(tmp_path / "g.db")
        r = ResearchRecord(question="q")
        r.set_plan(["s"])
        r.add_sources(["https://a.example/x", "https://mirror.example/x"])
        for url in ("https://a.example/x", "https://mirror.example/x"):
            r.add_claim(Claim(claim=f"claim via {url}", source_id=url[-12:], url=url, document_hash="9" * 64,
                              location="p1", passage="same words", retrieved_at=1.0, agent="a",
                              sub_question="s", final_url=url))
        sync_record(g, r)
        assert g.count("document") == 1
        (doc,) = g.find("document")
        assert sorted(s.body["url"] for s in g.related(doc.ref, "derived_from")) == [
            "https://a.example/x", "https://mirror.example/x"]

    def test_an_empty_or_missing_legacy_table_migrates_nothing(self, tmp_path):
        assert migrate_legacy(tmp_path / "fresh.db") == {"records": 0}


def test_a_sync_that_fails_part_way_leaves_nothing_behind(tmp_path, monkeypatch):
    g = GraphStore(tmp_path / "g.db")
    calls = {"n": 0}
    real_link = g.link

    def failing_link(*a, **k):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("disk full")
        return real_link(*a, **k)

    monkeypatch.setattr(g, "link", failing_link)
    with pytest.raises(RuntimeError, match="disk full"):
        sync_record(g, _legacy_record())
    assert all(g.count(t) == 0 for t in OBJECT_TYPES)
    assert g.edges() == []


class TestResearchStoreKeepsTheGraphInStep:
    def test_existing_records_are_migrated_once_when_the_store_opens(self, tmp_path):
        path = tmp_path / "research.db"
        # A database written before the graph existed: legacy table only.
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE research_records (question TEXT PRIMARY KEY, body TEXT NOT NULL, "
                     "stage TEXT NOT NULL, updated_at REAL NOT NULL)")
        import json

        from dourmouse.research_pipeline.store import _record_to_dict
        rec = _legacy_record()
        conn.execute("INSERT INTO research_records VALUES (?, ?, ?, ?)",
                     (rec.question, json.dumps(_record_to_dict(rec)), rec.stage.name, 1.0))
        conn.commit()
        conn.close()
        store = ResearchStore(path)
        assert store.graph.count("claim") == 2
        assert store.graph.meta_get("legacy_migrated_at") is not None

    def test_every_save_updates_the_graph(self, tmp_path):
        store = ResearchStore(tmp_path / "research.db")
        rec = ResearchRecord(question="new question")
        rec.set_plan(["sub one"])
        store.save(rec, now=1.0)
        ids = ids_for("new question")
        assert store.graph.get("task", ids["task"]).body["stage"] == "PLANNED"
        rec.add_sources(["https://x.example"])
        store.save(rec, now=2.0)
        assert [v.body["stage"] for v in store.graph.history("task", ids["task"])] == ["PLANNED", "SOURCES_DISCOVERED"]
