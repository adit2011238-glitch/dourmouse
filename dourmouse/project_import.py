"""Import a list of real projects — working directories the user has
actually worked in — from Claude Code's and Codex CLI's own on-disk
session history, for the PROJECTS bookshelf (a new source alongside the
existing client-side manual shelf; see ``ui/console.html`` PROJECTS pane,
"PROJECTS bookshelf", which this module does not touch — it only exposes
data for another agent to wire in).

READ-ONLY against both tools' own data: this module only opens files for
reading and connects to Codex's SQLite state file in ``mode=ro``. It never
writes to, modifies, or deletes anything under ``~/.claude`` or Codex's
storage.

Both on-disk formats were read from REAL files on this desktop before
writing a line of parser code (Rule 2.8 — no guessing at an undocumented
schema):

- Claude Code writes one JSONL "rollout" file per session under
  ``~/.claude/projects/<sanitized-cwd>/<sessionId>.jsonl``. Every
  session-scoped record (``type: "attachment"|"user"|"assistant"``, etc.)
  carries the session's real ``cwd`` and (on a git repo) ``gitBranch``
  near the top of the file — no need to read a whole transcript to find
  either.

  ``dourmouse/history_import.py`` (a sibling module that imports
  individual conversations as memory facts, not projects) filters
  sessions to real human conversations via the first user turn's
  ``origin.kind == "human"`` field, treating an absent/null origin as an
  internal orchestration sub-run. That signal turned out to be
  UNRELIABLE for THIS module's purpose: verified live on this desktop, an
  ordinary, genuinely human-initiated session whose first "user" turn is
  Claude Code's own auto-generated context-compaction summary ("This
  session is being continued from a previous conversation...") also
  carries ``origin: null`` — structurally identical to an internal
  sub-agent run. Excluding those sessions here would silently undercount
  a real project's history (a project could even vanish entirely if
  every one of its sessions happened to be a continuation). So this
  module deliberately does NOT classify human vs. orchestration; it
  counts every session file found under a project directory as one real,
  dated interaction with that directory via that tool. A directory that
  only ever hosted an internal sub-agent run is still a directory the
  tool genuinely touched — nothing here is fabricated, this is only a
  little more inclusive than "the user personally typed in it".

- Codex CLI keeps a SQLite index at ``~/.codex/state_5.sqlite``, table
  ``threads`` — one row per session, with ``cwd``, ``updated_at`` and
  ``git_branch`` already denormalized, so there is no need to open the
  underlying JSONL rollout files under ``~/.codex/sessions/`` at all for
  a project-level summary. Verified live: this machine's 43 thread rows
  resolve to the same 19 distinct ``cwd`` values, with the same per-cwd
  counts, as an independent filesystem walk of
  ``~/.codex/sessions/**/*.jsonl`` — the DB is a faithful, much cheaper
  index of the same real session history.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CLAUDE_TOOL = "claude_code"
_CODEX_TOOL = "codex_cli"
_CWD_SCAN_LINE_CAP = 200  # real files carry cwd/gitBranch within the first
# few lines; this is a safety cap against ever reading a whole multi-
# thousand-line transcript just to find two small fields.


def _claude_projects_root() -> Path:
    """DOURMOUSE_CLAUDE_HISTORY env, else ~/.claude/projects.

    Same env var and default as ``history_import._claude_projects_root``
    — Claude Code's own history lives on whatever machine the user is
    typing into, not necessarily the one running Dourmouse, and a single
    override should steer every importer that reads it.
    """
    raw = os.environ.get("DOURMOUSE_CLAUDE_HISTORY", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".claude" / "projects"


def _codex_state_db() -> Path:
    """DOURMOUSE_CODEX_STATE_DB env, else ~/.codex/state_5.sqlite."""
    raw = os.environ.get("DOURMOUSE_CODEX_STATE_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex" / "state_5.sqlite"


def _project_label(project_dir: Path) -> str:
    """Best-effort fallback name when no session file inside a project
    directory yields a real ``cwd`` (should be rare — normally every
    Claude Code session record carries one). Un-sanitizes the directory
    name back into something readable; there is no reliable way to
    recover the exact original path (a literal ``-`` in a real path is
    indistinguishable from Claude Code's own path-separator encoding), so
    callers must treat this as a display label, not a real path.
    """
    name = project_dir.name
    return name[1:] if name.startswith("-") else name


def _session_cwd_and_branch(path: Path) -> tuple[str, str]:
    """The real ``cwd`` and ``gitBranch`` recorded inside one Claude Code
    session JSONL file, or ``("", "")`` if the file is unreadable or
    neither field ever appears within the scan cap.
    """
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    cwd = ""
    branch = ""
    with fh:
        for _ in range(_CWD_SCAN_LINE_CAP):
            line = fh.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a truncated last line in an active session; skip
            if not isinstance(rec, dict):
                continue
            if not cwd:
                cwd = (rec.get("cwd") or "").strip()
            if not branch:
                branch = (rec.get("gitBranch") or "").strip()
            if cwd and branch:
                break
    return cwd, branch


def discover_claude_code_projects(root: Path | str | None = None) -> dict[str, Any]:
    """Walk every Claude Code project directory under ``root`` (default
    ``~/.claude/projects``) and summarize it as one project record per
    directory. Returns ``{"configured", "root", "records"}`` — never
    raises: a missing or unreadable root is reported as
    ``configured: False``, not fabricated as an empty success and not a
    crash.

    Each record: ``{"path", "tool", "session_count", "last_active"
    (epoch seconds or None), "git_branch", "path_is_real"}``.
    ``last_active`` is the newest session file's mtime — the filesystem's
    own record of when that file was last written, cheaper and just as
    honest as parsing every line's logical timestamp field.
    """
    base = Path(root) if root is not None else _claude_projects_root()
    if not base.is_dir():
        return {"configured": False, "root": str(base), "records": []}

    try:
        project_dirs = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return {"configured": False, "root": str(base), "records": []}

    records: list[dict[str, Any]] = []
    for project_dir in project_dirs:
        try:
            session_files = sorted(project_dir.glob("*.jsonl"))
        except OSError:
            continue
        cwd_votes: dict[str, int] = {}
        branch = ""
        last_active: float | None = None
        session_count = 0
        for f in session_files:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            session_count += 1
            if last_active is None or mtime > last_active:
                last_active = mtime
            found_cwd, found_branch = _session_cwd_and_branch(f)
            if found_cwd:
                cwd_votes[found_cwd] = cwd_votes.get(found_cwd, 0) + 1
            if found_branch and not branch:
                branch = found_branch
        if session_count == 0:
            continue  # an empty/unreadable project directory: not a project
        # Normally every session in a directory agrees on one real cwd; if
        # they ever disagree (e.g. a renamed folder reusing a sanitized
        # name), take the majority rather than guessing at which is
        # current.
        path = max(cwd_votes, key=lambda k: cwd_votes[k]) if cwd_votes else _project_label(project_dir)
        records.append({
            "path": path,
            "tool": _CLAUDE_TOOL,
            "session_count": session_count,
            "last_active": last_active,
            "git_branch": branch,
            "path_is_real": bool(cwd_votes),
        })
    return {"configured": True, "root": str(base), "records": records}


def discover_codex_projects(db_path: Path | str | None = None) -> dict[str, Any]:
    """Read Codex CLI's ``threads`` table and summarize it as one project
    record per distinct ``cwd``. Returns ``{"configured", "db",
    "records"}`` in the same shape as
    :func:`discover_claude_code_projects` — never raises: a missing DB,
    an unreadable one, or one without a ``threads`` table all degrade to
    ``configured: False``.

    Opened read-only (``mode=ro``) — Codex itself may hold this DB open
    (WAL-mode, live writer), and an importer must never risk touching the
    user's own tool's state.
    """
    path = Path(db_path) if db_path is not None else _codex_state_db()
    if not path.is_file():
        return {"configured": False, "db": str(path), "records": []}

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return {"configured": False, "db": str(path), "records": []}

    try:
        cur = con.cursor()
        cur.execute("SELECT cwd, git_branch, updated_at FROM threads")
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        con.close()
        return {"configured": False, "db": str(path), "records": []}
    finally:
        con.close()

    by_cwd: dict[str, dict[str, Any]] = {}
    for row in rows:
        cwd = (row["cwd"] or "").strip()
        if not cwd:
            continue  # a thread with no recorded cwd isn't a real project
        entry = by_cwd.setdefault(cwd, {"count": 0, "last": None, "branch": ""})
        entry["count"] += 1
        updated = row["updated_at"]
        if updated and (entry["last"] is None or updated > entry["last"]):
            entry["last"] = updated
        branch = (row["git_branch"] or "").strip()
        if branch and not entry["branch"]:
            entry["branch"] = branch

    records = [
        {
            "path": cwd,
            "tool": _CODEX_TOOL,
            "session_count": info["count"],
            "last_active": float(info["last"]) if info["last"] else None,
            "git_branch": info["branch"],
            "path_is_real": True,
        }
        for cwd, info in by_cwd.items()
    ]
    return {"configured": True, "db": str(path), "records": records}


def _merge_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe records from both tools by real project path (normalized,
    exact-match — the same directory recorded identically by both tools,
    which is what's actually observed on this desktop for at least one
    real project touched by both)."""
    by_path: dict[str, dict[str, Any]] = {}
    for rec in records:
        path = os.path.normpath(rec["path"]) if rec.get("path_is_real", True) else rec["path"]
        entry = by_path.setdefault(path, {
            "path": path,
            "sources": [],
            "session_counts": {},
            "last_active": None,
            "git_branch": "",
        })
        if rec["tool"] not in entry["sources"]:
            entry["sources"].append(rec["tool"])
        entry["session_counts"][rec["tool"]] = (
            entry["session_counts"].get(rec["tool"], 0) + rec["session_count"]
        )
        if rec["last_active"] is not None:
            if entry["last_active"] is None or rec["last_active"] > entry["last_active"]:
                entry["last_active"] = rec["last_active"]
        if rec.get("git_branch") and not entry["git_branch"]:
            entry["git_branch"] = rec["git_branch"]
    return list(by_path.values())


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _stat_label(session_counts: dict[str, int]) -> str:
    total = sum(session_counts.values())
    return f"{total} session{'s' if total != 1 else ''}"


def get_imported_projects(
    claude_root: Path | str | None = None,
    codex_db: Path | str | None = None,
) -> dict[str, Any]:
    """The one function ``webui.py``'s ``GET /api/projects/imported``
    calls. Combines both sources, deduped by real project path, sorted
    newest-active first. Never raises: each source that isn't on this
    machine, or isn't readable, degrades to ``configured: False`` in its
    own status block instead of failing the whole request (Rule 2.2 —
    honest, never fabricated).

    Response shape, one dict per project in ``"projects"``:
        path            real directory path (or a best-effort label —
                        see path_is_real note below — for the rare
                        Claude Code project whose session files never
                        yielded a real cwd)
        title           basename of path, for a bookshelf card title
        sources         sorted list of "claude_code" / "codex_cli"
        session_count   total sessions across both tools
        session_counts  {"claude_code": N, "codex_cli": N} (only tools
                        that actually touched this project appear)
        last_active     ISO-8601 UTC timestamp, or null
        git_branch      last known git branch, or "" if never recorded
        stat            short human string for the card, e.g. "12 sessions"
        exists          whether the path is a real directory on this
                        machine RIGHT NOW (a historical project's folder
                        may since have been moved or deleted)
    """
    claude = discover_claude_code_projects(claude_root)
    codex = discover_codex_projects(codex_db)
    merged = _merge_records(claude["records"] + codex["records"])
    merged.sort(key=lambda p: (p["last_active"] is None, -(p["last_active"] or 0)))

    projects = []
    for p in merged:
        try:
            exists = os.path.isdir(p["path"])
        except OSError:
            exists = False
        projects.append({
            "path": p["path"],
            "title": os.path.basename(p["path"].rstrip("/\\")) or p["path"],
            "sources": sorted(p["sources"]),
            "session_count": sum(p["session_counts"].values()),
            "session_counts": p["session_counts"],
            "last_active": _iso(p["last_active"]),
            "git_branch": p["git_branch"],
            "stat": _stat_label(p["session_counts"]),
            "exists": exists,
        })

    return {
        "claude_code": {"configured": claude["configured"], "root": claude["root"]},
        "codex_cli": {"configured": codex["configured"], "db": codex["db"]},
        "projects": projects,
    }
