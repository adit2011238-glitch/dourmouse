"""Finding N3: the three git wrappers must not let a repository's own config run a program.

A cloned or downloaded repository carries its own ``.git/config`` and ``.gitattributes``.
``log.showSignature`` (runs ``gpg.program`` for every signed commit shown), ``core.fsmonitor``
(runs a hook on every status), ``diff.external`` and a ``textconv`` driver (run a program per
diff) all turn a read into code execution. The wrappers in ``os_api/code.py``,
``git_safety.py`` and ``git_timetravel.py`` now switch those off on the command line.

Two layers of test: the exact argv each wrapper builds (captured), and real git against a
booby-trapped repository, where a marker file proves whether the program ran. Every trap is
first fired by plain, unhardened git so a pass cannot be a trap that never worked.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from dourmouse import git_safety, git_timetravel
from dourmouse.os_api import code as os_code

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

CONFIG = ["-c", "log.showSignature=false", "-c", "core.fsmonitor=false", "-c", "diff.external="]


def _plain_git(repo: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, input=stdin, env=_ENV, check=False)


# --------------------------------------------------------------------------- #
# argv shape
# --------------------------------------------------------------------------- #


class TestHardenArgs:
    def test_diff_show_and_log_gain_both_flags_after_the_subcommand(self):
        for sub in ("diff", "show", "log"):
            out = git_safety.harden_git_args([sub, "-n5", "HEAD"])
            assert out[0] == sub
            assert out[1:3] == ["--no-ext-diff", "--no-textconv"]
            assert out[3:] == ["-n5", "HEAD"]

    def test_other_subcommands_are_untouched(self):
        for args in (["status", "--porcelain"], ["rev-parse", "HEAD"], ["add", "--", "x"], ["commit", "-m", "m"], ["revert", "--no-edit", "abc1234"]):
            assert git_safety.harden_git_args(args) == args

    def test_flags_already_present_are_not_duplicated(self):
        out = git_safety.harden_git_args(["diff", "--numstat", "--no-ext-diff", "--no-textconv"])
        assert out.count("--no-ext-diff") == 1
        assert out.count("--no-textconv") == 1

    def test_empty_args_do_not_crash(self):
        assert git_safety.harden_git_args([]) == []

    def test_the_gpg_program_is_never_overridden(self):
        # git_safety makes real commits: the owner's commit signing must keep working.
        assert not any("gpg" in part for part in git_safety.GIT_HARDENING_CONFIG if part != "-c" and "showSignature" not in part)
        assert "gpg.program" not in " ".join(git_safety.GIT_HARDENING_CONFIG)


class _Capture:
    def __init__(self):
        self.argv: list[str] = []

    def __call__(self, argv, **kwargs):
        self.argv = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class TestEveryWrapperSendsTheHardening:
    def _assert_hardened(self, argv: list[str], sub: str):
        assert argv[0] == "git"
        flat = " ".join(argv)
        for pair in ("log.showSignature=false", "core.fsmonitor=false"):
            assert f"-c {pair}" in flat
        assert "-c diff.external=" in flat
        # the config options come before the subcommand, where git reads them
        assert argv.index("diff.external=") < argv.index(sub)
        if sub in ("diff", "show", "log"):
            assert "--no-ext-diff" in argv and "--no-textconv" in argv
            assert argv.index("--no-ext-diff") > argv.index(sub)

    @pytest.mark.parametrize("sub", ["diff", "show", "log", "status", "rev-parse"])
    def test_os_api_code(self, monkeypatch, tmp_path, sub):
        cap = _Capture()
        monkeypatch.setattr(os_code.subprocess, "run", cap)
        os_code._git(tmp_path, [sub, "x"])
        self._assert_hardened(cap.argv, sub)
        # the existing read-only posture is kept
        assert "--no-pager" in cap.argv and "--literal-pathspecs" in cap.argv

    @pytest.mark.parametrize("sub", ["diff", "show", "log", "add", "commit", "revert", "rev-parse"])
    def test_git_safety(self, monkeypatch, tmp_path, sub):
        cap = _Capture()
        monkeypatch.setattr(git_safety.subprocess, "run", cap)
        git_safety._run_git([sub, "x"], cwd=tmp_path)
        self._assert_hardened(cap.argv, sub)

    @pytest.mark.parametrize("sub", ["diff", "show", "log", "rev-parse"])
    def test_git_timetravel(self, monkeypatch, tmp_path, sub):
        cap = _Capture()
        monkeypatch.setattr(git_timetravel.subprocess, "run", cap)
        git_timetravel._run_git([sub, "x"], tmp_path)
        self._assert_hardened(cap.argv, sub)

    def test_no_wrapper_touches_gpg_program(self, monkeypatch, tmp_path):
        for mod, call in (
            (os_code, lambda: os_code._git(tmp_path, ["log"])),
            (git_safety, lambda: git_safety._run_git(["commit", "-m", "x"], cwd=tmp_path)),
            (git_timetravel, lambda: git_timetravel._run_git(["log"], tmp_path)),
        ):
            cap = _Capture()
            monkeypatch.setattr(mod.subprocess, "run", cap)
            call()
            assert not any("gpg" in a for a in cap.argv), mod.__name__

    def test_the_public_functions_go_through_the_hardened_runner(self, monkeypatch, tmp_path):
        seen: list[list[str]] = []

        def fake_run(argv, **kwargs):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="true\n", stderr="")

        monkeypatch.setattr(git_timetravel.subprocess, "run", fake_run)
        git_timetravel.log(tmp_path)
        git_timetravel.diff(tmp_path, "HEAD")
        git_timetravel.changed_files(tmp_path, "HEAD")
        git_timetravel.file_at(tmp_path, "HEAD", "a.txt")
        assert len(seen) >= 8
        assert all("core.fsmonitor=false" in " ".join(a) for a in seen)
        shows = [a for a in seen if "show" in a]
        assert shows and all("--no-ext-diff" in a and "--no-textconv" in a for a in shows)


# --------------------------------------------------------------------------- #
# real git against a booby-trapped repository
# --------------------------------------------------------------------------- #


def _script(path: Path, marker: Path, body: str = "") -> Path:
    path.write_text(f"#!/bin/sh\necho ran >> '{marker}'\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture(scope="module")
def _trapped_repo(tmp_path_factory):
    """A repo with four traps (built once: git is slow to start here). The marker files
    record which one fired."""
    tmp_path = tmp_path_factory.mktemp("n3")
    repo = tmp_path / "repo"
    repo.mkdir()
    marks = {k: tmp_path / f"{k}.marker" for k in ("external", "textconv", "fsmonitor", "gpg")}
    _plain_git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n")
    (repo / "b.dat").write_text("one\n")
    (repo / ".gitattributes").write_text("*.dat diff=evil\n")
    _plain_git(repo, "add", "-A")
    assert _plain_git(repo, "commit", "-q", "-m", "first").returncode == 0
    (repo / "a.txt").write_text("two\n")
    (repo / "b.dat").write_text("two\n")
    ext = _script(tmp_path / "ext.sh", marks["external"], "exit 0")
    tc = _script(tmp_path / "tc.sh", marks["textconv"], 'cat "$1"')
    fsm = _script(tmp_path / "fsm.sh", marks["fsmonitor"], "exit 0")
    gpg = _script(tmp_path / "gpg.sh", marks["gpg"], "exit 1")
    _plain_git(repo, "config", "diff.external", str(ext))
    _plain_git(repo, "config", "diff.evil.textconv", str(tc))
    _plain_git(repo, "config", "core.fsmonitor", str(fsm))
    _plain_git(repo, "config", "gpg.program", str(gpg))
    _plain_git(repo, "config", "log.showSignature", "true")
    # a commit that carries a (fake) signature header, so showSignature has something to verify
    tree = _plain_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    parent = _plain_git(repo, "rev-parse", "HEAD").stdout.strip()
    obj = (
        f"tree {tree}\nparent {parent}\nauthor T <t@example.com> 1700000000 +0000\n"
        "committer T <t@example.com> 1700000000 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n fake\n -----END PGP SIGNATURE-----\n\nsigned\n"
    )
    sha = _plain_git(repo, "hash-object", "-w", "-t", "commit", "--stdin", stdin=obj).stdout.strip()
    assert sha
    _plain_git(repo, "update-ref", "HEAD", sha)
    _plain_git(repo, "reset", "-q")  # leaves a.txt and b.dat modified in the work tree
    (repo / "a.txt").write_text("three\n")
    (repo / "b.dat").write_text("three\n")
    return repo, marks


@pytest.fixture
def trapped(_trapped_repo):
    repo, marks = _trapped_repo
    for m in marks.values():
        m.unlink(missing_ok=True)
    return repo, marks


def _fired(marks, key):
    return marks[key].exists()


class TestTrapsAreReal:
    """If plain git did not fire a trap, a hardened pass would prove nothing."""

    def test_plain_git_runs_the_signature_program(self, trapped):
        repo, marks = trapped
        _plain_git(repo, "log", "-1")
        assert _fired(marks, "gpg")

    def test_plain_git_runs_the_external_diff_and_textconv(self, trapped):
        repo, marks = trapped
        _plain_git(repo, "diff")
        assert _fired(marks, "external")
        marks["external"].unlink()
        _plain_git(repo, "diff", "--no-ext-diff")
        assert _fired(marks, "textconv")

    def test_plain_git_runs_fsmonitor(self, trapped):
        repo, marks = trapped
        _plain_git(repo, "status")
        assert _fired(marks, "fsmonitor")


class TestHardenedWrappersFireNothing:
    def _no_trap(self, marks):
        fired = sorted(k for k in marks if _fired(marks, k))
        assert fired == [], f"a repository-config program ran: {fired}"

    def test_os_api_code(self, trapped):
        repo, marks = trapped
        for args in (["log", "-n3"], ["diff", "--no-color"], ["show", "--no-color", "HEAD"], ["status", "--porcelain=v1"]):
            p = os_code._git(repo, args)
            assert p.returncode == 0, (args, p.stderr)
        self._no_trap(marks)

    def test_git_safety(self, trapped):
        repo, marks = trapped
        for args in (["log", "-1", "--format=%H%x00%s"], ["diff", "--no-color"], ["show", "--no-color", "HEAD"], ["status", "--porcelain"]):
            p = git_safety._run_git(args, cwd=repo)
            assert p.returncode == 0, (args, p.stderr)
        self._no_trap(marks)

    def test_git_timetravel(self, trapped):
        repo, marks = trapped
        res = git_timetravel.log(repo)
        assert res["ok"], res
        head = res["commits"][0]["hash"]
        assert git_timetravel.diff(repo, head)["ok"]
        assert git_timetravel.changed_files(repo, head)["ok"]
        assert git_timetravel.file_at(repo, head, "a.txt")["ok"]
        self._no_trap(marks)

    def test_output_is_still_real(self, trapped):
        repo, _ = trapped
        res = git_timetravel.log(repo)
        assert [c["subject"] for c in res["commits"]] == ["signed", "first"]
        shown = git_timetravel.diff(repo, res["commits"][1]["hash"])
        assert "first" in shown["diff"]
        # a real diff still works with the drivers switched off
        d = os_code._git(repo, ["diff", "--no-color"])
        assert "+three" in d.stdout.decode()


class TestSafetyStillCommits:
    def test_auto_commit_and_undo_still_work_with_the_hardening(self, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        _plain_git(repo, "init", "-q")
        _plain_git(repo, "config", "user.email", "t@example.com")
        _plain_git(repo, "config", "user.name", "T")
        f = repo / "f.txt"
        f.write_text("hi\n")
        h = git_safety.auto_commit(f, "wrote")
        assert h, "the auto commit must still be made"
        log = _plain_git(repo, "log", "--format=%s").stdout
        assert git_safety.AUTO_COMMIT_PREFIX in log
