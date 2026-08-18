"""Archive / trash / untrash — reversible by construction.

The design constraint these pin: Dourmouse can move mail out of sight, and
can put it back, but cannot destroy it. Gmail's DELETE endpoint bypasses
Trash with no recovery, so it is never called and no tool exposes it.
"""

from __future__ import annotations

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse import google_services as gs
from dourmouse.dispatch import Permission

MID = "1a0060b6ad85118b"


@pytest.fixture
def calls(monkeypatch):
    """Capture Gmail API calls; serve canned metadata for the description."""
    seen: list[dict] = []

    def fake_http_json(method, url, token, body=None):
        seen.append({"method": method, "url": url, "body": body})
        if "format=metadata" in url:
            return {"payload": {"headers": [
                {"name": "Subject", "value": "Quarterly numbers"},
                {"name": "From", "value": "finance@example.com"},
            ]}}
        return {"id": MID}

    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
    monkeypatch.setattr(gs, "_http_json", fake_http_json)
    return seen


def mutations(calls):
    return [c for c in calls if "format=metadata" not in c["url"]]


# --------------------------------------------------------------------------- #
# the safety invariant
# --------------------------------------------------------------------------- #

def test_no_tool_exposes_permanent_deletion():
    names = {t.name for t in build_general_registry().get_subagent("mail").tools}
    for banned in ("gmail_delete", "gmail_destroy", "gmail_purge", "gmail_empty_trash"):
        assert banned not in names


@pytest.mark.parametrize("fn", [gs.gmail_archive, gs.gmail_trash, gs.gmail_untrash])
def test_never_issues_an_http_delete(calls, fn):
    fn(MID)
    assert all(c["method"] == "POST" for c in mutations(calls))
    assert not any(c["method"] == "DELETE" for c in calls)


def test_trash_uses_the_recoverable_endpoint(calls):
    """/trash keeps the 30-day window; DELETE would not."""
    gs.gmail_trash(MID)
    url = mutations(calls)[0]["url"]
    assert url.endswith(f"/messages/{MID}/trash")


def test_archive_only_removes_the_inbox_label(calls):
    gs.gmail_archive(MID)
    call = mutations(calls)[0]
    assert call["url"].endswith(f"/messages/{MID}/modify")
    assert call["body"] == {"removeLabelIds": ["INBOX"]}
    assert "addLabelIds" not in call["body"]
    assert "TRASH" not in str(call["body"])


def test_untrash_is_the_documented_undo(calls):
    gs.gmail_untrash(MID)
    assert mutations(calls)[0]["url"].endswith(f"/messages/{MID}/untrash")


def test_trash_result_tells_the_user_how_to_undo(calls):
    out = gs.gmail_trash(MID)
    assert "gmail_untrash" in out
    assert "30 days" in out


def test_archive_result_says_nothing_was_deleted(calls):
    out = gs.gmail_archive(MID)
    assert "nothing was deleted" in out.lower()
    assert "All Mail" in out


# --------------------------------------------------------------------------- #
# input guards
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["", "   ", "no spaces here", "../../etc/passwd", "a"*200, "id/with/slash"])
@pytest.mark.parametrize("fn", [gs.gmail_archive, gs.gmail_trash, gs.gmail_untrash])
def test_malformed_ids_are_refused_before_any_request(calls, fn, bad):
    out = fn(bad)
    assert out.startswith("ERROR")
    assert "Nothing was changed" in out
    assert not calls, f"issued a request for malformed id {bad!r}"


@pytest.mark.parametrize("fn", [gs.gmail_archive, gs.gmail_trash, gs.gmail_untrash])
def test_no_session_changes_nothing(monkeypatch, fn):
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
    monkeypatch.setattr(gs, "_oauth_user_needs_reauth", lambda a: None)
    out = fn(MID)
    assert "NOT CONFIGURED" in out
    assert "Nothing was changed" in out


# --------------------------------------------------------------------------- #
# error paths
# --------------------------------------------------------------------------- #

def test_missing_scope_names_the_scope(monkeypatch):
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

    def boom(method, url, token, body=None):
        if "format=metadata" in url:
            return {"payload": {"headers": []}}
        raise RuntimeError("GOOGLE API 403 insufficientPermissions")

    monkeypatch.setattr(gs, "_http_json", boom)
    out = gs.gmail_trash(MID)
    assert "gmail.modify" in out
    assert "Nothing was changed" in out


def test_unknown_message_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

    def boom(method, url, token, body=None):
        if "format=metadata" in url:
            return {"payload": {"headers": []}}
        raise RuntimeError("GOOGLE API 404 not found")

    monkeypatch.setattr(gs, "_http_json", boom)
    out = gs.gmail_archive(MID)
    assert "no message with id" in out
    assert "Nothing was changed" in out


def test_a_failed_description_does_not_fail_a_good_mutation(monkeypatch):
    """The audit line is best effort; it must not invent a failure."""
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

    def half(method, url, token, body=None):
        if "format=metadata" in url:
            raise RuntimeError("GOOGLE API 500 metadata unavailable")
        return {"id": MID}

    monkeypatch.setattr(gs, "_http_json", half)
    out = gs.gmail_trash(MID)
    assert "GMAIL TRASHED" in out


def test_result_names_the_message_that_moved(calls):
    out = gs.gmail_trash(MID)
    assert "Quarterly numbers" in out
    assert "finance@example.com" in out


# --------------------------------------------------------------------------- #
# roster wiring
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["gmail_archive", "gmail_trash", "gmail_untrash"])
def test_all_three_are_confirmation_gated(name):
    registry = build_general_registry()
    spec = next(t for t in registry.get_subagent("mail").tools if t.name == name)
    assert spec.permission is Permission.REQUIRES_CONFIRMATION
    assert name in registry.gated_tool_names


def test_confirm_prompt_states_the_consequence():
    registry = build_general_registry()
    tools = {t.name: t for t in registry.get_subagent("mail").tools}

    trash = tools["gmail_trash"].confirm_prompt({"message_id": MID})
    assert MID in trash and "Trash" in trash and "30 days" in trash

    arch = tools["gmail_archive"].confirm_prompt({"message_id": MID})
    assert "nothing deleted" in arch.lower()


def test_trash_description_disclaims_permanent_deletion():
    registry = build_general_registry()
    spec = next(t for t in registry.get_subagent("mail").tools if t.name == "gmail_trash")
    assert "NOT permanent deletion" in spec.description


def test_modify_scope_is_requested_but_full_mail_access_is_not():
    from dourmouse import google_auth

    scopes = google_auth._FULL_SCOPES
    assert "gmail.modify" in scopes
    # mail.google.com is the scope that permits permanent deletion.
    assert "mail.google.com" not in scopes
