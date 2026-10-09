"""FR fixes P2-13 and P2-20: the workspace file tools' protected-folder guard
must not be a spelling match, and a symlinked workspace must not make the tools
raise after writing."""

from __future__ import annotations

import os

import pytest

from dourmouse import general_roster as gr


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(root))
    return root


@pytest.mark.parametrize("spelling", [
    "Self_Extensions/drafts.jsonl", "SELF_EXTENSIONS/approved/x.py", "State/a.txt",
    "Security/a.txt", "Sessions/a.txt", "AUTH/a.txt", "self_extensions/a.txt",
])
def test_write_refuses_every_spelling_of_a_protected_folder(ws, spelling):
    out = gr._write_file_tool({"path": spelling, "content": "x"})
    assert out.startswith("REFUSED"), out
    assert not any(p.is_file() for p in ws.rglob("*"))


def test_edit_refuses_a_case_variant(ws):
    (ws / "state").mkdir()
    (ws / "state" / "a.txt").write_text("hello")
    out = gr._edit_file_tool({"path": "STATE/a.txt", "old_str": "hello", "new_str": "bye"})
    assert out.startswith("REFUSED"), out
    assert (ws / "state" / "a.txt").read_text() == "hello"


def test_read_refuses_the_auth_folder_in_any_case(ws):
    (ws / "auth").mkdir()
    (ws / "auth" / "notes.txt").write_text("secret")
    for p in ("auth/notes.txt", "Auth/notes.txt", "AUTH/NOTES.TXT"):
        assert gr._read_file_tool({"path": p}).startswith("REFUSED"), p


def test_secret_file_names_in_any_case_and_decomposed_unicode(ws):
    assert gr._is_secret_workspace_path(ws, ws / ".ENV")
    assert gr._is_secret_workspace_path(ws, ws / "x" / "Store.DB")
    assert not gr._is_secret_workspace_path(ws, ws / "notes" / "a.txt")


def test_ordinary_files_are_still_writable_and_readable(ws):
    assert gr._write_file_tool({"path": "notes/a.txt", "content": "hi"}).startswith("WROTE")
    assert gr._read_file_tool({"path": "notes/a.txt"}) == "hi"


def test_symlinked_workspace_does_not_raise_after_writing(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(link))
    out = gr._write_file_tool({"path": "a.txt", "content": "one"})
    assert out.startswith("WROTE"), out
    out = gr._write_file_tool({"path": "a.txt", "content": "two"})
    assert out.startswith("UPDATED"), out
    out = gr._edit_file_tool({"path": "a.txt", "old_str": "two", "new_str": "three"})
    assert out.startswith("EDITED"), out
    assert "WORKSPACE" in gr._list_files_tool({"path": "."})
    assert gr._diff_preview_tool({"path": "b.txt", "content": "x"}).startswith("DIFF (new file)")


# ---- same defect class, found while sweeping the neighbours (H-FR-1, H-FR-2) ----

def test_diff_preview_cannot_read_what_read_file_refuses(ws):
    (ws / "auth").mkdir()
    (ws / "auth" / "notes.txt").write_text("the secret line")
    for p in ("auth/notes.txt", "AUTH/notes.txt"):
        out = gr._diff_preview_tool({"path": p, "content": "x"})
        assert out.startswith("REFUSED") and "secret line" not in out, p
    (ws / "notes").mkdir()
    (ws / "notes" / "a.txt").write_text("hello\n")
    assert "hello" in gr._diff_preview_tool({"path": "notes/a.txt", "content": "bye\n"})


def test_delete_refuses_the_protected_folders_in_any_spelling(ws):
    (ws / "self_extensions").mkdir()
    victim = ws / "self_extensions" / "drafts.jsonl"
    victim.write_text("{}")
    for p in ("self_extensions/drafts.jsonl", "Self_Extensions/drafts.jsonl"):
        assert gr._delete_file_tool({"path": p}).startswith("REFUSED"), p
    assert victim.exists()
    (ws / "scratch").mkdir()
    (ws / "scratch" / "x.txt").write_text("t")
    assert gr._delete_file_tool({"path": "scratch/x.txt"}).startswith("DELETED")
