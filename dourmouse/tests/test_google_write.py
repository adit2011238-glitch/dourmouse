"""Finding #122 (OS-7): Calendar write is actually granted by the sign-in,
and rows can be appended to an existing Sheet (request shape checked
against a fake Sheets API; a real call needs the owner's own sign-in)."""

from __future__ import annotations

from dourmouse import google_auth
from dourmouse import google_services as gs


def test_full_access_requests_calendar_and_sheets_write(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_FULL_SCOPES", "1")
    scopes = google_auth.requested_scopes().split()
    assert "https://www.googleapis.com/auth/calendar.events" in scopes
    assert "https://www.googleapis.com/auth/spreadsheets" in scopes
    assert not any("mail.google.com" in s for s in scopes)  # permanent mail deletion stays unrequested


def test_sheets_append_sends_the_real_request_shape(monkeypatch):
    calls = []

    def fake_http_json(method, url, token, body=None):
        calls.append((method, url, token, body))
        return {"updates": {"updatedRange": "Budget!A7:B8", "updatedRows": 2}}

    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
    monkeypatch.setattr(gs, "_http_json", fake_http_json)
    out = gs.sheets_append("1AbC_def-GHI", [["rent", 1200], ["food", "=B1*0.2"]], "Budget")
    assert out == "SHEETS APPENDED: 2 row(s) to Budget!A7:B8 in 1AbC_def-GHI."
    method, url, token, body = calls[0]
    assert method == "POST" and token == "tok" and body == {"values": [["rent", 1200], ["food", "=B1*0.2"]]}
    assert url.endswith("/1AbC_def-GHI/values/Budget:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS")


def test_sheets_append_validates_before_any_call(monkeypatch):
    monkeypatch.setattr(gs, "_http_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert gs.sheets_append("../etc", [["x"]]).startswith("ERROR")
    assert gs.sheets_append("abc", []).startswith("ERROR")
    assert gs.sheets_append("abc", ["not a row"]).startswith("ERROR")


def test_a_403_says_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")

    def forbidden(*a, **k):
        raise RuntimeError("HTTP 403 insufficient scopes")

    monkeypatch.setattr(gs, "_http_json", forbidden)
    out = gs.sheets_append("abc", [["x"]])
    assert "403" in out and "sign in again" in out and "Nothing was written" in out
