"""One way to ask "is this the same path" or "is this inside that folder".

Path guards in Dourmouse compared spellings: ``path.name in {".zshrc"}``,
``".git" in path.parts``, ``parts[0] == "auth"``. The default macOS volume
(APFS or HFS+) is case-insensitive and normalisation-insensitive, so
``~/.ZSHRC``, ``.GIT/hooks`` or a name typed in decomposed Unicode is the
SAME file as the protected one while comparing as different. Every guard
should go through this module so the rule cannot drift between copies.

Two kinds of check, because they fail in opposite directions:

- A REFUSAL (deny list: startup files, .git, .env, the app's own code)
  must not miss a match. Use ``fold`` / ``folded_parts`` / ``is_under_folded``:
  the resolved real path, NFC-normalised and casefolded on both sides. On
  a case-sensitive volume this can refuse a path that is only spelled like
  a protected one, which is the safe direction for a refusal.
- A CONFINEMENT (allow list: "only inside the design folder") must not
  accept a look-alike. Use ``is_within``: true only when the real path, or
  one of its existing ancestors, IS the root directory by file identity
  (``os.path.samefile``), so a case variant is accepted exactly when the
  filesystem itself says it is the same folder.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable
from pathlib import Path


def fold(text: str) -> str:
    """NFC-normalised, casefolded text: how a case- and normalisation-
    insensitive volume compares two names."""
    return unicodedata.normalize("NFC", text).casefold()


def real_path(path: str | os.PathLike[str]) -> Path:
    """``~`` expanded, symlinks and ``..`` resolved (also for a path that
    does not exist yet: the existing prefix is resolved, the rest kept)."""
    return Path(os.path.realpath(os.path.expanduser(os.fspath(path))))


def folded_parts(path: str | os.PathLike[str]) -> tuple[str, ...]:
    """The resolved real path's components, each folded."""
    return tuple(fold(part) for part in real_path(path).parts)


def name_in(name: str, names: Iterable[str]) -> bool:
    """True when ``name`` equals any of ``names`` under folding."""
    target = fold(name)
    return any(target == fold(n) for n in names)


def is_under_folded(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Deny-side check: ``path`` is ``root`` or inside it, comparing the
    resolved real paths component by component under folding."""
    p, r = folded_parts(path), folded_parts(root)
    return p[: len(r)] == r


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Allow-side check: ``path`` is ``root`` or inside it by file identity.

    Walks the resolved real path upwards and compares every existing
    ancestor (and the path itself) with the root via ``os.path.samefile``;
    a spelling match alone never counts. A root that does not exist
    contains nothing."""
    root_real = real_path(root)
    if not root_real.exists():
        return False
    current = real_path(path)
    while True:
        if current.exists():
            try:
                if os.path.samefile(current, root_real):
                    return True
            except OSError:
                return False
        parent = current.parent
        if parent == current:
            return False
        current = parent
