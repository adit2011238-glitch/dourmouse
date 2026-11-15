"""Finding 206: per-folder consent for the library (privacy.json) and its routes."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import privacy


@pytest.fixture
def ps(tmp_path):
    return privacy.PrivacyStore(tmp_path / "privacy.json")


def test_no_record_means_ask(ps, tmp_path):
    assert ps.folder_consent_for(tmp_path / "Documents" / "a" / "f.pdf") is None
    assert ps.folder_consents() == []


def test_allow_covers_files_and_subfolders_and_a_nearer_deny_wins(ps, tmp_path):
    root = tmp_path / "Documents"
    (root / "school" / "private").mkdir(parents=True)
    ps.set_folder_consent(root / "school", "allow")
    ps.set_folder_consent(root / "school" / "private", "deny")
    assert ps.folder_consent_for(root / "school" / "essay.pdf") == "allow"
    assert ps.folder_consent_for(root / "school" / "sub" / "x.md") == "allow"
    assert ps.folder_consent_for(root / "school" / "private" / "d.txt") == "deny"
    assert ps.folder_consent_for(root / "schoolwork" / "x.md") is None  # a sibling that shares a prefix
    assert ps.folder_consent_for(root) is None


def test_ask_removes_the_record_and_symlinks_resolve_to_the_real_folder(ps, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    ps.set_folder_consent(link, "allow")
    assert ps.folder_consents()[0]["folder"] == str(real.resolve())
    assert ps.folder_consent_for(real / "f.txt") == "allow"
    ps.set_folder_consent(real, "ask")
    assert ps.folder_consents() == []


def test_bad_folders_and_decisions_are_refused(ps, tmp_path):
    for bad in ("", "relative/dir", "/", "/Users", tmp_path / ".ssh", tmp_path / "x" / ".aws" / "y", "a\x00b"):
        with pytest.raises(ValueError):
            ps.set_folder_consent(bad, "allow")
    with pytest.raises(ValueError):
        ps.set_folder_consent(tmp_path / "ok", "maybe")


def test_the_record_limit_is_enforced_and_existing_rows_can_still_change(ps, tmp_path, monkeypatch):
    monkeypatch.setattr(privacy, "FOLDER_CONSENT_LIMIT", 2)
    ps.set_folder_consent(tmp_path / "a", "allow")
    ps.set_folder_consent(tmp_path / "b", "allow")
    with pytest.raises(ValueError):
        ps.set_folder_consent(tmp_path / "c", "allow")
    ps.set_folder_consent(tmp_path / "a", "deny")
    assert ps.folder_consent_for(tmp_path / "a" / "f") == "deny"


def test_existing_source_consent_is_untouched_and_a_bad_table_is_empty(ps, tmp_path):
    ps.set_consent("files", "deny")
    ps.set_folder_consent(tmp_path / "a", "allow")
    assert ps.consent("files") == "deny"
    data = json.loads(ps.path.read_text())
    data["folder_consent"] = {"relative": {"decision": "allow"}, "/ok/x": {"decision": "maybe"}, "/ok/y": 5}
    ps.path.write_text(json.dumps(data))
    assert ps.folder_consents() == [] and ps.consent("files") == "deny"
    data["folder_consent"] = "junk"
    ps.path.write_text(json.dumps(data))
    assert ps.folder_consents() == []


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.general_roster import build_general_registry
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(timeout=2)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_routes_list_set_and_refuse(server, tmp_path):
    docs = tmp_path / "Documents" / "kiva"
    docs.mkdir(parents=True)
    st, d = call(server, "GET", "/api/os/settings/privacy/folders")
    assert st == 200 and d["folders"] == []
    st, d = call(server, "POST", "/api/os/settings/privacy/folders", {"folder": str(docs), "decision": "allow"})
    assert st == 200 and d["folder"]["decision"] == "allow" and d["folders"][0]["folder"] == str(docs.resolve())
    assert privacy.PrivacyStore().folder_consent_for(docs / "a.pdf") == "allow"
    st, d = call(server, "POST", "/api/os/settings/privacy/folders", {"folder": "/", "decision": "allow"})
    assert st == 400
    st, d = call(server, "POST", "/api/os/settings/privacy/folders", {"folder": str(docs), "decision": "ask"})
    assert st == 200 and d["folders"] == []
