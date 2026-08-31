"""dourmouse/git_timetravel.py — real, read-only git history inspection
(Vision OS checklist item 9's safe subset). See that module's own
docstring for what's real (git log/show against a real repo) vs
deliberately not built (automated rollback of arbitrary user files).

Every test runs against a REAL, disposable git repo created in
tmp_path via real `git init`/`git commit` calls — not mocked
subprocess output — so this proves the module's own subprocess/parsing
logic against real git output, same discipline as test_desktop.py's
real build_app.command runs.
"""

from __future__ import annotations

import subprocess

import pytest

from dourmouse import git_timetravel as gt


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        env={
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": __import__("os").environ.get("PATH", ""),
        },
    )


@pytest.fixture
def real_repo(tmp_path):
    """A real 2-commit git repo: adds a.txt, then modifies it and adds b.txt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first commit")
    (repo / "a.txt").write_text("hello world\n")
    (repo / "b.txt").write_text("second file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second commit")
    return repo


class TestIsGitRepo:
    def test_real_repo_is_true(self, real_repo):
        assert gt.is_git_repo(real_repo) is True

    def test_non_repo_dir_is_false(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert gt.is_git_repo(plain) is False

    def test_missing_dir_is_false_not_a_crash(self, tmp_path):
        assert gt.is_git_repo(tmp_path / "does-not-exist") is False


class TestLog:
    def test_real_two_commit_history(self, real_repo):
        result = gt.log(real_repo, limit=10)
        assert result["ok"] is True
        assert len(result["commits"]) == 2
        subjects = [c["subject"] for c in result["commits"]]
        assert subjects == ["second commit", "first commit"]  # newest first
        for c in result["commits"]:
            assert len(c["hash"]) == 40
            assert c["author"] == "Test"

    def test_limit_is_respected(self, real_repo):
        result = gt.log(real_repo, limit=1)
        assert len(result["commits"]) == 1
        assert result["commits"][0]["subject"] == "second commit"

    def test_path_filter_only_returns_commits_touching_that_file(self, real_repo):
        result = gt.log(real_repo, path="b.txt")
        assert result["ok"] is True
        assert [c["subject"] for c in result["commits"]] == ["second commit"]

    def test_non_repo_is_honest_not_a_crash(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        result = gt.log(plain)
        assert result == {"ok": False, "commits": [], "error": "not a git repository"}


class TestDiff:
    def test_real_diff_for_a_commit(self, real_repo):
        log_result = gt.log(real_repo, limit=1)
        second_hash = log_result["commits"][0]["hash"]
        result = gt.diff(real_repo, second_hash)
        assert result["ok"] is True
        assert "second commit" in result["diff"]
        assert "b.txt" in result["diff"]
        assert "hello world" in result["diff"]

    def test_bad_hash_format_rejected(self, real_repo):
        result = gt.diff(real_repo, "not a hash; rm -rf /")
        assert result["ok"] is False
        assert result["error"] == "bad commit hash"

    def test_hash_that_never_existed_is_honest(self, real_repo):
        result = gt.diff(real_repo, "abc123def456")
        assert result["ok"] is False
        assert result["diff"] == ""
        assert result["error"]

    def test_head_alias_works(self, real_repo):
        result = gt.diff(real_repo, "HEAD")
        assert result["ok"] is True
        assert "second commit" in result["diff"]


class TestChangedFiles:
    def test_real_status_for_second_commit(self, real_repo):
        log_result = gt.log(real_repo, limit=1)
        second_hash = log_result["commits"][0]["hash"]
        result = gt.changed_files(real_repo, second_hash)
        assert result["ok"] is True
        paths = {f["path"]: f["status"] for f in result["files"]}
        assert paths["a.txt"] == "M"
        assert paths["b.txt"] == "A"

    def test_first_commit_shows_addition(self, real_repo):
        log_result = gt.log(real_repo)
        first_hash = log_result["commits"][1]["hash"]
        result = gt.changed_files(real_repo, first_hash)
        assert result["ok"] is True
        assert result["files"] == [{"status": "A", "path": "a.txt"}]


class TestFileAt:
    def test_real_file_content_at_first_commit(self, real_repo):
        log_result = gt.log(real_repo)
        first_hash = log_result["commits"][1]["hash"]
        result = gt.file_at(real_repo, first_hash, "a.txt")
        assert result == {"ok": True, "content": "hello\n", "error": None}

    def test_real_file_content_at_second_commit_shows_the_edit(self, real_repo):
        result = gt.file_at(real_repo, "HEAD", "a.txt")
        assert result["ok"] is True
        assert result["content"] == "hello world\n"

    def test_file_that_did_not_exist_yet_is_honest(self, real_repo):
        log_result = gt.log(real_repo)
        first_hash = log_result["commits"][1]["hash"]
        result = gt.file_at(real_repo, first_hash, "b.txt")
        assert result["ok"] is False
        assert result["content"] == ""
        assert result["error"]

    def test_leading_dash_path_rejected(self, real_repo):
        result = gt.file_at(real_repo, "HEAD", "--upload-pack=evil")
        assert result == {"ok": False, "content": "", "error": "bad file path"}

    def test_directory_traversal_rejected(self, real_repo):
        result = gt.file_at(real_repo, "HEAD", "../../../etc/passwd")
        assert result == {"ok": False, "content": "", "error": "bad file path"}

    def test_empty_path_rejected(self, real_repo):
        result = gt.file_at(real_repo, "HEAD", "")
        assert result["ok"] is False


class TestNeverMutatesTheWorkingTree:
    """The real safety property this whole module exists to hold: no
    function here ever changes real files on disk. Verified by hashing
    the real working tree before and after every read function runs."""

    def test_working_tree_untouched_after_every_read_operation(self, real_repo):
        before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(real_repo),
            capture_output=True, text=True, check=True,
        ).stdout
        assert before == ""  # clean tree to start
        gt.log(real_repo)
        gt.diff(real_repo, "HEAD")
        gt.changed_files(real_repo, "HEAD")
        gt.file_at(real_repo, "HEAD", "a.txt")
        after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(real_repo),
            capture_output=True, text=True, check=True,
        ).stdout
        assert after == ""
