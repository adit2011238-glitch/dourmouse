"""P6 semantic-recall tests — gate, cosine math, honest fallback chain.

All hermetic: the Ollama endpoint is NEVER hit (embed_texts is monkeypatched
or the gate is off), so this suite runs offline (Rules 2.1 / 2.8).
"""

from __future__ import annotations

import json
import math
import re

import pytest

from dourmouse import memory_embed
from dourmouse.learn import distill_query
from dourmouse.memory_embed import cosine_similarity, embed_enabled, semantic_search
from dourmouse.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# Gate parsing
# --------------------------------------------------------------------------- #

class TestEmbedGate:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_EMBED", raising=False)
        assert embed_enabled() is False

    def test_off_values(self):
        for v in ("0", "false", "no", "off", "", "FALSE"):
            assert embed_enabled(v) is False, v

    def test_on_values(self):
        for v in ("1", "true", "yes", "on", "ON"):
            assert embed_enabled(v) is True, v


# --------------------------------------------------------------------------- #
# Cosine math (pure, deterministic)
# --------------------------------------------------------------------------- #

class TestCosine:
    def test_identical_is_one(self):
        assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 1.0

    def test_orthogonal_is_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_empty_and_mismatched_are_zero(self):
        assert cosine_similarity([], [1.0]) == 0.0
        assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0
        assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


# --------------------------------------------------------------------------- #
# Fallback chain — gate off / endpoint down -> honest FTS5
# --------------------------------------------------------------------------- #

class TestFts5Fallback:
    def test_gate_off_returns_fts5(self, store, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_EMBED", raising=False)
        store.remember("agent", "risk params", "daily loss limit is 3%")
        result = semantic_search(store, "daily loss")
        assert result["method"] == "fts5"
        assert result["hits"]
        assert result["hits"][0]["title"] == "risk params"

    def test_empty_query_returns_fts5_no_hits(self, store, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_EMBED", "1")
        result = semantic_search(store, "   ")
        assert result["method"] == "fts5"
        assert result["hits"] == []

    def test_embed_failure_falls_back_to_fts5(self, store, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_EMBED", "1")
        store.remember("agent", "a", "the daily loss limit is three percent")
        monkeypatch.setattr(memory_embed, "embed_texts", lambda texts, timeout=10.0: None)
        result = semantic_search(store, "daily loss")
        assert result["method"] == "fts5"
        assert result["hits"], "FTS5 must still find the fact when embed fails"

    def test_conversational_query_is_distilled_for_fts5(self, store, monkeypatch):
        """A natural-language question must NOT be AND-matched word-by-word:
        the fallback distills to distinctive terms first (learn.distill_query)."""
        monkeypatch.delenv("DOURMOUSE_EMBED", raising=False)
        store.remember(
            "agent",
            "decision",
            "change the risk parameters: daily loss limit is now 3%",
        )
        result = semantic_search(store, "why did we change the risk parameters")
        assert result["method"] == "fts5"
        assert result["hits"], "conversational phrasing must still recall"
        assert result["hits"][0]["title"] == "decision"


# --------------------------------------------------------------------------- #
# Semantic path with a deterministic fake embedder (no network)
# --------------------------------------------------------------------------- #

def _fake_embed(texts, timeout=10.0):
    """Two-dimension fake: dim0='apple', dim1='risk'."""
    out = []
    for t in texts:
        out.append(
            [
                1.0 if "apple" in (t or "").lower() else 0.0,
                1.0 if "risk" in (t or "").lower() else 0.0,
            ]
        )
    return out


class TestSemanticPath:
    def test_semantic_ordering_by_similarity(self, store, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_EMBED", "1")
        store.remember("agent", "unrelated", "rocket launch sequence begins now")
        store.remember("agent", "relevant", "apple pie recipe with fresh apples")
        store.remember("agent", "risk", "risk parameters were tightened to 3%")
        monkeypatch.setattr(memory_embed, "embed_texts", _fake_embed)

        result = semantic_search(store, "apple", limit=3)
        assert result["method"] == "semantic"
        titles = [h["title"] for h in result["hits"]]
        assert titles[0] == "relevant"
        assert all(0.0 <= h["score"] <= 1.0 for h in result["hits"])
        assert result["hits"][0]["snippet"], "snippet must be populated"

    def test_weak_semantic_match_falls_back_to_fts5(self, store, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_EMBED", "1")
        store.remember("agent", "unrelated", "rocket launch sequence")
        monkeypatch.setattr(memory_embed, "embed_texts", _fake_embed)
        # 'apple' finds nothing semantically (best score 0.0 < 0.1) and
        # nothing via FTS5 either -> honest empty fts5 result.
        result = semantic_search(store, "apple")
        assert result["method"] == "fts5"
        assert result["hits"] == []

    def test_embeddings_cached_after_first_pass(self, store, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_EMBED", "1")
        store.remember("agent", "one", "apple core")
        monkeypatch.setattr(memory_embed, "embed_texts", _fake_embed)
        semantic_search(store, "apple")
        cached = store.get_embeddings()
        assert len(cached) == 1
        semantic_search(store, "apple")
        assert len(store.get_embeddings()) == 1, "cache must not grow on re-query"


# --------------------------------------------------------------------------- #
# P6 acceptance pinned: the live-verified working path (real Ollama + nomic)
# --------------------------------------------------------------------------- #

# Deterministic stand-in for nomic-embed-text: a fixed set of "concept"
# dimensions whose word-forms model the REAL meaning overlap the embed model
# captures (risk <-> loss/tighten/limit, etc.). Hermetic — the regression
# must never depend on a live Ollama — yet it exercises the REAL embed_texts
# HTTP path (request -> urlopen -> response parse -> vectors -> cosine).
_DOMAIN_DIMS = (
    ("risk", ("risk", "risks", "parameters", "parameter", "loss", "limit",
               "tighten", "tightened", "var", "cvar", "exposure", "scenario",
               "volatility", "tail")),
    ("data", ("data", "archive", "survivorship", "bias", "ingest", "raw")),
    ("scalping", ("scalp", "scalping", "spread", "pip", "intraday")),
    ("research", ("research", "alpha", "hypothesis", "sharpe", "backtest", "trial")),
    ("ui", ("screen", "ocr", "capture", "vision", "clipboard")),
)


def _domain_vector(text: str) -> list[float]:
    vec = [0.0] * len(_DOMAIN_DIMS)
    for word in re.findall(r"[a-z]+", text.lower()):
        for i, (_name, forms) in enumerate(_DOMAIN_DIMS):
            if word in forms:
                vec[i] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class TestChangelogSemanticRegression:
    """Pins the live-verified P6 acceptance: the conversational query "why
    did we change the risk parameters" surfaces the REAL CHANGELOG risk
    section ONLY through the semantic layer — keyword FTS5 finds nothing
    because the changelog says "tightened daily loss limit", not "risk
    parameters". The fake embedder stands in for nomic-embed-text; the real
    ``embed_texts`` HTTP request/response path is still exercised."""

    _RISK_SECTION = (
        "v9 — Phase 5: Risk management layer\n"
        "Historical and parametric VaR and CVaR at 95%/99%, plus risk_summary()\n"
        "Tightened the daily loss limit and exposure caps."
    )
    # Query words are risk-domain (parameter/scenario) so the semantic layer
    # matches the changelog, but NONE of the distilled terms (parameter,
    # scenario, trigger, change) appear in either stored fact — so even the
    # OR-matched FTS5 recall (v5.x fix) finds nothing. This keeps the guard
    # semantic-only instead of relying on the old AND-brittle FTS5 behavior.
    _QUERY = "what scenario would trigger a parameter change"

    @staticmethod
    def _fake_urlopen_factory():
        class _Resp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake(req, timeout=10.0):
            payload = json.loads(req.data.decode())
            return _Resp(json.dumps({"embedding": _domain_vector(payload["prompt"])}).encode())

        return _fake

    def test_semantic_finds_changelog_where_fts5_cannot(self, store, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_EMBED", "1")
        store.remember(
            "repo",
            "CHANGELOG.md: v9 — Phase 5: Risk management layer",
            self._RISK_SECTION,
        )
        store.remember("repo", "CHANGELOG.md: v13 — scalping", "scalping strategy spread pips")
        monkeypatch.setattr(
            memory_embed.urllib.request, "urlopen", self._fake_urlopen_factory()
        )

        # (1) FTS5 — even with the distilled terms — finds nothing:
        assert store.search(distill_query(self._QUERY), limit=5) == []

        # (2) the semantic layer surfaces the real changelog section:
        result = semantic_search(store, self._QUERY, limit=3)
        assert result["method"] == "semantic"
        assert result["hits"], "semantic recall must find the risk section"
        assert result["hits"][0]["title"].startswith("CHANGELOG.md: v9"), \
            f"top hit should be the changelog risk section, got {result['hits'][0]['title']!r}"
        # every fact got embedded through the real HTTP path, and the cache
        # is populated:
        assert len(store.get_embeddings()) == store.count()

    def test_weak_semantic_match_still_reports_honestly(self, store, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_EMBED", "1")
        store.remember("repo", "CHANGELOG.md: v13 — scalping", "scalping strategy spread pips")
        monkeypatch.setattr(
            memory_embed.urllib.request, "urlopen", self._fake_urlopen_factory()
        )
        result = semantic_search(store, self._QUERY)
        # nothing risk-related -> honest fts5 empty result, never fabricated
        assert result["method"] == "fts5"
        assert result["hits"] == []


# --------------------------------------------------------------------------- #
# Embedding cache roundtrip (pure store API)
# --------------------------------------------------------------------------- #

class TestEmbeddingCache:
    def test_save_and_load_roundtrip(self, store):
        store.remember("agent", "t", "body")
        facts = store.all_facts()
        assert len(facts) == 1
        store.save_embedding(facts[0]["id"], "nomic-embed-text", [0.1, 0.2, 0.3])
        loaded = store.get_embeddings()
        assert loaded[facts[0]["id"]] == [0.1, 0.2, 0.3]

    def test_upsert_by_fact_id(self, store):
        store.remember("agent", "t", "body")
        fid = store.all_facts()[0]["id"]
        store.save_embedding(fid, "m", [1.0])
        store.save_embedding(fid, "m", [2.0])
        assert store.get_embeddings()[fid] == [2.0]

    def test_fact_update_invalidates_embedding(self, store):
        """A changed fact body must drop its cached vector, or semantic recall
        would forever score against the OLD body (P6 correctness)."""
        store.remember("agent", "t", "old body")
        fid = store.all_facts()[0]["id"]
        store.save_embedding(fid, "m", [1.0, 0.0])
        assert store.get_embeddings()
        store.remember("agent", "t", "brand new body")  # upsert updates body
        assert store.get_embeddings() == {}, "stale vector must be dropped on update"
