#!/usr/bin/env python3
"""Auto-sync the local dourmouse checkout with upstream (merge-safe).

When upstream (github.com/adit2011238-glitch/dourmouse) pushes new
features, this fetches and MERGES them into the local checkout WITHOUT
clobbering the local integration work (the forex agent, ATLAS Terminal,
atlas_ui agent, morning-report sections).

Rules:
- never rewrites history, never force-pushes, never touches git config
  (identity is passed per-invocation via -c)
- on a merge conflict it ABORTS the merge and reports honestly — it never
  auto-resolves or discards either side
- after a successful merge it re-installs any requirement files that
  changed, then runs the test set that covers our integration
- every run is appended to <repo>/sync_log.txt

Exit codes: 0 = up-to-date or synced ok · 2 = conflict (aborted) ·
3 = error.

Run directly (or via the scheduled task):
    python tools/sync_dourmouse.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"  # Windows
if not VENV_PY.is_file():
    VENV_PY = ROOT / ".venv" / "bin" / "python"      # POSIX
LOG = ROOT / "sync_log.txt"
GIT_ID = ["-c", "user.name=ankit", "-c", "user.email=ankit@local"]
REQUIREMENT_FILES = ["requirements.txt", "requirements-dev.txt",
                     "requirements-atlas-ui.txt"]
# tests that cover the local integration (fast, meaningful)
TEST_PATHS = [
    "dourmouse/tests/test_forex_ops.py",
    "dourmouse/tests/test_atlas_terminal.py",
    "dourmouse/tests/test_dispatch.py",
    "dourmouse/tests/test_atlas_ops.py",
    "dourmouse/tests/test_report.py",
]


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"(sync_log.txt not writable: {exc})")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *GIT_ID, *args],
        cwd=str(ROOT), capture_output=True, text=True, check=check,
    )


def main() -> int:
    log("=== sync start ===")

    if not (ROOT / ".git").is_dir():
        log("ERROR: not a git checkout — aborting.")
        return 3

    # 1. fetch
    fet = git("fetch", "origin", check=False)
    if fet.returncode != 0:
        log(f"ERROR: git fetch failed: {fet.stderr.strip()[:300]}")
        return 3

    head = git("rev-parse", "HEAD").stdout.strip()
    up = git("rev-parse", "origin/main").stdout.strip()
    if not up:
        log("ERROR: origin/main not found — aborting.")
        return 3
    behind = int(git("rev-list", "--count", f"{head}..origin/main").stdout.strip() or 0)
    ahead = int(git("rev-list", "--count", "origin/main..HEAD").stdout.strip() or 0)

    if behind == 0:
        log(f"up to date (HEAD={head[:8]}, {ahead} local commit(s) ahead of upstream)")
        return 0

    log(f"upstream has {behind} new commit(s): {head[:8]} -> {up[:8]} "
        f"(we are {ahead} ahead locally)")

    # 2. dirty-tree check (informational)
    dirty = git("status", "--porcelain").stdout.strip()
    if dirty:
        log(f"WARNING: working tree has {len(dirty.splitlines())} uncommitted "
            f"change(s) — merging anyway (conflicts abort, never clobber).")

    # 3. merge (merge-safe: no rebase, no force)
    mg = git("merge", "--no-edit", "origin/main", check=False)
    if mg.returncode != 0:
        conflicted = git("status", "--porcelain").stdout
        if any(line[:2].strip() in ("UU", "AA", "DD") for line in conflicted.splitlines()):
            git("merge", "--abort", check=False)
            log(f"CONFLICT: upstream changes overlap local integration work. "
                f"Merge ABORTED — nothing was clobbered. Resolve manually, then "
                f"re-run. ({mg.stderr.strip()[:200]})")
            return 2
        log(f"ERROR: merge failed: {mg.stderr.strip()[:300]}")
        return 3

    new_head = git("rev-parse", "HEAD").stdout.strip()
    log(f"merged OK: {head[:8]} -> {new_head[:8]}")

    # 4. re-install requirements that changed
    changed = git("diff", "--name-only", f"{head}..HEAD", "--",
                  *REQUIREMENT_FILES).stdout.split()
    if changed:
        for req in REQUIREMENT_FILES:
            if req in changed:
                log(f"requirements changed: installing {req}")
                if VENV_PY.is_file():
                    inst = subprocess.run(
                        [str(VENV_PY), "-m", "pip", "install", "-q", "-r", str(ROOT / req)],
                        capture_output=True, text=True,
                    )
                    log(f"  pip {req}: {'ok' if inst.returncode == 0 else 'FAILED: ' + inst.stderr.strip()[:200]}")
                else:
                    log(f"  WARNING: no .venv python found ({VENV_PY}) — skip install")
    else:
        log("no requirement files changed — no reinstall needed")

    # 5. run the integration test set
    if VENV_PY.is_file():
        log("running integration tests (owned modules)...")
        t = subprocess.run(
            [str(VENV_PY), "-m", "pytest", *TEST_PATHS, "-q", "-p", "no:warnings"],
            capture_output=True, text=True,
        )
        tail = (t.stdout or t.stderr).strip().splitlines()[-3:]
        log(f"pytest exit={t.returncode} | " + " | ".join(tail))
        if t.returncode != 0:
            log("WARNING: tests failed after sync — inspect before relying on new features.")
    else:
        log("WARNING: no .venv — tests skipped (run start.sh or install requirements first)")

    log("=== sync complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
