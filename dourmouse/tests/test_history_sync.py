"""Tests for dourmouse/history_sync.py — the network layer in front of
history_import.py's local-file parsers.

``_run_ssh_command`` is mocked throughout (no test may make a real SSH
call) — that is deliberately the ONE function every test patches, not
raw subprocess.run, because its whole reason to exist is hiding an
implementation detail (temp-file redirection instead of PIPEs — see its
docstring for why) that tests have no business knowing about. The one
thing worth stress-testing without a mock is the marker logic, since a
bug there either re-copies everything forever or silently drops a gap in
what got imported — both wrong in ways that would look like success.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dourmouse import history_sync
from dourmouse.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem" / "test.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    """Every remote call retries with a real sleep() in production (see
    the module docstring on _run_with_retry -- a genuine second line of
    defense, kept even after the real hang was root-caused to subprocess
    PIPE plumbing rather than the network). Tests must never sleep: one
    attempt, zero backoff, so a mocked failure resolves instantly."""
    monkeypatch.setattr(history_sync, "_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(history_sync, "_RETRY_BACKOFF", 0)


def _ok(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _mock_ssh(side_effect=None, return_value=None):
    """patch() context manager for dourmouse.history_sync._run_ssh_command."""
    if side_effect is not None:
        return patch.object(history_sync, "_run_ssh_command", side_effect=side_effect)
    return patch.object(history_sync, "_run_ssh_command", return_value=return_value)


class TestRunSshCommand:
    def test_returns_stdout_stderr_returncode_from_a_real_subprocess(self, tmp_path):
        """The one test that exercises the real temp-file redirection
        path end to end (still no network -- runs a local, harmless
        command) rather than mocking subprocess.run, since that's the
        exact layer the hang lived in."""
        import sys

        cmd = [sys.executable, "-c", "print('hello'); import sys; sys.exit(3)"]
        result = history_sync._run_ssh_command(cmd, timeout=10)
        assert result.returncode == 3
        assert "hello" in result.stdout

    def test_timeout_raises_timeout_expired(self):
        import sys

        cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
        with pytest.raises(subprocess.TimeoutExpired):
            history_sync._run_ssh_command(cmd, timeout=0.2)


class TestRunWithRetry:
    """A transient first (or second) failure must be absorbed, not
    reported as unreachable -- a real, if secondary, defense on top of
    the actual hang fix (see the module docstring)."""

    def test_succeeds_on_a_later_attempt(self, monkeypatch):
        monkeypatch.setattr(history_sync, "_RETRY_ATTEMPTS", 3)
        monkeypatch.setattr(history_sync, "_RETRY_BACKOFF", 0)
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                return _ok(returncode=1)
            return _ok(returncode=0, stdout="finally")

        result = history_sync._run_with_retry(flaky)
        assert result.returncode == 0
        assert result.stdout == "finally"
        assert calls["n"] == 3

    def test_gives_up_after_exhausting_attempts(self, monkeypatch):
        monkeypatch.setattr(history_sync, "_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr(history_sync, "_RETRY_BACKOFF", 0)
        calls = {"n": 0}

        def always_fails():
            calls["n"] += 1
            return _ok(returncode=1)

        result = history_sync._run_with_retry(always_fails)
        assert result.returncode == 1
        assert calls["n"] == 2  # tried exactly _RETRY_ATTEMPTS times, no more

    def test_an_exception_on_a_later_attempt_recovers(self, monkeypatch):
        monkeypatch.setattr(history_sync, "_RETRY_ATTEMPTS", 3)
        monkeypatch.setattr(history_sync, "_RETRY_BACKOFF", 0)
        calls = {"n": 0}

        def hangs_once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.TimeoutExpired("ssh", 15)
            return _ok(returncode=0)

        result = history_sync._run_with_retry(hangs_once)
        assert result.returncode == 0
        assert calls["n"] == 2

    def test_exception_on_the_final_attempt_propagates(self, monkeypatch):
        monkeypatch.setattr(history_sync, "_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr(history_sync, "_RETRY_BACKOFF", 0)

        def always_times_out():
            raise subprocess.TimeoutExpired("ssh", 15)

        with pytest.raises(subprocess.TimeoutExpired):
            history_sync._run_with_retry(always_times_out)

    def test_default_attempts_reads_the_module_global_live(self, monkeypatch):
        """Regression: a plain `attempts: int = _RETRY_ATTEMPTS` default
        parameter is bound once at import time and would silently ignore
        a monkeypatch applied afterward -- attempts must be resolved
        inside the function body instead."""
        monkeypatch.setattr(history_sync, "_RETRY_ATTEMPTS", 1)
        monkeypatch.setattr(history_sync, "_RETRY_BACKOFF", 0)
        calls = {"n": 0}

        def always_fails():
            calls["n"] += 1
            return _ok(returncode=1)

        history_sync._run_with_retry(always_fails)
        assert calls["n"] == 1


class TestSshBaseOptions:
    def test_gssapi_auth_is_disabled(self):
        """A genuine (if not the whole) contributor to the connection
        variance traced live -- see the module docstring. Never hurts to
        skip a negotiation this setup never uses. Regression guard: this
        option must never quietly disappear from a future edit."""
        cmd = history_sync._ssh_base("k", "u", "h")
        assert "GSSAPIAuthentication=no" in cmd


class TestSyncConfig:
    def test_none_when_unconfigured(self, monkeypatch):
        for var in (
            "DOURMOUSE_HISTORY_SYNC_HOST", "DOURMOUSE_HISTORY_SYNC_USER",
            "DOURMOUSE_HISTORY_SYNC_KEY", "DOURMOUSE_HISTORY_SYNC_MIRROR",
        ):
            monkeypatch.delenv(var, raising=False)
        assert history_sync.sync_config() is None

    def test_present_when_fully_set(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_HISTORY_SYNC_HOST", "100.1.2.3")
        monkeypatch.setenv("DOURMOUSE_HISTORY_SYNC_USER", "alice")
        monkeypatch.setenv("DOURMOUSE_HISTORY_SYNC_KEY", "/keys/pull")
        monkeypatch.setenv("DOURMOUSE_HISTORY_SYNC_MIRROR", "/mirror")
        cfg = history_sync.sync_config()
        assert cfg == {
            "host": "100.1.2.3", "user": "alice", "key": "/keys/pull",
            "remote_root": "~/.claude/projects", "mirror_root": "/mirror",
        }

    def test_partial_config_is_treated_as_unconfigured(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_HISTORY_SYNC_HOST", "100.1.2.3")
        monkeypatch.delenv("DOURMOUSE_HISTORY_SYNC_USER", raising=False)
        monkeypatch.delenv("DOURMOUSE_HISTORY_SYNC_KEY", raising=False)
        monkeypatch.delenv("DOURMOUSE_HISTORY_SYNC_MIRROR", raising=False)
        assert history_sync.sync_config() is None


class TestListRemoteChangedFiles:
    def test_parses_file_list_on_success(self):
        with _mock_ssh(return_value=_ok(stdout="./a.jsonl\n./b/c.jsonl\n")):
            result = history_sync.list_remote_changed_files("k", "u", "h", "~/root", 0)
        assert result == ["a.jsonl", "b/c.jsonl"]

    def test_uses_find_newer_not_exec_or_newermt(self):
        """Regression, the load-bearing fix of this whole module: `find
        -exec ...` (any form, any action) and a single `stat` call given
        many files as arguments both hung the full timeout over a non-
        interactive SSH session on the real Mac this was built against --
        `find` with no -exec at all was always instant. `-newer
        <reference-file>` (touch a marker, then a bare find) is the fix:
        no -exec, no remote-side date-string parsing (rules out
        `-newermt` too, a separate, smaller contributor)."""
        captured = {}

        def fake_run(cmd, timeout):
            captured["remote_cmd"] = cmd[-1]
            return _ok(stdout="")

        with _mock_ssh(side_effect=fake_run):
            history_sync.list_remote_changed_files("k", "u", "h", "~/root", 0)
        remote_cmd = captured["remote_cmd"]
        assert "-exec" not in remote_cmd
        assert "-newermt" not in remote_cmd
        assert "touch -t" in remote_cmd
        assert "-newer" in remote_cmd

    def test_touch_timestamp_reflects_since_epoch(self):
        """The marker file's timestamp is what actually encodes
        since_epoch -- get the BSD touch -t format (CCYYMMDDhhmm.SS)
        wrong and every sync either re-pulls everything or misses
        everything, silently."""
        captured = {}

        def fake_run(cmd, timeout):
            captured["remote_cmd"] = cmd[-1]
            return _ok(stdout="")

        # 2024-01-15 10:30:00 UTC
        with _mock_ssh(side_effect=fake_run):
            history_sync.list_remote_changed_files("k", "u", "h", "~/root", 1705314600)
        assert "touch -t 202401151030.00" in captured["remote_cmd"]

    def test_tilde_remote_root_is_never_single_quoted(self):
        """Regression: `cd '~/root'` (single-quoted) silently fails on a
        real remote -- bash/zsh never expand ~ inside quotes, so it tries
        to cd into a directory literally named "~/root". $HOME must
        replace the ~ prefix before quoting."""
        captured = {}

        def fake_run(cmd, timeout):
            captured["remote_cmd"] = cmd[-1]
            return _ok(stdout="")

        with _mock_ssh(side_effect=fake_run):
            history_sync.list_remote_changed_files("k", "u", "h", "~/.claude/projects", 0)
        remote_cmd = captured["remote_cmd"]
        assert "'~" not in remote_cmd
        assert "$HOME/.claude/projects" in remote_cmd

    def test_empty_stdout_is_a_real_empty_list_not_none(self):
        """A working connection that finds nothing new must be
        distinguishable from a failed connection."""
        with _mock_ssh(return_value=_ok(stdout="")):
            result = history_sync.list_remote_changed_files("k", "u", "h", "~/root", 0)
        assert result == []

    def test_nonzero_exit_is_none(self):
        with _mock_ssh(return_value=_ok(returncode=255)):
            result = history_sync.list_remote_changed_files("k", "u", "h", "~/root", 0)
        assert result is None

    def test_timeout_is_none_not_a_crash(self):
        with _mock_ssh(side_effect=subprocess.TimeoutExpired("ssh", 5)):
            result = history_sync.list_remote_changed_files("k", "u", "h", "~/root", 0)
        assert result is None

    def test_connection_error_is_none_not_a_crash(self):
        with _mock_ssh(side_effect=OSError("no route to host")):
            result = history_sync.list_remote_changed_files("k", "u", "h", "~/root", 0)
        assert result is None


class TestPullFiles:
    def test_pulls_each_file_to_its_relative_path(self, tmp_path):
        mirror = tmp_path / "mirror"

        def fake_scp(cmd, timeout):
            import pathlib
            pathlib.Path(cmd[-1]).write_text("fake content")
            return _ok(returncode=0)

        with _mock_ssh(side_effect=fake_scp):
            n = history_sync.pull_files("k", "u", "h", "~/root", ["a.jsonl", "sub/b.jsonl"], mirror)
        assert n == 2
        assert (mirror / "a.jsonl").exists()
        assert (mirror / "sub" / "b.jsonl").exists()

    def test_one_failure_does_not_abort_the_rest(self, tmp_path):
        mirror = tmp_path / "mirror"
        calls = {"n": 0}

        def fake_scp(cmd, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return _ok(returncode=1)  # first file fails
            import pathlib
            pathlib.Path(cmd[-1]).write_text("ok")
            return _ok(returncode=0)

        with _mock_ssh(side_effect=fake_scp):
            n = history_sync.pull_files("k", "u", "h", "~/root", ["bad.jsonl", "good.jsonl"], mirror)
        assert n == 1
        assert not (mirror / "bad.jsonl").exists()
        assert (mirror / "good.jsonl").exists()

    def test_timeout_on_one_file_does_not_abort_the_rest(self, tmp_path):
        mirror = tmp_path / "mirror"
        calls = {"n": 0}

        def fake_scp(cmd, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.TimeoutExpired("scp", 5)
            import pathlib
            pathlib.Path(cmd[-1]).write_text("ok")
            return _ok(returncode=0)

        with _mock_ssh(side_effect=fake_scp):
            n = history_sync.pull_files("k", "u", "h", "~/root", ["slow.jsonl", "fast.jsonl"], mirror)
        assert n == 1


class TestMarker:
    def test_unset_marker_reads_as_zero(self, tmp_path):
        assert history_sync._read_marker(tmp_path / "mirror") == 0

    def test_write_then_read_round_trips(self, tmp_path):
        mirror = tmp_path / "mirror"
        history_sync._write_marker(mirror, 12345)
        assert history_sync._read_marker(mirror) == 12345

    def test_marker_lives_next_to_not_inside_the_mirror(self, tmp_path):
        """It must not be inside the mirror directory, or it would get
        swept up as a fake session file by the importer's rglob."""
        mirror = tmp_path / "mirror"
        mirror.mkdir()
        history_sync._write_marker(mirror, 1)
        assert not (mirror / ".last_sync").exists()
        assert history_sync._marker_path(mirror).parent == mirror.parent


class TestSyncAndImport:
    @pytest.fixture(autouse=True)
    def _no_real_codex(self, tmp_path, monkeypatch):
        """sync_and_import also runs the LOCAL Codex importer every round
        (no network involved there). Point it at a path that doesn't
        exist so every test in this class is isolated from whatever the
        machine actually running these tests has in its real Codex
        history, rather than depending on machine state pytest never
        controls."""
        monkeypatch.setenv("DOURMOUSE_CODEX_STATE_DB", str(tmp_path / "no_codex_here.sqlite"))

    def test_not_configured_returns_honest_status(self, store):
        with patch.object(history_sync, "sync_config", return_value=None):
            result = history_sync.sync_and_import(store)
        assert result == {"ok": False, "reason": "not_configured"}

    def test_unreachable_remote_does_not_advance_the_marker(self, store, tmp_path):
        mirror = tmp_path / "mirror"
        cfg = {"host": "h", "user": "u", "key": "k", "remote_root": "~/r", "mirror_root": str(mirror)}
        with _mock_ssh(side_effect=OSError("unreachable")):
            result = history_sync.sync_and_import(store, config=cfg)
        assert result["ok"] is False
        assert result["reason"] == "remote_unreachable"
        assert history_sync._read_marker(mirror) == 0  # untouched

    def test_successful_run_advances_the_marker_past_zero(self, store, tmp_path):
        mirror = tmp_path / "mirror"
        cfg = {"host": "h", "user": "u", "key": "k", "remote_root": "~/r", "mirror_root": str(mirror)}
        with _mock_ssh(return_value=_ok(stdout="")):  # reachable, nothing new
            result = history_sync.sync_and_import(store, config=cfg)
        assert result["ok"] is True
        assert result["changed_remote_files"] == 0
        assert history_sync._read_marker(mirror) > 0

    def test_pulled_files_actually_get_imported(self, store, tmp_path):
        import json

        mirror = tmp_path / "mirror"
        cfg = {"host": "h", "user": "u", "key": "k", "remote_root": "~/r", "mirror_root": str(mirror)}

        def fake_run(cmd, timeout):
            if cmd[0] == "ssh":
                return _ok(stdout="./s1.jsonl\n")
            # scp: write a real, importable session file to the destination
            import pathlib
            dest = pathlib.Path(cmd[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            line = {
                "type": "user",
                "message": {"role": "user", "content": "synced from the mac"},
                "timestamp": "2026-08-18T00:00:00Z",
                "origin": {"kind": "human"},
            }
            dest.write_text(json.dumps(line) + "\n")
            return _ok(returncode=0)

        with _mock_ssh(side_effect=fake_run):
            result = history_sync.sync_and_import(store, config=cfg)

        assert result["pulled"] == 1
        assert result["claude"]["imported"] == 1
        assert store.count() == 1
        assert "synced from the mac" in store.all_facts()[0]["title"]

    def test_second_run_with_nothing_new_does_not_duplicate(self, store, tmp_path):
        """The idempotency guarantee end to end: sync twice, count stays put."""
        mirror = tmp_path / "mirror"
        cfg = {"host": "h", "user": "u", "key": "k", "remote_root": "~/r", "mirror_root": str(mirror)}

        with _mock_ssh(return_value=_ok(stdout="")):
            history_sync.sync_and_import(store, config=cfg)
            n1 = store.count()
            history_sync.sync_and_import(store, config=cfg)
            n2 = store.count()
        assert n1 == n2
