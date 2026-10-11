"""Fix agent FD1: P4-29 (archive re-indexed), P4-30 (apply overwrites / trusts a stale index), P4-31 (undo
loses its bookkeeping on a failed move)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import dourmouse.librarian as lm
from dourmouse.librarian import Librarian


def _lib(tmp_path, root):
    return Librarian(db=tmp_path / "idx.db", roots=[root])


def _dupe_tree(tmp_path):
    root = tmp_path / "Documents"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "IMG_1.jpg").write_bytes(b"same bytes" * 50)
    (root / "b" / "IMG_1.jpg").write_bytes(b"same bytes" * 50)
    return root


# P4-29 ---------------------------------------------------------------------- #

def test_the_archive_folder_inside_a_root_is_not_indexed(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    root.mkdir()
    (root / "keep.txt").write_text("hello")
    arch = root / "Dourmouse Archive"
    (arch / "Duplicates" / "abc").mkdir(parents=True)
    (arch / "Duplicates" / "abc" / "keep.txt").write_text("hello")
    monkeypatch.setattr(lm, "archive_root", lambda: arch)
    lib = _lib(tmp_path, root)
    lib.index_batch()
    assert lib.stats()["files"] == 1
    assert [h["path"] for h in lib.find("keep")] == [str(root / "keep.txt")]


def test_proposals_converge_after_a_duplicates_apply(tmp_path, monkeypatch):
    root = _dupe_tree(tmp_path)
    arch = root / "Dourmouse Archive"
    monkeypatch.setattr(lm, "archive_root", lambda: arch)
    lib = _lib(tmp_path, root)
    lib.index_batch()
    lib.hash_candidates()
    assert lib.apply("duplicates")["moved"] == 1
    # a full new pass: the archived copy must not come back as a duplicate of its keeper
    lib.index_batch()
    lib.hash_candidates()
    assert not any(p["id"] == "duplicates" for p in lib.proposals())


def test_stale_archive_rows_are_not_proposed(tmp_path, monkeypatch):
    root = _dupe_tree(tmp_path)
    arch = root / "Dourmouse Archive"
    monkeypatch.setattr(lm, "archive_root", lambda: tmp_path / "elsewhere")
    lib = _lib(tmp_path, root)
    (arch / "Duplicates").mkdir(parents=True)
    shutil.copy(root / "a" / "IMG_1.jpg", arch / "Duplicates" / "IMG_1.jpg")
    lib.index_batch()  # the archive is indexed while archive_root points elsewhere (an older index)
    lib.hash_candidates()
    monkeypatch.setattr(lm, "archive_root", lambda: arch)
    for p in lib.proposals():
        for m in p["moves"]:
            assert not m["src"].startswith(str(arch))


# P4-30 ---------------------------------------------------------------------- #

def test_same_second_name_collisions_never_overwrite(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    for d in "abc":
        (root / d).mkdir(parents=True)
        (root / d / "IMG_1.jpg").write_bytes(b"same bytes" * 50)
    arch = tmp_path / "Archive"
    monkeypatch.setattr(lm, "archive_root", lambda: arch)
    monkeypatch.setattr(lm.time, "time", lambda: 1_700_000_000.0)
    lib = _lib(tmp_path, root)
    lib.index_batch()
    lib.hash_candidates()
    # pre-existing file with the destination name, so every extra collides
    dup = next(p for p in lib.proposals() if p["id"] == "duplicates")
    dst_dir = os.path.dirname(dup["moves"][0]["dst"])
    os.makedirs(dst_dir, exist_ok=True)
    for m in dup["moves"]:
        Path(m["dst"]).write_bytes(b"PRE-EXISTING")
    r = lib.apply("duplicates")
    assert r["moved"] == 2
    files = sorted(os.listdir(dst_dir))
    assert len(files) == 3, files  # the pre-existing file plus two distinct moved copies
    assert Path(dst_dir, "IMG_1.jpg").read_bytes() == b"PRE-EXISTING"


def test_apply_skips_a_duplicate_whose_keeper_is_gone(tmp_path, monkeypatch):
    root = _dupe_tree(tmp_path)
    monkeypatch.setattr(lm, "archive_root", lambda: tmp_path / "Archive")
    lib = _lib(tmp_path, root)
    lib.index_batch()
    lib.hash_candidates()
    dup = next(p for p in lib.proposals() if p["id"] == "duplicates")
    keeper = dup["moves"][0]["keeper"]
    os.unlink(keeper)  # the owner deleted the keeper after the last pass
    r = lib.apply("duplicates")
    assert r["moved"] == 0
    assert any("keeper" in s for s in r["skipped"])
    assert os.path.exists(dup["moves"][0]["src"])


def test_apply_skips_a_duplicate_whose_content_changed(tmp_path, monkeypatch):
    root = _dupe_tree(tmp_path)
    monkeypatch.setattr(lm, "archive_root", lambda: tmp_path / "Archive")
    lib = _lib(tmp_path, root)
    lib.index_batch()
    lib.hash_candidates()
    dup = next(p for p in lib.proposals() if p["id"] == "duplicates")
    Path(dup["moves"][0]["src"]).write_bytes(b"edited since the last pass")
    r = lib.apply("duplicates")
    assert r["moved"] == 0
    assert any("changed" in s for s in r["skipped"])


# P4-31 ---------------------------------------------------------------------- #

def test_undo_keeps_the_record_of_files_it_did_restore_when_a_later_move_fails(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    root.mkdir()
    arch = tmp_path / "Archive"
    monkeypatch.setattr(lm, "archive_root", lambda: arch)
    lib = _lib(tmp_path, root)
    srcs = []
    for i in range(3):
        src = root / f"f{i}.txt"
        src.write_text("x")
        dst = arch / f"f{i}.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        srcs.append(src)
        with lib._conn() as c:
            c.execute("INSERT INTO moves(proposal, src, dst, at) VALUES('p',?,?,0)", (str(src), str(dst)))
    real_move = shutil.move
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("permission denied")
        return real_move(a, b)

    monkeypatch.setattr(lm.shutil, "move", flaky)
    r = lib.undo("p")
    assert r["ok"] is True and r["restored"] == 2
    assert any("permission denied" in s for s in r["skipped"])
    with lib._conn() as c:
        undone = c.execute("SELECT COUNT(*) FROM moves WHERE undone=1").fetchone()[0]
    assert undone == 2
