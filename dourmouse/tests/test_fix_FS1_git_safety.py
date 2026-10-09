"""FS1 P5-18: auto_commit must commit exactly the one path, never whatever
the human already had staged, so undo_last cannot revert their work."""

from __future__ import annotations

import subprocess

from dourmouse import git_safety
from dourmouse.tests.test_git_safety import _git, repo  # noqa: F401 - fixture


def _staged(root):
    return set(_git(["diff", "--cached", "--name-only"], root).stdout.split())


def test_auto_commit_leaves_the_humans_staged_work_alone(repo):  # noqa: F811
    (repo / "refactor.py").write_text("human work\n")
    _git(["add", "refactor.py"], repo)
    agent_file = repo / "agent.txt"
    agent_file.write_text("agent\n")

    rev = git_safety.auto_commit(agent_file, "wrote")
    assert rev
    committed = set(_git(["show", "--name-only", "--format=", "HEAD"], repo).stdout.split())
    assert committed == {"agent.txt"}
    assert _staged(repo) == {"refactor.py"}

    # git refuses a revert while other changes are staged; either way the
    # human's staged refactor must come through untouched and still staged.
    out = git_safety.undo_last(repo)
    assert out.startswith(("UNDONE", "UNDO FAILED"))
    assert (repo / "refactor.py").read_text() == "human work\n"
    assert "refactor.py" in _staged(repo)
    if out.startswith("UNDONE"):
        assert not agent_file.exists()


def test_nothing_to_commit_for_the_path_even_if_other_files_are_staged(repo):  # noqa: F811
    (repo / "refactor.py").write_text("human work\n")
    _git(["add", "refactor.py"], repo)
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    tracked = repo / "README.md"
    # the agent "rewrote" an existing file with identical content: no change
    assert git_safety.auto_commit(tracked, "wrote") is None
    assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before
    assert _staged(repo) == {"refactor.py"}


def test_a_deleted_tracked_file_is_committed_alone(repo):  # noqa: F811
    tracked = repo / "README.md"
    (repo / "refactor.py").write_text("human work\n")
    _git(["add", "refactor.py"], repo)
    tracked.unlink()
    assert git_safety.auto_commit(tracked, "deleted")
    show = subprocess.run(
        ["git", "show", "--name-status", "--format=", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.split()
    assert show == ["D", tracked.name]
    assert _staged(repo) == {"refactor.py"}
