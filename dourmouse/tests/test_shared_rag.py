"""shared_rag.py tests — synthetic SQLite fixtures and a fake ``faiss``
module standing in for the network/native-library boundary, matching
test_global_memory.py's own urllib-mocking discipline: no real backend and
NO real desktop vault needed to exercise this (this environment genuinely
has no access to the real hybrid_vault.db / vector.index — a synthetic
stand-in exercises the same defensive schema-probing code paths without
ever assuming what that real file looks like).

One test (missing faiss dependency) deliberately does NOT mock anything —
faiss-cpu is genuinely not installed in this dev venv, confirmed directly,
so that path is exercised for real.
"""

from __future__ import annotations

import json
import sqlite3
import sys

import numpy as np
import pytest

from dourmouse import shared_rag
from dourmouse.global_memory import GlobalMemory
from dourmouse.shared_rag import (
    ExternalCorpusError,
    MergedResult,
    format_merged_result,
    merged_search,
    probe_vault_schema,
    query_spatial_vault,
    spatial_vault_configured,
)


def _make_db(path, *, table="chunks", id_col="id", text_col="text", rows=None, extra_tables=()):
    conn = sqlite3.connect(str(path))
    conn.execute(f"CREATE TABLE {table} ({id_col} INTEGER PRIMARY KEY, {text_col} TEXT)")
    for rid, text in rows or []:
        conn.execute(f"INSERT INTO {table} ({id_col}, {text_col}) VALUES (?, ?)", (rid, text))
    for t in extra_tables:
        conn.execute(f"CREATE TABLE {t} (x INTEGER)")
    conn.commit()
    conn.close()


class _FakeFaissIndex:
    """Stands in for a real faiss.Index: reports a dimension and hands
    back a fixed, pre-decided ranked hit list on .search() — exactly the
    surface query_spatial_vault actually touches (.d, .metric_type,
    .ntotal, .search()).

    v13.6: ``ordered_hits`` entries are real 0-based FAISS POSITIONS in
    the second slot, NOT SQL ids — matching what a real plain
    IndexFlatL2/IndexFlatIP genuinely returns (live-confirmed against
    the real desktop vault: it carries no id map at all). Every test
    below used to pass literal SQL ids there, which happened to equal
    the position for those specific fixtures and so never actually
    exercised (or could have caught) the real position!=id bug this
    session found live — see TestPositionToIdMapping below for a
    fixture where they genuinely differ. ``ntotal`` defaults to a
    generous sentinel for tests that don't care about the position/id
    boundary at all.
    """

    def __init__(self, d, metric_type, ordered_hits, ntotal=10_000):
        self.d = d
        self.metric_type = metric_type
        self.ntotal = ntotal
        self._ordered_hits = ordered_hits  # list[(score, faiss_position)]

    def search(self, q, k):
        scores = [s for s, _ in self._ordered_hits[:k]]
        ids = [i for _, i in self._ordered_hits[:k]]
        while len(ids) < k:
            ids.append(-1)
            scores.append(0.0)
        return np.array([scores], dtype="float32"), np.array([ids], dtype="int64")


class _FakeFaissModule:
    METRIC_L2 = 1
    METRIC_INNER_PRODUCT = 0

    def __init__(self, index):
        self._index = index

    def read_index(self, path):
        return self._index


class TestSpatialVaultConfigured:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_PATH", raising=False)
        assert spatial_vault_configured() is False

    def test_on_when_path_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(tmp_path / "x.db"))
        assert spatial_vault_configured() is True


class TestProbeVaultSchema:
    def test_missing_file_is_not_configured(self, tmp_path):
        with pytest.raises(ExternalCorpusError) as exc:
            probe_vault_schema(tmp_path / "nope.db")
        assert exc.value.kind == "NOT_CONFIGURED"

    def test_single_table_standard_columns_resolved(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_TABLE", raising=False)
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_ID_COL", raising=False)
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_TEXT_COL", raising=False)
        db = tmp_path / "vault.db"
        _make_db(db, table="chunks", id_col="id", text_col="text", rows=[(1, "hello")])
        schema = probe_vault_schema(db)
        assert schema.table == "chunks"
        assert schema.id_col == "id"
        assert schema.text_col == "text"
        assert schema.row_count == 1

    def test_multiple_tables_no_override_is_schema_mismatch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_TABLE", raising=False)
        db = tmp_path / "vault.db"
        _make_db(db, table="chunks", rows=[(1, "hi")], extra_tables=("other",))
        with pytest.raises(ExternalCorpusError) as exc:
            probe_vault_schema(db)
        assert exc.value.kind == "SCHEMA_MISMATCH"
        assert "chunks" in str(exc.value) and "other" in str(exc.value)

    def test_multiple_tables_with_override_resolved(self, tmp_path, monkeypatch):
        db = tmp_path / "vault.db"
        _make_db(db, table="chunks", rows=[(1, "hi")], extra_tables=("other",))
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_TABLE", "chunks")
        schema = probe_vault_schema(db)
        assert schema.table == "chunks"

    def test_override_naming_nonexistent_table_is_schema_mismatch(self, tmp_path, monkeypatch):
        db = tmp_path / "vault.db"
        _make_db(db, table="chunks", rows=[(1, "hi")])
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_TABLE", "nonexistent")
        with pytest.raises(ExternalCorpusError) as exc:
            probe_vault_schema(db)
        assert exc.value.kind == "SCHEMA_MISMATCH"
        assert "nonexistent" in str(exc.value)

    def test_nonstandard_columns_no_override_is_schema_mismatch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_ID_COL", raising=False)
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_TEXT_COL", raising=False)
        db = tmp_path / "vault.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE weird_rows (pk INTEGER PRIMARY KEY, payload TEXT)")
        conn.execute("INSERT INTO weird_rows (pk, payload) VALUES (1, 'hi')")
        conn.commit()
        conn.close()
        with pytest.raises(ExternalCorpusError) as exc:
            probe_vault_schema(db)
        assert exc.value.kind == "SCHEMA_MISMATCH"
        assert "pk" in str(exc.value) and "payload" in str(exc.value)

    def test_nonstandard_columns_with_override_resolved(self, tmp_path, monkeypatch):
        db = tmp_path / "vault.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE weird_rows (pk INTEGER PRIMARY KEY, payload TEXT)")
        conn.execute("INSERT INTO weird_rows (pk, payload) VALUES (1, 'hi')")
        conn.commit()
        conn.close()
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_ID_COL", "pk")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_TEXT_COL", "payload")
        schema = probe_vault_schema(db)
        assert schema.id_col == "pk"
        assert schema.text_col == "payload"

    def test_no_user_tables_is_schema_mismatch(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()
        with pytest.raises(ExternalCorpusError) as exc:
            probe_vault_schema(db)
        assert exc.value.kind == "SCHEMA_MISMATCH"


class TestQuerySpatialVault:
    def test_not_configured_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_PATH", raising=False)
        with pytest.raises(ExternalCorpusError) as exc:
            query_spatial_vault("hello")
        assert exc.value.kind == "NOT_CONFIGURED"

    def test_missing_index_file_is_not_configured(self, tmp_path, monkeypatch):
        db = tmp_path / "vault.db"
        _make_db(db, rows=[(1, "hi")])
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        # Deliberately no vector.index sibling written.
        with pytest.raises(ExternalCorpusError) as exc:
            query_spatial_vault("hello")
        assert exc.value.kind == "NOT_CONFIGURED"

    def test_missing_faiss_dependency_is_honest(self, tmp_path, monkeypatch):
        """REAL test, not a simulation: faiss-cpu is genuinely absent from
        this venv (confirmed directly via `import faiss` before writing
        this module at all), so this exercises the actual honest-degrade
        path this codebase will hit on any machine without it installed."""
        db = tmp_path / "vault.db"
        _make_db(db, rows=[(1, "hi")])
        (tmp_path / "vector.index").write_bytes(b"placeholder")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        monkeypatch.delitem(sys.modules, "faiss", raising=False)
        with pytest.raises(ExternalCorpusError) as exc:
            query_spatial_vault("hello")
        assert exc.value.kind == "MISSING_DEPENDENCY"
        assert "faiss-cpu" in str(exc.value)

    def test_embed_model_env_mismatch_refused_before_touching_index(self, tmp_path, monkeypatch):
        db = tmp_path / "vault.db"
        _make_db(db, rows=[(1, "hi")])
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_EMBED_MODEL", "some-other-embedding-model")
        # No vector.index written at all: if this raised NOT_CONFIGURED
        # instead of EMBEDDING_MISMATCH, the ordering regressed — the
        # configured-model check must run BEFORE the index is touched.
        with pytest.raises(ExternalCorpusError) as exc:
            query_spatial_vault("hello")
        assert exc.value.kind == "EMBEDDING_MISMATCH"

    def test_dimension_mismatch_reuses_validate_corpus_entry(self, tmp_path, monkeypatch):
        db = tmp_path / "vault.db"
        _make_db(db, rows=[(1, "hi")])
        (tmp_path / "vector.index").write_bytes(b"placeholder")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        fake_index = _FakeFaissIndex(d=3, metric_type=0, ordered_hits=[])
        monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(fake_index))
        monkeypatch.setattr(shared_rag, "EMBED_DIM", 768)  # real default; index reports 3
        with pytest.raises(ExternalCorpusError) as exc:
            query_spatial_vault("hello")
        assert exc.value.kind == "EMBEDDING_MISMATCH"
        # Comes straight from global_memory.validate_corpus_entry's own
        # message text — proves the guard is REUSED, not reimplemented.
        assert "incompatible space" in str(exc.value)
        assert "768" in str(exc.value) and "3" in str(exc.value)

    def test_query_embed_failure_is_honest(self, tmp_path, monkeypatch):
        db = tmp_path / "vault.db"
        _make_db(db, rows=[(1, "hi")])
        (tmp_path / "vector.index").write_bytes(b"placeholder")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        fake_index = _FakeFaissIndex(d=3, metric_type=0, ordered_hits=[])
        monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(fake_index))
        monkeypatch.setattr(shared_rag, "EMBED_DIM", 3)
        monkeypatch.setattr(shared_rag, "embed_text", lambda q: None)
        with pytest.raises(ExternalCorpusError) as exc:
            query_spatial_vault("hello")
        assert exc.value.kind == "EMBED_FAILED"

    def test_full_success_path_ranks_and_skips_stale_entries(self, tmp_path, monkeypatch):
        db = tmp_path / "vault.db"
        _make_db(
            db, table="chunks", id_col="id", text_col="text",
            rows=[(1, "alpha content"), (2, "beta content"), (3, "gamma content")],
        )
        (tmp_path / "vector.index").write_bytes(b"placeholder")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        d = 4
        fake_index = _FakeFaissIndex(
            d=d, metric_type=_FakeFaissModule.METRIC_INNER_PRODUCT,
            # Real 0-based FAISS positions: 0->id 1 (alpha), 1->id 2
            # (beta), both real rows in this 3-row table (id ASC order
            # is [1,2,3]). Position 99 is genuinely out of range for a
            # 3-row table -- a stale index entry, must be skipped
            # honestly, not crash and not silently vanish unnoted.
            ordered_hits=[(0.9, 0), (0.5, 1), (0.1, 99)],
            ntotal=100,
        )
        monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(fake_index))
        monkeypatch.setattr(shared_rag, "EMBED_DIM", d)
        monkeypatch.setattr(shared_rag, "embed_text", lambda q: [0.1, 0.2, 0.3, 0.4])

        hits = query_spatial_vault("find alpha", top_k=5)
        assert [h["id"] for h in hits] == ["1", "2"]
        assert hits[0]["text"] == "alpha content"
        assert hits[0]["score"] >= hits[1]["score"]
        assert hits[0]["screen"] == "spatial_vault"
        assert hits[0]["metadata"]["vault_stale_entries_skipped"] == 1

    def test_l2_metric_inverts_ranking_direction(self, tmp_path, monkeypatch):
        """Lower L2 distance = a BETTER match — the opposite direction from
        inner-product/cosine. Verifies _metric_is_lower_better actually
        flips the sort, since blindly trusting raw distance as 'score'
        would rank the worst match first."""
        db = tmp_path / "vault.db"
        _make_db(db, rows=[(1, "closest"), (2, "farthest")])
        (tmp_path / "vector.index").write_bytes(b"placeholder")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        d = 2
        # Real positions in a 2-row table (id ASC -> [1, 2]): position 0
        # is id 1, position 1 is id 2. id 1 has the SMALLER L2 distance
        # (0.1) -> should rank FIRST.
        fake_index = _FakeFaissIndex(
            d=d, metric_type=_FakeFaissModule.METRIC_L2,
            ordered_hits=[(2.0, 1), (0.1, 0)],
        )
        monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(fake_index))
        monkeypatch.setattr(shared_rag, "EMBED_DIM", d)
        monkeypatch.setattr(shared_rag, "embed_text", lambda q: [0.1, 0.2])

        hits = query_spatial_vault("q", top_k=5)
        assert hits[0]["id"] == "1"
        assert hits[0]["text"] == "closest"

    def test_empty_vault_is_honestly_empty_not_an_error(self, tmp_path, monkeypatch):
        db = tmp_path / "vault.db"
        _make_db(db, rows=[])
        (tmp_path / "vector.index").write_bytes(b"placeholder")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        fake_index = _FakeFaissIndex(d=2, metric_type=0, ordered_hits=[])
        monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(fake_index))
        monkeypatch.setattr(shared_rag, "EMBED_DIM", 2)
        monkeypatch.setattr(shared_rag, "embed_text", lambda q: [0.1, 0.2])
        assert query_spatial_vault("anything") == []


class TestPositionToIdMapping:
    """v13.6, the real bug this session found live against the actual
    desktop vault: a plain FAISS IndexFlatL2/IndexFlatIP carries NO id
    map — .search() returns 0-based POSITIONS, not SQL ids. Every test
    above this class uses fixtures where position happens to equal id
    (rows inserted 1,2,3 with none skipped), which could never have
    caught this.
    """

    def test_default_id_asc_resolves_a_true_prefix_correctly(self, tmp_path, monkeypatch):
        """The default (plain 'id ASC') is correct for the common case:
        an index built in one straight pass over the whole table, so
        the index covers a true PREFIX of it — position N really is
        the (N+1)-th row by id."""
        db = tmp_path / "vault.db"
        _make_db(db, rows=[(1, "row one"), (2, "row two"), (3, "row three")])
        (tmp_path / "vector.index").write_bytes(b"placeholder")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        d = 2
        # Only 2 of the 3 real rows are indexed (a real, common shape:
        # ingestion is still in progress) -- but crucially still a
        # PREFIX (ids 1, 2), not a gap in the middle.
        fake_index = _FakeFaissIndex(
            d=d, metric_type=_FakeFaissModule.METRIC_INNER_PRODUCT,
            ordered_hits=[(0.9, 1)],  # position 1 -> should be id 2
            ntotal=2,
        )
        monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(fake_index))
        monkeypatch.setattr(shared_rag, "EMBED_DIM", d)
        monkeypatch.setattr(shared_rag, "embed_text", lambda q: [0.1, 0.2])

        hits = query_spatial_vault("q", top_k=5)
        assert len(hits) == 1
        assert hits[0]["id"] == "2"
        assert hits[0]["text"] == "row two"

    def test_id_order_override_resolves_the_real_verified_gap_shape(self, tmp_path, monkeypatch):
        """The real desktop vault is NOT a simple prefix -- it indexes
        two DISJOINT id ranges with a large skipped range in between
        (real, live-verified this session: HuggingFace_Parquet_Stream
        1..81166 + English_Wikipedia 878856..1023765 indexed,
        Pristine_Filtered_Stream 81167..878855 in between is not). A
        naive "id = position" OR even a plain "id ASC over everything"
        default gets this wrong. DOURMOUSE_SPATIAL_VAULT_ID_ORDER_SQL
        is the real, documented escape hatch -- this test uses the
        EXACT real clause verified against the actual vault, on a
        small synthetic fixture with the same shape."""
        db = tmp_path / "vault.db"
        _make_db(
            db,
            rows=[
                (1, "hf row"),
                (2, "hf row two"),
                (500, "pristine row - never indexed, sits in the gap"),
                (900, "wikipedia row"),
                (901, "wikipedia row two"),
            ],
        )
        # Real column name in the actual vault -- exercised for real
        # here too (not just id/text) since the ORDER BY clause
        # references it by name.
        conn = sqlite3.connect(str(db))
        conn.execute("ALTER TABLE chunks ADD COLUMN source_pipeline TEXT")
        conn.execute("UPDATE chunks SET source_pipeline='HuggingFace_Parquet_Stream' WHERE id IN (1,2)")
        conn.execute("UPDATE chunks SET source_pipeline='Pristine_Filtered_Stream' WHERE id=500")
        conn.execute("UPDATE chunks SET source_pipeline='English_Wikipedia' WHERE id IN (900,901)")
        conn.commit()
        conn.close()
        (tmp_path / "vector.index").write_bytes(b"placeholder")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        monkeypatch.setenv(
            "DOURMOUSE_SPATIAL_VAULT_ID_FILTER_SQL",
            "source_pipeline IN ('HuggingFace_Parquet_Stream','English_Wikipedia')",
        )
        monkeypatch.setenv(
            "DOURMOUSE_SPATIAL_VAULT_ID_ORDER_SQL",
            "(source_pipeline='English_Wikipedia'), id ASC",
        )
        d = 2
        # Index covers 4 of the 5 real rows (everything except the
        # Pristine one in the middle): positions 0,1 = the two HF rows
        # (id ASC), positions 2,3 = the two Wikipedia rows (id ASC).
        fake_index = _FakeFaissIndex(
            d=d, metric_type=_FakeFaissModule.METRIC_INNER_PRODUCT,
            ordered_hits=[(0.9, 3)],  # position 3 -> should be id 901, NOT id 500
            ntotal=4,
        )
        monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(fake_index))
        monkeypatch.setattr(shared_rag, "EMBED_DIM", d)
        monkeypatch.setattr(shared_rag, "embed_text", lambda q: [0.1, 0.2])

        hits = query_spatial_vault("q", top_k=5)
        assert len(hits) == 1
        assert hits[0]["id"] == "901"
        assert hits[0]["text"] == "wikipedia row two"

    def test_position_beyond_the_real_id_map_is_honestly_stale(self, tmp_path, monkeypatch):
        """An index reporting more vectors (ntotal) than the table
        actually has real ids for a corrupt/mismatched pairing -- must
        degrade to a stale skip, never an IndexError or a wrong id."""
        db = tmp_path / "vault.db"
        _make_db(db, rows=[(1, "only row")])
        (tmp_path / "vector.index").write_bytes(b"placeholder")
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
        d = 2
        fake_index = _FakeFaissIndex(
            d=d, metric_type=_FakeFaissModule.METRIC_INNER_PRODUCT,
            ordered_hits=[(0.9, 0), (0.5, 5)],  # position 5 has no real id at all
            ntotal=10,
        )
        monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(fake_index))
        monkeypatch.setattr(shared_rag, "EMBED_DIM", d)
        monkeypatch.setattr(shared_rag, "embed_text", lambda q: [0.1, 0.2])

        hits = query_spatial_vault("q", top_k=5)
        assert len(hits) == 1
        assert hits[0]["id"] == "1"
        assert hits[0]["metadata"]["vault_stale_entries_skipped"] == 1


class TestMergedSearch:
    def test_neither_source_configured_reports_honestly(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_GLOBAL_MEMORY", raising=False)
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_PATH", raising=False)
        result = merged_search("anything")
        assert result.hits == []
        assert result.sources_used == []
        assert result.warnings == []
        text = format_merged_result("anything", result)
        assert text.startswith("NOT CONFIGURED")

    def test_local_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GLOBAL_MEMORY", "1")
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_PATH", raising=False)

        class _FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=None):
                return json.dumps(self._payload).encode("utf-8")

        monkeypatch.setattr(
            "urllib.request.urlopen", lambda req, timeout=None: _FakeResp({"embedding": [1.0, 0.0]})
        )
        mem = GlobalMemory(tmp_path / "mem.sqlite3")
        mem.add("locally stored fact", screen="RESEARCH")
        result = merged_search("anything", memory=mem)
        assert result.sources_used == ["local"]
        assert len(result.hits) == 1
        assert result.hits[0]["source"] == "local"
        text = format_merged_result("anything", result)
        assert "local store" in text
        mem.close()

    def test_vault_error_becomes_a_warning_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_GLOBAL_MEMORY", raising=False)
        monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(tmp_path / "missing.db"))
        result = merged_search("anything")  # must not raise
        assert result.hits == []
        assert result.sources_used == []
        assert len(result.warnings) == 1
        assert "NOT_CONFIGURED" in result.warnings[0]
        text = format_merged_result("anything", result)
        # A configured-but-broken source must be visible, never silently
        # collapsed into the same message as "nothing was ever set up".
        assert "WARNINGS" in text
        assert not text.startswith("NOT CONFIGURED")

    def test_format_merged_result_tags_sources_distinctly(self):
        result = MergedResult(
            hits=[
                {"text": "local hit", "score": 0.9, "source": "local"},
                {"text": "vault hit", "score": 0.7, "source": "spatial_vault"},
            ],
            sources_used=["local", "spatial_vault"],
            warnings=[],
        )
        text = format_merged_result("q", result)
        assert "local store" in text
        assert "desktop spatial vault" in text

    def test_format_merged_result_honest_no_matches(self):
        result = MergedResult(hits=[], sources_used=["local"], warnings=[])
        text = format_merged_result("q", result)
        assert "No matches" in text
