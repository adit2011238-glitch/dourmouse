"""Study tab backend (backlog #9) — real, read-only, sandboxed access to
the user's own study resource folder.

Real folder, verified on disk (not the name the user typed — "MYP data",
the actual folder is "MYP data folder"): ~/Documents/MYP data folder.
Both list and read are hard-sandboxed to that folder — a relative path
that would escape it (``../..``, an absolute path elsewhere, a symlink
target outside the folder) is refused, never silently clamped or
"helpfully" resolved elsewhere (Rule 2.2). The agent's own system prompt
(see general_roster.py's "study" subagent) instructs it to prioritize its
own knowledge first and use these tools only for material this specific
folder actually has — a textbook excerpt, a real past assessment, etc.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The real folder name on disk (verified — NOT "MYP data", the folder
#: has "folder" in its name too). Overridable only for tests.
_STUDY_DIRNAME = "MYP data folder"
_MAX_READ_CHARS = 20_000
_MAX_LIST_ENTRIES = 500


class StudyPathError(ValueError):
    """A requested path is outside the sandboxed study folder, or the
    folder itself isn't configured/doesn't exist. Always an honest,
    specific message (Rule 2.1) — never a generic 'not found'."""


def study_root() -> Path:
    """The real study folder, honored via an env override for tests
    (DOURMOUSE_STUDY_DIR) — no production caller ever needs to set it."""
    override = os.environ.get("DOURMOUSE_STUDY_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Documents" / _STUDY_DIRNAME


def study_folder_status() -> dict[str, object]:
    """Honest existence check (Rule 2.2) — the UI/tool should never claim
    the folder is there when it isn't."""
    root = study_root()
    return {"path": str(root), "exists": root.is_dir()}


def _resolve_within_root(rel_path: str) -> Path:
    """Resolve ``rel_path`` against the study root and REFUSE anything
    that escapes it, real containment check (resolve() + relative_to()),
    not a string-prefix guess that a "../../etc" or symlink could defeat.
    """
    root = study_root()
    if not root.is_dir():
        raise StudyPathError(f"study folder not found on disk: {root}")
    rel_path = (rel_path or "").strip().lstrip("/")
    candidate = (root / rel_path).resolve() if rel_path else root.resolve()
    real_root = root.resolve()
    try:
        candidate.relative_to(real_root)
    except ValueError:
        raise StudyPathError(
            f"refused: {rel_path!r} resolves outside the study folder"
        ) from None
    return candidate


def list_study_files(rel_path: str = "") -> dict[str, object]:
    """List real files/subfolders under ``rel_path`` within the study
    folder (non-recursive — one level, like a real directory listing).
    Hidden files (dotfiles) are excluded — they're never real study
    material, just OS/app bookkeeping (.DS_Store, .venv, .freebuff)."""
    target = _resolve_within_root(rel_path)
    if not target.is_dir():
        raise StudyPathError(f"not a directory: {rel_path!r}")
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith("."):
            continue
        entries.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "size": child.stat().st_size if child.is_file() else None,
        })
        if len(entries) >= _MAX_LIST_ENTRIES:
            break
    return {"path": rel_path, "entries": entries}


def read_study_file(rel_path: str, max_chars: int = _MAX_READ_CHARS) -> dict[str, object]:
    """Read a real text file from the study folder. Refuses non-text
    (binary) content honestly rather than dumping garbage — a UnicodeDecodeError
    is a real, specific signal, not silently swallowed into empty text."""
    target = _resolve_within_root(rel_path)
    if not target.is_file():
        raise StudyPathError(f"not a file: {rel_path!r}")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise StudyPathError(
            f"{rel_path!r} is not a readable text file (binary content)"
        ) from None
    truncated = len(text) > max_chars
    return {"path": rel_path, "content": text[:max_chars], "truncated": truncated}
