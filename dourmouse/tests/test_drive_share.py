"""Sharing is outward-facing: it must refuse clearly rather than half-succeed.

Granting access puts the user's document in someone else's Drive and can send
them mail, so every guard here is about not doing that by accident.
"""

from __future__ import annotations

import pytest

from dourmouse import google_services as gs


@pytest.fixture
def signed_out(monkeypatch):
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
    monkeypatch.setattr(gs, "_oauth_user_needs_reauth", lambda action: None)


@pytest.fixture
def signed_in(monkeypatch):
    """Capture the outgoing request instead of calling Google."""
    calls: list[dict] = []

    def fake_http_json(method, url, token, payload=None, **kw):
        calls.append({"method": method, "url": url, "payload": payload})
        return {"id": "perm123"}

    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
    monkeypatch.setattr(gs, "_http_json", fake_http_json)
    return calls


# --------------------------------------------------------------------------- #
# guards — nothing leaves without valid input
# --------------------------------------------------------------------------- #

def test_no_session_reports_not_configured_and_shares_nothing(signed_out):
    out = gs.drive_share("fileid", "someone@example.com")
    assert "NOT CONFIGURED" in out
    assert "Nothing was shared" in out


def test_missing_file_id_is_refused(signed_in):
    out = gs.drive_share("", "someone@example.com")
    assert out.startswith("ERROR")
    assert not signed_in, "made a request despite a missing file id"


@pytest.mark.parametrize("bad", ["notanemail", "", "two addrs@x.com", "@nope", "a@b", "a@@b.com", "a b@c.com"])
def test_malformed_email_is_refused_before_any_request(signed_in, bad):
    out = gs.drive_share("fileid", bad)
    assert out.startswith("ERROR")
    assert "Nothing was shared" in out
    assert not signed_in, f"made a request for malformed address {bad!r}"


def test_unknown_role_is_refused(signed_in):
    out = gs.drive_share("fileid", "a@b.com", role="owner")
    assert out.startswith("ERROR")
    assert not signed_in, "made a request for an unsupported role"


@pytest.mark.parametrize("role", ["reader", "commenter", "writer"])
def test_supported_roles_are_accepted(signed_in, role):
    out = gs.drive_share("fileid", "a@b.com", role=role)
    assert "DRIVE SHARED" in out
    assert signed_in[0]["payload"]["role"] == role


def test_role_is_case_insensitive(signed_in):
    gs.drive_share("fileid", "a@b.com", role="WRITER")
    assert signed_in[0]["payload"]["role"] == "writer"


# --------------------------------------------------------------------------- #
# the request itself
# --------------------------------------------------------------------------- #

def test_request_shape_matches_the_drive_permissions_api(signed_in):
    gs.drive_share("FID", "someone@example.com", role="reader")
    call = signed_in[0]
    assert call["method"] == "POST"
    assert "/files/FID/permissions" in call["url"]
    assert call["payload"] == {
        "type": "user", "role": "reader", "emailAddress": "someone@example.com",
    }


def test_notification_defaults_to_on_and_is_explicit_in_the_url(signed_in):
    gs.drive_share("FID", "a@b.com")
    assert "sendNotificationEmail=true" in signed_in[0]["url"]


def test_notification_can_be_suppressed(signed_in):
    out = gs.drive_share("FID", "a@b.com", notify=False)
    assert "sendNotificationEmail=false" in signed_in[0]["url"]
    assert "no notification sent" in out


def test_result_names_who_got_what(signed_in):
    out = gs.drive_share("FID", "someone@example.com", role="writer")
    assert "someone@example.com" in out
    assert "writer" in out
    assert "FID" in out


# --------------------------------------------------------------------------- #
# failures explain the actual cause
# --------------------------------------------------------------------------- #

def test_404_explains_the_drive_file_scope_limit(monkeypatch):
    """A file the user made by hand is invisible to drive.file — say so."""
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

    def boom(*a, **k):
        raise RuntimeError("GOOGLE API 404 on .../permissions")

    monkeypatch.setattr(gs, "_http_json", boom)

    out = gs.drive_share("FID", "a@b.com")
    assert "drive.file scope only covers files DOURMOUSE created" in out
    assert "Nothing was shared" in out


def test_403_points_at_the_missing_write_scope(monkeypatch):
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

    def boom(*a, **k):
        raise RuntimeError("GOOGLE API 403 on .../permissions")

    monkeypatch.setattr(gs, "_http_json", boom)

    out = gs.drive_share("FID", "a@b.com")
    assert "GOOGLE_OAUTH_FULL_SCOPES" in out
    assert "Nothing was shared" in out


def test_unexpected_errors_are_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

    def boom(*a, **k):
        raise RuntimeError("GOOGLE API 500 server error")

    monkeypatch.setattr(gs, "_http_json", boom)

    out = gs.drive_share("FID", "a@b.com")
    assert "reported honestly" in out
    assert "500" in out
