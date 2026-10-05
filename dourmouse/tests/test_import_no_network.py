"""Phase I2: importing the server never touches the network (finding #171).

On 2026-10-05 ``import dourmouse.webui`` took 437 seconds of wall time with under one
second of CPU while the Mac's network was unhealthy, and the full suite stalled. This
file is the guard for the cause people suspect first: network or DNS work at import time.

Each check runs in a fresh interpreter (the test process has already imported the
server), with every socket entry point patched so that a call is recorded AND refused,
and an audit hook watching the lower level events that a patch would miss (a C-level
connect, a name lookup, a bind, a child process). A second check makes the same entry
points sleep instead of raising, with a faulthandler dump armed, which reproduces the
reported symptom: if any import waited on the network it would stall and the dump would
name the line.

What these do not prove: that an import is fast on a cold disk, or that nothing in a
third party package waits on the operating system (a dynamic library load, a security
scan of a freshly installed file). They prove the code in this repository and the
libraries it imports make no network call, no name lookup and start no child process
while being imported.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Runs in the child. argv: mode ("refuse" or "sleep"), then the module names to import
# (or "ALL" for every top level module and package of dourmouse).
CHILD = textwrap.dedent(
    r'''
    import faulthandler, importlib, json, pkgutil, socket, sys, time, traceback

    mode = sys.argv[1]
    names = sys.argv[2:]
    calls = []

    def where():
        return [f"{f.filename.rsplit('dourmouse-recon/', 1)[-1]}:{f.lineno}" for f in traceback.extract_stack(limit=40) if "dourmouse-recon" in f.filename and ".venv" not in f.filename][-3:]

    def blocked(name):
        def f(*a, **k):
            calls.append([name, str(a)[:80], where()])
            if mode == "sleep":
                time.sleep(300)
            raise OSError(f"the network is not available during import ({name})")
        return f

    for fn in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo", "getfqdn", "create_connection"):
        setattr(socket, fn, blocked(fn))
    real_connect = socket.socket.connect
    socket.socket.connect = lambda self, *a: blocked("socket.connect")(*a)

    def audit(event, args):
        if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
                     "socket.bind", "socket.sendto", "socket.sendmsg", "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork"):
            calls.append([event, str(args)[:80], where()])
    sys.addaudithook(audit)

    if mode == "sleep":
        faulthandler.dump_traceback_later(25, exit=True)

    if names == ["ALL"]:
        import dourmouse
        names = ["dourmouse." + m.name for m in pkgutil.iter_modules(dourmouse.__path__) if m.name != "__main__"]
    started = time.time()
    failed = {}
    for name in names:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            # an optional third party package that is not installed is not what is tested here
            if exc.name and not exc.name.startswith("dourmouse"):
                failed[name] = repr(exc)[:120]
            else:
                raise
    print("RESULT:" + json.dumps({"seconds": round(time.time() - started, 2), "calls": calls, "skipped": failed}))
    '''
)


def _run(mode: str, *names: str, timeout: int = 120) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, mode, *names],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert lines, f"the import did not finish (exit {proc.returncode})\nSTDERR:\n{proc.stderr[-3000:]}"
    return json.loads(lines[-1][len("RESULT:"):])


def test_importing_the_server_makes_no_network_call_and_starts_no_process():
    result = _run("refuse", "dourmouse.webui")
    assert result["calls"] == [], f"import-time network or process work: {result['calls']}"


def test_importing_the_server_does_not_wait_when_every_network_call_would_sleep():
    # The reported symptom: a long wall time with no CPU. Every socket entry point sleeps for
    # five minutes here, and a faulthandler dump ends the child after 25 seconds, naming the
    # stack. A clean import finishes in a second or two.
    result = _run("sleep", "dourmouse.webui", timeout=90)
    assert result["calls"] == []
    assert result["seconds"] < 20


def test_no_module_of_the_package_touches_the_network_when_imported():
    result = _run("refuse", "ALL", timeout=240)
    # dlopen of a native library is not network work; only the events listed in CHILD are recorded.
    assert result["calls"] == [], f"import-time network or process work: {result['calls']}"
