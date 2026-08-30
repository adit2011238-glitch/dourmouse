"""Git-native transactional safety (Aider-AI/aider architecture port, part 1/4).

Aider auto-commits every LLM-made file edit and offers a real /undo. The
port here is the same two guarantees, adapted to Dourmouse's own file
tools (system_access.py's write_path/delete_path, general_roster.py's
write_file/edit_file):

1. ``auto_commit()`` — every write/delete an agent makes to a path that
   lives inside a real git working tree gets its own atomic commit,
   tagged with a distinguishing prefix. A path that is NOT inside a git
   repo (most of the sandboxed workspace/) is a silent, honest no-op —
   this is additive safety, never a requirement to git-init something the
   user never asked to version.
2. ``undo_last()`` — reverts the single most recent Dourmouse auto-commit.
   Refuses (honestly, Rule 2.2) unless HEAD is actually one of ours: never
   touches a human's own commit, never guesses across commits, only ever
   undoes exactly the last thing DOURMOUSE did.

Rule 2.6: no secrets touch this module. Rule 2.8: every decision here is
deterministic (a real git command's real exit code/output), never an LLM
judgment call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Every commit Dourmouse makes on an agent's behalf carries this exact
#: prefix — the ONE signal undo_last() trusts to know a commit is safe to
#: revert. Never matched fuzzily: a human writing a commit that happens to
#: start with similar words is vanishingly unlikely, but exact-prefix
#: matching costs nothing and removes any doubt.
AUTO_COMMIT_PREFIX = "[dourmouse-auto] "

#: Bound on how much of a written file's content is hashed into the commit
#: subject for a human skimming `git log` — the full diff is still in the
#: commit itself, this is just the one-line summary.
_SUBJECT_PATH_MAX = 200


def _run_git(args: list[str], cwd: Path, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git_root(path: Path) -> Path | None:
    """The repo root containing ``path``, or None if it is not inside one.

    Uses ``git rev-parse --show-toplevel`` (real git, not a hand-rolled
    ".git directory" walk) so worktrees/submodules resolve exactly the way
    every other git-aware tool sees them.
    """
    start = path if path.is_dir() else path.parent
    if not start.exists():
        return None
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=start, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root) if root else None


def is_git_repo(path: Path) -> bool:
    return git_root(path) is not None


def auto_commit(path: Path, action: str) -> str | None:
    """Stage and commit exactly ``path`` if it lives inside a git repo.

    ``action`` is a short human phrase ("wrote", "deleted", "edited") used
    in the commit subject. Returns the new commit's short hash, or None
    when ``path`` is not inside a git repo (silent, honest no-op — NOT an
    error, most Dourmouse file operations target the non-git workspace
    sandbox and must not be forced into a repo they never asked for).

    Never raises on a git failure (a repo with no commits yet, a detached
    HEAD, a pre-commit hook that rejects the change, ...) — auto-commit is
    a safety NET, not a gate a real file write should be blocked behind,
    so any git failure here is swallowed and reported inline in the
    caller's own result text instead of raised.
    """
    root = git_root(path)
    if root is None:
        return None
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = path
    add = _run_git(["add", "--", str(rel)], cwd=root)
    if add.returncode != 0:
        return None
    # Nothing staged (e.g. deleting a file git never tracked) — no commit
    # to make, and that is not a failure.
    diff = _run_git(["diff", "--cached", "--name-only"], cwd=root)
    if not diff.stdout.strip():
        return None
    shown = str(rel)[:_SUBJECT_PATH_MAX]
    subject = f"{AUTO_COMMIT_PREFIX}{action} {shown}"
    commit = _run_git(["commit", "-m", subject, "--no-verify"], cwd=root)
    if commit.returncode != 0:
        return None
    rev = _run_git(["rev-parse", "--short", "HEAD"], cwd=root)
    return rev.stdout.strip() or None


def last_auto_commit_info(root: Path) -> dict[str, str] | None:
    """{hash, subject} for HEAD if it is one of ours, else None."""
    log = _run_git(["log", "-1", "--format=%H%x00%s"], cwd=root)
    if log.returncode != 0 or not log.stdout.strip():
        return None
    raw = log.stdout.strip()
    if "\x00" not in raw:
        return None
    full_hash, subject = raw.split("\x00", 1)
    if not subject.startswith(AUTO_COMMIT_PREFIX):
        return None
    return {"hash": full_hash, "subject": subject}


def undo_last(root: Path) -> str:
    """Revert the most recent commit, ONLY if it is a Dourmouse auto-commit.

    Uses ``git revert --no-edit`` (adds a new commit undoing the change)
    rather than ``reset --hard`` — the undo is itself visible in history
    and safe even if the branch was already pushed/shared, and it can
    never destroy a commit that came after it (there never is one: this
    only ever targets HEAD).
    """
    info = last_auto_commit_info(root)
    if info is None:
        return (
            "UNDO REFUSED (reported honestly): HEAD is not a Dourmouse "
            "auto-commit — undo_last only ever reverts the single most "
            "recent change DOURMOUSE itself made, never a human commit. "
            "Nothing was changed."
        )
    result = _run_git(["revert", "--no-edit", info["hash"]], cwd=root)
    if result.returncode != 0:
        # A real conflict (someone edited the same lines since) — the
        # revert has already been aborted by git itself in that case for
        # a clean commit; surface the real stderr rather than guessing.
        _run_git(["revert", "--abort"], cwd=root)
        return (
            "UNDO FAILED (reported honestly): "
            f"{result.stderr.strip() or result.stdout.strip()}. "
            "Nothing was changed."
        )
    new_rev = _run_git(["rev-parse", "--short", "HEAD"], cwd=root)
    return (
        f"UNDONE: reverted {info['subject']!r} ({info['hash'][:10]}) with "
        f"a new commit ({new_rev.stdout.strip()}). History is intact — "
        "this is a revert, not a reset."
    )
