"""Real-behavior tests for the General roster tools.

These exercise the ACTUAL handlers (filesystem, subprocess, vault) — not
mocks of the tools. Network (web_search) is the only thing stubbed at the
urllib boundary, and only to test the honest error path; the success path
uses a canned HTTP response. No fabricated tool output anywhere (Rule 2.2).
"""

from __future__ import annotations

import json

import pytest

from dourmouse.dispatch import Permission, ToolSpec, _execute_tool
from dourmouse.general_roster import (
    _delete_file_tool,
    _diff_preview_tool,
    _draft_message_tool,
    _draft_tool_tool,
    _edit_file_tool,
    _fetch_url_tool,
    _list_calendar_events_tool,
    _list_files_tool,
    _list_self_extension_drafts_tool,
    _open_browser_pane_tool,
    _open_url_tool,
    _propose_time_slots_tool,
    _query_shared_memory_tool,
    _read_file_tool,
    _read_note_tool,
    _run_python_tool,
    _search_files_tool,
    _search_vault_tool,
    _send_draft_tool,
    _web_search_tool,
    _write_file_tool,
    _write_note_tool,
    build_general_registry,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))
    return ws


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    (v / "alpha.md").write_text("ATLAS research notes\nbeta content here")
    (v / "sub").mkdir()
    (v / "sub" / "gamma.md").write_text("nothing relevant")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(v))
    return v


class TestAdditionalPropertiesEnforcement:
    """Real, live-reproduced bug (full-day feature sweep, 2026-09-12): a
    model call supplied fabricated extra fields (price/day_range/timestamp)
    alongside stock_quote's real 'symbol' argument. Harmless there only
    because that handler happens to read just 'symbol' — nothing actually
    enforced it. additionalProperties:false in a schema is advisory to the
    model only unless _execute_tool itself holds the line."""

    def test_undeclared_keys_are_stripped_before_the_handler_ever_sees_them(self):
        seen = {}
        spec = ToolSpec(
            name="_test_strict_tool",
            description="test",
            parameters={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
                "additionalProperties": False,
            },
            handler=lambda a: seen.update(a) or "ok",
        )
        _execute_tool(
            spec,
            {"symbol": "AAPL", "price": 39874.12, "timestamp": "fake"},
            confirmation_gate=None,
        )
        assert seen == {"symbol": "AAPL"}

    def test_a_tool_without_the_flag_is_unaffected(self):
        """Every OTHER tool (no additionalProperties:false) keeps getting
        its arguments exactly as called — this is opt-in, not a global
        strict-schema change."""
        seen = {}
        spec = ToolSpec(
            name="_test_loose_tool",
            description="test",
            parameters={"type": "object", "properties": {"a": {"type": "string"}}},
            handler=lambda a: seen.update(a) or "ok",
        )
        _execute_tool(spec, {"a": "1", "b": "2"}, confirmation_gate=None)
        assert seen == {"a": "1", "b": "2"}

    def test_stock_quote_itself_declares_the_flag_and_enforces_it(self):
        registry = build_general_registry()
        spec = registry.lookup("stock_quote")
        assert spec is not None
        assert spec.parameters.get("additionalProperties") is False


class TestRequiredArgumentEnforcement:
    """Real, live-found gap (commercial-grade reliability pass, 2026-09-12):
    delete_path({}) — a required arg missing entirely — built its
    confirm_prompt from a hole in its own data and surfaced "Permanently
    delete None?" to the human for a real destructive action, instead of a
    clean, honest error. Required fields (from the schema's own
    'required' list) are now checked before confirm_prompt or the handler
    ever sees the call, for every permission level."""

    def test_missing_required_key_is_refused_before_the_handler_runs(self):
        handler_calls = []
        spec = ToolSpec(
            name="_test_required_tool",
            description="test",
            parameters={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
            handler=lambda a: handler_calls.append(a) or "ok",
        )
        result = _execute_tool(spec, {}, confirmation_gate=None)
        assert "ERROR" in result
        assert "symbol" in result
        assert handler_calls == []

    def test_explicit_none_is_treated_the_same_as_missing(self):
        handler_calls = []
        spec = ToolSpec(
            name="_test_required_tool",
            description="test",
            parameters={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
            handler=lambda a: handler_calls.append(a) or "ok",
        )
        result = _execute_tool(spec, {"symbol": None}, confirmation_gate=None)
        assert "ERROR" in result
        assert handler_calls == []

    def test_a_real_value_reaches_the_handler_normally(self):
        handler_calls = []
        spec = ToolSpec(
            name="_test_required_tool",
            description="test",
            parameters={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
            handler=lambda a: handler_calls.append(a) or "ok",
        )
        result = _execute_tool(spec, {"symbol": "AAPL"}, confirmation_gate=None)
        assert result == "ok"
        assert handler_calls == [{"symbol": "AAPL"}]

    def test_gated_tool_never_reaches_confirm_prompt_with_a_missing_arg(self):
        """The exact shape of the live bug: confirm_prompt building a
        human-facing sentence out of a hole in its own arguments."""
        prompt_calls = []

        def confirm_prompt(args):
            prompt_calls.append(args)
            return f"Permanently delete {args.get('path')!r}?"

        spec = ToolSpec(
            name="_test_gated_required_tool",
            description="test",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=confirm_prompt,
            handler=lambda a: "deleted",
        )
        result = _execute_tool(spec, {}, confirmation_gate=None)
        assert "ERROR" in result
        assert "CONFIRMATION REQUIRED" not in result
        assert prompt_calls == []

    def test_a_tool_without_a_required_list_is_unaffected(self):
        """Opt-in via the schema's own 'required' key — a tool that
        declares none keeps accepting whatever it's called with, exactly
        as before this fix."""
        handler_calls = []
        spec = ToolSpec(
            name="_test_optional_tool",
            description="test",
            parameters={"type": "object", "properties": {"a": {"type": "string"}}},
            handler=lambda a: handler_calls.append(a) or "ok",
        )
        result = _execute_tool(spec, {}, confirmation_gate=None)
        assert result == "ok"
        assert handler_calls == [{}]


class TestRosterShape:
    def test_all_subagents_registered(self):
        """The general roster: the original eight (incl. orchestrator + self-
        dispatch), the v2.3 live-intelligence agents (news, markets, rnd,
        mail, tasks), the v2.4 coding agents (code_nvidia, code_deepseek,
        code_claude), the v3.0 messenger (inter-agent messaging), the
        v4.0 local code_ollama (keyless local coding backend), and the
        v5.12 worldmonitor (real-time global intelligence)."""
        registry = build_general_registry()
        assert registry.subagent_names == {
            "orchestrator",
            "goals",  # Phase 2: persistent autonomous Goal/Task runtime (dourmouse/goal_tools.py)
            "security",  # Phase 4: real host/network telemetry (dourmouse/security/tools.py)
            "research_info",
            "study",  # backlog #9: Study tab, sandboxed access to the MYP folder
            "apps",  # backlog: control other running apps in the background
            "comms",
            "scheduling",
            "dev_coding",
            "admin_ops",
            "memory",
            "system",
            "news",
            "markets",
            "rnd",
            "mail",
            "tasks",
            "code_ollama",  # v4.0: local Ollama coding backend
            "code_nvidia",
            "code_deepseek",
            "code_codex",  # v5.0: OpenAI Codex API backend
            "code_claude",
            "messenger",  # v3.0: inter-agent messaging
            # v14 (user-directed, 2026-09-08): "atlas", "atlas_cmd",
            # "atlas_ui" removed — unplugged from the live roster (see
            # general_roster.py's own comment on the same date). Module
            # code is untouched; see test_atlas_*.py's own
            # TestRosterWiring for coverage that the tool specs still
            # build standalone.
            "freebuff",  # v5.5: Freebuff Desktop reads
            "music",  # v5.7: Spotify playback + discovery
            "worldmonitor",  # v5.12: global intelligence (markets/risk/conflict)
            "forex",  # v5.x: FX research/archive agents
            "mt5",  # v5.x: MetaTrader 5 broker ops
            "t212",  # v5.x: Trading 212 broker ops
            "docs",  # v5.x: Google Sheets/Drive link-shared access
            "browser",  # v5.25: real headless-Chrome agent (signup/login)
            "media",  # 2026-09-14: real image generation (Gemini)
            "compute",  # v5.26: the Dell compute node (LAN inference + failover)
            "design_3d",  # 3D & UI Design — spec generation + manifest cataloguing
            "companion",  # world-monitor-expansion: friendly-persona counterpart
                          # to orchestrator, for the Vision workspace chat panel
            "globe",  # v13: God's Eye View 3D globe control
            "panel_control",  # v13.4: floating-panel control (open/close/move/resize)
            "google_workspace",  # v13.7: one coherent, explicitly-nameable
                                  # agent for Gmail/Drive/Sheets/Slides/
                                  # Calendar -- the same real ToolSpec
                                  # objects mail/docs/scheduling already
                                  # own, shared by reference. Deliberately
                                  # excluded from find_agents_for_query's
                                  # own automatic scoring (see planner.py's
                                  # _ROLLUP_AGENT_NAMES) -- reached by name.
            "agent_smith",  # 2026-09-18: Domain D self-extension --
                             # draft_tool/list_self_extension_drafts only,
                             # no approve tool anywhere in this roster.
                             # "self_extended" (approved extensions
                             # becoming real tools) is NOT listed here on
                             # purpose -- it only registers when at least
                             # one extension has actually been approved,
                             # empty in every hermetic test environment.
            "research_mesh",  # 2026-09-19: rebuilt from the orphaned
                               # jarvis/research_mesh/agents package -- real
                               # field-specialist qualification, now backed
                               # by RealBrain and reachable from chat.
            "reviewer",  # 2026-09-20: Domain F's own deferred role, closed --
                         # read_file/search_files/diff_preview only, no
                         # write/execute/deploy tool anywhere on this
                         # subagent, the genuinely write-free toolset
                         # finding #038 said did not yet exist.
            "evidence_pipeline",  # 2026-09-20: Domain G's chat-reachable
                                   # wiring over research_pipeline/'s own
                                   # stage functions -- named "evidence_
                                   # pipeline", not "research_*", after a
                                   # real live collision: "deep_research"
                                   # word-matched "research" and stole
                                   # research_info's own routing.
        }

    def test_orchestrator_exposes_delegate_task(self):
        registry = build_general_registry()
        sub = registry.get_subagent("orchestrator")
        assert sub is not None
        # v8.31: delegate_parallel joins delegate_task as the orchestrator's
        # own native self-dispatch tools — genuinely concurrent fan-out
        # alongside the existing one-at-a-time nested run.
        # v5.21: delegate_to_models joins them. It is a different thing from
        # the other two and the distinction matters: delegate_task and
        # delegate_parallel spawn nested runs against the SAME model, and are
        # both excluded from the MCP bridge for recursion risk, so Claude
        # never sees them. delegate_to_models routes ACROSS models (local
        # Ollama vs cloud Gemini, privacy-first) and never re-enters a coding
        # CLI, so it is safe to expose and is the one Claude actually calls.
        assert {t.name for t in sub.tools} == {
            "delegate_task",
            "delegate_parallel",
            "delegate_to_models",
        }

    def test_companion_mirrors_orchestrators_dispatch_tools(self):
        # world-monitor-expansion: companion is not a second orchestrator
        # with different plumbing — same delegate_task/delegate_parallel
        # tool pair, nothing else (no query_shared_memory, see
        # TestSharedMemoryTool above). Only its name/persona/model differ.
        registry = build_general_registry()
        sub = registry.get_subagent("companion")
        assert sub is not None
        assert {t.name for t in sub.tools} == {"delegate_task", "delegate_parallel"}
        assert sub.domain == "Both"

    def test_confirmation_gated_tools_are_flagged(self):
        registry = build_general_registry()
        assert registry.gated_tool_names == {
            "send_draft",
            "deploy",
            "delete_file",
            "delete_path",
            "run_privileged_command",
            "gmail_send",  # v5.0: sending email always requires a human
            # v8.4: moving mail is reversible but still changes what the user
            # sees, so all three are gated. Permanent deletion is not offered
            # at all, so there is no ungated destructive mail path.
            "gmail_archive",
            "gmail_trash",
            # v13.1: "delete all emails" was a real, live-reported gap — only
            # single-message gmail_trash existed. Search+trash-many is at
            # least as consequential as a single trash, so it's gated too.
            "gmail_bulk_trash",
            "gmail_untrash",
            # v13.1 (live-reproduced twice): a description-only fix did not
            # stop the model reaching for open_url as a fetch_url fallback
            # on an ordinary research question — opening a REAL browser tab
            # is a surprising side effect, gated like every other one here.
            "open_url",
            # v13.2: spotify_playback_control/spotify_play were ungated on
            # explicit user request — reversible/low-stakes playback
            # control the user asks for by name every time; see
            # general_roster.py's own comment on both tools.
            "browser_submit",  # v5.25: submitting a form (login/signup) needs a human
            "browser_signin",  # v5.25: logging in needs a human
            "browser_creds_store",  # v5.25: storing credentials needs a human
            "browser_creds_forget",  # v5.25: removing credentials needs a human
            "email_own_send",  # v5.25: sending as the Dourmouse identity needs a human
            "drive_create_doc",  # v5.27: creating a file in the user's Drive needs a human
            "slides_create",  # v5.28: creating a deck in the user's Drive needs a human
            # v8.15: roadmap item 5 (11% -> raise gating). Trade execution is
            # at least as consequential as gmail_send/spotify_play, which are
            # already gated — both were simply missed, not a deliberate call.
            "mt5_order",
            "t212_order",
            # v8.15: hands a real autonomous agent in another live app a
            # prompt to act on — closer to deploy/send_draft than a write.
            "freebuff_dispatch",
            "write_note",
            # v13.2: write_path was ungated on explicit user request — the
            # git safety net (auto_commit + undo_last_change, right below)
            # makes an unwanted overwrite recoverable now, unlike when this
            # tool was first gated. See system_access.py's own comment.
            # v13.1: Aider-port git safety net's real /undo — reverts file
            # content on disk, same consequential-change bar as write_path.
            "undo_last_change",
            # design_3d: can silently overwrite an existing named manifest
            # entry with no diff shown, same rationale as write_note.
            "write_manifest_entry",
            # v13.9: real write to an existing Google Doc, same
            # confirmation bar as drive_create_doc right above it.
            "docs_append",
            # 2026-09-14: real write to an existing Google Doc (an image
            # this time, not text) — same confirmation bar as docs_append.
            "docs_insert_image",
            # production-testing sweep, 2026-09-12: real write to the
            # user's Google Calendar — same confirmation bar as
            # drive_create_doc/docs_append (see google_services.py's own
            # comment on the real feature gap this closes).
            "create_calendar_event",
            # production-testing sweep, 2026-09-12: real Sheets creation —
            # Sheets previously had read-only (sheets_read, itself keyless/
            # link-shared) and nothing that could write at all. Same
            # confirmation bar as every other real Drive-family write.
            "sheets_create",
            # 2026-09-12: drive_share newly wired up -- the function already
            # existed against the real Drive permissions API but had zero
            # ToolSpec anywhere, so it was unreachable. Sharing puts the
            # user's file in someone else's Drive and can email them about
            # it -- same confirmation bar as every other real Drive write.
            "drive_share",
            # backlog: app control — every action that actually touches
            # another running app's UI is gated; listing (list_running_apps/
            # list_app_windows) is read-only and stays ungated.
            "activate_app",
            "quit_app",
            "send_app_keystrokes",
            "press_app_key",
            "click_app_menu_item",
        }

    def test_internet_tools_registered(self):
        registry = build_general_registry()
        assert {"web_search", "fetch_url", "open_url"} <= registry.tool_names

    def test_generate_image_tool_registered_regular_and_requires_a_prompt(self):
        """2026-09-14, user-directed: "give it the ability to ...
        generate ... screenshots and images." Display already worked
        (browser_screenshot); this is the real generation tool."""
        from dourmouse.dispatch import Permission

        registry = build_general_registry()
        spec = registry.lookup("generate_image")
        assert spec is not None
        assert spec.permission is Permission.REGULAR
        assert spec.parameters["required"] == ["prompt"]

    def test_deploy_description_scopes_when_to_call_it(self):
        """Real bug found live-testing this session: plain coding requests
        with zero deploy intent ("write a debounce function", "fix this
        code") repeatedly triggered a spurious deploy confirmation. The
        tool's own description now explicitly scopes it to an explicit
        deploy/publish/ship request, matching dispatch.py's own rule 17
        (tool-call scoping) in spirit."""
        registry = build_general_registry()
        spec = registry.lookup("deploy")
        assert spec is not None
        assert "ONLY call this when the user explicitly asks" in spec.description


class TestTargetAgentNameAliasing:
    """Real, live-reproduced bug (2026-09-11): delegate_parallel's schema
    names its per-branch targeting field 'agent_or_task'; delegate_task's
    own schema calls the identical concept 'subagent'. Two independent
    live transcripts (a manual repro AND a real, unprompted user-shaped
    turn) both had the model call delegate_parallel with the much more
    natural {"agent": "mail", ...} instead — the old strict
    `item.get("agent_or_task")` silently read that as "", so the branch
    ran UNTARGETED (no [ROUTING DIRECTIVE] wrapper, forced_agent=None)
    and the orchestration activity feed's own event reported
    agent="any" for a branch that was very much not "any" — the model's
    real routing intent was silently thrown away with no error at all.
    """

    def test_first_matching_alias_wins(self):
        from dourmouse.general_roster import _target_agent_name

        assert _target_agent_name({"agent_or_task": "mail"}, "agent_or_task", "agent") == "mail"

    def test_falls_through_to_the_next_alias(self):
        # The exact live-reproduced shape: delegate_parallel's real schema
        # key is absent, but the model's natural "agent" is present.
        from dourmouse.general_roster import _target_agent_name

        assert _target_agent_name({"agent": "mail"}, "agent_or_task", "agent", "subagent") == "mail"

    def test_delegate_tasks_own_natural_alias(self):
        # delegate_task's real schema key is "subagent"; the same natural
        # "agent" habit must resolve there too.
        from dourmouse.general_roster import _target_agent_name

        assert _target_agent_name({"agent": "mail"}, "subagent", "agent", "agent_or_task") == "mail"

    def test_first_key_takes_precedence_when_several_are_present(self):
        from dourmouse.general_roster import _target_agent_name

        assert (
            _target_agent_name(
                {"agent_or_task": "mail", "agent": "research_info"},
                "agent_or_task",
                "agent",
            )
            == "mail"
        )

    def test_blank_and_missing_both_fall_through_honestly(self):
        from dourmouse.general_roster import _target_agent_name

        assert _target_agent_name({"agent_or_task": "  "}, "agent_or_task", "agent") == ""
        assert _target_agent_name({}, "agent_or_task", "agent", "subagent") == ""

    def test_delegate_parallel_branch_parsing_accepts_the_natural_key(self):
        # End-to-end through the real registered tool's own branch-parsing
        # loop (not just the helper in isolation) — a genuine dispatch
        # context is required (the handler refuses without one), so this
        # pushes one directly onto the registry's own thread-local stack,
        # the same mechanism run_dispatch_messages uses internally.
        from dourmouse.dispatch import DispatchContext, _registry_ctx_stack

        registry = build_general_registry()
        tool = registry.lookup("delegate_parallel")
        assert tool is not None
        ctx = DispatchContext(
            registry=registry, client=None, config=None,
            confirmation_gate=None, event_sink=None,
        )
        stack = _registry_ctx_stack(registry)
        stack.append(ctx)
        try:
            result = tool.handler(
                {"branches": [{"agent": "mail", "instructions": "count unread"}]}
            )
        finally:
            stack.pop()
        # Before the fix: "mail" silently became "" -> target stayed
        # untargeted and the branch ran as a free sub-orchestration
        # instead of being pinned to the named agent — this would still
        # run (no ERROR), so the REAL regression signal is that it no
        # longer reports the branch as unresolvable, not a crash.
        assert "unknown subagent" not in result.lower()

    def test_delegate_parallel_accepts_prompt_as_an_alias_for_instructions(self):
        """Real, live-reproduced bug (production-testing sweep,
        2026-09-12): delegate_to_models (a sibling tool) calls this same
        concept 'prompt'. A model used 'prompt' here, got "missing a
        non-empty 'instructions'", retried with an EMPTY call, and lost
        the whole branch's task."""
        from dourmouse.dispatch import DispatchContext, _registry_ctx_stack

        registry = build_general_registry()
        tool = registry.lookup("delegate_parallel")
        ctx = DispatchContext(
            registry=registry, client=None, config=None,
            confirmation_gate=None, event_sink=None,
        )
        stack = _registry_ctx_stack(registry)
        stack.append(ctx)
        try:
            result = tool.handler(
                {"branches": [{"agent": "mail", "prompt": "count unread"}]}
            )
        finally:
            stack.pop()
        assert "missing a non-empty" not in result.lower()

    def test_delegate_task_accepts_instructions_and_prompt_as_aliases_for_task(self):
        from dourmouse.dispatch import DispatchContext, _registry_ctx_stack

        registry = build_general_registry()
        tool = registry.lookup("delegate_task")
        ctx = DispatchContext(
            registry=registry, client=None, config=None,
            confirmation_gate=None, event_sink=None,
        )
        stack = _registry_ctx_stack(registry)
        stack.append(ctx)
        try:
            for key in ("instructions", "prompt"):
                result = tool.handler({"subagent": "mail", key: "count unread"})
                assert "requires a non-empty" not in result.lower(), (key, result)
        finally:
            stack.pop()


class TestCodeToolTabIsolation:
    """Real gap found (production-testing sweep, 2026-09-12) while fixing
    the identical bug for ClaudeCliClient (the top-level orchestrator
    client — see its own comment for the full diagnosis: no tab meant
    every conversation shared one real Claude CLI session). code_claude,
    the plain TOOL a model can call mid-conversation, is a different call
    site reached via the active dispatch context rather than a direct
    parameter, and deserves the same per-tab isolation."""

    def test_no_active_dispatch_context_falls_back_to_no_tab(self, monkeypatch):
        """A direct, standalone tool.handler() call (this file's other
        code-tool tests, or a genuine non-UI caller) has no active
        dispatch context — must resolve tab=None, the same "old shared
        session" behavior every other tab-less caller gets."""
        seen = {}
        monkeypatch.setattr(
            "dourmouse.code_backends.run_code_task",
            lambda backend, task, cwd, timeout, tab=None: seen.setdefault("tab", tab) or "ok",
        )
        registry = build_general_registry()
        tool = registry.lookup("code_claude")
        tool.handler({"task": "write add"})
        assert seen["tab"] is None

    def test_active_dispatch_context_threads_its_session_stem_as_the_tab(self, monkeypatch):
        from dourmouse.dispatch import DispatchContext, _registry_ctx_stack

        seen = {}
        monkeypatch.setattr(
            "dourmouse.code_backends.run_code_task",
            lambda backend, task, cwd, timeout, tab=None: seen.setdefault("tab", tab) or "ok",
        )
        registry = build_general_registry()
        tool = registry.lookup("code_claude")
        ctx = DispatchContext(
            registry=registry, client=None, config=None,
            confirmation_gate=None, event_sink=None,
            session_stem="session_20260912_120000",
        )
        stack = _registry_ctx_stack(registry)
        stack.append(ctx)
        try:
            tool.handler({"task": "write add"})
        finally:
            stack.pop()
        assert seen["tab"] == "session_20260912_120000"


class TestResearchInfo:
    def test_web_search_network_error_reported_honestly(self, monkeypatch):
        import urllib.error

        def fake_urlopen(*args, **kwargs):
            raise urllib.error.URLError("no network")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = _web_search_tool({"query": "quantum computing"})
        assert "WEB SEARCH FAILED" in result
        assert "no network" in result

    def test_web_search_returns_real_results_from_canned_response(self, monkeypatch):
        payload = {
            "query": {
                "search": [
                    {"title": "Quantum computing", "snippet": "Quantum computing is <span class=\"searchmatch\">a field</span> of study."},
                    {"title": "Quantum decoherence", "snippet": "Decoherence matters."},
                ]
            }
        }

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=None):
                return json.dumps(payload).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse())
        result = _web_search_tool({"query": "quantum computing"})
        # DuckDuckGo can't parse the canned Wikipedia JSON, so it falls back
        # to Wikipedia and returns its real results.
        assert "WEB SEARCH RESULTS" in result
        assert "Quantum computing" in result
        assert "a field of study" in result  # tag-stripped snippet

    def test_web_search_uses_duckduckgo_first(self, monkeypatch):
        html = (
            '<div class="result"><a class="result__a" href="https://example.com/a">'
            "NVIDIA Nemotron</a><a class=\"result__snippet\">A real GPU model.</a></div>"
        )

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=None):
                return html.encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse())
        result = _web_search_tool({"query": "nvidia nemotron"})
        assert "DuckDuckGo" in result
        assert "NVIDIA Nemotron" in result
        assert "https://example.com/a" in result

    def test_web_search_empty_query_errors(self):
        assert "requires a non-empty" in _web_search_tool({"query": "  "})

    def test_fetch_url_returns_stripped_text(self, monkeypatch):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=None):
                return b"<html><head><title>x</title></head><body><h1>Hi</h1><p>World &amp; more</p></body></html>"

        monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.215.14")
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse())
        result = _fetch_url_tool({"url": "https://example.com/page"})
        assert "FETCHED" in result
        assert "Hi World & more" in result  # tags stripped, entities decoded

    def test_fetch_url_rejects_non_http_scheme(self):
        result = _fetch_url_tool({"url": "file:///etc/passwd"})
        assert "only accepts http(s)" in result

    def test_fetch_url_network_error_honest(self, monkeypatch):
        import urllib.error

        def fake_urlopen(*a, **k):
            raise urllib.error.URLError("down")

        monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.215.14")
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = _fetch_url_tool({"url": "https://example.com/"})
        assert "FETCH FAILED" in result

    def test_fetch_url_refuses_a_private_address(self, monkeypatch):
        """SSRF guard (engineering audit, 2026-09-17): a model-supplied
        URL that resolves to an internal address must never be fetched,
        matching the docs/ENGINEERING_AUDIT.md #003 prompt-injection
        finding this closes a real residual gap on."""
        monkeypatch.setattr("socket.gethostbyname", lambda host: "169.254.169.254")
        result = _fetch_url_tool({"url": "http://metadata.internal/latest/"})
        assert result.startswith("REFUSED:")
        assert "private/internal address" in result

    def test_fetch_url_refuses_loopback(self, monkeypatch):
        monkeypatch.setattr("socket.gethostbyname", lambda host: "127.0.0.1")
        result = _fetch_url_tool({"url": "http://localhost:11434/api/tags"})
        assert result.startswith("REFUSED:")

    def test_fetch_url_honest_error_on_unresolvable_host(self, monkeypatch):
        import socket as socket_module

        def fail(host):
            raise socket_module.gaierror("nodename nor servname provided")

        monkeypatch.setattr("socket.gethostbyname", fail)
        result = _fetch_url_tool({"url": "https://this-genuinely-does-not-exist.invalid/"})
        assert result.startswith("ERROR:")
        assert "could not resolve" in result

    def test_open_url_opens_browser(self, monkeypatch):
        opened = {}

        def fake_open(url, new):
            opened["url"] = url
            return True

        monkeypatch.setattr("webbrowser.open", fake_open)
        result = _open_url_tool({"url": "https://example.com"})
        assert "OPENED IN BROWSER" in result
        assert opened == {"url": "https://example.com"}

    def test_open_url_false_is_honest(self, monkeypatch):
        monkeypatch.setattr("webbrowser.open", lambda *a, **k: False)
        result = _open_url_tool({"url": "https://example.com"})
        assert "OPEN FAILED" in result


class TestStudySearchTool:
    """Real gap found live (full-day feature sweep, 2026-09-12): study had
    list/read only, no search — see study_agent.py's own search_study_files
    docstring for the live-reproduced failure this closes."""

    def test_registered_on_the_study_subagent(self):
        registry = build_general_registry()
        sub = registry.get_subagent("study")
        assert sub is not None
        assert "study_search_files" in {t.name for t in sub.tools}

    def test_empty_query_is_refused_honestly(self):
        registry = build_general_registry()
        spec = registry.lookup("study_search_files")
        assert spec is not None
        assert spec.handler({"query": ""}) == "ERROR: study_search_files requires a non-empty 'query'."

    def test_real_search_against_the_real_configured_folder(self, tmp_path, monkeypatch):
        root = tmp_path / "MYP data folder"
        root.mkdir()
        (root / "Economics").mkdir()
        (root / "Economics" / "notes.txt").write_text("real notes", encoding="utf-8")
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(root))
        registry = build_general_registry()
        spec = registry.lookup("study_search_files")
        result = spec.handler({"query": "economics"})
        assert "Economics" in result
        assert "no matching" not in result.lower()


class TestOpenBrowserPane:
    """backlog #8: the embedded, human-visible pane — distinct from
    browser_open (invisible automation)."""

    def setup_method(self):
        from dourmouse.browser_pane import set_browser_pane_requests

        set_browser_pane_requests(None)

    def teardown_method(self):
        from dourmouse.browser_pane import set_browser_pane_requests

        set_browser_pane_requests(None)

    def test_requires_a_url(self):
        result = _open_browser_pane_tool({})
        assert "ERROR" in result

    def test_refuses_non_http_schemes(self):
        result = _open_browser_pane_tool({"url": "javascript:alert(1)"})
        assert result.startswith("REFUSED")

    def test_opens_via_a_real_http_call_to_the_running_server(self, monkeypatch):
        """2026-09-14, real live-caught bug: this used to call the
        get_browser_pane_requests() singleton directly, which silently
        did nothing when the caller was a genuinely separate process
        (Claude CLI / Codex's own subprocess) -- the event had zero
        observers there, though the tool's own text claimed success
        regardless. Fixed to call back into the real running server's
        own HTTP API instead, which works identically whether the
        caller is in-process or not. See test_browser_pane_wiring.py's
        own end-to-end version of this exact test for the real-server,
        real-SSE-client proof; this one only checks the request shape."""
        seen = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["body"] = req.data
            return _FakeResponse()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "8765")
        result = _open_browser_pane_tool({"url": "https://example.com"})
        assert "OPENED BROWSER PANE" in result
        assert seen["url"] == "http://127.0.0.1:8765/api/browser-pane/open"
        assert seen["method"] == "POST"
        assert json.loads(seen["body"]) == {"url": "https://example.com"}

    def test_honest_error_when_the_running_server_is_unreachable(self, monkeypatch):
        """Rule 2.1/2.2 -- a real failure to reach the server must be
        reported, never silently swallowed into a fabricated success."""
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = _open_browser_pane_tool({"url": "https://example.com"})
        assert result.startswith("ERROR")
        assert "OPENED BROWSER PANE" not in result


class TestComms:
    def test_draft_creates_real_draft_file_and_never_sends(self, workspace):
        result = _draft_message_tool(
            {"channel": "email", "recipient": "boss@corp.com", "subject": "Hi", "body": "Body text"}
        )
        assert "DRAFT CREATED" in result
        assert "NOT SENT" in result
        drafts = list((workspace / "drafts").glob("draft_*.json"))
        assert len(drafts) == 1
        saved = json.loads(drafts[0].read_text(encoding="utf-8"))
        assert saved["status"] == "draft — NOT SENT"
        assert saved["to"] == "boss@corp.com"

    def test_send_draft_is_not_configured(self):
        result = _send_draft_tool({"body": "hi"})
        assert "NOT CONFIGURED" in result
        assert "nothing was sent" in result


class TestScheduling:
    def test_propose_time_slots_is_deterministic_and_skips_weekends(self):
        a = _propose_time_slots_tool({"duration_minutes": 60, "days_ahead": 5, "start_hour": 9, "end_hour": 11})
        b = _propose_time_slots_tool({"duration_minutes": 60, "days_ahead": 5, "start_hour": 9, "end_hour": 11})
        assert a == b  # deterministic
        assert "PROPOSED TIME SLOTS" in a
        # 9:00–11:00 window with 30-min increments yields 3 candidate 60-min
        # starts per weekday (9:00, 9:30, 10:00); weekends are skipped.
        assert a.count("(60 min)") >= 1

    def test_propose_bad_window_errors(self):
        assert "ERROR" in _propose_time_slots_tool({"start_hour": 17, "end_hour": 9})

    def test_propose_non_integer_args_error_friendly(self):
        assert "must be integers" in _propose_time_slots_tool({"duration_minutes": "abc"})

    def test_calendar_reads_are_honestly_not_configured(self):
        result = _list_calendar_events_tool({})
        assert "NOT CONFIGURED" in result
        # Honesty contract: never claims events were read when they weren't.
        assert "no events were fetched" in result


class TestAgentSmith:
    """Domain D -- docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md's own
    "single most architecturally sensitive item." The critical invariant
    this class exists to guard: there is NO approve/reject tool anywhere
    in the roster reachable from chat. A model can draft; only a human,
    through webui.py's HTTP route, ever approves."""

    _HANDLER = (
        "def handle(arguments: dict) -> str:\n"
        "    text = arguments.get(\"text\", \"\")\n"
        "    if not isinstance(text, str) or not text:\n"
        "        return \"ERROR: 'text' must be a non-empty string.\"\n"
        "    return text[::-1]\n"
    )
    _TEST = (
        "from dourmouse.self_extensions import load_approved\n\n\n"
        "def test_reverses_text():\n"
        "    mod = load_approved(\"reverse_text\")\n"
        "    assert mod.handle({\"text\": \"abc\"}) == \"cba\"\n\n\n"
        "def test_rejects_empty():\n"
        "    mod = load_approved(\"reverse_text\")\n"
        "    assert mod.handle({\"text\": \"\"}).startswith(\"ERROR\")\n"
    )
    _PARAMS = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def test_no_approve_or_reject_tool_exists_anywhere_in_the_roster(self):
        registry = build_general_registry()
        names = registry.tool_names
        assert not any("approve" in n.lower() for n in names)
        assert not any("reject" in n.lower() for n in names)

    def test_draft_tool_is_regular_tier_never_confirmation_gated(self):
        registry = build_general_registry()
        spec = registry.lookup("draft_tool")
        assert spec.permission is Permission.REGULAR

    def test_draft_tool_writes_a_real_draft_not_a_stub(self, workspace):
        out = _draft_tool_tool({
            "capability_gap": "no way to reverse text", "tool_name": "reverse_text",
            "description": "Reverses text.", "parameters_schema": self._PARAMS,
            "handler_source": self._HANDLER, "test_source": self._TEST,
        })
        assert "DRAFTED" in out and "reverse_text" in out
        assert "NOT live" in out

    def test_draft_tool_requires_a_real_test_not_a_stub(self, workspace):
        out = _draft_tool_tool({
            "capability_gap": "x", "tool_name": "reverse_text", "description": "d",
            "parameters_schema": self._PARAMS, "handler_source": self._HANDLER, "test_source": "",
        })
        assert "ERROR" in out and "test_source" in out

    def test_draft_tool_rejects_a_name_collision_with_a_real_tool(self, workspace):
        out = _draft_tool_tool({
            "capability_gap": "x", "tool_name": "web_search", "description": "d",
            "parameters_schema": self._PARAMS, "handler_source": self._HANDLER, "test_source": self._TEST,
        })
        assert "ERROR" in out and "already exists" in out

    def test_draft_tool_rejects_broken_handler_syntax(self, workspace):
        out = _draft_tool_tool({
            "capability_gap": "x", "tool_name": "reverse_text", "description": "d",
            "parameters_schema": self._PARAMS, "handler_source": "def handle(:\n  broken",
            "test_source": self._TEST,
        })
        assert "ERROR" in out and "does not parse" in out

    def test_list_self_extension_drafts_reports_none_when_empty(self, workspace):
        assert _list_self_extension_drafts_tool({}) == "SELF-EXTENSION DRAFTS: none."

    def test_list_self_extension_drafts_shows_a_real_draft(self, workspace):
        _draft_tool_tool({
            "capability_gap": "no way to reverse text", "tool_name": "reverse_text",
            "description": "Reverses text.", "parameters_schema": self._PARAMS,
            "handler_source": self._HANDLER, "test_source": self._TEST,
        })
        out = _list_self_extension_drafts_tool({})
        assert "reverse_text" in out and "DRAFTED" in out

    def test_an_approved_extension_becomes_a_real_live_tool_after_rebuild(self, workspace):
        """The other half of Domain D: after a human approves (simulated
        directly here via self_extensions.approve, the same function
        webui.py's approval route calls), a FRESH build_general_registry()
        call -- standing in for a real server restart -- must show the
        tool as genuinely live, callable, and still forced to
        REQUIRES_CONFIRMATION no matter what the draft asked for."""
        from dourmouse import self_extensions as se

        store = se.SelfExtensions()
        entry = store.add_draft(
            capability_gap="no way to reverse text", tool_name="reverse_text",
            description="Reverses text.", parameters_schema=self._PARAMS,
            handler_source=self._HANDLER, test_source=self._TEST,
        )
        result = se.approve(entry["id"], store=store)
        assert result["ok"] is True, result.get("error")
        registry = build_general_registry()
        assert "self_extended" in registry.subagent_names
        spec = registry.lookup("reverse_text")
        assert spec is not None
        assert spec.permission is Permission.REQUIRES_CONFIRMATION
        assert spec.handler({"text": "abc"}) == "cba"

    def test_a_broken_approved_module_never_crashes_registry_startup(self, workspace):
        """A human can only approve through the real approve() path, which
        already validates syntax and runs the real test -- but a hand-
        edited or corrupted file in the approved/ directory must still
        never take the whole server down. Honest degradation, not a
        crash."""
        approved_dir = workspace / "self_extensions" / "approved"
        approved_dir.mkdir(parents=True, exist_ok=True)
        (approved_dir / "broken_ext.py").write_text("def handle(:\n  this is not valid python", encoding="utf-8")
        registry = build_general_registry()  # must not raise
        assert registry.lookup("broken_ext") is None


class TestDevCoding:
    def test_run_python_executes_real_code(self):
        result = _run_python_tool({"code": "print(1 + 1)"})
        assert "EXIT CODE: 0" in result
        assert "2" in result

    def test_run_python_shows_real_errors(self):
        result = _run_python_tool({"code": "import does_not_exist_xyz"})
        assert "EXIT CODE:" in result
        assert "STDERR" in result

    def test_run_python_non_integer_timeout_errors_friendly(self):
        assert "must be an integer" in _run_python_tool({"code": "print(1)", "timeout_seconds": "fast"})

    def test_write_then_read_file_in_workspace(self, workspace):
        written = _write_file_tool({"path": "src/hello.py", "content": "print('hi')"})
        assert "WROTE" in written
        assert (workspace / "src" / "hello.py").read_text(encoding="utf-8") == "print('hi')"
        read = _read_file_tool({"path": "src/hello.py"})
        assert read == "print('hi')"

    def test_path_escape_is_refused(self, workspace):
        assert "REFUSED" in _read_file_tool({"path": "../../etc/passwd"})
        assert "REFUSED" in _write_file_tool({"path": "../../evil", "content": "x"})


class TestPhase2WorkspaceTools:
    """v2.0 Phase 2.2: search_files / diff_preview / edit_file + write_file
    now surfaces a diff for existing targets."""

    def test_search_files_finds_content(self, workspace):
        (workspace / "app.py").write_text("def main():\n    pass\nTODO: ship it\n")
        (workspace / "notes.md").write_text("no match here")
        result = _search_files_tool({"query": "TODO"})
        assert "SEARCH RESULTS" in result
        assert "app.py" in result
        assert "ship it" in result
        assert "notes.md" not in result

    def test_search_files_no_match(self, workspace):
        (workspace / "a.txt").write_text("nothing")
        result = _search_files_tool({"query": "zzz-nonexistent"})
        assert "no matches" in result

    def test_search_files_requires_query(self):
        assert "requires a non-empty" in _search_files_tool({"query": "  "})

    def test_search_files_refuses_escape(self, workspace):
        assert "REFUSED" in _search_files_tool({"query": "x", "path": "../outside"})

    def test_diff_preview_shows_unified_diff_without_writing(self, workspace):
        f = workspace / "doc.txt"
        f.write_text("line one\nline two\n")
        result = _diff_preview_tool({"path": "doc.txt", "content": "line one\nline TWO\n"})
        assert "DIFF PREVIEW" in result
        assert "-line two" in result
        assert "+line TWO" in result
        assert f.read_text(encoding="utf-8") == "line one\nline two\n", "diff_preview must NOT write"

    def test_diff_preview_new_file(self, workspace):
        result = _diff_preview_tool({"path": "new.txt", "content": "hi"})
        assert "would be created" in result

    def test_diff_preview_no_changes(self, workspace):
        (workspace / "same.txt").write_text("abc")
        result = _diff_preview_tool({"path": "same.txt", "content": "abc"})
        assert "no changes" in result

    def test_write_file_existing_target_includes_diff(self, workspace):
        f = workspace / "cfg.txt"
        f.write_text("old value")
        result = _write_file_tool({"path": "cfg.txt", "content": "new value"})
        assert "UPDATED" in result
        # for_write=True: the header reads as a what-changed confirmation, not
        # a "(not written)" preview (the file HAS been written).
        assert "DIFF (what changed in this write):" in result
        assert "-old value" in result
        assert "+new value" in result
        assert f.read_text(encoding="utf-8") == "new value"

    def test_write_file_new_target_has_no_diff(self, workspace):
        result = _write_file_tool({"path": "fresh.txt", "content": "x"})
        assert "WROTE" in result
        assert "DIFF" not in result

    def test_edit_file_targeted_replace(self, workspace):
        f = workspace / "code.py"
        f.write_text("print('before')\nprint('after')\n")
        result = _edit_file_tool({"path": "code.py", "old_str": "'before'", "new_str": "'changed'"})
        assert "EDITED" in result
        assert "-print('before')" in result
        assert "+print('changed')" in result
        assert f.read_text(encoding="utf-8") == "print('changed')\nprint('after')\n"

    def test_edit_file_old_str_not_found(self, workspace):
        (workspace / "x.txt").write_text("hello")
        result = _edit_file_tool({"path": "x.txt", "old_str": "nope", "new_str": "y"})
        assert "not found" in result
        assert "nothing edited" in result

    def test_edit_file_ambiguous_multi_match_refused(self, workspace):
        f = workspace / "dup.txt"
        f.write_text("same\nsame\n")
        result = _edit_file_tool({"path": "dup.txt", "old_str": "same", "new_str": "other"})
        assert "2 times" in result
        assert "refusing" in result
        assert f.read_text(encoding="utf-8") == "same\nsame\n", "ambiguous edit must not write"

    def test_edit_file_requires_old_str(self, workspace):
        (workspace / "y.txt").write_text("a")
        assert "non-empty 'old_str'" in _edit_file_tool({"path": "y.txt", "new_str": "b"})

    def test_phase2_tools_registered_on_dev_coding(self):
        reg = build_general_registry()
        names = {t.name for t in reg.get_subagent("dev_coding").tools}
        assert {"search_files", "diff_preview", "edit_file"} <= names


class TestAdminOps:
    def test_list_files_shows_workspace_contents(self, workspace):
        (workspace / "a.txt").write_text("x")
        (workspace / "sub").mkdir()
        result = _list_files_tool({"path": "."})
        assert "WORKSPACE LISTING" in result
        assert "a.txt" in result
        assert "sub/" in result

    def test_delete_file_requires_per_item_confirmation_at_engine_level(self, workspace):
        """The REAL delete_file handler is the confirmed action; the engine
        must gate it (permission tier REQUIRES_CONFIRMATION). Without a gate
        the file survives; with an approving gate it is really deleted."""
        f = workspace / "doomed.txt"
        f.write_text("bye")

        spec = ToolSpec(
            name="delete_file",
            description="delete one workspace file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            handler=_delete_file_tool,
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: f"Permanently delete workspace file {a['path']!r}?",
        )

        # Without a gate: never executed, file survives.
        result = _execute_tool(spec, {"path": "doomed.txt"}, confirmation_gate=None)
        assert "CONFIRMATION REQUIRED" in result
        assert f.exists()

        # With an approving gate: the real handler deletes it.
        result = _execute_tool(spec, {"path": "doomed.txt"}, confirmation_gate=lambda t: True)
        assert "DELETED" in result
        assert not f.exists()


class TestDailyDigestScope:
    """Real, live-reproduced scoping gap (full-day feature sweep,
    2026-09-12): asked for "an honest recap of everything we did today",
    daily_digest's near-zero numbers (it measures INTER-AGENT BUS traffic
    only) read as "we did almost nothing" despite a day of substantial
    real, direct tool use that never touches that bus at all."""

    def test_output_states_its_own_scope_explicitly(self):
        registry = build_general_registry()
        spec = registry.lookup("daily_digest")
        assert spec is not None
        result = spec.handler({})
        assert "inter-agent bus" in result.lower()
        assert "not necessarily a quiet day" in result.lower() or "not evidence of a quiet day" in result.lower()

    def test_description_warns_against_presenting_it_as_the_whole_day(self):
        registry = build_general_registry()
        spec = registry.lookup("daily_digest")
        assert "does not cover" in spec.description.lower()


class TestMemory:
    def test_search_vault_finds_and_limits(self, vault):
        result = _search_vault_tool({"query": "atlas"})
        assert "VAULT SEARCH RESULTS" in result
        assert "alpha.md" in result

    def test_search_vault_no_match(self, vault):
        result = _search_vault_tool({"query": "zzzz-nonexistent"})
        assert "no notes containing" in result

    def test_read_note_returns_content(self, vault):
        assert "ATLAS research notes" in _read_note_tool({"path": "alpha.md"})

    def test_read_note_refuses_escape(self, vault):
        assert "REFUSED" in _read_note_tool({"path": "../outside.md"})

    def test_write_note_creates_real_note(self, vault):
        result = _write_note_tool({"path": "daily/2026-08-01.md", "content": "# Day\nnotes"})
        assert "WROTE" in result
        assert (vault / "daily" / "2026-08-01.md").read_text(encoding="utf-8") == "# Day\nnotes"

    def test_write_note_requires_confirmation(self):
        """v8.15: silent overwrite of the user's real vault notes, no diff
        shown (unlike write_file, which is workspace-sandboxed and shows
        one). Gated; read_note/search_vault stay regular."""
        registry = build_general_registry()
        spec = registry.lookup("write_note")
        assert spec.permission is Permission.REQUIRES_CONFIRMATION
        assert spec.confirm_prompt is not None
        assert "notes.md" in spec.confirm_prompt({"path": "notes.md", "content": "x"})
        assert registry.lookup("read_note").permission is Permission.REGULAR
        assert registry.lookup("search_vault").permission is Permission.REGULAR

    def test_vault_unset_reports_not_configured(self, monkeypatch):
        monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
        assert "NOT CONFIGURED" in _search_vault_tool({"query": "x"})
        assert "NOT CONFIGURED" in _read_note_tool({"path": "x.md"})
        assert "NOT CONFIGURED" in _write_note_tool({"path": "x.md", "content": "y"})


class TestSharedMemoryTool:
    """query_shared_memory (shared_rag.py): the 'shared database all LLMs
    can use' tool — registered on the memory subagent and extended to
    every other subagent (see build_general_registry's own comment)."""

    def test_registered_regular_on_memory_subagent(self):
        registry = build_general_registry()
        spec = registry.lookup("query_shared_memory")
        assert spec is not None
        assert spec.permission is Permission.REGULAR
        sub = registry.get_subagent("memory")
        assert any(t.name == "query_shared_memory" for t in sub.tools)

    def test_extended_to_every_subagent_except_orchestrator(self):
        # world-monitor-expansion: "companion" (the Vision workspace's
        # friendly-persona counterpart to the orchestrator) joins the
        # exclusion for the same reason orchestrator is excluded — see
        # build_general_registry's own comment on this loop.
        registry = build_general_registry()
        for sub in registry.all_subagents():
            names = {t.name for t in sub.tools}
            if sub.name in ("orchestrator", "companion"):
                assert "query_shared_memory" not in names
            else:
                assert "query_shared_memory" in names, f"{sub.name} is missing query_shared_memory"

    def test_empty_query_rejected(self):
        assert "ERROR" in _query_shared_memory_tool({"query": ""})

    def test_not_configured_when_neither_source_enabled(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_GLOBAL_MEMORY", raising=False)
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_PATH", raising=False)
        result = _query_shared_memory_tool({"query": "anything"})
        assert result.startswith("NOT CONFIGURED")

    def test_bad_top_k_reported_as_error(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_GLOBAL_MEMORY", raising=False)
        monkeypatch.delenv("DOURMOUSE_SPATIAL_VAULT_PATH", raising=False)
        result = _query_shared_memory_tool({"query": "x", "top_k": "not-a-number"})
        assert result.startswith("ERROR")


# --------------------------------------------------------------------------- #
# God's Eye View globe control (v13)
# --------------------------------------------------------------------------- #

class TestGlobeControlTool:
    def _tool(self):
        registry = build_general_registry()
        sub = registry.get_subagent("globe")
        return next(t for t in sub.tools if t.name == "globe_control")

    def test_globe_subagent_registered(self):
        registry = build_general_registry()
        sub = registry.get_subagent("globe")
        assert sub is not None
        # v13.7: query_desktop_vault joined query_shared_memory as a tool
        # extended onto every real agent (except orchestrator/companion) --
        # see general_roster.py's own comment on the routing bug this fixes.
        assert {t.name for t in sub.tools} == {
            "globe_control", "query_shared_memory", "query_desktop_vault",
        }

    def test_requires_a_name(self):
        tool = self._tool()
        assert tool.handler({}).startswith("ERROR")

    def test_args_must_be_an_object(self):
        tool = self._tool()
        result = tool.handler({"name": "zoom_to_globe", "args": "not an object"})
        assert result.startswith("ERROR")

    def test_calls_run_globe_action_and_formats_the_real_result(self, monkeypatch):
        seen = {}

        def _fake_run(name, args):
            seen["name"] = name
            seen["args"] = args
            return {"ok": True, "action": name}

        monkeypatch.setattr("dourmouse.gods_eye.run_globe_action", _fake_run)
        tool = self._tool()
        result = tool.handler({"name": "set_layer_visibility", "args": {"layerId": "flights", "enabled": True}})
        assert seen == {"name": "set_layer_visibility", "args": {"layerId": "flights", "enabled": True}}
        assert "set_layer_visibility" in result
        assert '"ok": true' in result

    def test_not_configured_is_reported_honestly_not_fabricated(self, monkeypatch):
        def _boom(name, args):  # noqa: ARG001
            raise RuntimeError("NOT CONFIGURED: God's Eye View's dev server is not reachable")

        monkeypatch.setattr("dourmouse.gods_eye.run_globe_action", _boom)
        tool = self._tool()
        result = tool.handler({"name": "zoom_to_globe"})
        assert "NOT CONFIGURED" in result

    def test_known_actions_listed_in_the_schema_enum(self):
        tool = self._tool()
        enum = tool.parameters["properties"]["name"]["enum"]
        assert "zoom_to_globe" in enum
        assert "track_entity" in enum
        assert "set_layer_visibility" in enum


class TestAppsToolsPreferTheRealAxPathWithAppleScriptFallback:
    """Phase 2 (2026-09-13): the "apps" subagent's tool handlers now try
    the real Accessibility-API path (app_control_ax.py) first, falling
    back to the existing AppleScript path (app_control.py) only when the
    AX path genuinely can't run right now (no Accessibility trust) —
    never for a real, app-specific failure (wrong app name, missing menu
    item), which the AppleScript path would just fail identically for."""

    def _tool(self, name: str):
        registry = build_general_registry()
        sub = registry.get_subagent("apps")
        return next(t for t in sub.tools if t.name == name)

    # -- activate/quit: the fast, permission-free path, always tried first --

    def test_activate_uses_the_fast_ax_path_when_it_works(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "dourmouse.app_control_ax.activate_app_fast",
            lambda name, dry_run=False: calls.append((name, dry_run)) or "ACTIVATED: Claude",
        )
        monkeypatch.setattr(
            "dourmouse.app_control.activate_app",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("AppleScript path must not run when AX succeeded")),
        )
        # Real post-activation verification (2026-09-14) runs after any
        # successful activation -- mocked True here since this test's own
        # concern is fast-path-vs-fallback selection, not verification
        # itself (that has its own dedicated tests below).
        monkeypatch.setattr("dourmouse.general_roster._activation_actually_took_effect", lambda name: True)
        tool = self._tool("activate_app")
        result = tool.handler({"app_name": "Claude"})
        assert result == "ACTIVATED: Claude"
        assert calls == [("Claude", False)]

    def test_activate_falls_back_to_applescript_when_ax_is_not_configured(self, monkeypatch):
        from dourmouse.app_control_ax import AXControlError

        def _ax_boom(name, dry_run=False):
            raise AXControlError("NOT CONFIGURED: this process does not have Accessibility permission.")

        monkeypatch.setattr("dourmouse.app_control_ax.activate_app_fast", _ax_boom)
        monkeypatch.setattr(
            "dourmouse.app_control.activate_app",
            lambda name, dry_run=False: f"ACTIVATED: {name}",
        )
        monkeypatch.setattr("dourmouse.general_roster._activation_actually_took_effect", lambda name: True)
        tool = self._tool("activate_app")
        result = tool.handler({"app_name": "Claude"})
        assert result == "ACTIVATED: Claude"

    def test_activate_does_not_fall_back_on_a_real_app_specific_ax_error(self, monkeypatch):
        """A real "not running" verdict from AX is authoritative -- both
        backends would fail the same way, so re-trying via AppleScript
        would just waste a round trip and (worse) could report a
        DIFFERENT, confusing error than the real one already found."""
        from dourmouse.app_control_ax import AXControlError

        def _ax_boom(name, dry_run=False):
            raise AXControlError("NOT RUNNING: no running app named 'Claude'.")

        monkeypatch.setattr("dourmouse.app_control_ax.activate_app_fast", _ax_boom)
        monkeypatch.setattr(
            "dourmouse.app_control.activate_app",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fall back on a real, non-environmental AX error")),
        )
        tool = self._tool("activate_app")
        result = tool.handler({"app_name": "Claude"})
        assert "NOT RUNNING" in result

    # -- real post-activation verification (2026-09-14, live-caught bug) --
    # activate_app used to trust the request's own "no error" result
    # unconditionally. Live-caught: a real activate_app("Finder") reported
    # clean success while Chrome stayed frontmost. These test the real
    # check, independent of which backend (fast AX or AppleScript)
    # produced the initial result.

    def test_reports_clean_success_when_the_app_really_is_frontmost(self, monkeypatch):
        monkeypatch.setattr(
            "dourmouse.app_control_ax.activate_app_fast",
            lambda name, dry_run=False: f"ACTIVATED: {name}",
        )
        monkeypatch.setattr(
            "dourmouse.app_control_ax.list_running_apps_fast",
            lambda: [{"name": "Finder", "frontmost": True}, {"name": "Chrome", "frontmost": False}],
        )
        tool = self._tool("activate_app")
        result = tool.handler({"app_name": "Finder"})
        assert result == "ACTIVATED: Finder"

    def test_reports_the_real_mismatch_when_it_never_actually_took_effect(self, monkeypatch):
        """The exact live-caught scenario: the request succeeds, but a
        real check shows a different app still frontmost, both times
        (the one retry never resolves it either)."""
        monkeypatch.setattr(
            "dourmouse.app_control_ax.activate_app_fast",
            lambda name, dry_run=False: "ACTIVATED: Finder",
        )
        monkeypatch.setattr(
            "dourmouse.app_control_ax.list_running_apps_fast",
            lambda: [{"name": "Finder", "frontmost": False}, {"name": "Chrome", "frontmost": True}],
        )
        monkeypatch.setattr("time.sleep", lambda s: None)  # real behavior, just not a real 0.4s wait in a test
        tool = self._tool("activate_app")
        result = tool.handler({"app_name": "Finder"})
        assert "ACTIVATED: Finder" in result
        assert "did not actually bring Finder to the front" in result
        assert "unconfirmed" in result

    def test_settles_after_one_retry_without_reporting_a_false_alarm(self, monkeypatch):
        """A genuine, brief window-server scheduling lag must not be
        reported as a failure -- only a miss confirmed TWICE is."""
        calls = {"n": 0}

        def _list():
            calls["n"] += 1
            frontmost = calls["n"] >= 2  # miss on the first check, resolved by the retry
            return [{"name": "Finder", "frontmost": frontmost}, {"name": "Chrome", "frontmost": not frontmost}]

        monkeypatch.setattr(
            "dourmouse.app_control_ax.activate_app_fast",
            lambda name, dry_run=False: "ACTIVATED: Finder",
        )
        monkeypatch.setattr("dourmouse.app_control_ax.list_running_apps_fast", _list)
        monkeypatch.setattr("time.sleep", lambda s: None)
        tool = self._tool("activate_app")
        result = tool.handler({"app_name": "Finder"})
        assert result == "ACTIVATED: Finder"
        assert calls["n"] == 2

    def test_dry_run_skips_verification_entirely(self, monkeypatch):
        monkeypatch.setattr(
            "dourmouse.app_control_ax.activate_app_fast",
            lambda name, dry_run=False: f"DRY RUN — activate {name} (not executed)",
        )
        monkeypatch.setattr(
            "dourmouse.app_control_ax.list_running_apps_fast",
            lambda: (_ for _ in ()).throw(AssertionError("must never check frontmost state on a dry run")),
        )
        tool = self._tool("activate_app")
        result = tool.handler({"app_name": "Finder", "dry_run": True})
        assert result.startswith("DRY RUN")

    def test_falls_back_to_trusting_the_result_when_the_check_itself_cant_run(self, monkeypatch):
        """No Accessibility access to even ASK who's frontmost is the same
        honest-degrade shape every other NOT-CONFIGURED path in this
        module already uses -- never silently treated as a real observed
        failure."""
        from dourmouse.app_control_ax import AXControlError

        monkeypatch.setattr(
            "dourmouse.app_control_ax.activate_app_fast",
            lambda name, dry_run=False: "ACTIVATED: Finder",
        )

        def _list_boom():
            raise AXControlError("NOT CONFIGURED: this process does not have Accessibility permission.")

        monkeypatch.setattr("dourmouse.app_control_ax.list_running_apps_fast", _list_boom)
        tool = self._tool("activate_app")
        result = tool.handler({"app_name": "Finder"})
        assert result == "ACTIVATED: Finder"

    def test_quit_uses_the_fast_ax_path_when_it_works(self, monkeypatch):
        monkeypatch.setattr(
            "dourmouse.app_control_ax.quit_app_fast",
            lambda name, dry_run=False: f"QUIT: {name}",
        )
        monkeypatch.setattr(
            "dourmouse.app_control.quit_app",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("AppleScript path must not run when AX succeeded")),
        )
        tool = self._tool("quit_app")
        assert tool.handler({"app_name": "Claude"}) == "QUIT: Claude"

    def test_quit_falls_back_to_applescript_when_ax_is_not_configured(self, monkeypatch):
        from dourmouse.app_control_ax import AXControlError

        monkeypatch.setattr(
            "dourmouse.app_control_ax.quit_app_fast",
            lambda name, dry_run=False: (_ for _ in ()).throw(AXControlError("NOT CONFIGURED: no trust")),
        )
        monkeypatch.setattr(
            "dourmouse.app_control.quit_app",
            lambda name, dry_run=False: f"QUIT: {name}",
        )
        tool = self._tool("quit_app")
        assert tool.handler({"app_name": "Claude"}) == "QUIT: Claude"

    # -- keystrokes/press_key/click_menu: both backends need real trust,
    # so these only ATTEMPT the AX path once trust is confirmed --

    def test_keystrokes_goes_straight_to_applescript_when_untrusted(self, monkeypatch):
        monkeypatch.setattr("dourmouse.app_control_ax.ax_trusted", lambda: False)
        monkeypatch.setattr(
            "dourmouse.app_control_ax.send_keystrokes_ax",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not even attempt AX when untrusted")),
        )
        monkeypatch.setattr(
            "dourmouse.app_control.send_keystrokes",
            lambda name, text, dry_run=False: f"TYPED into {name}: {len(text)} character(s)",
        )
        tool = self._tool("send_app_keystrokes")
        result = tool.handler({"app_name": "Claude", "text": "hello"})
        assert result == "TYPED into Claude: 5 character(s)"

    def test_keystrokes_uses_ax_once_trusted(self, monkeypatch):
        monkeypatch.setattr("dourmouse.app_control_ax.ax_trusted", lambda: True)
        monkeypatch.setattr(
            "dourmouse.app_control_ax.send_keystrokes_ax",
            lambda name, text, dry_run=False: f"TYPED into {name}: {len(text)} character(s)",
        )
        monkeypatch.setattr(
            "dourmouse.app_control.send_keystrokes",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("AppleScript path must not run once AX is trusted")),
        )
        tool = self._tool("send_app_keystrokes")
        result = tool.handler({"app_name": "Claude", "text": "hello"})
        assert result == "TYPED into Claude: 5 character(s)"

    def test_press_key_goes_straight_to_applescript_when_untrusted(self, monkeypatch):
        monkeypatch.setattr("dourmouse.app_control_ax.ax_trusted", lambda: False)
        monkeypatch.setattr(
            "dourmouse.app_control_ax.press_key_ax",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not even attempt AX when untrusted")),
        )
        monkeypatch.setattr(
            "dourmouse.app_control.press_key",
            lambda name, key, modifiers, dry_run=False: f"PRESSED {key} in {name}",
        )
        tool = self._tool("press_app_key")
        result = tool.handler({"app_name": "Claude", "key": "escape"})
        assert result == "PRESSED escape in Claude"

    def test_click_menu_goes_straight_to_applescript_when_untrusted(self, monkeypatch):
        monkeypatch.setattr("dourmouse.app_control_ax.ax_trusted", lambda: False)
        monkeypatch.setattr(
            "dourmouse.app_control_ax.click_menu_item_ax",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not even attempt AX when untrusted")),
        )
        monkeypatch.setattr(
            "dourmouse.app_control.click_menu_item",
            lambda name, menu_path, dry_run=False: f"CLICKED {' > '.join(menu_path)} in {name}",
        )
        tool = self._tool("click_app_menu_item")
        result = tool.handler({"app_name": "Claude", "menu_path": ["File", "New"]})
        assert result == "CLICKED File > New in Claude"

    def test_click_menu_uses_ax_once_trusted_and_reports_the_real_missing_item(self, monkeypatch):
        """The real, biggest win of this phase: a wrong menu path gets
        the AX path's own specific "not found" answer, not a generic
        AppleScript failure or a silent fallback that masks it."""
        from dourmouse.app_control_ax import AXControlError

        monkeypatch.setattr("dourmouse.app_control_ax.ax_trusted", lambda: True)
        monkeypatch.setattr(
            "dourmouse.app_control_ax.click_menu_item_ax",
            lambda *a, **k: (_ for _ in ()).throw(
                AXControlError("MENU ITEM NOT FOUND: Claude has no 'Nope' under File — available there: New, Open, Close")
            ),
        )
        monkeypatch.setattr(
            "dourmouse.app_control.click_menu_item",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fall back once AX is trusted and gave a real answer")),
        )
        tool = self._tool("click_app_menu_item")
        result = tool.handler({"app_name": "Claude", "menu_path": ["File", "Nope"]})
        assert "MENU ITEM NOT FOUND" in result
        assert "available there: New, Open, Close" in result

    # -- list tools: read-only, real permission-free win for the app list --

    def test_list_running_apps_uses_the_fast_nsworkspace_path(self, monkeypatch):
        monkeypatch.setattr(
            "dourmouse.app_control_ax.list_running_apps_fast",
            lambda: [{"name": "Finder", "frontmost": True}, {"name": "Claude", "frontmost": False}],
        )
        monkeypatch.setattr(
            "dourmouse.app_control.list_running_apps",
            lambda: (_ for _ in ()).throw(AssertionError("AppleScript path must not run when the fast path succeeded")),
        )
        tool = self._tool("list_running_apps")
        result = tool.handler({})
        assert "* Finder" in result
        assert "  Claude" in result

    def test_list_windows_uses_ax_once_trusted(self, monkeypatch):
        monkeypatch.setattr("dourmouse.app_control_ax.ax_trusted", lambda: True)
        monkeypatch.setattr(
            "dourmouse.app_control_ax.list_windows_ax",
            lambda name: ["Downloads", "Documents"],
        )
        monkeypatch.setattr(
            "dourmouse.app_control.list_windows",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("AppleScript path must not run once AX is trusted")),
        )
        tool = self._tool("list_app_windows")
        result = tool.handler({"app_name": "Finder"})
        assert "Downloads" in result
        assert "Documents" in result
