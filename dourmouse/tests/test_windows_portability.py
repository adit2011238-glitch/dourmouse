"""Finding #088: guards for defects the first Windows CI run exposed."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent
_SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}


def _product_files():
    for path in _PKG.rglob("*.py"):
        if "tests" not in path.relative_to(_PKG).parts:
            yield path


def test_every_text_mode_subprocess_call_names_its_encoding():
    """With text=True and no encoding, Python decodes a child's output with
    the locale code page: cp1252 on Windows. A UTF-8 character from a CLI
    (an em dash, an emoji in a Claude reply) then kills the reader thread
    and the output comes back as None."""
    offenders = []
    for path in _product_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in _SUBPROCESS_FUNCS:
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            text_mode = any(
                isinstance(kw.get(k), ast.Constant) and kw[k].value is True
                for k in ("text", "universal_newlines")
            )
            if text_mode and "encoding" not in kw:
                offenders.append(f"{path.relative_to(_PKG)}:{node.lineno}")
    assert offenders == []


def test_coding_clis_never_receive_the_task_in_argv():
    """The task must travel on stdin (see code_backends._run_claude_once):
    a Windows .cmd shim caps its command line at 8191 characters and the
    first-turn preamble alone is about 16.5 KB."""
    for rel in ("code_backends.py", "general_roster.py"):
        src = (_PKG / rel).read_text(encoding="utf-8")
        assert '*session_args, task, *mcp_args' not in src
        assert '[cli, "exec", task,' not in src


def test_windows_cli_names_carry_their_extensions(monkeypatch):
    from dourmouse import general_roster

    monkeypatch.setattr(general_roster.os, "name", "nt")
    assert general_roster._cli_names("claude") == ["claude.exe", "claude.cmd", "claude.bat"]


@pytest.mark.skipif(os.name != "nt", reason="real Windows path resolution")
def test_npm_shim_is_found_outside_path_on_windows(monkeypatch, tmp_path):
    from dourmouse import general_roster

    npm = tmp_path / "AppData" / "Roaming" / "npm"
    npm.mkdir(parents=True)
    (npm / "claude.cmd").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("PATH", r"C:\Windows\System32")
    monkeypatch.delenv("CLAUDE_CODE_CLI", raising=False)
    assert general_roster._find_claude_cli() == str(npm / "claude.cmd")


@pytest.mark.skipif(os.name != "nt", reason="real Windows path handling")
def test_child_path_gets_no_posix_system_dirs_on_windows(monkeypatch):
    """On Windows Path("/usr/bin") became "D:\\usr\\bin": junk on PATH."""
    from dourmouse import code_backends

    monkeypatch.setenv("PATH", "")
    parts = code_backends._cli_env(None)["PATH"].split(os.pathsep)
    posix = {"/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"}
    assert not [p for p in parts if p.replace("\\", "/").split(":", 1)[-1] in posix]


def test_a_port_already_listening_cannot_be_bound_twice():
    """On Windows plain ThreadingHTTPServer's SO_REUSEADDR let a second
    server share a listening port; DourmouseHTTPServer must refuse it on
    every OS."""
    from http.server import BaseHTTPRequestHandler

    from dourmouse.http_server import DourmouseHTTPServer

    first = DourmouseHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        with pytest.raises(OSError):
            DourmouseHTTPServer(("127.0.0.1", first.server_address[1]), BaseHTTPRequestHandler)
    finally:
        first.server_close()


def test_no_product_code_binds_with_plain_threading_http_server():
    offenders = [
        str(p.relative_to(_PKG)) for p in _product_files()
        if p.name != "http_server.py" and "ThreadingHTTPServer((" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
