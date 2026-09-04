"""dourmouse/general_roster.py — the google_workspace agent (v13.7).

Explicit user request: "is there an agent for the google workspace, make
one, for using everything in google workspace, give it access to every
single tool, every agent should be able to use this, all models should be
able to communicate and prompt other agents."

Built as a real, explicitly-nameable roll-up of every genuine Google tool
already registered elsewhere in the roster -- shared BY REFERENCE (the
same real ToolSpec objects mail/docs/scheduling already own), never
redefined, so permission levels and behaviour can never drift between the
two places a tool is reachable from.
"""

from __future__ import annotations

from dourmouse.dispatch import Permission
from dourmouse.general_roster import build_general_registry


class TestGoogleWorkspaceAgentExists:
    def test_it_is_a_real_registered_agent(self):
        registry = build_general_registry()
        assert registry.get_subagent("google_workspace") is not None

    def test_it_owns_the_full_real_google_toolset(self):
        registry = build_general_registry()
        sub = registry.get_subagent("google_workspace")
        names = {t.name for t in sub.tools}
        expected = {
            "gmail_search", "gmail_read", "gmail_send", "gmail_archive",
            "gmail_trash", "gmail_bulk_trash", "gmail_untrash",
            "email_identity_status", "email_own_send",
            "drive_search", "drive_read", "drive_download", "drive_create_doc",
            "sheets_read", "slides_create",
            "list_calendar_events", "propose_time_slots",
            "query_shared_memory",
        }
        assert names == expected

    def test_it_excludes_the_imap_and_atlas_calendar_tools(self):
        """read_inbox is a different, non-Google protocol (any IMAP
        provider); atlas_calendar is ATLAS's own paper-trading calendar.
        Including either would misrepresent what this agent does."""
        registry = build_general_registry()
        sub = registry.get_subagent("google_workspace")
        names = {t.name for t in sub.tools}
        assert "read_inbox" not in names
        assert "atlas_calendar" not in names


class TestToolsAreSharedByReferenceNotDuplicated:
    """The whole safety guarantee rests on this: if google_workspace ever
    held a SEPARATE gmail_send with a different permission level, this
    agent would be a way to bypass the confirmation gate mail's own
    gmail_send enforces."""

    def test_gmail_send_is_the_identical_object_as_on_mail(self):
        registry = build_general_registry()
        mail_tool = next(t for t in registry.get_subagent("mail").tools if t.name == "gmail_send")
        gw_tool = next(t for t in registry.get_subagent("google_workspace").tools if t.name == "gmail_send")
        assert mail_tool is gw_tool

    def test_drive_create_doc_is_the_identical_object_as_on_docs(self):
        registry = build_general_registry()
        docs_tool = next(t for t in registry.get_subagent("docs").tools if t.name == "drive_create_doc")
        gw_tool = next(t for t in registry.get_subagent("google_workspace").tools if t.name == "drive_create_doc")
        assert docs_tool is gw_tool

    def test_every_write_send_or_delete_tool_still_requires_confirmation(self):
        registry = build_general_registry()
        sub = registry.get_subagent("google_workspace")
        gated = {
            "gmail_send", "gmail_archive", "gmail_trash", "gmail_bulk_trash",
            "gmail_untrash", "email_own_send", "drive_create_doc",
        }
        for tool in sub.tools:
            if tool.name in gated:
                assert tool.permission is Permission.REQUIRES_CONFIRMATION, (
                    f"{tool.name} lost its confirmation gate on the roll-up agent"
                )


class TestExistingAgentsAreUnaffected:
    """Adding the roll-up must change nothing about the narrower agents it
    draws from -- zero regression risk to their existing routing/tests."""

    def test_mail_keeps_its_full_original_toolset(self):
        registry = build_general_registry()
        names = {t.name for t in registry.get_subagent("mail").tools}
        for expected in (
            "read_inbox", "gmail_search", "gmail_read", "gmail_send",
            "gmail_archive", "gmail_trash", "gmail_bulk_trash",
            "gmail_untrash", "email_identity_status", "email_own_send",
        ):
            assert expected in names

    def test_docs_keeps_its_full_original_toolset(self):
        registry = build_general_registry()
        names = {t.name for t in registry.get_subagent("docs").tools}
        for expected in ("sheets_read", "drive_create_doc", "slides_create"):
            assert expected in names

    def test_scheduling_keeps_its_full_original_toolset(self):
        registry = build_general_registry()
        names = {t.name for t in registry.get_subagent("scheduling").tools}
        assert "list_calendar_events" in names
        assert "propose_time_slots" in names


class TestReachableEverywhereItNeedsToBe:
    def test_exposed_via_the_mcp_bridge_so_claude_can_call_it_directly(self):
        from dourmouse.mcp_bridge import exposed_tools

        registry = build_general_registry()
        names = {t.name for t in exposed_tools(registry)}
        # The gated tools are exposed too (see test_mcp_bridge.py's own
        # gating tests for the safety proof) -- here we just confirm the
        # roll-up's own tools are among what Claude can see.
        assert "gmail_send" in names
        assert "drive_create_doc" in names

    def test_named_in_the_orchestrators_own_capability_briefing(self):
        from dourmouse.model_context import claude_orchestrator_preamble, reset_cache

        reset_cache()
        try:
            text = claude_orchestrator_preamble()
            assert "google_workspace" in text
        finally:
            reset_cache()

    def test_reachable_as_a_delegate_to_models_target(self):
        """The whole point: any model can prompt this agent by name."""
        from dourmouse.model_delegation import route_for

        # Whatever the route (local, given it touches real private data --
        # see the next test), it must not be silently unroutable.
        assert route_for("google_workspace") in ("ollama", "gemini")

    def test_never_routes_to_the_cloud_model(self):
        """Textbook private data -- Gmail/Drive/Calendar leaving the
        machine to answer a question the user thought was local is exactly
        what the delegation policy's privacy-first split exists to
        prevent."""
        from dourmouse.model_delegation import route_for

        assert route_for("google_workspace", allow_cloud=True) == "ollama"


class TestPlannerDoesNotAutoRouteToTheRollup:
    """Regression test for a real, live-reproduced bug: before this
    exclusion, "create a file called hello.txt inside the workspace" (a
    request with nothing to do with Google) scored google_workspace at 7,
    ahead of dev_coding's 4 -- because the roll-up's 18 tools accumulate
    generic capability-verb credit ("create" matches drive_create_doc/
    slides_create) that the narrower original agents don't."""

    def test_a_generic_create_request_still_routes_to_dev_coding(self):
        from dourmouse.planner import find_agents_for_query

        registry = build_general_registry()
        matches = find_agents_for_query(
            registry, "create a file called hello.txt inside the workspace", limit=6
        )
        assert matches
        assert matches[0]["name"] == "dev_coding"
        assert all(m["name"] != "google_workspace" for m in matches)

    def test_google_workspace_never_appears_in_scored_results_at_all(self):
        from dourmouse.planner import find_agents_for_query

        registry = build_general_registry()
        for query in (
            "search my gmail for invoices",
            "create a new google doc",
            "check my calendar",
            "download a file from drive",
        ):
            matches = find_agents_for_query(registry, query, limit=10)
            assert all(m["name"] != "google_workspace" for m in matches), (
                f"google_workspace appeared in automatic scoring for {query!r}"
            )
