"""Finding #115 (OS-5): the librarian indexes incrementally, finds files,
proposes (never acts), applies only on request, and undoes."""

from __future__ import annotations

import json
import os
import time

from dourmouse.librarian import Librarian, kind_of


def _tree(tmp_path):
    root = tmp_path / "Documents"
    (root / "thesis").mkdir(parents=True)
    (root / "thesis" / "Chapter 1 draft.docx").write_bytes(b"chapter one" * 100)
    (root / "copy of chapter.docx").write_bytes(b"chapter one" * 100)  # identical content
    (root / "budget.xlsx").write_bytes(b"numbers")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "skip.js").write_text("x", encoding="utf-8")
    (root / ".hidden").write_text("x", encoding="utf-8")
    return root


def _lib(tmp_path, root, **kw):
    return Librarian(db=tmp_path / "idx.db", roots=[root], **kw)


def test_index_is_incremental_and_notices_changes_and_removals(tmp_path):
    root = _tree(tmp_path)
    lib = _lib(tmp_path, root)
    r = lib.index_batch()
    assert (r["added"], r["pass_complete"]) == (3, 1)  # node_modules and dotfiles skipped
    assert lib.index_batch() == {"added": 0, "changed": 0, "missing": 0, "pass_complete": 1}
    (root / "budget.xlsx").write_bytes(b"new numbers!")
    os.utime(root / "budget.xlsx", (time.time() + 5, time.time() + 5))
    (root / "copy of chapter.docx").unlink()
    r = lib.index_batch()
    assert (r["changed"], r["missing"]) == (1, 1)
    assert lib.stats()["files"] == 2


def test_a_batch_limit_spreads_one_pass_over_ticks(tmp_path):
    lib = _lib(tmp_path, _tree(tmp_path))
    assert lib.index_batch(batch=2)["pass_complete"] == 0
    assert lib.index_batch(batch=2)["pass_complete"] == 1
    assert lib.stats()["files"] == 3


def test_find_ranks_name_matches_first(tmp_path):
    lib = _lib(tmp_path, _tree(tmp_path))
    lib.index_batch()
    hits = lib.find("chapter")
    assert {h["name"] for h in hits} == {"Chapter 1 draft.docx", "copy of chapter.docx"}
    assert [h["name"] for h in lib.find("thesis")] == ["Chapter 1 draft.docx"]  # folder match still found
    assert lib.find("", kind="spreadsheet")[0]["name"] == "budget.xlsx"
    assert kind_of("x.PDF") == "document" and kind_of("noext") == "other"


def test_duplicates_are_proposed_applied_only_on_request_and_undone(tmp_path, monkeypatch):
    import dourmouse.librarian as lm

    root = _tree(tmp_path)
    monkeypatch.setattr(lm, "archive_root", lambda: tmp_path / "Archive")
    lib = _lib(tmp_path, root)
    lib.index_batch()
    assert lib.hash_candidates() == 2
    props = lib.proposals()
    dup = next(p for p in props if p["id"] == "duplicates")
    assert len(dup["moves"]) == 1 and (root / "copy of chapter.docx").exists()  # proposing moved nothing
    # The copy moves, whatever its path length: the original is kept.
    assert dup["moves"][0]["src"].endswith("copy of chapter.docx"), dup
    r = lib.apply("duplicates")
    assert r["moved"] == 1 and not (root / "copy of chapter.docx").exists()
    assert list((tmp_path / "Archive" / "Duplicates").rglob("*.docx"))
    assert lib.undo("duplicates")["restored"] == 1 and (root / "copy of chapter.docx").exists()
    assert lib.apply("nonexistent")["ok"] is False


def test_old_downloads_and_installers(tmp_path, monkeypatch):
    import dourmouse.librarian as lm

    home = tmp_path / "home"
    dl = home / "Downloads"
    (dl / "sub").mkdir(parents=True)
    old = time.time() - 60 * 86400
    for name in ("setup.dmg", "paper.pdf", "sub/nested.pdf"):
        (dl / name).write_bytes(b"x")
        os.utime(dl / name, (old, old))
    (dl / "fresh.pdf").write_bytes(b"x")
    monkeypatch.setattr(lm.Path, "home", classmethod(lambda cls: home))
    lib = Librarian(db=tmp_path / "idx.db", roots=[dl])
    lib.index_batch()
    props = {p["id"]: p for p in lib.proposals()}
    assert [m["src"].rsplit("/", 1)[1] for m in props["old-installers"]["moves"]] == ["setup.dmg"]
    assert [m["src"].rsplit("/", 1)[1] for m in props["old-downloads"]["moves"]] == ["paper.pdf"]  # top level only
    assert "/Downloads/document/" in props["old-downloads"]["moves"][0]["dst"]


def test_other_agents_can_ask_it(tmp_path):
    lib = _lib(tmp_path, _tree(tmp_path))
    lib.index_batch()
    reply = json.loads(lib.handle_message("find", "budget", "research"))
    assert reply[0]["path"].endswith("budget.xlsx")
    assert "No files match" in lib.handle_message("where is", "nothing-like-this", "x")


def test_an_unreadable_root_is_reported(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        lib = _lib(tmp_path, locked)
        lib.index_batch()
        assert str(locked) in lib.stats()["root_errors"]
    finally:
        locked.chmod(0o755)


def test_new_proposals_are_announced_once(tmp_path, monkeypatch):
    import dourmouse.librarian as lm

    root = _tree(tmp_path)
    monkeypatch.setattr(lm, "archive_root", lambda: tmp_path / "Archive")
    lib = _lib(tmp_path, root)
    told = []
    lib.on_new_proposals = told.append
    lib.tick()  # first pass: indexes, hashes the two same-size files
    lib.tick()  # second pass sees the duplicate proposal
    lib.tick()
    assert [[p["id"] for p in batch] for batch in told] == [["duplicates"]]
