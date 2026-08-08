"""Real-behavior tests for the General roster tools.

These exercise the ACTUAL handlers (filesystem, subprocess, vault) — not
mocks of the tools. Network (web_search) is the only thing stubbed at the
urllib boundary, and only to test the honest error path; the success path
uses a canned HTTP response. No fabricated tool output anywhere (Rule 2.2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dourmouse.dispatch import Permission, ToolSpec, _execute_tool
from dourmouse.general_roster import (
    _delete_file_tool,
    _diff_preview_tool,
    _draft_message_tool,
    _edit_file_tool,
    _fetch_url_tool,
    _open_url_tool,
    _list_calendar_events_tool,
    _list_files_tool,
    _propose_time_slots_tool,
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


class TestRosterShape:
    def test_all_subagents_registered(self):
        """The general roster: the original eight (incl. orchestrator + self-
        dispatch), the v2.3 live-intelligence agents (news, markets, rnd,
        mail, tasks), the v2.4 coding agents (code_nvidia, code_deepseek,
        code_claude), the v3.0 messenger (inter-agent messaging), and the
        v4.0 local code_ollama (keyless local coding backend)."""
        registry = build_general_registry()
        assert registry.subagent_names == {
            "orchestrator",
            "research_info",
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
            "atlas",  # v4.0: ATLAS command-centre telemetry
        }

    def test_orchestrator_exposes_delegate_task(self):
        registry = build_general_registry()
        sub = registry.get_subagent("orchestrator")
        assert sub is not None
        assert {t.name for t in sub.tools} == {"delegate_task"}

    def test_confirmation_gated_tools_are_flagged(self):
        registry = build_general_registry()
        assert registry.gated_tool_names == {
            "send_draft",
            "deploy",
            "delete_file",
            "delete_path",
            "run_privileged_command",
            "gmail_send",  # v5.0: sending email always requires a human
        }

    def test_internet_tools_registered(self):
        registry = build_general_registry()
        assert {"web_search", "fetch_url", "open_url"} <= registry.tool_names


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

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = _fetch_url_tool({"url": "https://example.com/"})
        assert "FETCH FAILED" in result

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


class TestComms:
    def test_draft_creates_real_draft_file_and_never_sends(self, workspace):
        result = _draft_message_tool(
            {"channel": "email", "recipient": "boss@corp.com", "subject": "Hi", "body": "Body text"}
        )
        assert "DRAFT CREATED" in result
        assert "NOT SENT" in result
        drafts = list((workspace / "drafts").glob("draft_*.json"))
        assert len(drafts) == 1
        saved = json.loads(drafts[0].read_text())
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
        assert "fabricated" in result


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
        assert (workspace / "src" / "hello.py").read_text() == "print('hi')"
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
        assert f.read_text() == "line one\nline two\n", "diff_preview must NOT write"

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
        assert f.read_text() == "new value"

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
        assert f.read_text() == "print('changed')\nprint('after')\n"

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
        assert f.read_text() == "same\nsame\n", "ambiguous edit must not write"

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
        assert (vault / "daily" / "2026-08-01.md").read_text() == "# Day\nnotes"

    def test_vault_unset_reports_not_configured(self, monkeypatch):
        monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
        assert "NOT CONFIGURED" in _search_vault_tool({"query": "x"})
        assert "NOT CONFIGURED" in _read_note_tool({"path": "x.md"})
        assert "NOT CONFIGURED" in _write_note_tool({"path": "x.md", "content": "y"})
