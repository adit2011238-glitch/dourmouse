"""FIX-P R5-8: the app's own state folders inside the workspace are not plain T1 files."""

from __future__ import annotations

from dourmouse import privacy, system_access
from dourmouse.config import workspace_dir


def test_state_folders_are_t2_under_their_own_source_and_other_workspace_files_stay_t1():
    ws = workspace_dir()
    for folder in ("sessions", "memory", "device_wiki", "state"):
        (ws / folder).mkdir(parents=True, exist_ok=True)
    assert privacy.classify("read_path", {"path": str(ws / "sessions" / "x.json")}) == ("T2", "memory")
    assert privacy.classify("read_path", {"path": str(ws / "memory" / "note.md")}) == ("T2", "memory")
    assert privacy.classify("extract_pdf", {"path": str(ws / "device_wiki" / "a.md")}) == ("T2", "documents")
    assert privacy.classify("read_path", {"path": str(ws / "state" / "x.json")}) == ("T2", "files")
    assert privacy.classify("read_path", {"path": str(ws / "notes.txt")}) == ("T1", None)
    assert privacy.classify("read_path", {"path": "sessions/x.json"}) == ("T2", "memory")  # relative to the workspace


def test_read_path_refuses_the_state_folders(tmp_path):
    ws = workspace_dir()
    (ws / "sessions").mkdir(parents=True, exist_ok=True)
    f = ws / "sessions" / "chat.json"
    f.write_text("mail from alice@example.org", encoding="utf-8")
    out = system_access._read_path_tool({"path": str(f)})
    assert out.startswith("REFUSED") and "alice" not in out
    ok = ws / "plain.txt"
    ok.write_text("hello", encoding="utf-8")
    assert system_access._read_path_tool({"path": str(ok)}) == "hello"
