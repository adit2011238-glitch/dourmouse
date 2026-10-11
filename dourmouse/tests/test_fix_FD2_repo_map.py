"""FD2 fixes for repo_map: P4-46 (ancestor named like a skip dir), P4-47 (prune while walking)."""

from __future__ import annotations

import os

from dourmouse import repo_map


def test_root_under_a_dir_named_build_is_not_filtered_out(tmp_path):
    root = tmp_path / "build" / "proj"
    root.mkdir(parents=True)
    (root / "a.py").write_text("def f(): pass\n")
    found = repo_map.iter_source_files(root)
    assert [p.name for p in found] == ["a.py"]


def test_skip_dirs_below_the_root_are_still_skipped(tmp_path):
    root = tmp_path / "dist" / "proj"
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "bad.py").write_text("def f(): pass\n")
    (root / "pkg.egg-info").mkdir()
    (root / "pkg.egg-info" / "bad2.py").write_text("def f(): pass\n")
    (root / "ok.py").write_text("def f(): pass\n")
    assert [p.name for p in repo_map.iter_source_files(root)] == ["ok.py"]


def test_skipped_directories_are_never_entered(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "node_modules").mkdir(parents=True)
    for i in range(50):
        (root / "node_modules" / f"f{i}.py").write_text("x = 1\n")
    (root / "ok.py").write_text("def f(): pass\n")
    entered = []
    real_scandir = os.scandir

    def spy(path="."):
        entered.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", spy)
    found = repo_map.iter_source_files(root)
    assert [p.name for p in found] == ["ok.py"]
    assert not any("node_modules" in e for e in entered)


def test_order_matches_sorted_path_order_and_max_files_stops_early(tmp_path):
    root = tmp_path / "proj"
    (root / "b").mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n")
    (root / "b" / "x.py").write_text("x = 1\n")
    (root / "c.py").write_text("x = 1\n")
    (root / "z.py").write_text("x = 1\n")
    rels = [p.relative_to(root).as_posix() for p in repo_map.iter_source_files(root)]
    assert rels == ["a.py", "b/x.py", "c.py", "z.py"]
    assert len(repo_map.iter_source_files(root, max_files=2)) == 2
