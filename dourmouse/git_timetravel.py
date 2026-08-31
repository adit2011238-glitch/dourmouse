"""Real, safe subset of Vision OS checklist item 9: "visual git
time-travel timeline scrubber".

Honest scope, stated plainly (see ``NATIVE_REWRITE_ROADMAP.md``'s own
"Explicitly NOT built yet" note for item 9, which this closes out
partially, not fully):

- **Real, built here: read-only history of DOURMOUSE'S OWN repo.** Real
  ``git log``/``git show`` calls (via ``subprocess``, never a
  reimplementation of git) against the actual project repository — a
  real commit list, a real diff for any commit, and a real
  file-as-of-a-past-commit view. This is genuinely useful time-travel:
  see what changed, when, and why, scrubbing through this project's
  own real history.
- **Deliberately NOT built: automated rollback of ARBITRARY user
  files.** The checklist's own framing ("Auto-versions every file...
  Users can 'scrub' back... to instantly and safely revert") implies a
  general auto-versioning-plus-instant-revert system over ANY file a
  user touches — a real, separate, data-safety-critical feature (what
  gets versioned, how often, where the storage lives, what "safely
  revert" does to files with uncommitted changes) that deserves its own
  deliberate design pass, not a rushed bolt-on. This module never
  writes, checks out, resets, or otherwise mutates the working tree —
  every function here is read-only (``git log``/``git show``/
  ``git diff``, never ``git checkout``/``git reset``/``git revert``).
  An actual revert stays a manual action the user takes themselves in
  a real terminal, matching the same "irreversible/destructive actions
  require a human, not an automated tool" discipline the rest of this
  codebase already follows (gmail_send is REQUIRES_CONFIRMATION,
  uploads are sandboxed, etc.) — not a gap, a deliberate boundary.

Every function takes an explicit ``repo_root`` rather than reaching for
a module-level constant, so tests exercise this against real, disposable
git repos in ``tmp_path`` (real ``git init``/``git commit`` calls, not
mocked subprocess output) instead of depending on this checkout's own
git state. The one real caller (``dourmouse/webui.py``) passes the
actual project root.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

_DEFAULT_TIMEOUT = 10.0
_FIELD_SEP = "\x1f"  # unit separator -- real git commit subjects can contain any other punctuation
_HASH_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _run_git(args: list[str], repo_root: Path, timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def is_git_repo(repo_root: Path) -> bool:
    try:
        proc = _run_git(["rev-parse", "--is-inside-work-tree"], repo_root, timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _valid_hash(commit_hash: str) -> bool:
    return bool(_HASH_RE.match(commit_hash)) or commit_hash in ("HEAD",)


def log(repo_root: Path, limit: int = 50, path: str | None = None) -> dict[str, Any]:
    """Real commit list — hash, short hash, author, ISO date, subject.
    Honest empty/error result on any failure (not a git repo, git not
    on PATH, repo with zero commits yet), never a crash."""
    if not is_git_repo(repo_root):
        return {"ok": False, "commits": [], "error": "not a git repository"}
    fmt = _FIELD_SEP.join(["%H", "%h", "%an", "%ad", "%s"])
    args = ["log", f"-n{max(1, min(limit, 500))}", f"--pretty=format:{fmt}", "--date=iso-strict"]
    if path:
        args += ["--", path]
    try:
        proc = _run_git(args, repo_root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "commits": [], "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        # A real, common non-error case: a brand-new repo with zero commits
        # yet reports this on stderr rather than an empty stdout list.
        return {"ok": False, "commits": [], "error": proc.stderr.strip() or "git log failed"}
    commits = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(_FIELD_SEP)
        if len(parts) != 5:
            continue
        commits.append({
            "hash": parts[0], "short_hash": parts[1],
            "author": parts[2], "date": parts[3], "subject": parts[4],
        })
    return {"ok": True, "commits": commits, "error": None}


def diff(repo_root: Path, commit_hash: str) -> dict[str, Any]:
    """Real unified diff for ONE commit (``git show <hash>``). Read-only
    — never checks anything out."""
    if not _valid_hash(commit_hash):
        return {"ok": False, "diff": "", "error": "bad commit hash"}
    if not is_git_repo(repo_root):
        return {"ok": False, "diff": "", "error": "not a git repository"}
    try:
        proc = _run_git(["show", "--no-color", commit_hash], repo_root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "diff": "", "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "diff": "", "error": proc.stderr.strip() or "git show failed"}
    return {"ok": True, "diff": proc.stdout, "error": None}


def changed_files(repo_root: Path, commit_hash: str) -> dict[str, Any]:
    """Real per-file status (A/M/D/R...) for one commit — what the
    timeline scrubber's file list is built from."""
    if not _valid_hash(commit_hash):
        return {"ok": False, "files": [], "error": "bad commit hash"}
    if not is_git_repo(repo_root):
        return {"ok": False, "files": [], "error": "not a git repository"}
    try:
        proc = _run_git(
            ["show", "--no-color", "--name-status", "--pretty=format:", commit_hash],
            repo_root,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "files": [], "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "files": [], "error": proc.stderr.strip() or "git show failed"}
    files = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        files.append({"status": parts[0], "path": parts[-1]})
    return {"ok": True, "files": files, "error": None}


def file_at(repo_root: Path, commit_hash: str, file_path: str) -> dict[str, Any]:
    """Real content of ONE file as it existed at ONE past commit
    (``git show <hash>:<path>``) — the "scrub back and see the file"
    capability. ``file_path`` is validated (no leading ``-`` so it can
    never be mistaken for a git flag, no ``..`` traversal component) but
    otherwise passed straight to git as a real revision spec — git
    itself is the authority on whether that path existed at that
    commit, this never touches the filesystem directly."""
    if not _valid_hash(commit_hash):
        return {"ok": False, "content": "", "error": "bad commit hash"}
    if not file_path or file_path.startswith("-") or ".." in Path(file_path).parts:
        return {"ok": False, "content": "", "error": "bad file path"}
    if not is_git_repo(repo_root):
        return {"ok": False, "content": "", "error": "not a git repository"}
    try:
        proc = _run_git(["show", f"{commit_hash}:{file_path}"], repo_root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "content": "", "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "content": "", "error": proc.stderr.strip() or "git show failed"}
    return {"ok": True, "content": proc.stdout, "error": None}
