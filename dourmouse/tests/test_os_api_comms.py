"""COMMS routes (finding #149): inbox, message, archive, trash, flag, unflag.

Gmail itself is replaced by a small in-memory stand-in for the module's
functions, so what is pinned here is the route: what it validates, what it
refuses, what it reports in the module's own words, and that a change clears
the cache. The new google_services functions are tested against a fake HTTP
layer at the bottom.
"""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from dourmouse import google_services as gs
from dourmouse.general_roster import build_general_registry
from dourmouse.os_api import comms


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.webui import run_server

    comms._cache.clear()
    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)
    comms._cache.clear()


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


ROWS = [
    {"id": "m1aaaa", "from": "GitHub <noreply@github.com>", "subject": "3 new commits", "date": "Fri, 26 Sep 2026 17:02:00 +0000",
     "ts": 1790442120000, "unread": True, "flagged": False, "snippet": "abc"},
    {"id": "m2bbbb", "from": "school@example.edu", "subject": "Checkpoint", "date": "", "ts": 0, "unread": False, "flagged": True, "snippet": ""},
]


@pytest.fixture
def oauth(monkeypatch):
    """A signed-in Google account whose mailbox is ROWS."""
    calls = {"rows": 0, "read": [], "acted": []}

    def rows(q, n):
        calls["rows"] += 1
        return list(ROWS)

    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
    monkeypatch.setattr(gs, "gmail_inbox_rows", rows)
    monkeypatch.setattr(gs, "gmail_read", lambda mid: calls["read"].append(mid) or "FROM: A <a@b.co>\nSUBJECT: Hello\nDATE: today\nBODY:\nline one\nline two")
    monkeypatch.setattr(gs, "gmail_archive", lambda mid: calls["acted"].append(("archive", mid)) or f"GMAIL ARCHIVED: 'x' left the inbox (id {mid}).")
    monkeypatch.setattr(gs, "gmail_trash", lambda mid: calls["acted"].append(("trash", mid)) or f"GMAIL TRASHED: 'x' moved to Trash (id {mid}).")
    monkeypatch.setattr(gs, "gmail_flag", lambda mid, flagged=True: calls["acted"].append(("flag" if flagged else "unflag", mid)) or (f"GMAIL FLAGGED: 'x' is starred (id {mid})." if flagged else f"GMAIL UNFLAGGED: 'x' (id {mid})."))
    return calls


class TestInbox:
    def test_rows_come_back_with_real_label_state_and_a_stated_age(self, server, oauth):
        status, data = call(server, "GET", "/api/os/comms/inbox")
        assert status == 200 and data["state"] == "populated" and data["mode"] == "oauth"
        assert [r["id"] for r in data["rows"]] == ["m1aaaa", "m2bbbb"]
        assert data["rows"][0]["unread"] is True and data["rows"][1]["flagged"] is True
        assert data["cached"] is False and data["cached_at"] > 0

    def test_a_second_read_is_served_from_the_cache_and_says_so(self, server, oauth):
        call(server, "GET", "/api/os/comms/inbox")
        _, again = call(server, "GET", "/api/os/comms/inbox")
        assert again["cached"] is True and oauth["rows"] == 1

    def test_fresh_skips_the_cache(self, server, oauth):
        call(server, "GET", "/api/os/comms/inbox")
        _, again = call(server, "GET", "/api/os/comms/inbox?fresh=1")
        assert again["cached"] is False and oauth["rows"] == 2

    def test_a_search_is_never_cached(self, server, oauth):
        call(server, "GET", "/api/os/comms/inbox?q=from:github")
        call(server, "GET", "/api/os/comms/inbox?q=from:github")
        assert oauth["rows"] == 2

    def test_the_cache_expires_after_the_ttl(self, server, oauth, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GMAIL_INBOX_TTL", "0")
        call(server, "GET", "/api/os/comms/inbox")
        call(server, "GET", "/api/os/comms/inbox")
        assert oauth["rows"] == 2

    def test_an_empty_mailbox_is_an_empty_state_not_an_error(self, server, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        monkeypatch.setattr(gs, "gmail_inbox_rows", lambda q, n: [])
        status, data = call(server, "GET", "/api/os/comms/inbox")
        assert status == 200 and data["state"] == "empty" and data["rows"] == []

    def test_app_password_rows_end_in_uid_and_signed_in_rows_end_in_id(self, server, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        monkeypatch.setattr(gs, "gmail_configured", lambda: True)
        monkeypatch.setattr(gs, "gmail_inbox_rows", lambda q, n: None)
        monkeypatch.setattr(gs, "gmail_search", lambda q, n: "GMAIL SEARCH RESULTS (newest first):\n- [2026-09-26 17:02] from GitHub <n@g.com> | 3 commits (uid 4711)\n- [Fri, 26 Sep 2026 16:41] from a@b.co | Figma (id 18c0ffee12)")
        _, data = call(server, "GET", "/api/os/comms/inbox")
        assert data["mode"] == "imap" and [r["id"] for r in data["rows"]] == ["4711", "18c0ffee12"]
        assert data["rows"][0]["unread"] is None and data["rows"][0]["flagged"] is None, "the text search cannot say, so the row does not claim it"

    def test_not_configured_is_unavailable_with_the_words_it_gave(self, server, monkeypatch):
        def refuse(q, n):
            raise RuntimeError("NOT CONFIGURED: set GOOGLE_GMAIL_USER + GOOGLE_GMAIL_APP_PASSWORD. Nothing was fetched.")

        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        monkeypatch.setattr(gs, "gmail_configured", lambda: False)
        monkeypatch.setattr(gs, "gmail_search", refuse)
        status, data = call(server, "GET", "/api/os/comms/inbox")
        assert status == 200 and data["state"] == "unavailable" and data["mode"] == "none"
        assert data["note"].startswith("NOT CONFIGURED")

    def test_an_expired_google_session_is_unavailable(self, server, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        monkeypatch.setattr(gs, "gmail_inbox_rows", lambda q, n: None)
        monkeypatch.setattr(gs, "gmail_search", lambda q, n: "GOOGLE SEARCH: your Google session is missing or expired. Sign in again at /login.")
        _, data = call(server, "GET", "/api/os/comms/inbox")
        assert data["state"] == "unavailable" and "expired" in data["note"]

    def test_a_google_failure_is_a_502_in_googles_words(self, server, monkeypatch):
        def boom(q, n):
            raise RuntimeError("GOOGLE API 500 on https://gmail.googleapis.com/x: backend error")

        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        monkeypatch.setattr(gs, "gmail_inbox_rows", boom)
        status, data = call(server, "GET", "/api/os/comms/inbox")
        assert status == 502 and data["ok"] is False and "backend error" in data["error"]

    def test_the_search_and_the_limit_are_bounded(self, server, oauth):
        assert call(server, "GET", "/api/os/comms/inbox?q=" + "x" * 201)[0] == 400
        assert call(server, "GET", "/api/os/comms/inbox?limit=abc")[0] == 400

    def test_the_limit_is_clamped_to_fifty(self, server, monkeypatch):
        seen = []
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        monkeypatch.setattr(gs, "gmail_inbox_rows", lambda q, n: seen.append(n) or [])
        call(server, "GET", "/api/os/comms/inbox?limit=9999")
        assert seen == [50]


class TestMessage:
    def test_reads_headers_and_body(self, server, oauth):
        status, data = call(server, "GET", "/api/os/comms/message?id=m1aaaa")
        assert status == 200 and data["subject"] == "Hello" and data["from"] == "A <a@b.co>"
        assert data["body"] == "line one\nline two" and oauth["read"] == ["m1aaaa"]

    @pytest.mark.parametrize("bad", ["../x", "a%20b", "x" * 81, "a/b", "%00", "a;b"])
    def test_a_bad_id_is_refused_before_anything_is_read(self, server, oauth, bad):
        status, data = call(server, "GET", "/api/os/comms/message?id=" + bad)
        assert status == 400 and oauth["read"] == []

    def test_a_missing_id_is_refused(self, server, oauth):
        assert call(server, "GET", "/api/os/comms/message")[0] == 400

    def test_an_error_text_from_the_reader_is_a_502_not_a_message(self, server, monkeypatch):
        monkeypatch.setattr(gs, "gmail_read", lambda mid: "ERROR: gmail_read needs a numeric message uid, got 'zz'.")
        status, data = call(server, "GET", "/api/os/comms/message?id=zzzzzz")
        assert status == 502 and "numeric message uid" in data["error"]


class TestRowChanges:
    @pytest.mark.parametrize("action,expect", [("archive", "archive"), ("trash", "trash"), ("flag", "flag"), ("unflag", "unflag")])
    def test_each_action_calls_its_function_once_with_the_id(self, server, oauth, action, expect):
        status, data = call(server, "POST", f"/api/os/comms/{action}", {"id": "m1aaaa"})
        assert status == 200 and data["action"] == action and oauth["acted"] == [(expect, "m1aaaa")]

    def test_the_owner_click_reports_the_functions_own_sentence(self, server, oauth):
        _, data = call(server, "POST", "/api/os/comms/trash", {"id": "m1aaaa"})
        assert data["message"].startswith("GMAIL TRASHED:")

    @pytest.mark.parametrize("action", ["archive", "trash", "flag", "unflag"])
    def test_a_missing_or_bad_id_is_refused_and_nothing_runs(self, server, oauth, action):
        assert call(server, "POST", f"/api/os/comms/{action}", {})[0] == 400
        assert call(server, "POST", f"/api/os/comms/{action}", {"id": "../../etc"})[0] == 400
        assert call(server, "POST", f"/api/os/comms/{action}", {"id": "a" * 81})[0] == 400
        assert oauth["acted"] == []

    def test_a_refusal_in_the_functions_words_is_a_502_and_says_nothing_changed(self, server, monkeypatch):
        monkeypatch.setattr(gs, "gmail_archive", lambda mid: "NOT CONFIGURED: archiving needs the gmail.modify scope. Nothing was changed.")
        status, data = call(server, "POST", "/api/os/comms/archive", {"id": "m1aaaa"})
        assert status == 502 and data["ok"] is False and "Nothing was changed" in data["error"]

    def test_a_change_clears_the_cache_so_the_next_read_is_live(self, server, oauth):
        call(server, "GET", "/api/os/comms/inbox")
        call(server, "POST", "/api/os/comms/archive", {"id": "m1aaaa"})
        _, after = call(server, "GET", "/api/os/comms/inbox")
        assert after["cached"] is False and oauth["rows"] == 2

    def test_there_is_no_route_that_deletes_or_sends(self):
        from dourmouse.os_api import routes

        comms_routes = [p for _, p in routes() if p.startswith("/api/os/comms/")]
        assert sorted(comms_routes) == sorted(f"/api/os/comms/{n}" for n in ("inbox", "message", "archive", "trash", "flag", "unflag"))

    def test_a_get_on_a_change_route_is_not_served(self, server, oauth):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("GET", "/api/os/comms/archive?id=m1aaaa")
        status = conn.getresponse().status
        conn.close()
        assert status != 200 and oauth["acted"] == []


class TestParsers:
    def test_rows_from_text_are_bounded(self):
        line = "- [2026-09-26 17:02] from " + "a" * 60_000 + " | s (uid 1)"
        big = "\n".join([line] * 2) + "\n" + "- [x] from y | z (uid 9)"
        t = time.perf_counter()
        rows = comms.parse_rows(big)
        assert time.perf_counter() - t < 1.0
        assert [r["id"] for r in rows] == ["9"], "an over-long line is skipped, a good one still parses"

    def test_pathological_text_is_fast(self):
        raw = ("- [" + "]" * 3 + " from " + "|" * 400 + " ") * 400 + "\n"
        raw = (raw * 60)[:200_000]
        t = time.perf_counter()
        comms.parse_rows(raw)
        comms.parse_rows("- [a] from b | " * 13000)
        assert time.perf_counter() - t < 2.0

    def test_message_split(self):
        m = comms.parse_message("FROM: a\nSUBJECT: b\nDATE: c\nBODY:\nx\ny")
        assert m == {"from": "a", "subject": "b", "date": "c", "body": "x\ny"}

    def test_a_body_that_contains_the_word_body_keeps_everything_after_the_first_marker(self):
        m = comms.parse_message("FROM: a\nSUBJECT: b\nDATE: c\nBODY:\nfirst BODY:\nsecond")
        assert m["body"] == "first BODY:\nsecond"


# ---- the two new google_services functions, against a fake HTTP layer ---------------------


@pytest.fixture
def wire(monkeypatch):
    seen = []

    def fake(method, url, token, body=None):
        seen.append({"method": method, "url": url, "body": body})
        if url.endswith("/messages?maxResults=25&labelIds=INBOX") or "/messages?" in url and "format" not in url:
            return {"messages": [{"id": "m1aaaa"}, {"id": "m2bbbb"}, {"id": "../bad"}]}
        if "format=metadata" in url and "Subject&metadataHeaders=From" not in url:
            mid = url.split("/messages/")[1].split("?")[0]
            return {
                "labelIds": ["INBOX", "UNREAD"] if mid == "m1aaaa" else ["INBOX", "STARRED"],
                "internalDate": "1790442120000", "snippet": "hello there",
                "payload": {"headers": [{"name": "From", "value": "A <a@b.co>"}, {"name": "Subject", "value": f"S {mid}"}, {"name": "Date", "value": "Fri, 26 Sep 2026"}]},
            }
        return {"id": "m1aaaa"}

    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
    monkeypatch.setattr(gs, "_http_json", fake)
    return seen


class TestGoogleServices:
    def test_rows_carry_unread_starred_and_the_real_timestamp_in_order(self, wire):
        rows = gs.gmail_inbox_rows("", 25)
        assert [r["id"] for r in rows] == ["m1aaaa", "m2bbbb"], "an id that is not a message id is dropped"
        assert rows[0]["unread"] is True and rows[0]["flagged"] is False
        assert rows[1]["flagged"] is True and rows[1]["unread"] is False
        assert rows[0]["ts"] == 1790442120000 and rows[0]["snippet"] == "hello there"

    def test_an_empty_query_lists_the_inbox_label_and_a_query_does_not(self, wire):
        gs.gmail_inbox_rows("", 25)
        assert "labelIds=INBOX" in wire[0]["url"]
        wire.clear()
        gs.gmail_inbox_rows("from:x", 25)
        assert "labelIds" not in wire[0]["url"] and "q=from%3Ax" in wire[0]["url"]

    def test_no_token_means_none_so_the_caller_falls_back(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        assert gs.gmail_inbox_rows("", 5) is None

    def test_max_results_is_clamped(self, wire):
        gs.gmail_inbox_rows("", 9999)
        assert "maxResults=50" in wire[0]["url"]

    def test_flag_adds_only_the_starred_label(self, wire):
        out = gs.gmail_flag("m1aaaa")
        mut = [c for c in wire if "format" not in c["url"] and c["method"] == "POST"]
        assert out.startswith("GMAIL FLAGGED:")
        assert mut[0]["url"].endswith("/messages/m1aaaa/modify") and mut[0]["body"] == {"addLabelIds": ["STARRED"]}

    def test_unflag_removes_only_the_starred_label(self, wire):
        out = gs.gmail_flag("m1aaaa", False)
        mut = [c for c in wire if "format" not in c["url"] and c["method"] == "POST"]
        assert out.startswith("GMAIL UNFLAGGED:") and mut[0]["body"] == {"removeLabelIds": ["STARRED"]}

    def test_flag_never_deletes_or_moves(self, wire):
        gs.gmail_flag("m1aaaa")
        assert all(c["method"] in ("GET", "POST") for c in wire)
        assert not any("trash" in c["url"] or c["method"] == "DELETE" for c in wire)

    def test_flag_refuses_a_bad_id_without_a_call(self, wire):
        assert gs.gmail_flag("../x").startswith("ERROR:") and wire == []

    def test_flag_says_so_when_nobody_is_signed_in(self, monkeypatch):
        monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
        assert gs.gmail_flag("m1aaaa").startswith("NOT CONFIGURED") or gs.gmail_flag("m1aaaa").startswith("GOOGLE")

    def test_flag_reports_a_google_error_honestly(self, monkeypatch):
        def boom(method, url, token, body=None):
            raise RuntimeError("GOOGLE API 403 on x: insufficientPermissions")

        monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
        monkeypatch.setattr(gs, "_http_json", boom)
        out = gs.gmail_flag("m1aaaa")
        assert "reported honestly" in out and "gmail.modify" in out and not out.startswith("GMAIL FLAGGED")

    def test_flag_is_not_an_agent_tool(self):
        names = {t.name for t in build_general_registry().get_subagent("mail").tools}
        assert "gmail_flag" not in names, "flagging is the owner's click on COMMS, not a model tool"
