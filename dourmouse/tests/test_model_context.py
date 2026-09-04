"""Tests for the capability briefing given to each model."""

from __future__ import annotations

from dourmouse import model_context
from dourmouse.model_context import agent_context, claude_orchestrator_preamble, reset_cache


class TestOrchestratorPreamble:
    """Regression tests for "the models don't know what tools or agents they
    can use". MCP tools are discoverable but not announced, so every turn
    opened with a ToolSearch round trip and sometimes ended with the model
    concluding it had no relevant tool when it plainly did.
    """

    def setup_method(self):
        reset_cache()

    def teardown_method(self):
        reset_cache()

    def test_the_roster_is_read_from_the_real_registry(self):
        """Hardcoding the roster here would reintroduce exactly the drift the
        briefing exists to prevent, so it must name agents that really exist
        and tools they really have."""
        from dourmouse.general_roster import build_general_registry

        text = claude_orchestrator_preamble()
        real = {sa.name for sa in build_general_registry().all_subagents()}
        named = {name for name in real if f"  {name} — " in text}
        # Not every agent has tools, but the great majority must be listed.
        assert len(named) > len(real) * 0.8, (
            f"only {len(named)} of {len(real)} real agents appear in the briefing"
        )

    def test_it_names_the_tools_that_were_actually_being_missed(self):
        text = claude_orchestrator_preamble()
        for tool in ("gmail_search", "drive_search", "delegate_to_models"):
            assert tool in text, f"{tool} is missing from the briefing"

    def test_it_states_the_privacy_routing_rule(self):
        text = claude_orchestrator_preamble().lower()
        assert "privacy-first" in text
        assert "local model" in text

    def test_it_forbids_presenting_a_failure_as_an_answer(self):
        assert "NOT CONFIGURED" in claude_orchestrator_preamble()

    def test_it_is_cached_rather_than_rebuilt_per_session(self):
        first = claude_orchestrator_preamble()
        assert claude_orchestrator_preamble() is first

    def test_a_broken_registry_degrades_to_a_briefing_not_a_crash(self, monkeypatch):
        """A briefing must never take down a chat turn."""
        import dourmouse.general_roster as gr

        def boom():
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(gr, "build_general_registry", boom)
        reset_cache()
        text = claude_orchestrator_preamble()
        assert "orchestrator inside Dourmouse" in text


class TestDelegatedAgentContext:
    def test_it_names_the_specialist(self):
        assert "mail specialist" in agent_context("mail")

    def test_it_handles_an_unnamed_agent(self):
        assert "general assistant" in agent_context(None)

    def test_it_insists_tools_are_real_and_failures_honest(self):
        text = agent_context("markets")
        assert "real" in text
        assert "NOT CONFIGURED" in text


class TestPreambleIsSentOncePerSession:
    def test_stream_claude_prepends_only_on_the_first_turn(self):
        """The CLI holds the conversation via --session-id/--resume, so a
        ~2,300-token briefing on every turn would be paid for every turn and
        tell the model nothing new."""
        import inspect

        from dourmouse import code_backends

        source = inspect.getsource(code_backends.stream_claude)
        assert "claude_orchestrator_preamble" in source
        assert "_first_turn" in source
        assert "session_key not in _CLAUDE_SESSIONS" in source
