"""FD2 fixes for project_bookkeeper: P3-47 (race/atomic/corrupt store), P3-46 (dirname)."""

from __future__ import annotations

import json
import os

import pytest

from dourmouse import project_bookkeeper as pb
from dourmouse.tests.test_project_bookkeeper import _user_line, _write_claude_session


def _session(root, cwd, name="s1"):
    d = root / pb._sanitized_dirname(cwd)
    _write_claude_session(d / f"{name}.jsonl", [_user_line("hello", cwd=cwd)])


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "claude"
    store = tmp_path / "store.json"
    return root, store, tmp_path / "no_codex.db"


def test_sanitized_dirname_matches_claude_code_convention():
    assert pb._sanitized_dirname("/Users/aditagrawal/Claude code") == "-Users-aditagrawal-Claude-code"
    assert (
        pb._sanitized_dirname("/Volumes/ATLAS /Atlas/dourmouse-4.0.0")
        == "-Volumes-ATLAS--Atlas-dourmouse-4-0-0"
    )
    assert pb._sanitized_dirname("/a/b_c") == "-a-b-c"


def test_manual_project_created_during_refresh_survives(env, monkeypatch):
    root, store, db = env
    _session(root, "/Users/x/proj")
    orig = pb._claude_recent_items

    def hooked(project_dir, cap):
        # a request thread creates a project while the refresh is scanning
        pb.create_project("Mine", path="/tmp/fd2-mine", store_path=store)
        return orig(project_dir, cap)

    monkeypatch.setattr(pb, "_claude_recent_items", hooked)
    pb.refresh(claude_root=root, codex_db=db, store_path=store)
    data = json.loads(store.read_text())
    assert "/tmp/fd2-mine" in data["manual_projects"]


def test_project_hidden_during_refresh_stays_hidden(env, monkeypatch):
    root, store, db = env
    _session(root, "/Users/x/a", "sa")
    pb.refresh(claude_root=root, codex_db=db, store_path=store)  # A is now stored
    _session(root, "/Users/x/b", "sb")  # B is new, so its extraction runs
    orig = pb._claude_recent_items

    def hooked(project_dir, cap):
        pb.delete_project("/Users/x/a", store_path=store)
        return orig(project_dir, cap)

    monkeypatch.setattr(pb, "_claude_recent_items", hooked)
    pb.refresh(claude_root=root, codex_db=db, store_path=store)
    assert "/Users/x/a" in json.loads(store.read_text())["hidden_paths"]


def test_corrupt_store_is_kept_aside_and_reported_not_overwritten(env):
    root, store, db = env
    _session(root, "/Users/x/proj")
    store.write_text('{"projects": {"/Users/x/old": {', encoding="utf-8")  # truncated
    result = pb.refresh(claude_root=root, codex_db=db, store_path=store)
    asides = list(store.parent.glob("store.json.corrupt-*"))
    assert len(asides) == 1
    assert asides[0].read_text() == '{"projects": {"/Users/x/old": {'
    assert result.get("store_warning")
    assert "corrupt" in result["store_warning"] or "kept" in result["store_warning"]
    assert json.loads(store.read_text())["projects"]  # a fresh valid store was written


def test_create_project_on_corrupt_store_keeps_the_old_file(env):
    root, store, db = env
    store.write_text("not json at all", encoding="utf-8")
    pb.create_project("P", path="/tmp/fd2-p", store_path=store)
    asides = list(store.parent.glob("store.json.corrupt-*"))
    assert len(asides) == 1 and asides[0].read_text() == "not json at all"


def test_save_is_atomic_a_failed_replace_leaves_the_old_file(env, monkeypatch):
    root, store, db = env
    pb.create_project("P", path="/tmp/fd2-p", store_path=store)
    before = store.read_text()

    def boom(src, dst):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            pb.create_project("Q", path="/tmp/fd2-q", store_path=store)
    assert store.read_text() == before
    assert not [p for p in store.parent.iterdir() if p.name.endswith(".tmp")]


def test_concurrent_creates_do_not_lose_each_other(env):
    import threading

    root, store, db = env
    errs = []

    def mk(i):
        try:
            pb.create_project(f"P{i}", path=f"/tmp/fd2-c{i}", store_path=store)
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=mk, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert len(json.loads(store.read_text())["manual_projects"]) == 12
