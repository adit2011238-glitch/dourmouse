"""Pull fresh Claude Code session files from a remote machine over SSH,
then run the existing local importer (dourmouse/history_import.py).

Built for the real topology on this deployment: Dourmouse runs on the
Windows desktop, but the user's actual Claude Code history lives on
their Mac (verified: this desktop's own ~/.claude/projects has 10 files;
the Mac has 81). history_import.py's importers are pure local-file
parsers with no opinion about WHERE the files came from — this module is
the network layer in front of them, kept separate on purpose so the
importer stays testable without a real SSH round-trip.

Incremental by construction, not by re-scanning: every run asks the
remote host to list only session files modified since the last
successful sync (via `find -newer <marker-file>` — see the comment on
list_remote_changed_files for why every OTHER way this was tried hung),
pulls those over `scp`, and only THEN runs the (already idempotent,
already fast — ~3s for 81 files) local importer over the whole mirror.
Re-scanning the full mirror each time is deliberate: MemoryStore.remember
upserts on (source, title), so re-importing unchanged sessions is a
no-op, and skipping that re-scan to "optimize" it would need its own
staleness tracking for zero real benefit.

THE REAL BUGS, traced live and worth recording because most of the
obvious suspects were NOT it. Two independent, stacked causes, both
required for a reliable connection from this Windows desktop:

1. ``subprocess.run(cmd, capture_output=True)`` — PIPE-based stdout/
   stderr — hangs when it spawns `ssh.exe`/`scp.exe` on this machine, an
   arbitrary amount (0.3s to a full 40s timeout, no pattern), REGARDLESS
   of the remote command (even bare `echo` was affected). Redirecting
   stdout/stderr to real temp FILES instead (``_run_ssh_command`` below)
   was 100% reproducible in both directions: hangs every time with PIPE,
   ~0.2-0.3s every time with files, across many repeated trials. Root
   cause not pinned to a specific OpenSSH-for-Windows/Python interaction
   beyond that; the file-redirection fix is what matters.

2. Separately, and only visible once #1 was fixed: `find -exec ... {}`
   (both `+` and `\\;` forms, action irrelevant — even `-exec echo {} \\;`
   was affected) hangs the full timeout over a NON-INTERACTIVE SSH
   session on this Mac specifically. So does a single `stat` call given
   many files as direct arguments. `find` with no `-exec` at all (a bare
   listing) was always instant. Root cause not conclusively diagnosed
   (a plausible guess: something -exec's child-process spawn touches —
   Gatekeeper, XPC, some per-launch check — needs a session context a
   raw SSH exec without a controlling terminal doesn't have; not
   confirmed). The fix: `find -newer <reference-file>` — the original
   POSIX mtime-comparison form, no `-exec`, no date-string parsing
   either (also ruling out `-newermt` as a contributor, though it turned
   out not to be the actual cause) — one `touch -t` to set up, then one
   plain `find`. 0.2s for all 81 files, every time.

GSSAPI auth negotiation (``-o GSSAPIAuthentication=no`` below) is a
genuine, if minor, third contributor — a raw shell invocation with it
disabled was 3-for-3 instant even before either fix above, though it
alone did not fully explain the hangs. Left on since it never hurts.

``_run_with_retry`` stays as a second line of defense for whatever
genuine transient network hiccup gets through on top of all the above —
it was never the fix for the two hangs above, which were both entirely
local/remote-shell plumbing bugs, not a network condition (`tailscale
ping` confirmed a direct LAN path both directions throughout, single-
digit-to-low-double-digit ms, never a relay).

Auth: a DEDICATED keypair (~/.ssh/dourmouse_pull on the machine running
Dourmouse), authorized on the remote host's authorized_keys, distinct
from any key used in the other direction. Never the user's own SSH
identity — a compromised or misconfigured sync must not be able to
reach anywhere the user's own key can.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from dourmouse.history_import import import_all_history
from dourmouse.memory_store import MemoryStore

_SSH_TIMEOUT = 30  # seconds per remote command; a hung network call must
# never wedge a scheduled sync forever.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 5  # seconds between attempts; see the module docstring


def _run_ssh_command(cmd: list[str], timeout: int) -> SimpleNamespace:
    """Run one ssh/scp subprocess. Returns an object with .returncode,
    .stdout, .stderr — same shape as subprocess.run's result, so callers
    don't care that the plumbing underneath is different.

    Deliberately NOT ``capture_output=True`` (PIPE-based stdio) — see the
    module docstring for the live trace: that hangs unpredictably when
    spawning ssh.exe/scp.exe on this machine, while redirecting to real
    files is fast and reliable every time. This is the ONE function that
    tests mock (``patch("dourmouse.history_sync._run_ssh_command", ...)``)
    rather than raw subprocess.run, precisely so this implementation
    detail can change again later without every test needing to know
    about temp files.
    """
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "stdout.txt"
        err_path = Path(td) / "stderr.txt"
        with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
            proc = subprocess.run(cmd, stdout=out_f, stderr=err_f, timeout=timeout)
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
    return SimpleNamespace(returncode=proc.returncode, stdout=stdout, stderr=stderr)


def _run_with_retry(fn: Callable[[], Any], attempts: int | None = None) -> Any:
    """Call ``fn`` (a zero-arg thunk wrapping one _run_ssh_command) up to
    ``attempts`` times, returning the first result whose returncode is 0.
    A TimeoutExpired/OSError on the final attempt propagates; on an
    earlier attempt it's swallowed and retried like a normal failure.

    ``attempts`` reads the module-level ``_RETRY_ATTEMPTS`` at CALL time
    (not as a baked-in default) specifically so tests can monkeypatch
    ``history_sync._RETRY_ATTEMPTS`` / ``_RETRY_BACKOFF`` and have it take
    effect — a default parameter value would already be bound at import
    time and ignore a later monkeypatch.
    """
    if attempts is None:
        attempts = _RETRY_ATTEMPTS
    last_exc: Exception | None = None
    result = None
    for attempt in range(attempts):
        try:
            result = fn()
        except (subprocess.TimeoutExpired, OSError) as exc:
            last_exc = exc
            result = None
        if result is not None and result.returncode == 0:
            return result
        if attempt < attempts - 1:
            time.sleep(_RETRY_BACKOFF)
    if last_exc is not None:
        raise last_exc
    return result


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def sync_config() -> dict[str, str] | None:
    """Reads DOURMOUSE_HISTORY_SYNC_* env vars. None if not configured —
    honest "not set up" rather than a guessed default, since this
    connects to a SPECIFIC remote machine, not a generic path."""
    host = _env("DOURMOUSE_HISTORY_SYNC_HOST")
    user = _env("DOURMOUSE_HISTORY_SYNC_USER")
    key = _env("DOURMOUSE_HISTORY_SYNC_KEY")
    remote_root = _env("DOURMOUSE_HISTORY_SYNC_REMOTE_ROOT", "~/.claude/projects")
    mirror_root = _env("DOURMOUSE_HISTORY_SYNC_MIRROR")
    if not (host and user and key and mirror_root):
        return None
    return {
        "host": host, "user": user, "key": key,
        "remote_root": remote_root, "mirror_root": mirror_root,
    }


def _marker_path(mirror_root: Path) -> Path:
    return mirror_root.parent / (mirror_root.name + ".last_sync")


def _read_marker(mirror_root: Path) -> int:
    p = _marker_path(mirror_root)
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return 0  # never synced before -> pull everything, once


def _write_marker(mirror_root: Path, epoch: int) -> None:
    _marker_path(mirror_root).write_text(str(epoch))


def _ssh_base(key: str, user: str, host: str) -> list[str]:
    return [
        "ssh", "-i", key,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        # A genuine (if not the whole) contributor to the connection
        # variance traced live -- see the module docstring. Never hurts
        # to skip a negotiation this setup never uses.
        "-o", "GSSAPIAuthentication=no",
        "-o", f"ConnectTimeout={_SSH_TIMEOUT}",
        f"{user}@{host}",
    ]


def list_remote_changed_files(
    key: str, user: str, host: str, remote_root: str, since_epoch: int
) -> list[str] | None:
    """SSH in and list session files modified after ``since_epoch``.
    Returns remote-relative paths (relative to ``remote_root``), or None
    if the remote could not be reached at all — kept distinct from an
    empty list (reached the remote, genuinely nothing new) so the caller
    never advances its sync marker past a connection failure. Advancing
    it anyway would look identical to success and silently drop every
    session created during the outage — there would be no error, no
    retry, just a permanent gap in what got imported.
    """
    # POSIX shell on the remote: expand ~ there, not here — the remote
    # user's home is not necessarily this process's HOME. Traced live: a
    # naive `cd '~/.claude/projects'` (single-quoted) SILENTLY FAILED —
    # tilde expansion never happens inside quotes in bash/zsh, so it
    # tried to cd into a directory literally named "~/.claude/projects".
    # `$HOME` expands fine inside double quotes, so swap the prefix
    # instead of quoting the raw `~`.
    remote_dir = remote_root
    if remote_dir.startswith("~/"):
        remote_dir = "$HOME/" + remote_dir[2:]
    elif remote_dir == "~":
        remote_dir = "$HOME"
    # Two things traced live and both matter here:
    #
    # 1. NOT `find -newermt '@epoch'`: BSD find's date-comparison flag
    #    needs to PARSE a date string, which turned out unrelated to the
    #    actual hang (see #2) but is still worth avoiding — simpler and
    #    strictly more portable to skip remote-side date parsing.
    #
    # 2. NOT `find -exec ... {} +` (nor `\;`), and NOT a single `stat`
    #    call listing every match as arguments either: BOTH hung the
    #    full timeout, reproducibly, over non-interactive SSH on this
    #    Mac — even `find -exec echo {} \;`, action irrelevant, hung.
    #    find's own bare listing (no -exec) was always instant. The fix
    #    that actually worked: `find -newer <reference-file>`, the
    #    original POSIX form — a pure mtime COMPARISON against another
    #    file, no `-exec`, no date-string parsing, and (unlike
    #    `-newermt`) it needed only ONE touch to set up. 0.2s for all 81
    #    files, repeatedly, versus never completing within 15s any other
    #    way tried.
    dt = datetime.fromtimestamp(since_epoch, tz=timezone.utc)
    touch_ts = dt.strftime("%Y%m%d%H%M.%S")  # BSD touch -t: CCYYMMDDhhmm.SS
    remote_cmd = (
        f'cd "{remote_dir}" 2>/dev/null && '
        f"touch -t {touch_ts} /tmp/dourmouse_sync_marker && "
        f"find . -name '*.jsonl' -newer /tmp/dourmouse_sync_marker 2>/dev/null"
    )
    try:
        result = _run_with_retry(
            lambda: _run_ssh_command(
                _ssh_base(key, user, host) + [remote_cmd], _SSH_TIMEOUT + 10
            )
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result is None or result.returncode != 0:
        return None
    return [line.strip().lstrip("./") for line in result.stdout.splitlines() if line.strip()]


def pull_files(
    key: str, user: str, host: str, remote_root: str,
    relative_paths: list[str], mirror_root: Path,
) -> int:
    """scp each file individually, preserving its relative subdirectory
    under ``mirror_root``. Returns how many were pulled successfully.

    Individually, not one batched scp -r: a single bad/renamed path in a
    directory-wide copy can abort the whole transfer, and the caller
    already knows the exact file list — no reason to re-derive it.
    """
    pulled = 0
    for rel in relative_paths:
        dest = mirror_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        remote_path = f"{remote_root.rstrip('/')}/{rel}"
        cmd = [
            "scp", "-i", key,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "GSSAPIAuthentication=no",
            "-o", f"ConnectTimeout={_SSH_TIMEOUT}",
            f"{user}@{host}:{remote_path}", str(dest),
        ]
        try:
            result = _run_with_retry(
                lambda cmd=cmd: _run_ssh_command(cmd, _SSH_TIMEOUT + 30)
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result is not None and result.returncode == 0 and dest.exists():
            pulled += 1
    return pulled


def sync_and_import(
    store: MemoryStore, config: dict[str, str] | None = None
) -> dict[str, Any]:
    """Full cycle: list what changed remotely, pull it, import the whole
    (now-fresh) local mirror, advance the marker only on a clean run.
    """
    cfg = config if config is not None else sync_config()
    if cfg is None:
        return {"ok": False, "reason": "not_configured"}

    mirror_root = Path(cfg["mirror_root"])
    mirror_root.mkdir(parents=True, exist_ok=True)
    since = _read_marker(mirror_root)
    now = int(datetime.now(tz=timezone.utc).timestamp())

    changed = list_remote_changed_files(
        cfg["key"], cfg["user"], cfg["host"], cfg["remote_root"], since
    )
    if changed is None:
        # Couldn't reach the remote at all this round. Still import
        # whatever is already in the mirror (from a prior successful
        # sync) so a network blip doesn't also block recall of history
        # already pulled — but the marker stays put, so the NEXT
        # successful sync picks up from the same point rather than a gap.
        import_result = import_all_history(store, claude_root=mirror_root, codex_db=None)
        return {
            "ok": False, "reason": "remote_unreachable", "since": since,
            "claude": import_result["claude"], "codex": import_result["codex"],
        }

    pulled = pull_files(
        cfg["key"], cfg["user"], cfg["host"], cfg["remote_root"], changed, mirror_root
    )
    import_result = import_all_history(store, claude_root=mirror_root, codex_db=None)
    _write_marker(mirror_root, now)  # reached the remote cleanly this round

    return {
        "ok": True,
        "since": since,
        "changed_remote_files": len(changed),
        "pulled": pulled,
        "claude": import_result["claude"],
        "codex": import_result["codex"],
    }
