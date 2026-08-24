"""Global memory tests — real logic, a fake Ollama endpoint standing in
for the network (matching test_general_roster.py's own urllib.request
mocking discipline: no real backend needed to exercise this)."""

from __future__ import annotations

import json

import pytest

from dourmouse.global_memory import (
    GlobalMemory,
    cosine_similarity,
    embed_text,
    global_memory_enabled,
    ingest_corpus_file,
    validate_corpus_entry,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=None):
        return json.dumps(self._payload).encode("utf-8")


def _fake_ollama(vector):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"embedding": vector})

    return fake_urlopen


class TestEmbedText:
    def test_returns_real_vector_from_ollama(self, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", _fake_ollama([0.1, 0.2, 0.3]))
        vec = embed_text("hello world")
        assert vec == [0.1, 0.2, 0.3]

    def test_network_failure_returns_none_not_a_fabricated_vector(self, monkeypatch):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("ollama not running")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert embed_text("hello") is None

    def test_malformed_response_returns_none(self, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", _fake_ollama(None))
        assert embed_text("hello") is None


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector_does_not_crash_divide_by_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


class TestValidateCorpusEntry:
    def test_valid_raw_text_entry_passes(self):
        assert validate_corpus_entry({"id": "x1", "text": "real content"}) is None

    def test_missing_id_rejected(self):
        problem = validate_corpus_entry({"text": "content"})
        assert "id" in problem

    def test_missing_text_rejected(self):
        problem = validate_corpus_entry({"id": "x1"})
        assert "text" in problem

    def test_wrong_dimension_vector_rejected_not_silently_accepted(self):
        # This is the exact real risk flagged to the user: a vector from a
        # DIFFERENT embedding model lives in an incompatible space, and
        # cosine similarity across two spaces is meaningless garbage, not
        # just lower quality — so a dimension mismatch is a hard refusal.
        problem = validate_corpus_entry(
            {"id": "x1", "text": "content", "vector": [0.1, 0.2, 0.3]},
            expected_dim=768,
        )
        assert problem is not None
        assert "3 dimensions, expected 768" in problem

    def test_correct_dimension_vector_accepted(self):
        vec = [0.01] * 768
        assert validate_corpus_entry({"id": "x1", "text": "content", "vector": vec}, expected_dim=768) is None

    def test_non_numeric_vector_rejected(self):
        problem = validate_corpus_entry({"id": "x1", "text": "content", "vector": ["a", "b"]})
        assert "flat list of numbers" in problem


class TestGlobalMemoryStore:
    def test_add_and_search_real_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", _fake_ollama([1.0, 0.0, 0.0]))
        mem = GlobalMemory(tmp_path / "test.sqlite3")
        ok = mem.add("the sky is blue", screen="RESEARCH")
        assert ok is True

        # A query that embeds to the SAME vector should match with score 1.0.
        results = mem.search("what color is the sky")
        assert len(results) == 1
        assert results[0]["text"] == "the sky is blue"
        assert results[0]["screen"] == "RESEARCH"
        assert results[0]["score"] == pytest.approx(1.0)
        mem.close()

    def test_add_returns_false_on_embed_failure_no_fabricated_row(self, tmp_path, monkeypatch):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("down")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        mem = GlobalMemory(tmp_path / "test.sqlite3")
        assert mem.add("anything") is False
        assert mem.search("anything") == []
        mem.close()

    def test_search_filters_by_screen(self, tmp_path, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", _fake_ollama([1.0, 0.0]))
        mem = GlobalMemory(tmp_path / "test.sqlite3")
        mem.add("comms item", screen="COMMS")
        mem.add("code item", screen="CODE")
        results = mem.search("anything", screen="CODE")
        assert len(results) == 1
        assert results[0]["screen"] == "CODE"
        mem.close()

    def test_retrieve_context_for_prompt_empty_when_nothing_clears_the_bar(self, tmp_path, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", _fake_ollama([1.0, 0.0]))
        mem = GlobalMemory(tmp_path / "test.sqlite3")
        # Nothing stored at all — must be an honest empty string, not a
        # placeholder block injected into the prompt for nothing.
        assert mem.retrieve_context_for_prompt("anything") == ""
        mem.close()

    def test_retrieve_context_for_prompt_real_formatted_block(self, tmp_path, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", _fake_ollama([1.0, 0.0]))
        mem = GlobalMemory(tmp_path / "test.sqlite3")
        mem.add("Python 3.13 shipped a new REPL", screen="RESEARCH")
        block = mem.retrieve_context_for_prompt("tell me about Python", min_score=0.0)
        assert "RELEVANT PAST CONTEXT" in block
        assert "Python 3.13 shipped a new REPL" in block
        assert "from RESEARCH" in block
        mem.close()

    def test_pre_embedded_vector_path_skips_the_embed_call(self, tmp_path, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise AssertionError("should not call Ollama when a vector is already supplied")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        mem = GlobalMemory(tmp_path / "test.sqlite3")
        ok = mem.add("pre-embedded text", vector=[0.5, 0.5])
        assert ok is True
        assert calls == []
        mem.close()


class TestGlobalMemoryEnabled:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_GLOBAL_MEMORY", raising=False)
        assert global_memory_enabled() is False

    def test_on_only_with_exact_flag(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GLOBAL_MEMORY", "1")
        assert global_memory_enabled() is True
        monkeypatch.setenv("DOURMOUSE_GLOBAL_MEMORY", "true")
        assert global_memory_enabled() is False  # deterministic: exact "1" only, no fuzzy truthiness


class TestIngestCorpusFile:
    def test_real_mixed_corpus_reports_per_row_outcomes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", _fake_ollama([1.0, 0.0]))
        corpus = [
            {"id": "a", "text": "real entry one", "source": "user-handoff"},
            {"id": "b", "text": "real entry two", "source": "user-handoff"},
            {"text": "missing id, must be rejected"},
            {"id": "c", "text": "wrong dim vector", "vector": [1.0]},
        ]
        corpus_path = tmp_path / "corpus.json"
        corpus_path.write_text(json.dumps(corpus))
        mem = GlobalMemory(tmp_path / "test.sqlite3")

        report = ingest_corpus_file(corpus_path, memory=mem)

        assert report["total"] == 4
        assert report["accepted"] == 2
        assert len(report["rejected"]) == 2
        rejected_ids = {r["id"] for r in report["rejected"]}
        assert rejected_ids == {"?", "c"}
        mem.close()

    def test_non_list_corpus_raises_clearly(self, tmp_path):
        corpus_path = tmp_path / "corpus.json"
        corpus_path.write_text(json.dumps({"not": "a list"}))
        with pytest.raises(ValueError, match="must contain a JSON array"):
            ingest_corpus_file(corpus_path)
