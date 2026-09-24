"""dourmouse/research_pipeline_tools.py -- the evidence_pipeline subagent,
chat-reachable wiring over Domain G's already-tested stage functions. Real
persistence (a real SQLite file per test via monkeypatched default_db()),
real doubles for the model/dispatch layer -- same conventions as
test_research_pipeline.py, not re-derived."""

from __future__ import annotations

import pytest

import dourmouse.chat as chat_module
import dourmouse.research_pipeline_tools as rpt
from dourmouse.dispatch import Subagent, ToolSpec
from dourmouse.research_pipeline.core import Stage
from dourmouse.research_pipeline.store import ResearchStore


class _FakeSession:
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


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(rpt, "default_db", lambda: tmp_path / "research.db")


def _registry():
    from dourmouse.dispatch import DispatchRegistry

    registry = DispatchRegistry()
    registry.register_subagent(
        Subagent(
            name="research_info",
            domain="General",
            description="test double",
            tools=(
                ToolSpec(
                    name="web_search", description="test",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
                    handler=lambda a: "WEB SEARCH RESULTS (Brave, live):\n1. Title - desc\n   https://example.com/hit1",
                ),
                ToolSpec(
                    name="fetch_url", description="test",
                    parameters={"type": "object", "properties": {"url": {"type": "string"}}},
                    handler=lambda a: "(fetched page text)",
                ),
            ),
        )
    )
    return registry


class TestResearchPlanTool:
    def test_creates_and_persists_a_real_plan(self, monkeypatch):
        _install_chat_fake(monkeypatch, ["- What is X?\n- What is Y?"])
        out = rpt._research_plan_tool({"question": "Is Z real?"})
        assert "What is X?" in out
        assert "What is Y?" in out
        loaded = ResearchStore(rpt.default_db()).load("Is Z real?")
        assert loaded is not None
        assert loaded.plan == ("What is X?", "What is Y?")

    def test_second_call_reports_the_already_real_plan_no_replan(self, monkeypatch):
        _install_chat_fake(monkeypatch, ["- What is X?"])
        rpt._research_plan_tool({"question": "Is Z real?"})
        assert len(_FakeSession.calls) == 1
        out = rpt._research_plan_tool({"question": "Is Z real?"})
        assert len(_FakeSession.calls) == 1  # no second real model call
        assert "Already planned" in out

    def test_empty_question_is_an_honest_error(self):
        assert "ERROR" in rpt._research_plan_tool({"question": "  "})


class TestResearchDiscoverSourcesTool:
    def test_requires_a_real_plan_first(self):
        tool = rpt._build_discover_sources_tool(_registry())
        out = tool.handler({"question": "Is Z real?"})
        assert "ERROR" in out
        assert "research_plan" in out

    def test_real_sources_are_found_and_persisted(self, monkeypatch):
        import dourmouse.dispatch as dispatch_module

        _install_chat_fake(monkeypatch, ["- sub-question"])
        rpt._research_plan_tool({"question": "Is Z real?"})
        monkeypatch.setattr(
            dispatch_module, "run_dispatch_messages",
            lambda messages, registry, **kw: {
                "final_text": "ok",
                "transcript": [{"type": "tool_use", "name": "fetch_url",
                                 "raw_arguments": '{"url": "https://example.com/hit1"}'}],
            },
        )
        tool = rpt._build_discover_sources_tool(_registry())
        out = tool.handler({"question": "Is Z real?"})
        assert "example.com/hit1" in out
        loaded = ResearchStore(rpt.default_db()).load("Is Z real?")
        assert loaded.sources == ("https://example.com/hit1",)


class TestResearchExtractEvidenceTool:
    def test_requires_real_sources_first(self, monkeypatch):
        _install_chat_fake(monkeypatch, ["- sub-question"])
        rpt._research_plan_tool({"question": "Is Z real?"})
        tool = rpt._build_extract_evidence_tool(_registry())
        out = tool.handler({"question": "Is Z real?"})
        assert "ERROR" in out
        assert "research_discover_sources" in out

    def test_a_real_hallucinated_passage_reports_an_honest_error(self, monkeypatch):
        import dourmouse.dispatch as dispatch_module

        _install_chat_fake(monkeypatch, ["- sub-question", "CLAIM: x\nPASSAGE: not in the page\nLOCATION: nowhere"])
        rpt._research_plan_tool({"question": "Is Z real?"})
        monkeypatch.setattr(
            dispatch_module, "run_dispatch_messages",
            lambda messages, registry, **kw: {
                "final_text": "ok",
                "transcript": [{"type": "tool_result", "name": "fetch_url",
                                 "text": "FETCHED https://example.com/hit1 (20 chars):\nreal page content here"}],
            },
        )
        record = ResearchStore(rpt.default_db()).load("Is Z real?")
        record.add_sources(["https://example.com/hit1"])
        ResearchStore(rpt.default_db()).save(record, now=1.0)

        tool = rpt._build_extract_evidence_tool(_registry())
        out = tool.handler({"question": "Is Z real?"})
        assert "ERROR" in out


class TestResearchRunPipelineTool:
    def test_requires_a_real_plan_first(self):
        tool = rpt._build_run_pipeline_tool(_registry())
        out = tool.handler({"question": "Is Z real?"})
        assert "ERROR" in out
        assert "research_plan" in out

    def test_runs_every_sub_question_and_reports_the_real_totals(self, monkeypatch):
        import dourmouse.dispatch as dispatch_module

        _install_chat_fake(monkeypatch, [
            "- sub A\n- sub B",
            "CLAIM: claim A\nPASSAGE: Body A content real.\nLOCATION: whole",
            "CLAIM: claim B\nPASSAGE: Body B content real.\nLOCATION: whole",
        ])
        rpt._research_plan_tool({"question": "Is Z real?"})

        def _dispatch(messages, registry, **kw):
            content = messages[0]["content"]
            if "Research this question for real" in content and "sub A" in content:
                return {"final_text": "ok", "transcript": [
                    {"type": "tool_use", "name": "fetch_url",
                     "raw_arguments": '{"url": "https://a.example/1"}'},
                ]}
            if "Research this question for real" in content and "sub B" in content:
                return {"final_text": "ok", "transcript": [
                    {"type": "tool_use", "name": "fetch_url",
                     "raw_arguments": '{"url": "https://b.example/1"}'},
                ]}
            if "https://a.example/1" in content:
                return {"final_text": "ok", "transcript": [
                    {"type": "tool_result", "name": "fetch_url",
                     "text": "FETCHED https://a.example/1 (20 chars):\nBody A content real."},
                ]}
            return {"final_text": "ok", "transcript": [
                {"type": "tool_result", "name": "fetch_url",
                 "text": "FETCHED https://b.example/1 (20 chars):\nBody B content real."},
            ]}

        monkeypatch.setattr(dispatch_module, "run_dispatch_messages", _dispatch)
        tool = rpt._build_run_pipeline_tool(_registry())
        out = tool.handler({"question": "Is Z real?"})
        assert "Sources: 0 -> 2" in out
        assert "Claims: 0 -> 2" in out
        loaded = ResearchStore(rpt.default_db()).load("Is Z real?")
        assert len(loaded.claims) == 2

    def test_empty_question_is_an_honest_error(self):
        tool = rpt._build_run_pipeline_tool(_registry())
        assert "ERROR" in tool.handler({"question": "  "})


class TestResearchDetectContradictionsTool:
    def test_reports_zero_contradictions_honestly(self, monkeypatch):
        from dourmouse.research_pipeline.core import ResearchRecord

        record = ResearchRecord(question="Is Z real?")
        record.set_plan(["sub-question"])
        record.add_sources(["https://a.com"])
        ResearchStore(rpt.default_db()).save(record, now=1.0)
        out = rpt._research_detect_contradictions_tool({"question": "Is Z real?"})
        assert "No real contradictions" in out


class TestResearchSynthesizeTool:
    def test_a_real_synthesis_is_produced_and_persisted(self, monkeypatch):
        from dourmouse.research_pipeline.core import Claim, ResearchRecord

        record = ResearchRecord(question="Is Z real?")
        record.set_plan(["sub-question"])
        record.add_sources(["https://a.com"])
        record.add_claim(Claim(
            claim="Z is real", source_id="a", url="https://a.com",
            document_hash="h", location="p1", passage="Z is real",
            retrieved_at=1.0, agent="research_info",
        ))
        ResearchStore(rpt.default_db()).save(record, now=1.0)
        _install_chat_fake(monkeypatch, ["Yes, Z is real."])
        out = rpt._research_synthesize_tool({"question": "Is Z real?"})
        assert out == "Yes, Z is real."
        loaded = ResearchStore(rpt.default_db()).load("Is Z real?")
        assert loaded.stage is Stage.SYNTHESIZED


class TestResearchStatusTool:
    def test_no_record_yet_is_an_honest_message(self):
        out = rpt._research_status_tool({"question": "Never asked"})
        assert "No real research record" in out

    def test_a_real_record_reports_its_real_stage(self, monkeypatch):
        _install_chat_fake(monkeypatch, ["- sub-question"])
        rpt._research_plan_tool({"question": "Is Z real?"})
        out = rpt._research_status_tool({"question": "Is Z real?"})
        assert "PLANNED" in out
        assert "1 real sub-question" in out


class TestBuildResearchPipelineSubagent:
    def test_registers_all_real_tools(self):
        sub = rpt.build_research_pipeline_subagent(_registry())
        assert sub.name == "evidence_pipeline"
        names = {t.name for t in sub.tools}
        assert names == {
            "research_plan", "research_discover_sources", "research_extract_evidence",
            "research_run_pipeline", "research_detect_contradictions",
            "research_synthesize", "research_status",
        }
