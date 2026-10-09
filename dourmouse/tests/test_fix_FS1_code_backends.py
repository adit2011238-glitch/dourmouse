"""FS1 fixes in dourmouse/code_backends.py (Claude CLI runner).

P3-8: the native-tool deny list survives a failing MCP config step.
P3-9: a streamed run killed by the timeout says "timed out", not "NOT SIGNED IN".
P3-10: the shared-desk hint reaches the streaming CODE chat too.
P3-11: the MCP-connection retry resumes the session it just created and is
skipped when the bridge evidently served tool calls.
P3-12: the first-turn briefing follows the session actually used, so a
fresh session after a failed first run still gets it.
P3-13: stderr is drained while stdout streams, so a chatty CLI cannot stall.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time

import pytest

from dourmouse import code_backends


@pytest.fixture(autouse=True)
def _reset_state():
    code_backends._CLAUDE_SESSIONS.clear()
    code_backends._CLAUDE_SESSION_RUN_LOCKS.clear()
    code_backends._mcp_config_path_cache = None
    yield
    code_backends._CLAUDE_SESSIONS.clear()
    code_backends._CLAUDE_SESSION_RUN_LOCKS.clear()
    code_backends._mcp_config_path_cache = None


@pytest.fixture
def fake_cli(monkeypatch):
    monkeypatch.setattr("dourmouse.general_roster._find_claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(
        "dourmouse.model_context.claude_orchestrator_preamble", lambda: "PREAMBLE-TEXT"
    )


class _Proc:
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _Rec:
    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []


def _record_run(monkeypatch, responses):
    rec = _Rec()
    it = iter(responses)

    def _fake_run(argv, **kwargs):
        rec.calls.append((list(argv), kwargs))
        resp = next(it)
        return resp(argv, kwargs) if callable(resp) else resp

    monkeypatch.setattr(code_backends.subprocess, "run", _fake_run)
    return rec


class _StreamProc:
    def __init__(self, lines, returncode=0, stderr_text="", block_until_killed=False):
        self._lines = lines
        self._block = block_until_killed
        self._killed = threading.Event()
        self.returncode = returncode
        self.stdin = self
        self.stderr = self
        self._stderr_text = stderr_text
        self.written = ""
        self.stdout = self._iter()

    def _iter(self):
        yield from self._lines
        if self._block:
            self._killed.wait(10)

    def write(self, text):
        self.written += text

    def close(self):
        pass

    def read(self, *_a):
        text, self._stderr_text = self._stderr_text, ""
        return text

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._killed.set()


def _record_popen(monkeypatch, procs):
    seen = []
    it = iter(procs)

    def _fake_popen(argv, **kwargs):
        proc = next(it)
        seen.append((list(argv), proc))
        return proc

    monkeypatch.setattr(code_backends.subprocess, "Popen", _fake_popen)
    return seen


def _result_line(text):
    return json.dumps({"type": "result", "result": text}) + "\n"


# -- P3-8 -------------------------------------------------------------------- #

def _boom():
    raise OSError("read-only volume")


def test_deny_list_kept_when_mcp_config_fails_blocking_path(monkeypatch, fake_cli, caplog):
    monkeypatch.setattr(code_backends, "user_config_dir", _boom)
    rec = _record_run(monkeypatch, [_Proc()])
    with caplog.at_level(logging.WARNING, logger="dourmouse.code_backends"):
        code_backends.run_code_task("claude", "task", cwd="/tmp/fs1")
    argv = rec.calls[0][0]
    assert "--permission-mode" in argv
    assert argv[argv.index("--disallowedTools") + 1] == code_backends._DISALLOWED_NATIVE_TOOLS
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv
    assert "read-only volume" in caplog.text


def test_deny_list_kept_when_mcp_config_fails_streaming_path(monkeypatch, fake_cli):
    monkeypatch.setattr(code_backends, "user_config_dir", _boom)
    seen = _record_popen(monkeypatch, [_StreamProc([_result_line("ok")])])
    code_backends.stream_claude("task", cwd="/tmp/fs1", timeout=30, on_delta=lambda t: None)
    argv = seen[0][0]
    assert argv[argv.index("--disallowedTools") + 1] == code_backends._DISALLOWED_NATIVE_TOOLS
    assert "--strict-mcp-config" in argv


# -- P3-9 -------------------------------------------------------------------- #

def test_stream_timeout_is_reported_as_a_timeout(monkeypatch, fake_cli):
    proc = _StreamProc([], returncode=None, block_until_killed=True)
    _record_popen(monkeypatch, [proc])
    with pytest.raises(RuntimeError) as exc:
        code_backends.stream_claude("task", cwd="/tmp/fs1", timeout=1, on_delta=lambda t: None)
    assert "timed out after 1s" in str(exc.value)
    assert "NOT SIGNED IN" not in str(exc.value)


# -- P3-10 ------------------------------------------------------------------- #

def test_stream_first_turn_carries_the_shared_desk_hint(monkeypatch, fake_cli):
    first = _StreamProc([_result_line("ok")])
    second = _StreamProc([_result_line("ok")])
    _record_popen(monkeypatch, [first, second])
    code_backends.stream_claude("hello", cwd="/tmp/fs1", timeout=30, on_delta=lambda t: None)
    code_backends.stream_claude("again", cwd="/tmp/fs1", timeout=30, on_delta=lambda t: None)
    assert code_backends._SHARED_DESK_HINT in first.written
    assert "PREAMBLE-TEXT" in first.written
    assert second.written == "again"


# -- P3-11 ------------------------------------------------------------------- #

_MCP_FAIL = "Dourmouse MCP server failed to connect (CONNECTION_CLOSED)."


def test_mcp_retry_resumes_the_session_the_first_attempt_created(monkeypatch, fake_cli):
    rec = _record_run(monkeypatch, [_Proc(stdout=_MCP_FAIL), _Proc(stdout="done")])
    out = code_backends.run_code_task("claude", "task", cwd="/tmp/fs1")
    assert out == "done"
    first, second = rec.calls[0][0], rec.calls[1][0]
    sid = first[first.index("--session-id") + 1]
    assert "--session-id" not in second
    assert second[second.index("--resume") + 1] == sid


def test_no_mcp_retry_when_the_bridge_already_served_tool_calls(monkeypatch, fake_cli):
    def _first(argv, kwargs):
        log_path = kwargs["env"][code_backends._MCP_TOOLCALL_LOG_ENV_VAR]
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"name": "send_email", "raw_arguments": "{}", "result_text": "SENT"}) + "\n")
        return _Proc(stdout="Sent it. (Earlier the MCP server failed to connect, CONNECTION_CLOSED, then recovered.)")

    rec = _record_run(monkeypatch, [_first, _Proc(stdout="second run must not happen")])
    out = code_backends.run_code_task("claude", "send the mail", cwd="/tmp/fs1")
    assert len(rec.calls) == 1
    assert out.startswith("Sent it.")


def test_own_toolcall_log_is_removed_afterwards(monkeypatch, fake_cli):
    paths = []

    def _first(argv, kwargs):
        paths.append(kwargs["env"][code_backends._MCP_TOOLCALL_LOG_ENV_VAR])
        with open(paths[0], "a", encoding="utf-8") as f:
            f.write("{}\n")
        return _Proc()

    _record_run(monkeypatch, [_first])
    code_backends.run_code_task("claude", "task", cwd="/tmp/fs1")
    assert paths and not os.path.exists(paths[0])


# -- P3-12 ------------------------------------------------------------------- #

def test_fresh_session_after_a_failed_first_run_still_gets_the_briefing(monkeypatch, fake_cli):
    no_session = _Proc(returncode=1, stdout="", stderr="No conversation found with session ID: x")
    rec = _record_run(monkeypatch, [_Proc(returncode=1, stdout="", stderr="boom"), no_session, _Proc(stdout="ok")])
    with pytest.raises(RuntimeError):
        code_backends.run_code_task("claude", "first", cwd="/tmp/fs1")
    assert code_backends.run_code_task("claude", "second", cwd="/tmp/fs1") == "ok"
    third_argv, third_kwargs = rec.calls[2]
    assert "--session-id" in third_argv
    assert "PREAMBLE-TEXT" in third_kwargs["input"]
    assert code_backends._SHARED_DESK_HINT in third_kwargs["input"]
    # the --resume attempt of an existing (or maybe existing) session is not re-briefed
    assert "PREAMBLE-TEXT" not in rec.calls[1][1]["input"]


def test_stream_fresh_session_after_a_failed_first_run_still_gets_the_briefing(monkeypatch, fake_cli):
    first = _StreamProc([], returncode=1, stderr_text="boom")
    stale = _StreamProc([], returncode=1, stderr_text="No conversation found with session ID: x")
    fresh = _StreamProc([_result_line("ok")])
    _record_popen(monkeypatch, [first, stale, fresh])
    with pytest.raises(RuntimeError):
        code_backends.stream_claude("first", cwd="/tmp/fs1", timeout=30, on_delta=lambda t: None)
    code_backends.stream_claude("second", cwd="/tmp/fs1", timeout=30, on_delta=lambda t: None)
    assert "PREAMBLE-TEXT" in fresh.written
    assert stale.written == "second"


# -- P3-13 ------------------------------------------------------------------- #

def test_stream_does_not_stall_on_a_large_stderr(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX shell fake CLI")
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "head -c 300000 /dev/zero | tr '\\0' e >&2\n"
        "echo '{\"type\":\"result\",\"result\":\"done\"}'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr("dourmouse.general_roster._find_claude_cli", lambda: str(script))
    monkeypatch.setattr(code_backends, "user_config_dir", lambda: tmp_path)
    started = time.monotonic()
    out = code_backends.stream_claude("task", cwd=str(tmp_path), timeout=8, on_delta=lambda t: None)
    assert out == "done"
    assert time.monotonic() - started < 6
