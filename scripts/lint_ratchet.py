#!/usr/bin/env python3
"""Lint ratchet: ruff and mypy findings may never go UP.

The codebase carries a known lint backlog (REMAINING_WORK BACK-1 for ruff,
BACK-2 for mypy). Gating CI on zero findings would fail every build today;
not gating at all lets new findings pile onto the backlog unseen. So this
compares the current counts against the committed baseline in
scripts/lint_baseline.json and fails if any single ruff rule's count, or the
mypy error total, went UP. A drop is reported so the baseline can be
tightened with --update, which refuses to raise any number: the only way a
count can go up is a human editing the JSON by hand, visibly, in review.

Exit codes: 0 no regression, 1 regression (or --update refused), 2 tool crash.
Finding #085.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts" / "lint_baseline.json"


def ruff_counts() -> dict[str, int]:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "dourmouse", "--output-format", "json", "--exit-zero"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout, proc.stderr, sep="\n", file=sys.stderr)
        raise SystemExit(2)
    items = json.loads(proc.stdout or "[]")
    # A syntax error has no rule code; count it under its own honest bucket.
    return dict(collections.Counter(i.get("code") or "SYNTAX-ERROR" for i in items))


_MYPY_ERRORS: list[str] = []


def mypy_total() -> int:
    proc = subprocess.run(
        # --platform pins sys.platform branches so a Mac and the Linux CI
        # runner count the same code; otherwise the totals legitimately differ.
        [sys.executable, "-m", "mypy", "--platform", "linux", "dourmouse"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    # mypy exits 1 when it finds type errors (expected, that is the backlog);
    # 2 means mypy itself failed, which must never be read as "zero errors".
    if proc.returncode == 2:
        print(proc.stdout, proc.stderr, sep="\n", file=sys.stderr)
        raise SystemExit(2)
    _MYPY_ERRORS[:] = [ln for ln in proc.stdout.splitlines() if ": error:" in ln]
    m = re.search(r"Found (\d+) errors? in", proc.stdout)
    if m:
        return int(m.group(1))
    if "Success: no issues found" in proc.stdout:
        return 0
    print(proc.stdout, proc.stderr, sep="\n", file=sys.stderr)
    raise SystemExit(2)  # unrecognised output: refuse to guess a number


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="tighten the baseline to today's lower counts")
    args = ap.parse_args()

    current = {"ruff": ruff_counts(), "mypy_errors": mypy_total()}
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else None

    if base is None:
        if not args.update:
            print("no baseline yet; run with --update to create it", file=sys.stderr)
            return 1
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"baseline created: ruff {sum(current['ruff'].values())}, mypy {current['mypy_errors']}")
        return 0

    worse, better = [], []
    for code in sorted(set(base["ruff"]) | set(current["ruff"])):
        was, now = base["ruff"].get(code, 0), current["ruff"].get(code, 0)
        if now > was:
            worse.append(f"ruff {code}: {was} -> {now}")
        elif now < was:
            better.append(f"ruff {code}: {was} -> {now}")
    if current["mypy_errors"] > base["mypy_errors"]:
        worse.append(f"mypy errors: {base['mypy_errors']} -> {current['mypy_errors']}")
        # The total alone cannot say WHICH error is new; print them all so the
        # new one can be found by diffing against a local run.
        print("mypy errors (all):")
        for ln in _MYPY_ERRORS:
            print("  " + ln)
    elif current["mypy_errors"] < base["mypy_errors"]:
        better.append(f"mypy errors: {base['mypy_errors']} -> {current['mypy_errors']}")

    for line in better:
        print("improved  " + line)
    for line in worse:
        print("REGRESSED " + line)

    if args.update:
        if worse:
            print("refusing --update: counts went up. Fix them, do not ratchet them in.", file=sys.stderr)
            return 1
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print("baseline tightened")
        return 0

    total_r, total_m = sum(current["ruff"].values()), current["mypy_errors"]
    if worse:
        print(f"LINT RATCHET FAILED (ruff {total_r}, mypy {total_m})")
        return 1
    print(f"lint ratchet OK (ruff {total_r}, mypy {total_m})" + ("; run with --update to lock in the gains" if better else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
