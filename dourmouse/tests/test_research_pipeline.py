"""dourmouse/research_pipeline/ -- Domain G's real data model, store, and
stage functions. The data-model/store tests below are pure logic + a real
SQLite file, no model calls. The stage tests exercise plan()/
discover_sources() hermetically -- same real doubles as test_dispatch.py's
own FakeClient (plain reconstruction, not a cross-file import: this
codebase's own convention is a local copy per test file, not a shared
test-internals import) and test_goal_runtime.py's own _FakeSession
monkeypatch pattern, real transcript parsing, no mocked shortcuts."""

from __future__ import annotations

import hashlib
import json

import pytest

import dourmouse.chat as chat_module
from dourmouse.dispatch import Subagent, ToolSpec
from dourmouse.research_pipeline.core import Claim, Contradiction, ResearchRecord, Stage
from dourmouse.research_pipeline.stages import (
    _claim_fingerprint,
    _extract_fetched_text,
    _extract_urls_from_transcript,
    _source_id_for_url,
    _strip_internal_diagnostics,
    detect_contradictions,
    discover_sources,
    extract_evidence,
    plan,
    synthesize,
)
from dourmouse.research_pipeline.store import ResearchStore


def _claim(text="water is wet", source="src1", claim_status="ACTIVE", sub_question="") -> Claim:
    return Claim(
        claim=text, source_id=source, url=f"https://example.com/{source}",
        document_hash="abc123", location="p.1", passage="Water is wet.",
        retrieved_at=1000.0, agent="research_info", status=claim_status,
        sub_question=sub_question,
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
        r.add_claim(_claim("claim A", sub_question="sub-question one"))
        r.add_claim(_claim("claim B", source="src2", sub_question="sub-question one"))
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
        assert loaded.claims[0].sub_question == "sub-question one"
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


# --- real fakes, local to this file (this codebase's own convention --
# see test_dispatch.py's own identical shapes, not cross-imported) ---


class _FakeSession:
    """Same monkeypatch target as research_mesh/tests/test_brain.py's own
    _FakeSession -- plan() uses the identical tool-less ChatSession
    primitive."""

    calls: list[str] = []
    responses: list[object] = []

    def __init__(self, registry, session_file=None):
        pass

    def ask(self, prompt, force_plain_dispatch=False):
        type(self).calls.append(prompt)
        responses = type(self).responses
        scripted = responses[0] if len(responses) == 1 else responses.pop(0)
        return {"final_text": scripted}


def _install_chat_fake(monkeypatch, responses: list[str]):
    _FakeSession.calls = []
    _FakeSession.responses = list(responses)
    monkeypatch.setattr(chat_module, "ChatSession", _FakeSession)


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)

    def create(self, **kwargs):
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class FakeClient:
    def __init__(self, responses: list[_FakeResponse]):
        self.chat = _FakeChat(_FakeCompletions(responses))


def _research_info_registry():
    """A minimal, real registry with a real 'research_info' subagent
    carrying real web_search/fetch_url-NAMED tools (the handlers never
    run in these tests -- the fake client never actually calls them, it
    just returns scripted assistant text/tool_calls -- only the NAMES
    need to match what discover_sources's own transcript parser looks
    for)."""
    from dourmouse.dispatch import DispatchRegistry

    registry = DispatchRegistry()
    registry.register_subagent(
        Subagent(
            name="research_info",
            domain="General",
            description="test double",
            tools=(
                ToolSpec(
                    name="web_search",
                    description="test",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
                    handler=lambda a: "WEB SEARCH RESULTS (Brave, live):\n1. Title - desc\n   https://example.com/hit1",
                ),
                ToolSpec(
                    name="fetch_url",
                    description="test",
                    parameters={"type": "object", "properties": {"url": {"type": "string"}}},
                    handler=lambda a: "(fetched page text)",
                ),
            ),
        )
    )
    return registry


class TestPlanStage:
    def test_real_dash_list_response_becomes_the_plan(self, monkeypatch):
        record = ResearchRecord(question="Is water wet?")
        _install_chat_fake(monkeypatch, [
            "- What does 'wet' mean?\n- Does water satisfy that definition?\n",
        ])
        result = plan(record)
        assert result is record
        assert record.plan == ("What does 'wet' mean?", "Does water satisfy that definition?")
        assert record.stage is Stage.PLANNED  # set_plan does not itself advance the stage

    def test_the_real_question_reaches_the_model(self, monkeypatch):
        record = ResearchRecord(question="Is water wet?")
        _install_chat_fake(monkeypatch, ["- a sub-question"])
        plan(record)
        assert "Is water wet?" in _FakeSession.calls[0]

    def test_a_response_with_no_dash_lines_falls_back_to_the_whole_answer(self, monkeypatch):
        record = ResearchRecord(question="Is water wet?")
        _install_chat_fake(monkeypatch, ["Water is wet by most common definitions."])
        plan(record)
        assert record.plan == ("Water is wet by most common definitions.",)

    def test_a_genuinely_empty_response_falls_back_to_the_original_question(self, monkeypatch):
        record = ResearchRecord(question="Is water wet?")
        _install_chat_fake(monkeypatch, [""])
        plan(record)
        assert record.plan == ("Is water wet?",)


class TestExtractUrlsFromTranscript:
    def test_fetch_url_arguments_are_the_most_reliable_source(self):
        transcript = [
            {"type": "tool_use", "name": "fetch_url", "raw_arguments": json.dumps({"url": "https://real.example/page"})},
        ]
        assert _extract_urls_from_transcript(transcript) == ["https://real.example/page"]

    def test_web_search_result_text_is_scanned_for_real_urls(self):
        transcript = [
            {"type": "tool_result", "name": "web_search",
             "text": "1. Title - desc\n   https://a.example/one\n2. Other - desc\n   https://b.example/two"},
        ]
        assert _extract_urls_from_transcript(transcript) == [
            "https://a.example/one", "https://b.example/two",
        ]

    def test_duplicates_across_both_sources_are_deduped(self):
        transcript = [
            {"type": "tool_result", "name": "web_search", "text": "https://same.example/x"},
            {"type": "tool_use", "name": "fetch_url", "raw_arguments": json.dumps({"url": "https://same.example/x"})},
        ]
        assert _extract_urls_from_transcript(transcript) == ["https://same.example/x"]

    def test_malformed_fetch_url_arguments_never_crash(self):
        transcript = [
            {"type": "tool_use", "name": "fetch_url", "raw_arguments": "not json"},
        ]
        assert _extract_urls_from_transcript(transcript) == []

    def test_unrelated_transcript_entries_are_ignored(self):
        transcript = [
            {"type": "assistant_text", "text": "https://ignored.example/because-wrong-type"},
            {"type": "tool_result", "name": "some_other_tool", "text": "https://ignored.example/because-wrong-name"},
        ]
        assert _extract_urls_from_transcript(transcript) == []


class TestDiscoverSourcesStage:
    def test_requires_a_real_plan_first(self):
        record = ResearchRecord(question="Is water wet?")
        with pytest.raises(ValueError):
            discover_sources(record, _research_info_registry())

    def test_real_urls_from_a_real_dispatch_run_are_added(self):
        record = ResearchRecord(question="Is water wet?")
        record.set_plan(["What does wet mean?"])
        search_call = _FakeToolCall("c1", "web_search", json.dumps({"query": "what does wet mean"}))
        client = FakeClient([
            _FakeResponse(_FakeMessage(content=None, tool_calls=[search_call])),
            _FakeResponse(_FakeMessage(content="Found a real definition.")),
        ])
        result = discover_sources(record, _research_info_registry(), client=client)
        assert result is record
        assert record.sources == ("https://example.com/hit1",)
        assert record.stage is Stage.SOURCES_DISCOVERED

    def test_zero_real_sources_found_is_an_honest_empty_not_an_error(self):
        record = ResearchRecord(question="Is water wet?")
        record.set_plan(["What does wet mean?"])
        client = FakeClient([_FakeResponse(_FakeMessage(content="I don't know."))])
        discover_sources(record, _research_info_registry(), client=client)
        assert record.sources == ()
        assert record.stage is Stage.SOURCES_DISCOVERED  # still advances -- an honest empty result

    def test_sub_question_index_selects_which_real_sub_question_drives_the_call(self, monkeypatch):
        record = ResearchRecord(question="Is water wet?")
        record.set_plan(["first sub-question", "second sub-question"])
        seen_prompts = []

        def _capture(messages, registry, **kwargs):
            seen_prompts.append(messages[0]["content"])
            return {"final_text": "ok", "transcript": []}

        import dourmouse.dispatch as dispatch_module

        monkeypatch.setattr(dispatch_module, "run_dispatch_messages", _capture)
        discover_sources(record, _research_info_registry(), sub_question_index=1)
        assert "second sub-question" in seen_prompts[0]
        assert "first sub-question" not in seen_prompts[0]


def _fetch_transcript(url: str, body: str) -> list[dict[str, object]]:
    return [
        {"type": "tool_result", "name": "fetch_url", "text": f"FETCHED {url} ({len(body)} chars):\n{body}"},
    ]


class TestExtractFetchedText:
    def test_matches_by_exact_url_prefix(self):
        transcript = _fetch_transcript("https://x.example/page", "real body text")
        assert _extract_fetched_text(transcript, "https://x.example/page") == "real body text"

    def test_ignores_a_fetch_of_a_different_url(self):
        transcript = _fetch_transcript("https://other.example/page", "wrong page")
        assert _extract_fetched_text(transcript, "https://x.example/page") is None

    def test_ignores_non_fetch_url_tool_results(self):
        transcript = [{"type": "tool_result", "name": "web_search", "text": "FETCHED https://x.example/page (4 chars):\nreal"}]
        assert _extract_fetched_text(transcript, "https://x.example/page") is None

    def test_a_real_no_readable_text_response_does_not_match(self):
        transcript = [{"type": "tool_result", "name": "fetch_url", "text": "FETCH: page returned no readable text (honest)."}]
        assert _extract_fetched_text(transcript, "https://x.example/page") is None


class TestExtractEvidenceStage:
    def _planned_and_discovered(self, url="https://example.com/mcp") -> ResearchRecord:
        record = ResearchRecord(question="What is MCP?")
        record.set_plan(["What are MCP's core components?"])
        record.add_sources([url])
        return record

    def test_requires_a_real_plan_first(self):
        record = ResearchRecord(question="What is MCP?")
        with pytest.raises(ValueError):
            extract_evidence(record, _research_info_registry())

    def test_requires_real_sources_first(self):
        record = ResearchRecord(question="What is MCP?")
        record.set_plan(["a sub-question"])
        with pytest.raises(ValueError):
            extract_evidence(record, _research_info_registry())

    def test_successful_extraction_produces_a_validated_claim(self, monkeypatch):
        url = "https://example.com/mcp"
        body = "Intro text.\n\nMCP has three core parts: hosts, servers, and clients.\n\nMore text."
        record = self._planned_and_discovered(url)
        import dourmouse.dispatch as dispatch_module

        monkeypatch.setattr(
            dispatch_module, "run_dispatch_messages",
            lambda messages, registry, **kw: {"final_text": "ok", "transcript": _fetch_transcript(url, body)},
        )
        _install_chat_fake(monkeypatch, [
            "CLAIM: MCP has three core parts.\n"
            "PASSAGE: MCP has three core parts: hosts, servers, and clients.\n"
            "LOCATION: second paragraph",
        ])

        result = extract_evidence(record, _research_info_registry())
        assert result is record
        assert record.stage is Stage.EVIDENCE_EXTRACTED
        assert len(record.claims) == 1
        claim = record.claims[0]
        assert claim.claim == "MCP has three core parts."
        assert claim.passage == "MCP has three core parts: hosts, servers, and clients."
        assert claim.location == "second paragraph"
        assert claim.url == url
        assert claim.source_id == _source_id_for_url(url)
        assert claim.document_hash == hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert claim.agent == "research_info"
        assert claim.status == "ACTIVE"
        assert claim.retrieved_at > 0

    def test_source_id_is_deterministic_and_distinct_per_url(self):
        a = _source_id_for_url("https://example.com/a")
        b = _source_id_for_url("https://example.com/b")
        assert a == _source_id_for_url("https://example.com/a")
        assert a != b

    def test_fetch_failure_raises_honestly(self, monkeypatch):
        record = self._planned_and_discovered()
        import dourmouse.dispatch as dispatch_module

        monkeypatch.setattr(
            dispatch_module, "run_dispatch_messages",
            lambda messages, registry, **kw: {"final_text": "ok", "transcript": []},
        )
        with pytest.raises(ValueError):
            extract_evidence(record, _research_info_registry())

    def test_malformed_model_reply_raises_honestly(self, monkeypatch):
        url = "https://example.com/mcp"
        record = self._planned_and_discovered(url)
        import dourmouse.dispatch as dispatch_module

        monkeypatch.setattr(
            dispatch_module, "run_dispatch_messages",
            lambda messages, registry, **kw: {"final_text": "ok", "transcript": _fetch_transcript(url, "some body")},
        )
        _install_chat_fake(monkeypatch, ["I don't know, sorry."])
        with pytest.raises(ValueError):
            extract_evidence(record, _research_info_registry())

    def test_hallucinated_passage_raises_honestly(self, monkeypatch):
        url = "https://example.com/mcp"
        body = "The real fetched sentence is right here."
        record = self._planned_and_discovered(url)
        import dourmouse.dispatch as dispatch_module

        monkeypatch.setattr(
            dispatch_module, "run_dispatch_messages",
            lambda messages, registry, **kw: {"final_text": "ok", "transcript": _fetch_transcript(url, body)},
        )
        _install_chat_fake(monkeypatch, [
            "CLAIM: A claim not backed by the text.\n"
            "PASSAGE: This exact sentence was never in the fetched page.\n"
            "LOCATION: nowhere real",
        ])
        with pytest.raises(ValueError):
            extract_evidence(record, _research_info_registry())

    def test_whitespace_only_difference_in_passage_still_validates(self, monkeypatch):
        url = "https://example.com/mcp"
        body = "MCP   has\nthree   core parts."
        record = self._planned_and_discovered(url)
        import dourmouse.dispatch as dispatch_module

        monkeypatch.setattr(
            dispatch_module, "run_dispatch_messages",
            lambda messages, registry, **kw: {"final_text": "ok", "transcript": _fetch_transcript(url, body)},
        )
        _install_chat_fake(monkeypatch, [
            "CLAIM: MCP has three core parts.\n"
            "PASSAGE: MCP has three core parts.\n"
            "LOCATION: only paragraph",
        ])
        extract_evidence(record, _research_info_registry())
        assert len(record.claims) == 1

    def test_source_index_selects_which_real_source_drives_the_fetch(self, monkeypatch):
        record = ResearchRecord(question="What is MCP?")
        record.set_plan(["a sub-question"])
        record.add_sources(["https://example.com/first", "https://example.com/second"])
        seen_urls = []
        import dourmouse.dispatch as dispatch_module

        def _capture(messages, registry, **kw):
            seen_urls.append(messages[0]["content"])
            return {"final_text": "ok", "transcript": []}

        monkeypatch.setattr(dispatch_module, "run_dispatch_messages", _capture)
        with pytest.raises(ValueError):  # empty transcript -> honest fetch-failure, still proves selection
            extract_evidence(record, _research_info_registry(), source_index=1)
        assert "https://example.com/second" in seen_urls[0]
        assert "https://example.com/first" not in seen_urls[0]


class TestSynthesizeStage:
    def _record_with_claims(self, *claim_texts) -> ResearchRecord:
        record = ResearchRecord(question="Is water wet?")
        record.set_plan(["sub-question"])
        record.add_sources(["https://a.com"])
        for i, text in enumerate(claim_texts):
            record.add_claim(_claim(text, source=f"src{i}"))
        return record

    def test_real_model_reply_becomes_the_synthesis(self, monkeypatch):
        record = self._record_with_claims("water is wet")
        _install_chat_fake(monkeypatch, ["Yes, water is wet (see example.com/src0)."])
        result = synthesize(record)
        assert result is record
        assert record.stage is Stage.SYNTHESIZED
        assert record.synthesis == "Yes, water is wet (see example.com/src0)."

    def test_active_claims_reach_the_prompt(self, monkeypatch):
        record = self._record_with_claims("claim A", "claim B")
        _install_chat_fake(monkeypatch, ["synthesis text"])
        synthesize(record)
        prompt = _FakeSession.calls[0]
        assert "claim A" in prompt
        assert "claim B" in prompt
        assert "Is water wet?" in prompt

    def test_rejected_claims_are_excluded_from_the_prompt(self, monkeypatch):
        record = self._record_with_claims("real claim", "bad claim")
        record.reject_claim(1, "unsupported")
        _install_chat_fake(monkeypatch, ["synthesis text"])
        synthesize(record)
        prompt = _FakeSession.calls[0]
        assert "real claim" in prompt
        assert "bad claim" not in prompt

    def test_zero_active_claims_skips_the_model_call_entirely(self, monkeypatch):
        record = self._record_with_claims("only claim")
        record.reject_claim(0, "later found unsupported")
        _install_chat_fake(monkeypatch, ["should never be used"])
        result = synthesize(record)
        assert result is record
        assert record.stage is Stage.SYNTHESIZED
        assert record.synthesis == (
            "No claims currently survive review for this question -- evidence "
            "extraction has not yet produced a supported answer."
        )
        assert _FakeSession.calls == []  # no model call was made

    def test_empty_model_reply_degrades_to_an_honest_placeholder_not_a_raise(self, monkeypatch):
        record = self._record_with_claims("real claim")
        _install_chat_fake(monkeypatch, [""])
        result = synthesize(record)  # must not raise -- real claims already exist to protect
        assert result is record
        assert record.stage is Stage.SYNTHESIZED
        assert "1 real claim(s) remain on record" in record.synthesis

    def test_requires_evidence_extracted_stage(self):
        record = ResearchRecord(question="Is water wet?")
        with pytest.raises(ValueError):
            synthesize(record)


_REAL_PLAN_STEP_DIAGNOSTIC = (
    "\n\n[DOURMOUSE: plan step(s) not executed via tools -- "
    "STEP 1/2 (orchestrator): write the answer; "
    "STEP 2/2 (orchestrator): cite every source]"
)


class TestStripInternalDiagnostics:
    """Live-caught 2026-09-20: force_plain_dispatch's zero-tool sessions
    can still trip dispatch.py's own plan-reminder loop, which appends a
    "[DOURMOUSE: plan step(s) not executed via tools ...]" caveat meant
    for the live chat UI, never for a stored data field."""

    def test_strips_a_trailing_diagnostic_suffix(self):
        text = "This is the real answer." + _REAL_PLAN_STEP_DIAGNOSTIC
        assert _strip_internal_diagnostics(text) == "This is the real answer."

    def test_leaves_ordinary_text_untouched(self):
        text = "This is a perfectly normal real answer, no diagnostics."
        assert _strip_internal_diagnostics(text) == text

    def test_a_mid_text_bracket_that_is_not_a_trailing_suffix_is_left_alone(self):
        text = "The doc says [DOURMOUSE: internal note] and then continues for real."
        assert _strip_internal_diagnostics(text) == text


class TestSynthesizeStripsLeakedDiagnostics:
    def test_synthesize_strips_the_diagnostic_from_stored_synthesis(self, monkeypatch):
        record = ResearchRecord(question="Is water wet?")
        record.set_plan(["sub-question"])
        record.add_sources(["https://a.com"])
        record.add_claim(_claim())
        scripted = "Yes, water is wet." + _REAL_PLAN_STEP_DIAGNOSTIC
        _install_chat_fake(monkeypatch, [scripted])
        synthesize(record)
        assert record.synthesis == "Yes, water is wet."
        assert "[DOURMOUSE" not in record.synthesis


class TestExtractEvidenceStripsLeakedDiagnostics:
    def test_leaked_diagnostic_never_contaminates_the_location_field(self, monkeypatch):
        url = "https://example.com/mcp"
        body = "The real fetched sentence is right here."
        record = TestExtractEvidenceStage()._planned_and_discovered(url)
        import dourmouse.dispatch as dispatch_module

        monkeypatch.setattr(
            dispatch_module, "run_dispatch_messages",
            lambda messages, registry, **kw: {"final_text": "ok", "transcript": _fetch_transcript(url, body)},
        )
        scripted = (
            "CLAIM: A real claim.\n"
            "PASSAGE: The real fetched sentence is right here.\n"
            "LOCATION: only sentence" + _REAL_PLAN_STEP_DIAGNOSTIC
        )
        _install_chat_fake(monkeypatch, [scripted])
        extract_evidence(record, _research_info_registry())
        assert record.claims[0].location == "only sentence"
        assert "[DOURMOUSE" not in record.claims[0].location


class TestExtractEvidenceDocumentCache:
    """Real gap closed 2026-09-20: a fetched page used to live only in
    memory for one call. Now cached workspace-relative by a hash of the
    URL, so a repeated source costs one real fetch, not one per call, and
    document_hash stops fingerprinting something already gone."""

    def _record(self, url="https://example.com/mcp") -> ResearchRecord:
        record = ResearchRecord(question="What is MCP?")
        record.set_plan(["a sub-question"])
        record.add_sources([url])
        return record

    def test_second_extraction_reuses_the_cached_document_no_new_fetch(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        url = "https://example.com/mcp"
        body = "The real fetched sentence is right here."
        fetch_calls = []
        import dourmouse.dispatch as dispatch_module

        def _fake_fetch(messages, registry, **kw):
            fetch_calls.append(url)
            return {"final_text": "ok", "transcript": _fetch_transcript(url, body)}

        monkeypatch.setattr(dispatch_module, "run_dispatch_messages", _fake_fetch)
        _install_chat_fake(monkeypatch, [
            "CLAIM: A real claim.\n"
            "PASSAGE: The real fetched sentence is right here.\n"
            "LOCATION: only sentence",
            "CLAIM: A second real claim.\n"
            "PASSAGE: The real fetched sentence is right here.\n"
            "LOCATION: only sentence",
        ])

        extract_evidence(self._record(url), _research_info_registry())
        assert len(fetch_calls) == 1

        # A brand new record, same URL -- the SECOND extraction must hit the
        # real on-disk cache, never call run_dispatch_messages again.
        extract_evidence(self._record(url), _research_info_registry())
        assert len(fetch_calls) == 1  # still 1 -- the cache served it

    def test_cache_file_actually_exists_on_disk_with_the_real_fetched_text(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        url = "https://example.com/mcp"
        body = "The real fetched sentence is right here."
        import dourmouse.dispatch as dispatch_module

        monkeypatch.setattr(
            dispatch_module, "run_dispatch_messages",
            lambda messages, registry, **kw: {"final_text": "ok", "transcript": _fetch_transcript(url, body)},
        )
        _install_chat_fake(monkeypatch, [
            "CLAIM: A real claim.\n"
            "PASSAGE: The real fetched sentence is right here.\n"
            "LOCATION: only sentence",
        ])
        extract_evidence(self._record(url), _research_info_registry())

        cache_path = tmp_path / "research_pipeline" / "documents" / f"{_source_id_for_url(url)}.txt"
        assert cache_path.exists()
        assert cache_path.read_text(encoding="utf-8") == body

    def test_different_urls_never_collide_in_the_cache(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        import dourmouse.dispatch as dispatch_module

        def _fake_fetch(messages, registry, **kw):
            url = "https://example.com/a" if "example.com/a" in messages[0]["content"] else "https://example.com/b"
            body = "Body A." if url.endswith("/a") else "Body B."
            return {"final_text": "ok", "transcript": _fetch_transcript(url, body)}

        monkeypatch.setattr(dispatch_module, "run_dispatch_messages", _fake_fetch)
        _install_chat_fake(monkeypatch, [
            "CLAIM: Claim A.\nPASSAGE: Body A.\nLOCATION: only line",
            "CLAIM: Claim B.\nPASSAGE: Body B.\nLOCATION: only line",
        ])
        extract_evidence(self._record("https://example.com/a"), _research_info_registry())
        extract_evidence(self._record("https://example.com/b"), _research_info_registry())

        docs_dir = tmp_path / "research_pipeline" / "documents"
        assert (docs_dir / f"{_source_id_for_url('https://example.com/a')}.txt").read_text() == "Body A."
        assert (docs_dir / f"{_source_id_for_url('https://example.com/b')}.txt").read_text() == "Body B."


class TestDetectContradictionsStage:
    def _record_with_claims(self, *specs) -> ResearchRecord:
        """specs: list of (claim_text, sub_question) tuples."""
        record = ResearchRecord(question="Is water wet?")
        record.set_plan(["sub-question one", "sub-question two"])
        record.add_sources(["https://a.com"])
        for i, (text, sub_question) in enumerate(specs):
            record.add_claim(_claim(text, source=f"src{i}", sub_question=sub_question))
        return record

    def test_zero_or_one_claim_per_sub_question_skips_the_model_call_entirely(self, monkeypatch):
        record = self._record_with_claims(("only claim", "sub-question one"))
        _install_chat_fake(monkeypatch, ["should never be used"])
        result = detect_contradictions(record)
        assert result is record
        assert record.contradictions == ()
        assert _FakeSession.calls == []

    def test_claims_from_different_sub_questions_are_never_compared(self, monkeypatch):
        record = self._record_with_claims(
            ("claim A", "sub-question one"), ("claim B", "sub-question two"),
        )
        _install_chat_fake(monkeypatch, ["should never be used"])
        detect_contradictions(record)
        assert record.contradictions == ()
        assert _FakeSession.calls == []

    def test_a_real_contradiction_is_recorded(self, monkeypatch):
        record = self._record_with_claims(
            ("The sky is blue", "what color is the sky"),
            ("The sky is green", "what color is the sky"),
        )
        _install_chat_fake(monkeypatch, [
            "CONTRADICTION: yes\nNOTE: one claim says blue, the other says green.",
        ])
        detect_contradictions(record)
        assert len(record.contradictions) == 1
        c = record.contradictions[0]
        assert c.sub_question == "what color is the sky"
        assert c.note == "one claim says blue, the other says green."
        assert c.claim_a_id == _claim_fingerprint(record.claims[0])
        assert c.claim_b_id == _claim_fingerprint(record.claims[1])

    def test_a_real_non_contradiction_is_not_recorded(self, monkeypatch):
        record = self._record_with_claims(
            ("MCP defines tools", "what is MCP"),
            ("MCP defines resources", "what is MCP"),
        )
        _install_chat_fake(monkeypatch, [
            "CONTRADICTION: no\nNOTE: these describe different, compatible parts of MCP.",
        ])
        detect_contradictions(record)
        assert record.contradictions == ()

    def test_malformed_model_reply_is_skipped_never_raised(self, monkeypatch):
        record = self._record_with_claims(
            ("claim A", "same question"), ("claim B", "same question"),
        )
        _install_chat_fake(monkeypatch, ["I'm not sure how to answer that."])
        result = detect_contradictions(record)  # must not raise
        assert result is record
        assert record.contradictions == ()

    def test_only_active_claims_are_compared_rejected_ones_excluded(self, monkeypatch):
        record = self._record_with_claims(
            ("claim A", "same question"),
            ("claim B", "same question"),
            ("claim C", "same question"),
        )
        record.reject_claim(1, "unsupported")  # claim B now REJECTED
        _install_chat_fake(monkeypatch, [
            "CONTRADICTION: no\nNOTE: no real disagreement.",
        ])
        detect_contradictions(record)
        # Only claim A vs claim C remain active -- exactly one real pair,
        # exactly one real model call.
        assert len(_FakeSession.calls) == 1

    def test_three_way_group_checks_every_real_pair(self, monkeypatch):
        record = self._record_with_claims(
            ("claim A", "same question"),
            ("claim B", "same question"),
            ("claim C", "same question"),
        )
        _install_chat_fake(monkeypatch, [
            "CONTRADICTION: no\nNOTE: fine.",
            "CONTRADICTION: no\nNOTE: fine.",
            "CONTRADICTION: no\nNOTE: fine.",
        ])
        detect_contradictions(record)
        # 3 claims -> C(3,2) = 3 real pairs, 3 real model calls.
        assert len(_FakeSession.calls) == 3

    def test_leaked_diagnostic_never_breaks_the_contradiction_parse(self, monkeypatch):
        record = self._record_with_claims(
            ("claim A", "same question"), ("claim B", "same question"),
        )
        _install_chat_fake(monkeypatch, [
            "CONTRADICTION: yes\nNOTE: real disagreement." + _REAL_PLAN_STEP_DIAGNOSTIC,
        ])
        detect_contradictions(record)
        assert len(record.contradictions) == 1
        assert "[DOURMOUSE" not in record.contradictions[0].note
