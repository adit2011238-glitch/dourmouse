"""dourmouse/research_pipeline/ -- Domain G's real data model and store.
Pure logic + a real SQLite file, no model calls (this is the first,
smallest piece; plan/extract/synthesize stage functions are real,
separate follow-on, each with its own live-verified tests when built)."""

from __future__ import annotations

import pytest

from dourmouse.research_pipeline.core import Claim, Contradiction, ResearchRecord, Stage
from dourmouse.research_pipeline.store import ResearchStore


def _claim(text="water is wet", source="src1", claim_status="ACTIVE") -> Claim:
    return Claim(
        claim=text, source_id=source, url=f"https://example.com/{source}",
        document_hash="abc123", location="p.1", passage="Water is wet.",
        retrieved_at=1000.0, agent="research_info", status=claim_status,
    )


class TestResearchRecordLifecycle:
    def test_starts_planned_with_no_plan(self):
        r = ResearchRecord(question="Is water wet?")
        assert r.stage is Stage.PLANNED
        assert r.plan == ()

    def test_set_plan_requires_real_sub_questions(self):
        r = ResearchRecord(question="Is water wet?")
        with pytest.raises(ValueError):
            r.set_plan([])
        r.set_plan(["What does 'wet' mean?", "Does water satisfy that definition?"])
        assert len(r.plan) == 2

    def test_add_sources_requires_a_plan_first(self):
        r = ResearchRecord(question="Is water wet?")
        with pytest.raises(ValueError):
            r.add_sources(["https://example.com/a"])

    def test_add_sources_deduplicates_and_advances_stage(self):
        r = ResearchRecord(question="Is water wet?")
        r.set_plan(["sub-question"])
        r.add_sources(["https://a.com", "https://b.com", "https://a.com"])
        assert r.sources == ("https://a.com", "https://b.com")
        assert r.stage is Stage.SOURCES_DISCOVERED

    def test_add_claim_requires_sources_discovered_first(self):
        r = ResearchRecord(question="Is water wet?")
        with pytest.raises(ValueError):
            r.add_claim(_claim())

    def test_add_claim_advances_to_evidence_extracted(self):
        r = ResearchRecord(question="Is water wet?")
        r.set_plan(["sub-question"])
        r.add_sources(["https://a.com"])
        r.add_claim(_claim())
        assert r.stage is Stage.EVIDENCE_EXTRACTED
        assert len(r.claims) == 1

    def test_reject_claim_never_deletes_only_marks(self):
        r = ResearchRecord(question="Is water wet?")
        r.set_plan(["sub-question"])
        r.add_sources(["https://a.com"])
        r.add_claim(_claim())
        r.reject_claim(0, "later found unsupported")
        assert len(r.claims) == 1  # still present, not removed
        assert r.claims[0].status == "REJECTED"
        assert r.claims[0].claim == "water is wet"  # same real provenance kept
        assert r.active_claims() == ()

    def test_active_claims_excludes_rejected_only(self):
        r = ResearchRecord(question="Is water wet?")
        r.set_plan(["sub-question"])
        r.add_sources(["https://a.com"])
        r.add_claim(_claim("claim A"))
        r.add_claim(_claim("claim B"))
        r.reject_claim(0, "bad source")
        active = r.active_claims()
        assert len(active) == 1
        assert active[0].claim == "claim B"

    def test_synthesis_requires_evidence_extracted_stage(self):
        r = ResearchRecord(question="Is water wet?")
        with pytest.raises(ValueError):
            r.set_synthesis("Water is wet because...")

    def test_synthesis_requires_real_nonempty_text(self):
        r = ResearchRecord(question="Is water wet?")
        r.set_plan(["sub-question"])
        r.add_sources(["https://a.com"])
        r.add_claim(_claim())
        with pytest.raises(ValueError):
            r.set_synthesis("   ")

    def test_full_happy_path_reaches_synthesized(self):
        r = ResearchRecord(question="Is water wet?")
        r.set_plan(["What does wet mean?"])
        r.add_sources(["https://a.com"])
        r.add_claim(_claim())
        r.set_synthesis("Yes, water is wet by the common definition.")
        assert r.stage is Stage.SYNTHESIZED
        assert r.synthesis == "Yes, water is wet by the common definition."

    def test_contradictions_are_additive_never_overwritten(self):
        r = ResearchRecord(question="Is water wet?")
        c1 = Contradiction(claim_a_id="a", claim_b_id="b", sub_question="q1")
        c2 = Contradiction(claim_a_id="c", claim_b_id="d", sub_question="q2")
        r.add_contradiction(c1)
        r.add_contradiction(c2)
        assert r.contradictions == (c1, c2)


class TestResearchStore:
    def test_load_missing_question_is_honest_none(self, tmp_path):
        store = ResearchStore(tmp_path / "r.db")
        assert store.load("never asked") is None

    def test_save_and_load_round_trips_losslessly(self, tmp_path):
        store = ResearchStore(tmp_path / "r.db")
        r = ResearchRecord(question="Is water wet?")
        r.set_plan(["sub-question one", "sub-question two"])
        r.add_sources(["https://a.com", "https://b.com"])
        r.add_claim(_claim("claim A"))
        r.add_claim(_claim("claim B", source="src2"))
        r.reject_claim(0, "superseded")
        r.add_contradiction(Contradiction(claim_a_id="x", claim_b_id="y", sub_question="q"))
        r.set_synthesis("Real synthesis text.")
        store.save(r, now=1000.0)

        loaded = store.load("Is water wet?")
        assert loaded is not None
        assert loaded.stage is Stage.SYNTHESIZED
        assert loaded.plan == r.plan
        assert loaded.sources == r.sources
        assert len(loaded.claims) == 2
        assert loaded.claims[0].status == "REJECTED"
        assert loaded.claims[1].status == "ACTIVE"
        assert loaded.contradictions == r.contradictions
        assert loaded.synthesis == "Real synthesis text."

    def test_save_is_idempotent_update_not_a_duplicate_row(self, tmp_path):
        store = ResearchStore(tmp_path / "r.db")
        r = ResearchRecord(question="Is water wet?")
        store.save(r, now=1000.0)
        r.set_plan(["a sub-question"])
        store.save(r, now=2000.0)
        assert store.list_questions() == ["Is water wet?"]
        assert store.load("Is water wet?").plan == ("a sub-question",)

    def test_store_persists_across_instances(self, tmp_path):
        path = tmp_path / "r.db"
        r = ResearchRecord(question="Is water wet?")
        ResearchStore(path).save(r, now=1000.0)
        assert ResearchStore(path).load("Is water wet?") is not None

    def test_list_questions_most_recent_first(self, tmp_path):
        store = ResearchStore(tmp_path / "r.db")
        store.save(ResearchRecord(question="first"), now=1000.0)
        store.save(ResearchRecord(question="second"), now=2000.0)
        assert store.list_questions() == ["second", "first"]
