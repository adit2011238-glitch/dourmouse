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
