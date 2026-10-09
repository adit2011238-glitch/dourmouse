"""FS1: the shared path-identity helper (dourmouse/path_identity.py)."""

from __future__ import annotations

import os
import unicodedata

from dourmouse import path_identity as pi


def _case_insensitive(tmp_path) -> bool:
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    return (tmp_path / "caseprobe").exists()


def test_fold_handles_case_and_unicode_normalisation():
    nfd = unicodedata.normalize("NFD", "Café")
    assert pi.fold(nfd) == pi.fold("CAFÉ")
    assert pi.name_in(".ZSHRC", {".zshrc", ".bashrc"})
    assert not pi.name_in(".zshrc.bak", {".zshrc"})


def test_is_under_folded_matches_a_case_variant_and_dotdot(tmp_path):
    root = tmp_path / "Repo" / "dourmouse"
    root.mkdir(parents=True)
    assert pi.is_under_folded(tmp_path / "REPO" / "DOURMOUSE" / "x.py", root)
    assert pi.is_under_folded(tmp_path / "Repo" / "other" / ".." / "dourmouse" / "y", root)
    assert not pi.is_under_folded(tmp_path / "Repo" / "dourmouse2" / "y", root)


def test_is_under_folded_follows_symlinks(tmp_path):
    target = tmp_path / "protected"
    target.mkdir()
    link = tmp_path / "innocent"
    os.symlink(target, link)
    assert pi.is_under_folded(link / "file", target)


def test_is_within_uses_file_identity(tmp_path):
    root = tmp_path / "design_3d"
    root.mkdir()
    assert pi.is_within(root / "new.json", root)
    assert pi.is_within(root / "sub" / "deeper" / "new.json", root)
    assert not pi.is_within(tmp_path / "design_3d_other" / "x.json", root)
    assert not pi.is_within(root / ".." / "escape.json", root)
    # a case variant counts exactly when the volume says it is the same folder
    variant = tmp_path / "DESIGN_3D" / "x.json"
    assert pi.is_within(variant, root) == _case_insensitive(tmp_path)


def test_is_within_rejects_a_symlink_out_of_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "jump")
    assert not pi.is_within(root / "jump" / "secret.json", root)


def test_missing_root_contains_nothing(tmp_path):
    assert not pi.is_within(tmp_path / "x", tmp_path / "missing")
