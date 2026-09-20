"""Real filesystem walker for the device wiki (Domain E, step 4). Real
I/O (`os.walk`, real file hashing) but no model call and fully
deterministic given the same real files on disk.

Explicit, user-configured root-folder allowlist ONLY -- `DOURMOUSE_WIKI_
ROOTS` (``os.pathsep``-separated real absolute paths), never silently the
whole filesystem, matching Domain E's own requirement literally. An unset
or empty allowlist means "the wiki has nothing to scan yet" -- an honest
empty result, never a fallback to walking everything.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .core import WikiEntry, reconcile
from .store import WikiStore

ROOTS_ENV = "DOURMOUSE_WIKI_ROOTS"

#: Real, named system/cache/build directory names to skip, matching the
#: founding spec's own literal wording ("skip system/cache/build files").
#: Any directory NAME starting with "." is also skipped (covers .git,
#: .venv, .cache, and every other real dotfile-convention directory this
#: fixed list would otherwise have to enumerate by hand).
_SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", "venv", "dist", "build", "target",
    "Library", "Applications", "System",
})

#: A real, named cost bound (5 MB) -- an oversized file is simply never
#: indexed, not silently truncated-hashed, matching this codebase's own
#: "honest skip over silent partial work" discipline.
_MAX_FILE_SIZE_BYTES = 5_000_000


def configured_roots() -> list[Path]:
    """The real, explicit allowlist from `DOURMOUSE_WIKI_ROOTS`. A
    configured path that does not actually exist as a real directory is
    silently excluded (not an error -- config drift, e.g. a folder
    since renamed, should not break every other configured root)."""
    raw = os.environ.get(ROOTS_ENV, "").strip()
    if not raw:
        return []
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        p = Path(part).expanduser()
        if p.is_dir():
            roots.append(p)
    return roots


def _hash_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def walk_roots(roots: list[Path], max_file_size: int = _MAX_FILE_SIZE_BYTES) -> dict[str, tuple[str, int]]:
    """Every real file found under the given real roots, as
    ``{path: (content_hash, size_bytes)}`` -- the exact shape
    `core.reconcile()`'s own `found` parameter expects. Skips known
    system/cache/build directory names and any dotfile-convention
    directory; skips a file over `max_file_size` (an honest, complete
    skip, never a partial/truncated hash of an oversized file); skips a
    file this process cannot read (permission error, a real broken
    symlink) -- an honest omission, not a crash."""
    found: dict[str, tuple[str, int]] = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIR_NAMES and not d.startswith(".")
            ]
            for name in filenames:
                fpath = Path(dirpath) / name
                try:
                    size = fpath.stat().st_size
                except OSError:
                    continue
                if size > max_file_size:
                    continue
                content_hash = _hash_file(fpath)
                if content_hash is None:
                    continue
                found[str(fpath)] = (content_hash, size)
    return found


def scan(store: WikiStore, roots: list[Path], now: float) -> dict[str, WikiEntry]:
    """The real, complete read-modify-write cycle a caller drives on
    every scan: read the store's current state, walk the real allowed
    roots, reconcile, persist the result. Returns the same dict it just
    persisted so a caller (a chat tool, step 5) can report exactly what
    changed without a second real store read."""
    found = walk_roots(roots)
    existing = store.all_as_dict()
    result = reconcile(existing, found, now)
    store.save_all(result)
    return result
