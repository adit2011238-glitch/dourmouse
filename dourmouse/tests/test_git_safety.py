"""git_safety.py — Aider port part 1/4: auto-commit + /undo.

Real git operations against a real temp repo (Rule 2.1: hermetic, but
"hermetic" means isolated, not faked — a real `git init` in tmp_path is
exactly as isolated as a mock and actually proves the git commands work).
"""

from __future__ import annotations

import subprocess

import pytest

from dourmouse import git_safety


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "README.md").write_text("hello\n")
    _git(["add", "."], root)
    _git(["commit", "-m", "initial"], root)
    return root


class TestGitRoot:
    def test_finds_root_from_nested_path(self, repo):
        nested = repo / "a" / "b"
        nested.mkdir(parents=True)
        assert git_safety.git_root(nested).resolve() == repo.resolve()

    def test_none_outside_any_repo(self, tmp_path):
        outside = tmp_path / "not_a_repo"
        outside.mkdir()
        assert git_safety.git_root(outside) is None

    def test_is_git_repo(self, repo, tmp_path):
        assert git_safety.is_git_repo(repo) is True
        outside = tmp_path / "plain"
        outside.mkdir()
        assert git_safety.is_git_repo(outside) is False


class TestAutoCommit:
    def test_commits_a_new_file(self, repo):
        target = repo / "new.txt"
        target.write_text("content\n")
        rev = git_safety.auto_commit(target, "wrote")
        assert rev is not None
        log = _git(["log", "-1", "--format=%s"], repo).stdout.strip()
        assert log.startswith(git_safety.AUTO_COMMIT_PREFIX)
        assert "new.txt" in log
        # working tree is clean — the commit really happened
        status = _git(["status", "--porcelain"], repo).stdout
        assert status.strip() == ""

    def test_commits_a_modification(self, repo):
        target = repo / "README.md"
        target.write_text("changed\n")
        rev = git_safety.auto_commit(target, "edited")
        assert rev is not None
        show = _git(["show", "--stat", "HEAD"], repo).stdout
        assert "README.md" in show

    def test_returns_none_outside_a_repo(self, tmp_path):
        outside = tmp_path / "plain"
        outside.mkdir()
        target = outside / "x.txt"
        target.write_text("hi\n")
        assert git_safety.auto_commit(target, "wrote") is None

    def test_no_op_when_content_unchanged(self, repo):
        target = repo / "README.md"
        # write byte-identical content — nothing to stage, nothing to commit
        target.write_text("hello\n")
        before = _git(["rev-parse", "HEAD"], repo).stdout
        rev = git_safety.auto_commit(target, "edited")
        after = _git(["rev-parse", "HEAD"], repo).stdout
        assert rev is None
        assert before == after

    def test_commits_a_deletion(self, repo):
        target = repo / "README.md"
        target.unlink()
        rev = git_safety.auto_commit(target, "deleted")
        assert rev is not None
        show = _git(["show", "--stat", "HEAD"], repo).stdout
        assert "README.md" in show
        assert "deleted" in _git(["log", "-1", "--format=%s"], repo).stdout


class TestUndoLast:
    def test_reverts_an_auto_commit(self, repo):
        target = repo / "scratch.txt"
        target.write_text("v1\n")
        git_safety.auto_commit(target, "wrote")
        assert target.read_text() == "v1\n"

        out = git_safety.undo_last(repo)
        assert "UNDONE" in out
        assert not target.exists()  # the revert removed the file it added

    def test_refuses_to_undo_a_human_commit(self, repo):
        """The safety invariant: undo_last must NEVER touch a commit it
        did not make itself, even if it is the most recent one."""
        (repo / "human.txt").write_text("human work\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "human wrote this"], repo)

        out = git_safety.undo_last(repo)
        assert "REFUSED" in out
        assert (repo / "human.txt").exists()  # untouched

    def test_undo_is_a_revert_not_a_reset(self, repo):
        """History must grow, not shrink — a shared/pushed branch must
        stay safe to undo on."""
        target = repo / "scratch.txt"
        target.write_text("v1\n")
        git_safety.auto_commit(target, "wrote")
        before_count = len(_git(["log", "--format=%H"], repo).stdout.splitlines())
        git_safety.undo_last(repo)
        after_count = len(_git(["log", "--format=%H"], repo).stdout.splitlines())
        assert after_count == before_count + 1  # a NEW commit, nothing removed

    def test_double_undo_second_call_is_refused(self, repo):
        """After one undo, HEAD is the REVERT commit (not prefixed) — a
        second undo must not cascade and start reverting real history."""
        target = repo / "scratch.txt"
        target.write_text("v1\n")
        git_safety.auto_commit(target, "wrote")
        first = git_safety.undo_last(repo)
        assert "UNDONE" in first
        second = git_safety.undo_last(repo)
        assert "REFUSED" in second
