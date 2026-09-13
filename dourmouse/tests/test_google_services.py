"""Google Sheets + Drive tests (v5.x) — link-shared access, no network.

Covers the honest contract: real gviz parsing via a stubbed urlopen (no
network in tests), the exact private-sheet / sign-in error messages, and
the roster wiring of the new ``docs`` subagent.
"""

from __future__ import annotations

import json

import pytest

from dourmouse import google_services as gs
from dourmouse.dispatch import Permission
from dourmouse.general_roster import build_general_registry


class TestDriveCreateDoc:
    """v5.27 — the signed-in-user Drive WRITE tool (hermetic: fake token +
    fake REST, no network; the real write path is OAuth + Drive API)."""

    def test_not_configured_without_signed_in_user(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        monkeypatch.setattr(gs, "_oauth_user_needs_reauth", lambda a: None)
        out = gs.drive_create_doc("Report")
        assert out.startswith("NOT CONFIGURED")
        assert "Nothing was created" in out

    def test_happy_path_creates_and_writes(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        created = {}
        patched = []

        def fake_http_json(method, url, token, body=None):
            if method == "POST" and url.endswith("/files"):
                created["name"] = body["name"]
                return {"id": "doc123", "name": body["name"]}
            raise AssertionError(f"unexpected call {method} {url}")

        def fake_http_raw(method, url, token, **kw):
            patched.append((method, url, kw.get("data"), kw.get("content_type")))
            return b"{}"

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        monkeypatch.setattr(gs, "_http_raw", fake_http_raw)
        out = gs.drive_create_doc("Freebuff Report", "Freebuff is...")
        assert "DRIVE DOC CREATED" in out
        assert "doc123" in out
        assert created["name"] == "Freebuff Report"
        assert patched and patched[0][2] == b"Freebuff is..."
        assert patched[0][3].startswith("text/plain")

    def test_403_surfaces_scope_fix(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fake_http_json(method, url, token, body=None):
            raise RuntimeError("GOOGLE API 403 on .../files: insufficient permissions")

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.drive_create_doc("Report")
        assert "403" in out
        assert "GOOGLE_OAUTH_FULL_SCOPES" in out
        assert "Nothing was created" in out


class TestCreateCalendarEvent:
    """Real, confirmed feature gap closed (production-testing sweep,
    2026-09-12): scheduling had list_calendar_events + propose_time_slots
    and NOTHING that actually writes a real event — checked directly, no
    calendar-create function existed anywhere in this module, exposed or
    not. Rule 10's own text said "booking confirmed", which was false:
    there was nothing to confirm. Same real write pattern as
    drive_create_doc/docs_append (hermetic: fake token + fake REST)."""

    def test_not_configured_without_signed_in_user(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        monkeypatch.setattr(gs, "_oauth_user_needs_reauth", lambda a: None)
        out = gs.create_calendar_event("Team sync", "2026-09-15T14:00:00", "2026-09-15T14:30:00")
        assert out.startswith("NOT CONFIGURED")
        assert "Nothing was created" in out

    def test_missing_summary_is_a_clear_error(self):
        out = gs.create_calendar_event("", "2026-09-15T14:00:00", "2026-09-15T14:30:00")
        assert "ERROR" in out
        assert "summary" in out

    def test_missing_times_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        out = gs.create_calendar_event("Team sync", "", "")
        assert "ERROR" in out
        assert "start_iso" in out and "end_iso" in out

    def test_happy_path_creates_a_real_event(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        seen = {}

        def fake_http_json(method, url, token, body=None):
            if method == "POST" and url.endswith("/calendars/primary/events"):
                seen.update(body)
                return {"id": "evt123", "htmlLink": "https://calendar.google.com/event?eid=evt123"}
            raise AssertionError(f"unexpected call {method} {url}")

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.create_calendar_event(
            "Team sync", "2026-09-15T14:00:00", "2026-09-15T14:30:00",
            description="Weekly check-in", timezone_name="Asia/Dubai",
        )
        assert "CALENDAR EVENT CREATED" in out
        assert "evt123" in out
        assert seen["summary"] == "Team sync"
        assert seen["start"] == {"dateTime": "2026-09-15T14:00:00", "timeZone": "Asia/Dubai"}
        assert seen["end"] == {"dateTime": "2026-09-15T14:30:00", "timeZone": "Asia/Dubai"}
        assert seen["description"] == "Weekly check-in"

    def test_no_description_is_omitted_not_sent_blank(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        seen = {}

        def fake_http_json(method, url, token, body=None):
            seen.update(body)
            return {"id": "evt123"}

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        gs.create_calendar_event("Team sync", "2026-09-15T14:00:00", "2026-09-15T14:30:00")
        assert "description" not in seen

    def test_403_surfaces_scope_fix(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fake_http_json(method, url, token, body=None):
            raise RuntimeError("GOOGLE API 403 on .../events: insufficient permissions")

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.create_calendar_event("Team sync", "2026-09-15T14:00:00", "2026-09-15T14:30:00")
        assert "403" in out
        assert "GOOGLE_OAUTH_FULL_SCOPES" in out
        assert "Nothing was created" in out

    def test_registered_on_scheduling_and_google_workspace_gated(self):
        registry = build_general_registry()
        for agent in ("scheduling", "google_workspace"):
            sub = registry.get_subagent(agent)
            spec = next((t for t in sub.tools if t.name == "create_calendar_event"), None)
            assert spec is not None, agent
            assert spec.permission is Permission.REQUIRES_CONFIRMATION


class TestSheetsCreate:
    """Real, confirmed feature gap closed (production-testing sweep,
    2026-09-12): sheets_read is the ONLY Sheets tool that existed, and is
    deliberately keyless/link-shared (the public gviz endpoint) — genuinely
    read-only by design. No write/create path existed at all. Same real
    per-user OAuth write pattern as drive_create_doc/create_calendar_event
    (hermetic: fake token + fake REST), but against a genuinely different
    real Google API (sheets.googleapis.com, not the gviz endpoint)."""

    def test_not_configured_without_signed_in_user(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        monkeypatch.setattr(gs, "_oauth_user_needs_reauth", lambda a: None)
        out = gs.sheets_create("Budget")
        assert out.startswith("NOT CONFIGURED")
        assert "Nothing was created" in out

    def test_missing_title_is_a_clear_error(self):
        assert "ERROR" in gs.sheets_create("")

    def test_happy_path_creates_with_no_rows(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        calls = []

        def fake_http_json(method, url, token, body=None):
            calls.append((method, url, body))
            assert url == gs._SHEETS_API
            return {"spreadsheetId": "sheet123", "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet123"}

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.sheets_create("Budget")
        assert "SHEETS CREATED" in out
        assert "sheet123" in out
        assert len(calls) == 1  # no rows -> no follow-up values.update call

    def test_happy_path_writes_initial_rows(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        calls = []

        def fake_http_json(method, url, token, body=None):
            calls.append((method, url, body))
            if method == "POST":
                return {"spreadsheetId": "sheet123", "spreadsheetUrl": "https://x/sheet123"}
            return {}

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        rows = [["Name", "Score"], ["Alice", 90]]
        out = gs.sheets_create("Budget", rows)
        assert "SHEETS CREATED" in out
        assert "2 row(s) written" in out
        assert len(calls) == 2
        put_method, put_url, put_body = calls[1]
        assert put_method == "PUT"
        assert "sheet123/values/Sheet1!A1" in put_url
        assert put_body == {"values": rows}

    def test_403_surfaces_scope_fix(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fake_http_json(method, url, token, body=None):
            raise RuntimeError("GOOGLE API 403 on .../spreadsheets: insufficient permissions")

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.sheets_create("Budget")
        assert "403" in out
        assert "GOOGLE_OAUTH_FULL_SCOPES" in out
        assert "Nothing was created" in out

    def test_registered_on_docs_and_google_workspace_gated(self):
        registry = build_general_registry()
        for agent in ("docs", "google_workspace"):
            sub = registry.get_subagent(agent)
            spec = next((t for t in sub.tools if t.name == "sheets_create"), None)
            assert spec is not None, agent
            assert spec.permission is Permission.REQUIRES_CONFIRMATION


class TestDriveShare:
    """Real, confirmed feature gap closed (2026-09-12, user-requested while
    asking to share a file with an alt Google account): the function itself
    (drive_share/_drive_share_oauth) already existed, fully implemented
    against the real Drive permissions API, with zero test coverage and zero
    ToolSpec wiring anywhere in general_roster.py — completely unreachable
    by the model despite being real, working code. Same hermetic pattern as
    every other write tool here (fake token + fake REST, no network)."""

    def test_not_configured_without_signed_in_user(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        monkeypatch.setattr(gs, "_oauth_user_needs_reauth", lambda a: None)
        out = gs.drive_share("doc123", "someone@example.com")
        assert out.startswith("NOT CONFIGURED")
        assert "Nothing was shared" in out

    def test_missing_file_id_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        out = gs.drive_share("", "someone@example.com")
        assert out.startswith("ERROR")
        assert "file_id" in out

    def test_malformed_email_is_refused_before_any_network_call(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fail_if_called(*a, **kw):
            raise AssertionError("must not call the network on a malformed email")

        monkeypatch.setattr(gs, "_http_json", fail_if_called)
        out = gs.drive_share("doc123", "not-an-email")
        assert out.startswith("ERROR")
        assert "not a valid email" in out
        assert "Nothing was shared" in out

    def test_invalid_role_is_refused_before_any_network_call(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fail_if_called(*a, **kw):
            raise AssertionError("must not call the network on an invalid role")

        monkeypatch.setattr(gs, "_http_json", fail_if_called)
        out = gs.drive_share("doc123", "someone@example.com", role="owner")
        assert out.startswith("ERROR")
        assert "role" in out
        assert "Nothing was shared" in out

    def test_happy_path_shares_with_default_reader_role(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        calls = []

        def fake_http_json(method, url, token, body=None):
            calls.append((method, url, body))
            assert method == "POST"
            assert "doc123/permissions" in url
            assert "sendNotificationEmail=true" in url
            return {}

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.drive_share("doc123", "someone@example.com")
        assert "DRIVE SHARED" in out
        assert "someone@example.com" in out
        assert "reader" in out
        assert len(calls) == 1
        assert calls[0][2] == {"type": "user", "role": "reader", "emailAddress": "someone@example.com"}

    def test_notify_false_is_passed_through_to_the_real_api_call(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        seen_url = {}

        def fake_http_json(method, url, token, body=None):
            seen_url["url"] = url
            return {}

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.drive_share("doc123", "someone@example.com", role="writer", notify=False)
        assert "DRIVE SHARED" in out
        assert "writer" in out
        assert "no notification sent" in out
        assert "sendNotificationEmail=false" in seen_url["url"]

    def test_404_surfaces_the_drive_file_scope_explanation(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fake_http_json(method, url, token, body=None):
            raise RuntimeError("GOOGLE API 404 on .../permissions: not found")

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.drive_share("doc123", "someone@example.com")
        assert "404" in out
        assert "drive.file scope" in out
        assert "Nothing was shared" in out

    def test_403_surfaces_scope_fix(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fake_http_json(method, url, token, body=None):
            raise RuntimeError("GOOGLE API 403 on .../permissions: insufficient permissions")

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.drive_share("doc123", "someone@example.com")
        assert "403" in out
        assert "GOOGLE_OAUTH_FULL_SCOPES" in out
        assert "Nothing was shared" in out

    def test_registered_on_docs_and_google_workspace_gated(self):
        registry = build_general_registry()
        for agent in ("docs", "google_workspace"):
            sub = registry.get_subagent(agent)
            spec = next((t for t in sub.tools if t.name == "drive_share"), None)
            assert spec is not None, agent
            assert spec.permission is Permission.REQUIRES_CONFIRMATION

    def test_confirm_prompt_names_the_recipient_and_role(self):
        registry = build_general_registry()
        spec = next(t for t in registry.get_subagent("docs").tools if t.name == "drive_share")
        prompt = spec.confirm_prompt({"file_id": "doc123", "email": "someone@example.com", "role": "writer"})
        assert "doc123" in prompt
        assert "someone@example.com" in prompt
        assert "writer" in prompt


class TestDocsAppend:
    """v13.9 — real capability gap fix, live-caught this session:
    drive_create_doc's content write is a full media-upload PATCH (it
    REPLACES the whole body), so there was no way to build a long document
    incrementally across multiple turns. docs_append uses the real Docs
    API's own batchUpdate + insertText(endOfSegmentLocation) mechanism to
    append at the end without touching existing content (hermetic: fake
    token + fake REST, no network)."""

    def test_not_configured_without_signed_in_user(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        monkeypatch.setattr(gs, "_oauth_user_needs_reauth", lambda a: None)
        out = gs.docs_append("doc123", "more text")
        assert out.startswith("NOT CONFIGURED")
        assert "Nothing was appended" in out

    def test_missing_document_id_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        out = gs.docs_append("", "some text")
        assert out.startswith("ERROR")
        assert "document_id" in out

    def test_empty_text_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        out = gs.docs_append("doc123", "   ")
        assert out.startswith("ERROR")
        assert "text" in out

    def test_happy_path_appends_via_batch_update(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        calls = []

        def fake_http_json(method, url, token, body=None):
            calls.append((method, url, body))
            return {"documentId": "doc123"}

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.docs_append("doc123", "Section two: the aftermath.")
        assert "DOCS APPEND OK" in out
        assert "doc123" in out
        assert len(calls) == 1
        method, url, body = calls[0]
        assert method == "POST"
        assert url == "https://docs.googleapis.com/v1/documents/doc123:batchUpdate"
        req = body["requests"][0]["insertText"]
        # endOfSegmentLocation, not a hand-computed index -- this is the
        # real reason it can never mis-index into existing content.
        assert req["endOfSegmentLocation"] == {}
        assert req["text"] == "Section two: the aftermath."

    def test_403_surfaces_scope_fix(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fake_http_json(method, url, token, body=None):
            raise RuntimeError("GOOGLE API 403 on .../batchUpdate: insufficient permissions")

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.docs_append("doc123", "more text")
        assert "403" in out
        assert "GOOGLE_OAUTH_FULL_SCOPES" in out
        assert "Nothing was appended" in out

    def test_404_reports_the_bad_id_honestly(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fake_http_json(method, url, token, body=None):
            raise RuntimeError("GOOGLE API 404 on .../batchUpdate: not found")

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.docs_append("bogus-id", "more text")
        assert "404" in out
        assert "bogus-id" in out
        assert "Nothing was appended" in out

    def test_wired_onto_the_docs_agent_and_gated(self):
        registry = build_general_registry()
        sub = registry.get_subagent("docs")
        spec = next(t for t in sub.tools if t.name == "docs_append")
        assert spec.permission == Permission.REQUIRES_CONFIRMATION
        assert "docs_append" in registry.gated_tool_names

    def test_wired_onto_google_workspace_too(self):
        registry = build_general_registry()
        sub = registry.get_subagent("google_workspace")
        names = {t.name for t in sub.tools}
        assert "docs_append" in names


class TestDriveSearchFileType:
    """v13.8 (real, live-reproduced bug): drive_search's ONLY parameter was
    a freeform text string, unconditionally wrapped as a
    name/fullText-"contains" clause -- a real request to filter by file
    type had no working way to express that, so the model tried passing
    raw Drive query syntax (mimeType='...') straight into the freeform
    field, which got wrapped AGAIN as a literal quoted string inside the
    function's own clause -- doubly-nested quotes that are not valid Drive
    query syntax. Google's real API correctly rejected it with a real 400
    "Invalid Value" (confirmed live against the actual running app, not a
    mock). Fixed with a separate, safely-built file_type clause and
    friendly aliases so the model never has to guess Google's raw mimeType
    strings or smuggle query syntax into the text field."""

    def _capture_q(self, monkeypatch):
        captured = {}

        def fake_http_json(method, url, token):
            import urllib.parse

            captured["q"] = urllib.parse.parse_qs(url.split("?", 1)[1])["q"][0]
            return {"files": []}

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        return captured

    def test_bare_query_unchanged_from_before(self, monkeypatch):
        captured = self._capture_q(monkeypatch)
        gs._drive_search_oauth("tok", "q3 report", 10)
        assert captured["q"] == (
            "trashed = false and (name contains 'q3 report' "
            "or fullText contains 'q3 report')"
        )

    def test_file_type_alias_builds_a_separate_safe_clause(self, monkeypatch):
        captured = self._capture_q(monkeypatch)
        gs._drive_search_oauth("tok", "", 10, file_type="spreadsheet")
        assert captured["q"] == (
            "trashed = false and mimeType = "
            "'application/vnd.google-apps.spreadsheet'"
        )

    def test_file_type_combines_with_a_real_text_query(self, monkeypatch):
        captured = self._capture_q(monkeypatch)
        gs._drive_search_oauth("tok", "budget", 10, file_type="doc")
        assert captured["q"] == (
            "trashed = false and (name contains 'budget' or "
            "fullText contains 'budget') and mimeType = "
            "'application/vnd.google-apps.document'"
        )

    def test_a_literal_mimetype_not_in_the_alias_table_still_works(self, monkeypatch):
        captured = self._capture_q(monkeypatch)
        gs._drive_search_oauth("tok", "", 10, file_type="image/png")
        assert captured["q"] == "trashed = false and mimeType = 'image/png'"

    def test_raw_query_syntax_never_gets_smuggled_into_the_text_clause(self, monkeypatch):
        """The exact live-reproduced shape: query itself holding
        "mimeType='...'" is treated as ordinary literal search TEXT (single
        quotes escaped, never interpreted as Drive syntax) -- this is what
        makes the fix safe, not just what makes file_type work."""
        captured = self._capture_q(monkeypatch)
        gs._drive_search_oauth(
            "tok", "mimeType='application/vnd.google-apps.spreadsheet'", 10
        )
        # The single quotes are escaped by doubling, per the function's own
        # existing (unchanged) contract -- never left as raw Drive syntax.
        assert "''application/vnd.google-apps.spreadsheet''" in captured["q"]
        assert captured["q"].count("mimeType =") == 0

    def test_public_drive_search_threads_file_type_through(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        captured = self._capture_q(monkeypatch)
        gs.drive_search("", 10, file_type="folder")
        assert "application/vnd.google-apps.folder" in captured["q"]


class TestGmailSearchDescriptionWarnsAboutTrashDefault:
    """v13.8 (real, live-reproduced confusion, not a code bug): Gmail's own
    search API excludes Trash/Spam by default, exactly like Gmail's own
    search box in the browser -- gmail_search passes the query straight
    through with no default label filter of its own, so this was always
    real, correct, expected Gmail behavior. Live-reproduced: asked to
    "restore the most recently trashed email," the model searched with
    plain keywords, got a real (accurate, per Gmail's own default
    semantics) "no messages matched," and honestly-but-wrongly concluded
    there was nothing in Trash at all -- a real message WAS there
    (confirmed via a separate follow-up query). Not fixable by changing
    _gmail_search_oauth's real behavior (that would silently change what
    an ordinary "search my inbox" returns for every other case) -- fixed by
    telling the model the real, correct in:trash/in:spam/in:anywhere
    operators up front, the same documentation-level fix as drive_search's
    file_type guidance above."""

    def test_tool_description_names_the_real_gmail_operators(self):
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        spec = registry.lookup("gmail_search")
        assert spec is not None
        assert "in:trash" in spec.description
        assert "in:spam" in spec.description
        assert "Trash and Spam are excluded by default" in spec.description

    def test_roster_wiring_gated(self):
        registry = build_general_registry()
        # v5.27: drive_create_doc lives on the docs agent — the planner
        # routes Drive directives to docs, and the registry forbids the same
        # tool name on two agents, so the write tool is on docs (mail keeps
        # the Drive read tools).
        sub = registry.get_subagent("docs")
        spec = next(t for t in sub.tools if t.name == "drive_create_doc")
        assert spec.permission == Permission.REQUIRES_CONFIRMATION
        assert "drive_create_doc" in registry.gated_tool_names


class TestSlidesCreate:
    """v5.28 — the signed-in-user Slides WRITE tool (hermetic: fake token +
    fake REST, no network; the real write path is OAuth + Slides API)."""

    def test_not_configured_without_signed_in_user(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        monkeypatch.setattr(gs, "_oauth_user_needs_reauth", lambda a: None)
        out = gs.slides_create("Deck")
        assert out.startswith("NOT CONFIGURED")
        assert "Nothing was created" in out

    def test_happy_path_builds_deck(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        calls = []

        def fake_http_json(method, url, token, body=None):
            calls.append((method, url, body))
            if method == "POST" and url.endswith("/presentations"):
                return {
                    "presentationId": "deck1",
                    "slides": [{"objectId": "p1"}],
                }
            return {}

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.slides_create(
            "Dourmouse Overview",
            [{"title": "What is Dourmouse", "body": "A neural agent."},
             {"title": "Capabilities", "body": "Mail, web, code."}],
        )
        assert "SLIDES DECK CREATED" in out
        assert "deck1" in out
        assert "2 slide(s)" in out
        # create + two batchUpdates
        assert calls[0][0] == "POST" and calls[0][1].endswith("/presentations")
        batch = [c for c in calls if ":batchUpdate" in c[1]]
        assert len(batch) == 2
        # First batch deletes the default slide + creates 2 slides.
        #
        # This assertion previously required "insertLayout", and the second
        # required "createTextBox". Neither is a Slides API request type, so
        # the test passed against code the API rejected outright — the deck
        # feature never once worked, and this test is why nobody noticed.
        # Pinning the real names is the point.
        assert batch[0][2]["requests"][0] == {"deleteObject": {"objectId": "p1"}}
        creates = [r for r in batch[0][2]["requests"] if "createSlide" in r]
        assert len(creates) == 2
        assert all(
            r["createSlide"]["slideLayoutReference"]["predefinedLayout"] == "BLANK"
            for r in creates
        )
        # Second batch draws real text boxes: createShape(TEXT_BOX) + insertText.
        shapes = [r for r in batch[1][2]["requests"] if "createShape" in r]
        assert shapes
        assert all(r["createShape"]["shapeType"] == "TEXT_BOX" for r in shapes)
        assert any("insertText" in r for r in batch[1][2]["requests"])
        assert not any("createTextBox" in r for r in batch[1][2]["requests"])

    def test_403_surfaces_scope_fix(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

        def fake_http_json(method, url, token, body=None):
            raise RuntimeError("GOOGLE API 403 on .../presentations: insufficient permissions")

        monkeypatch.setattr(gs, "_http_json", fake_http_json)
        out = gs.slides_create("Deck")
        assert "403" in out
        assert "GOOGLE_OAUTH_FULL_SCOPES" in out
        assert "Nothing was created" in out

    def test_roster_wiring_gated(self):
        registry = build_general_registry()
        sub = registry.get_subagent("docs")
        spec = next(t for t in sub.tools if t.name == "slides_create")
        assert spec.permission == Permission.REQUIRES_CONFIRMATION
        assert "slides_create" in registry.gated_tool_names


class _FakeResp:
    status = 200

    def __init__(self, body: str, ctype: str = "text/javascript; charset=utf-8"):
        self._body = body.encode("utf-8")
        self._ctype = ctype

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body

    @property
    def headers(self):
        class _H:
            def get(self, key, default=""):
                return self._map.get(key, default)
            _map = {"Content-Type": self._ctype}
        return _H()


_GVIZ_OK = """/*O_o*/
google.visualization.Query.setResponse({version:'0.6',reqId:'0',status:'ok',sig:'123',table:{cols:[{id:'A',label:'Item',type:'string'},{id:'B',label:'Amount',type:'number'},{id:'C',label:'Due',type:'date'}],rows:[{c:[{v:'Rent'},{v:1200},{v:Date(2026,8,1,0,0,0)}]},{c:[{v:"Contractor's fee"},{v:340.5},{v:Date(2026,8,15,0,0,0)}]}]}});"""

_GVIZ_EMPTY = """google.visualization.Query.setResponse({version:'0.6',status:'ok',table:{cols:[],rows:[]}});"""

_GVIZ_PRIVATE = """google.visualization.Query.setResponse({version:'0.6',status:'error',errors:[{reason:'userRateLimitExceeded',message:'Sign in required to access this spreadsheet'}]});"""


def _stub(monkeypatch, body: str, ctype: str = "text/javascript; charset=utf-8"):
    monkeypatch.setattr(
        gs.urllib.request, "urlopen",
        lambda req, timeout=10: _FakeResp(body, ctype),
    )


class TestSheetsRead:
    def test_rejects_bad_ids(self):
        for bad in ("", "  ", "../../evil", "a/b", "x" * 300):
            out = gs.sheets_read(bad)
            assert "ERROR" in out and "spreadsheet ID" in out, bad

    def test_parses_real_gviz_shape(self, monkeypatch):
        _stub(monkeypatch, _GVIZ_OK)
        out = gs.sheets_read("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms", "Sheet1", 10, 10)
        assert "Item | Amount | Due" in out
        assert "Rent | 1200 | 2026-8-1" in out
        assert "Contractor's fee | 340.5 | 2026-8-15" in out  # apostrophe survives
        assert "2 rows x 3 cols" in out

    def test_empty_sheet_honest(self, monkeypatch):
        _stub(monkeypatch, _GVIZ_EMPTY)
        out = gs.sheets_read("someid", "Sheet1")
        assert "empty" in out.lower()

    def test_private_sheet_reports_exact_fix(self, monkeypatch):
        _stub(monkeypatch, _GVIZ_PRIVATE)
        out = gs.sheets_read("private_id", "Sheet1")
        assert "SHEETS READ FAILED" in out
        assert "Anyone with the link" in out
        assert "No data was fabricated" in out

    def test_non_gviz_response_honest(self, monkeypatch):
        _stub(monkeypatch, "<html>oops</html>")
        out = gs.sheets_read("someid")
        assert "unexpected response" in out

    def test_http_error_honest(self, monkeypatch):
        class _Err(_FakeResp):
            status = 403
        monkeypatch.setattr(gs.urllib.request, "urlopen", lambda req, timeout=10: _Err("", "text/html"))
        out = gs.sheets_read("someid")
        assert "HTTP 403" in out
        assert "Anyone with the link" in out


class TestDriveDownload:
    def test_rejects_bad_ids(self, tmp_path):
        for bad in ("", "../../evil", "a/b"):
            out = gs.drive_download(bad, str(tmp_path / "f"))
            assert "ERROR" in out and "file ID" in out, bad

    def test_downloads_link_shared_file(self, monkeypatch, tmp_path):
        _stub(monkeypatch, "hello file bytes", "text/plain; charset=utf-8")
        dest = tmp_path / "out.txt"
        out = gs.drive_download("1abcDEF", str(dest))
        assert "DRIVE DOWNLOAD OK" in out
        assert dest.read_text() == "hello file bytes"

    def test_sign_in_page_honest(self, monkeypatch, tmp_path):
        _stub(monkeypatch, "<html>Sign in to continue</html>", "text/html")
        out = gs.drive_download("1abc", str(tmp_path / "f"))
        assert "not link-shared" in out
        assert "Nothing was downloaded" in out
        assert not (tmp_path / "f").exists()

    def test_virus_scan_page_honest(self, monkeypatch, tmp_path):
        _stub(monkeypatch, '<html>confirm=tokenshere virus scan</html>', "text/html")
        out = gs.drive_download("1abc", str(tmp_path / "f"))
        assert "confirm" in out.lower() or "virus" in out.lower()
        assert not (tmp_path / "f").exists()

    def test_network_error_raises_honestly(self, monkeypatch, tmp_path):
        def _boom(req, timeout=10):
            raise OSError("connection refused")
        monkeypatch.setattr(gs.urllib.request, "urlopen", _boom)
        with pytest.raises(RuntimeError, match="NETWORK ERROR"):
            gs.drive_download("1abc", str(tmp_path / "f"))


class TestStatusAndRoster:
    def test_status_reports_sheets_and_drive_capability(self):
        s = gs.status()
        assert "sheets" in s and "drive" in s
        assert "link-shared" in s["sheets"] and "link-shared" in s["drive"]

    def test_email_identity_defaults_to_dourmouse(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_EMAIL_NAME", raising=False)
        assert gs.email_display_name() == "Dourmouse"
        monkeypatch.setenv("DOURMOUSE_EMAIL_NAME", "Adit's Assistant")
        assert gs.email_display_name() == "Adit's Assistant"

    def test_status_includes_identity_when_configured(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "sender@gmail.com")
        monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "1234567890abcdef")
        monkeypatch.delenv("DOURMOUSE_EMAIL_NAME", raising=False)
        s = gs.status()
        assert s["configured"] is True
        assert s["identity"] == "Dourmouse <sender@gmail.com>"

    def test_docs_subagent_registered_with_tools(self):
        registry = build_general_registry()
        docs = registry.get_subagent("docs")
        assert docs is not None
        names = {t.name for t in docs.tools}
        assert {"sheets_read", "drive_download"} <= names

    def test_docs_handler_wires_honest_errors(self):
        registry = build_general_registry()
        docs = registry.get_subagent("docs")
        read_tool = next(t for t in docs.tools if t.name == "sheets_read")
        out = read_tool.handler({"spreadsheet_id": ""})
        assert "ERROR" in out


class TestImapTimeout:
    """v13: a real bug fixed here, live-caught through an actual directive
    against the sibling read_inbox path in live_feeds.py ("summarize my 5
    most recent emails") — imaplib.IMAP4_SSL's own default is
    timeout=None, so an unresponsive IMAP server blocks the socket
    FOREVER; live-observed holding the server's single shared
    session_lock past 110 real seconds with zero result. This module's
    own SMTP send path already passes timeout=30 (smtplib.SMTP_SSL,
    just below); _imap() was the one overlooked spot using the same
    "imap.gmail.com" host that send already treats as needing a real
    bound."""

    def test_imap_connection_passes_a_real_timeout(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "u@gmail.com")
        monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "1234567890abcdef")
        captured: dict = {}
        import imaplib as _imaplib

        class _FakeConn:
            def __init__(self, *a, **k):
                captured["args"] = a
                captured["kwargs"] = k

            def login(self, u, p):
                return ("OK", [b""])

        monkeypatch.setattr(_imaplib, "IMAP4_SSL", _FakeConn)
        gs._imap()
        assert captured["kwargs"].get("timeout") is not None
        assert captured["kwargs"]["timeout"] == 30
