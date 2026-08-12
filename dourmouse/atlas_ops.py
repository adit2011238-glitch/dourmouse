"""ATLAS command-centre telemetry (v4.0) — real project status for Dourmouse.

Gives the roster honest, deterministic answers about the REAL ATLAS quant
repo without fabricating anything (Rules 2.1 / 2.2 / 2.8):

- ``atlas_status()`` — repo present? branch, last commit, dirty-file count,
  source/test file counts (via ``git`` + path walks — deterministic, no LLM).
- ``atlas_bootstrap_status()`` — the deep-FX-archive backfill: pair-day counts
  per major from ``data/fx_archive/raw``, the backfill log tail, and whether
  the completion marker exists. Pure filesystem reads — no subprocess.
- ``atlas_deliverables()`` — newest research outputs under ``deliverables/``
  with sizes and mtimes.

Configuration (same convention as research_agent): ``ATLAS_REPO_PATH`` env.
Until it is set, every tool reports NOT CONFIGURED honestly — never a stub.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


class AtlasNotConfiguredError(NotImplementedError):
    pass


def get_atlas_repo_path() -> Path:
    """The real ATLAS repo root, or raise honestly (Rule 2.2).

    Single source of truth is :func:`research_agent.get_atlas_repo_path`,
    which resolves ``ATLAS_REPO_PATH`` first and then falls back to the
    bundled ``atlas/`` engine shipped next to the package in a personal
    dist (so the app works with NO external repo path configured). The
    exception class is re-raised as this module's own so the public
    contract (``AtlasNotConfiguredError``) is unchanged for callers.
    """
    from dourmouse.research_agent import (
        AtlasNotConfiguredError as _ResearchAtlasNotConfiguredError,
        get_atlas_repo_path as _resolve_repo_path,
    )

    try:
        return _resolve_repo_path()
    except _ResearchAtlasNotConfiguredError as exc:
        raise AtlasNotConfiguredError(str(exc)) from None


# --------------------------------------------------------------------------- #
# Repo status (git + path walks — deterministic, Rule 2.8)
# --------------------------------------------------------------------------- #

def _git(repo: Path, *args: str) -> str:
    """Run one read-only git command; returns trimmed stdout or ''."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()


def atlas_status() -> dict[str, Any]:
    """Real ATLAS repo status: branch, last commit, dirty files, code counts."""
    repo = get_atlas_repo_path()
    branch = _git(repo, "branch", "--show-current")
    last_commit = _git(repo, "log", "-1", "--format=%h %s")
    dirty_raw = _git(repo, "status", "--porcelain")
    dirty = len([ln for ln in dirty_raw.splitlines() if ln.strip()]) if dirty_raw else 0

    sources = list(repo.glob("atlas/**/*.py"))
    tests = list(repo.glob("tests/**/test_*.py"))
    deliverables_dir = repo / "deliverables"
    deliverables_count = (
        len([p for p in deliverables_dir.rglob("*") if p.is_file()])
        if deliverables_dir.is_dir()
        else 0
    )
    return {
        "configured": True,
        "repo": str(repo),
        "branch": branch or "(detached/unknown)",
        "last_commit": last_commit or "(no commits)",
        "dirty_files": dirty,
        "source_files": len(sources),
        "test_files": len(tests),
        "deliverable_files": deliverables_count,
    }


# --------------------------------------------------------------------------- #
# FX archive bootstrap status (pure filesystem reads)
# --------------------------------------------------------------------------- #

_BOOTSTRAP_LOG_REL = "data/fx-backfill.log"
_BOOTSTRAP_DONE_REL = "data/fx-bootstrap.done"
_ARCHIVE_REL = "data/fx_archive/raw"


def _pair_days(archive_raw: Path) -> dict[str, int]:
    """Count bid-day files per pair: raw/<PAIR>/<YYYY>/<MM>/<DD>_bid.bi5."""
    counts: dict[str, int] = {}
    if not archive_raw.is_dir():
        return counts
    for pair_dir in sorted(p for p in archive_raw.iterdir() if p.is_dir()):
        n = len(list(pair_dir.glob("*/*/*_bid.bi5")))
        if n:
            counts[pair_dir.name] = n
    return counts


def atlas_bootstrap_status() -> dict[str, Any]:
    """The deep-FX backfill state: per-pair days, log tail, done marker."""
    repo = get_atlas_repo_path()
    raw_dir = repo / _ARCHIVE_REL
    pairs = _pair_days(raw_dir)

    log = repo / _BOOTSTRAP_LOG_REL
    log_tail = ""
    if log.is_file():
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(lines[-5:])
        except OSError:
            log_tail = "(log unreadable)"
    done = (repo / _BOOTSTRAP_DONE_REL).is_file()
    return {
        "configured": True,
        "pair_days": pairs,
        "total_pair_days": sum(pairs.values()),
        "done_marker": done,
        "log_tail": log_tail,
    }


# --------------------------------------------------------------------------- #
# Deliverables listing
# --------------------------------------------------------------------------- #

def atlas_deliverables(limit: int = 10) -> list[dict[str, Any]]:
    """Newest research deliverables under deliverables/ (name, size, mtime)."""
    repo = get_atlas_repo_path()
    base = repo / "deliverables"
    if not base.is_dir():
        return []
    limit = max(1, min(int(limit), 50))
    files = [p for p in base.rglob("*") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        rel = p.relative_to(repo)
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            mtime = ""
        out.append(
            {
                "path": str(rel),
                "size": p.stat().st_size,
                "modified": mtime,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Tool handlers (same shape as every roster tool: dict in, str out)
# --------------------------------------------------------------------------- #

def _atlas_status_tool(arguments: dict[str, Any]) -> str:
    try:
        status = atlas_status()
    except AtlasNotConfiguredError as exc:
        return f"ATLAS STATUS (reported honestly): NOT CONFIGURED — {exc}"
    return (
        "ATLAS REPO STATUS:\n"
        f"  repo: {status['repo']}\n"
        f"  branch: {status['branch']}\n"
        f"  last_commit: {status['last_commit']}\n"
        f"  dirty_files: {status['dirty_files']}\n"
        f"  source_files: {status['source_files']}\n"
        f"  test_files: {status['test_files']}\n"
        f"  deliverable_files: {status['deliverable_files']}"
    )


def _atlas_bootstrap_tool(arguments: dict[str, Any]) -> str:
    try:
        state = atlas_bootstrap_status()
    except AtlasNotConfiguredError as exc:
        return f"ATLAS BOOTSTRAP (reported honestly): NOT CONFIGURED — {exc}"
    lines = ["ATLAS FX BOOTSTRAP:"]
    pairs = state["pair_days"] or {"(none)": 0}
    for pair, days in sorted(pairs.items()):
        lines.append(f"  {pair}: {days} bid-days")
    lines.append(f"  total_pair_days: {state['total_pair_days']}")
    lines.append(f"  done_marker: {'PRESENT' if state['done_marker'] else 'absent'}")
    if state["log_tail"]:
        lines.append("  log_tail:")
        lines.extend(f"    {ln}" for ln in state["log_tail"].splitlines())
    return "\n".join(lines)


def _atlas_deliverables_tool(arguments: dict[str, Any]) -> str:
    try:
        limit = int(arguments.get("limit", 10))
    except (TypeError, ValueError):
        return "ERROR: limit must be an integer."
    try:
        items = atlas_deliverables(limit)
    except AtlasNotConfiguredError as exc:
        return f"ATLAS DELIVERABLES (reported honestly): NOT CONFIGURED — {exc}"
    if not items:
        return "ATLAS DELIVERABLES: none (empty or missing deliverables/ dir)."
    lines = ["ATLAS DELIVERABLES (newest first):"]
    for it in items:
        lines.append(f"  {it['path']}  ({it['size']}B, {it['modified']})")
    return "\n".join(lines)


def _atlas_report_tool(arguments: dict[str, Any]) -> str:
    """One consolidated ATLAS telemetry block (status + bootstrap + top deliverables)."""
    parts = []
    for fn in (_atlas_status_tool, _atlas_bootstrap_tool, _atlas_deliverables_tool):
        try:
            parts.append(fn({"limit": 5}))
        except Exception as exc:  # defensive: one section must never kill the rest
            parts.append(f"(section failed: {exc})")
    return "\n\n".join(parts)


def build_atlas_tool_specs() -> list[Any]:
    """ToolSpecs for the ``atlas`` subagent (defined here to keep
    general_roster.py focused on assembly; imports ToolSpec lazily to avoid
    import cycles). v4.1 (P6): appends the repo-knowledge index tools;
    v5.4: appends the real CLI-bridge tools (fx-research/fx-daily/...)."""
    from dourmouse.dispatch import ToolSpec

    def _spec(name: str, description: str, handler, props: dict[str, Any]) -> Any:
        return ToolSpec(
            name=name,
            description=description,
            parameters={
                "type": "object",
                "properties": props,
                "required": [],
            },
            handler=handler,
        )

    return [
        _spec(
            "atlas_status",
            "Real ATLAS repo status: branch, last commit, dirty-file count, "
            "source/test file counts. Deterministic git + path reads.",
            _atlas_status_tool,
            {},
        ),
        _spec(
            "atlas_bootstrap",
            "Real deep-FX-archive backfill state: per-pair bid-day counts, "
            "the backfill log tail, and whether the completion marker exists.",
            _atlas_bootstrap_tool,
            {},
        ),
        _spec(
            "atlas_deliverables",
            "Newest research deliverables under ATLAS deliverables/ with "
            "sizes and modification times.",
            _atlas_deliverables_tool,
            {"limit": {"type": "integer", "default": 10}},
        ),
        _spec(
            "atlas_report",
            "One consolidated ATLAS telemetry block: repo status + FX "
            "bootstrap state + newest deliverables.",
            _atlas_report_tool,
            {},
        ),
    ] + _cli_specs() + _repo_specs()


def _cli_specs() -> list[Any]:
    """The v5.4 CLI-bridge tools (real atlas commands via the ATLAS venv)."""
    from dourmouse.atlas_cli import build_atlas_cli_specs

    return build_atlas_cli_specs()


def _repo_specs() -> list[Any]:
    """The v4.1 repo-knowledge tools (lazy import keeps no cycles)."""
    from dourmouse.repo_index import build_repo_tool_specs

    return build_repo_tool_specs()
