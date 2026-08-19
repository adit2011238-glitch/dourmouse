"""Tests for dourmouse/atlas_generator.py (v8.16 Phase 2 — autonomous idea
generator). Workspace isolation is automatic (conftest.py's autouse
_workspace_isolated fixture), same as test_atlas_proposals.py — this
module shares atlas_proposals' persistent store.
"""

from __future__ import annotations

import json

import pytest

from dourmouse import atlas_generator as gen
from dourmouse import atlas_proposals as ap
from dourmouse.tests.test_webui import server  # noqa: F401 — shared server fixture


def _mock_llm_router(idea_text="buy EURUSD on Mondays", code=None):
    """A single _llm_chat replacement that answers BOTH roles this feature
    uses — the generator's own idea-writing call AND propose_from_idea's
    codegen call — by inspecting which system prompt it was given. Proves
    generate_and_propose() genuinely exercises both real code paths, not
    just the generator's own half."""
    code = code or "def run(load, params):\n    return {'mean_return': 0.0, 'std_dev': 0.0, 'n_obs': 0}\n"

    def _router(prompt, system=""):
        if "quantitative strategy researcher" in system:
            return idea_text
        return json.dumps({
            "strategy_name": "Generated Strategy",
            "explanation": "test explanation",
            "params": {},
            "code": code,
        })
    return _router


class TestSummarizeHistory:
    def test_empty_history_is_honest(self):
        assert "no past proposals" in gen._summarize_history()

    def test_includes_strategy_names_and_status(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router())
        ap.propose_from_idea("an idea", source="chat")
        summary = gen._summarize_history()
        assert "Generated Strategy" in summary
        assert "chat" in summary
        assert "pending" in summary

    def test_includes_reviewer_note_on_rejection(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router())
        p = ap.propose_from_idea("an idea", source="chat")
        ap.reject_proposal(p["id"], "too similar to an existing one")
        summary = gen._summarize_history()
        assert "too similar to an existing one" in summary

    def test_includes_run_verdict_and_explanation(self, monkeypatch):
        if not ap.sandbox_available():
            pytest.skip("sandbox-exec not available on this machine")
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router())
        monkeypatch.setattr(ap, "_explain_run", lambda pid, run: "explained honestly here")
        p = ap.propose_from_idea("an idea", source="chat")
        ap.approve_and_run(p["id"], target="local")
        summary = gen._summarize_history()
        assert "NO DATA" in summary
        assert "explained honestly here" in summary

    def test_caps_history_depth(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router())
        for i in range(gen._HISTORY_DEPTH + 5):
            ap.propose_from_idea(f"idea {i}", source="chat")
        summary = gen._summarize_history()
        assert summary.count("Generated Strategy") == gen._HISTORY_DEPTH


class TestGenerateOneIdea:
    def test_returns_llm_text(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router(idea_text="short USDJPY on CPI beats"))
        idea = gen.generate_one_idea()
        assert idea == "short USDJPY on CPI beats"

    def test_empty_response_raises(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", lambda prompt, system="": "   ")
        with pytest.raises(RuntimeError, match="empty idea"):
            gen.generate_one_idea()


class TestPendingGeneratedCount:
    def test_zero_when_empty(self):
        assert gen._pending_generated_count() == 0

    def test_counts_only_pending_generator_source(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router())
        ap.propose_from_idea("chat idea", source="chat")
        ap.propose_from_idea("gen idea 1", source="generator")
        p2 = ap.propose_from_idea("gen idea 2", source="generator")
        ap.reject_proposal(p2["id"], "no")
        # 1 chat (wrong source) + 1 generator-but-rejected (wrong status)
        # must NOT count; only the one still-pending generator proposal.
        assert gen._pending_generated_count() == 1


class TestGenerateAndPropose:
    def test_happy_path_creates_generator_sourced_proposal(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router(idea_text="a fresh idea"))
        proposal = gen.generate_and_propose()
        assert proposal is not None
        assert proposal["source"] == "generator"
        assert proposal["prompt"] == "a fresh idea"
        assert proposal["status"] == "pending"

    def test_skips_when_queue_full(self, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router())
        monkeypatch.setattr(gen, "_MAX_PENDING_GENERATED", 2)
        gen.generate_and_propose()
        gen.generate_and_propose()
        assert gen._pending_generated_count() == 2
        result = gen.generate_and_propose()
        assert result is None
        assert gen._pending_generated_count() == 2  # unchanged — genuinely skipped

    def test_unsafe_generated_code_still_goes_through_the_real_gate(self, monkeypatch):
        """The generator doesn't get a shortcut around the safety check —
        proves generate_and_propose() calls the real propose_from_idea,
        not a bypassed variant."""
        monkeypatch.setattr(
            ap, "_llm_chat",
            _mock_llm_router(code="import os\ndef run(load, params):\n    return {}\n"),
        )
        proposal = gen.generate_and_propose()
        assert proposal["status"] == "rejected_unsafe"


class TestWebRoutes:
    def _get(self, server, path):
        import http.client
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def _post(self, server, path, body):
        import http.client
        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_status_route(self, server):
        status, data = self._get(server, "/api/atlas-lab/generator/status")
        assert status == 200
        assert data["ok"] is True
        assert "interval_seconds" in data
        assert "max_pending" in data
        assert data["pending_generated_count"] == 0

    def test_run_now_creates_proposal(self, server, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router(idea_text="route-triggered idea"))
        status, data = self._post(server, "/api/atlas-lab/generator/run-now", {})
        assert status == 200, data
        assert data["ok"] is True
        assert data["skipped"] is False
        assert data["proposal"]["prompt"] == "route-triggered idea"
        assert data["proposal"]["source"] == "generator"

    def test_run_now_reports_skip_honestly(self, server, monkeypatch):
        monkeypatch.setattr(ap, "_llm_chat", _mock_llm_router())
        monkeypatch.setattr(gen, "_MAX_PENDING_GENERATED", 1)
        self._post(server, "/api/atlas-lab/generator/run-now", {})
        status, data = self._post(server, "/api/atlas-lab/generator/run-now", {})
        assert status == 200
        assert data["ok"] is True
        assert data["skipped"] is True
        assert "reason" in data
