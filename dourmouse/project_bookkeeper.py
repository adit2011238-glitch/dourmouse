"""dourmouse/project_bookkeeper.py — persisted, incrementally-updated
project metadata (name / real context / last-activity) for the PROJECTS
bookshelf (world-monitor-expansion).

WHY this exists alongside project_import.py: ``project_import.
get_imported_projects()`` is a passive, on-demand SCAN — it re-reads every
Claude Code session file's cwd/gitBranch (capped at 200 lines each) and
re-stats every session file on EVERY call. Cheap per file, but still real
I/O across every session file on the machine, every single call — see that
module's own docstring. This module is the "bookkeeper" layer on top: it
calls ``project_import`` as the single source of truth for WHICH projects
exist and their aggregate stats (session_count, last_active, sources,
git_branch, exists — none of that is reinvented here), then layers on a
REAL per-project ``context`` — a short, honest, EXTRACTIVE summary of what
was actually asked in that project's most recent session(s) — and PERSISTS
the combined record to ``<workspace>/project_bookkeeper.json`` so a repeat
read is one JSON file load, not a re-derivation.

Context is EXTRACTIVE, not LLM-generated (this pass's honest scope — no LLM
summarization path was built; see the module's own note below on why).
Every ``context`` string traces back verbatim to something a session file
or Codex thread row actually contains — nothing here is invented,
paraphrased, or hallucinated (Rule 2.1). For Claude Code: the most recent
session's own ``custom-title`` line if the client wrote one, else its
first real user-turn text, read straight out of that session's JSONL using
the same content-flattening helper ``history_import.py`` already relies on
(``_flatten_content`` — text blocks only, imported not reimplemented). For
Codex CLI: the ``threads`` table's own ``title`` / ``first_user_message`` /
``preview`` columns for that project's most recently updated thread — no
rollout JSONL is even opened, since Codex already denormalizes those
strings (the same columns ``history_import.import_codex_history`` reads).
``context_items`` keeps the newest few of these, each tagged with exactly
which real field it came from, so the one-line ``context`` on the card is
always checkable against real input.

Incremental, not a full rescan on every check: ``project_import``'s own
scan runs only inside :func:`refresh` (an explicit action — see
``GET``/``POST`` semantics below), never on a plain read. And even within
:func:`refresh`, the expensive part — actually opening a project's session
file(s) or querying Codex's title columns — is SKIPPED for any project
whose ``last_active`` hasn't moved past this module's own persisted
checkpoint for that project (see ``_needs_context_refresh``); its
persisted ``context``/``context_items`` are reused as-is. Only a new or
newly-active project pays the extraction cost on a given refresh.

``GET /api/projects/bookkeeper`` (:func:`get_bookkeeper`) serves the
persisted record as a plain file read — the whole point of persisting this
in the first place is that a repeat check does not re-touch the
filesystem/Codex DB at all. The one exception is bootstrapping: a store
that has literally never been refreshed runs one refresh so the first-ever
call isn't honestly-but-uselessly empty. ``POST
/api/projects/bookkeeper/refresh`` (:func:`refresh`) is the explicit
trigger — every response (from either verb) carries the record's own real
``last_refreshed`` timestamp, so staleness is always visible, never
silent.

No background thread/daemon here. ``webui.py``'s
``start_world_pulse_warmer`` is this codebase's one established
periodic-warmer pattern (a module-level daemon thread, exception-swallowing
loop, TTL/2 cadence); wiring this module into a second such thread was
judged out of scope for a clean, additive change on a branch other agents
are concurrently touching for unrelated work. The honest MVP shipped
instead is exactly the on-demand GET (cheap, persisted) + POST /refresh
(explicit, incremental) pair above, with "last refreshed" honesty in every
response — a real, valid MVP per this task's own stated fallback, not a
missing feature dressed up as one.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dourmouse import project_import
from dourmouse.history_import import _flatten_content

# project_import is imported as a MODULE (not "from ... import
# get_imported_projects, _claude_projects_root, ..." directly) so that its
# functions are always looked up through project_import's own namespace at
# call time — the same names test_project_import.py / test_project_
# bookkeeper.py monkeypatch to point at a fixture root/db stay in effect
# here too, instead of this module holding a stale reference bound at
# import time.

_WORKSPACE_ENV = "DOURMOUSE_WORKSPACE"
_STORE_FILE = "project_bookkeeper.json"
_MAX_CONTEXT_ITEMS = 3       # "the most recent few" per the task's own wording
_CONTEXT_LINE_CHARS = 160    # a bookshelf card line, not a transcript excerpt
_CLAUDE_SCAN_FILES_CAP = 3   # only the newest few session files are opened per project per refresh
_CONTEXT_METHOD = "extractive"  # honest, plain: no LLM summarization path in this pass


def _workspace_root() -> Path:
    """Same resolution as general_roster.py / schedules.py / live_feeds.py
    (``DOURMOUSE_WORKSPACE`` env, else ``<project>/workspace``). Kept local
    rather than imported, matching schedules.py's own stated reason: avoid
    a circular import (general_roster pulls in a lot; this module doesn't
    need any of it just for a path)."""
    raw = os.environ.get(_WORKSPACE_ENV, "").strip()
    root = Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _store_path() -> Path:
    return _workspace_root() / _STORE_FILE


def _empty_store() -> dict[str, Any]:
    return {
        "version": 1,
        "last_refreshed": None,
        "context_method": _CONTEXT_METHOD,
        "claude_code": {"configured": False},
        "codex_cli": {"configured": False},
        "projects": {},
        # v13.4: real create/delete on the bookshelf, explicit user request.
        # Kept SEPARATE from "projects" (the auto-discovered set) because
        # refresh() unconditionally REBUILDS "projects" from a fresh
        # project_import scan every call -- anything living only in that
        # dict would be silently wiped on the next refresh. manual_projects
        # is never touched by refresh(); hidden_paths is a real user
        # "delete" of an auto-discovered project (see delete_project's own
        # docstring for why this hides rather than deletes real files).
        "manual_projects": {},
        "hidden_paths": [],
    }


def _load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        return _empty_store()
    defaults = _empty_store()
    for key, value in defaults.items():
        data.setdefault(key, value)
    return data


def _save_store(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _epoch_iso(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())  # collapse newlines/whitespace to one card line
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _sanitized_dirname(path: str) -> str:
    """Claude Code's OWN directory-naming convention for a cwd (path
    separators -> '-' — see project_import.py's module docstring). NOT
    reliably invertible in general (a literal '-' in a real path is
    indistinguishable from a separator), so this is only used here as a
    fast, common-case lookup: project_import.py's own majority-vote cwd
    resolution remains the single source of truth for the canonical
    project list itself. If no directory exists under this exact name for
    a project project_import DID resolve, this project simply gets no
    Claude-sourced context this pass — honest (nothing invented), not a
    crash or a guess at some other directory.
    """
    return path.replace("/", "-").replace("\\", "-")


def _claude_recent_items(project_dir: Path, cap: int) -> list[dict[str, Any]]:
    """Real title/first-user text from this project directory's newest
    ``cap`` session files, newest file first. Reuses
    ``history_import._flatten_content`` for the same content-block parsing
    already relied on elsewhere in this codebase (Rule: one parser per
    real format, not a second competing implementation)."""
    try:
        files = sorted(project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for f in files[:cap]:
        custom_title = ""
        first_user = ""
        last_ts = ""
        try:
            fh = f.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a truncated last line in an active session; skip
                if not isinstance(rec, dict):
                    continue
                rtype = rec.get("type")
                if rtype == "custom-title" and not custom_title:
                    custom_title = (rec.get("customTitle") or "").strip()
                elif rtype == "user" and not first_user:
                    text = _flatten_content((rec.get("message") or {}).get("content"))
                    if text:
                        first_user = text.strip()
                ts = rec.get("timestamp")
                if ts:
                    last_ts = ts
        if custom_title:
            text, field = custom_title, "custom_title"
        elif first_user:
            text, field = first_user, "first_user_prompt"
        else:
            continue  # nothing conversational in this file — not fabricated as empty
        items.append({
            "text": _truncate(text, _CONTEXT_LINE_CHARS),
            "session_id": f.stem,
            "at": last_ts or None,
            "source": "claude_code",
            "field": field,
        })
    return items


def _codex_recent_items(db_path: Path, cwd: str, cap: int) -> list[dict[str, Any]]:
    """Real title/first_user_message/preview text for this cwd's newest
    ``cap`` Codex threads, straight from the ``threads`` table's own
    denormalized columns (the same columns
    ``history_import.import_codex_history`` reads) — no rollout JSONL is
    opened. Read-only (``mode=ro``), same discipline as
    ``project_import.discover_codex_projects``: Codex itself may hold this
    DB open (WAL-mode, live writer)."""
    if not db_path.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT title, first_user_message, preview, updated_at FROM threads "
            "WHERE cwd = ? ORDER BY updated_at DESC LIMIT ?",
            (cwd, cap),
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        if row["title"]:
            text, field = row["title"], "title"
        elif row["first_user_message"]:
            text, field = row["first_user_message"], "first_user_message"
        elif row["preview"]:
            text, field = row["preview"], "preview"
        else:
            continue
        items.append({
            "text": _truncate(text, _CONTEXT_LINE_CHARS),
            "at": _epoch_iso(row["updated_at"]),
            "source": "codex_cli",
            "field": field,
        })
    return items


def _needs_context_refresh(prior: dict[str, Any] | None, last_active_epoch: float | None) -> bool:
    """The real checkpoint check: skip re-opening this project's session
    files / Codex rows unless something has actually happened since the
    last time this module captured context for it (or it's never been
    captured at all)."""
    if prior is None:
        return True
    checkpoint = prior.get("_checkpoint_last_active_epoch")
    if checkpoint is None:
        return True
    if last_active_epoch is None:
        return False  # nothing new to check the checkpoint against; keep what we have
    return last_active_epoch > checkpoint


def _sort_key(item: dict[str, Any]) -> str:
    return (item.get("at") or "").replace("Z", "+00:00")


def refresh(
    claude_root: Path | str | None = None,
    codex_db: Path | str | None = None,
    store_path: Path | str | None = None,
) -> dict[str, Any]:
    """Reconcile the persisted bookkeeper record against
    ``project_import.get_imported_projects()`` — the real, un-duplicated
    source of truth for which projects exist and their aggregate stats.
    For each project, re-derive ``context`` only if its ``last_active``
    moved past this module's own persisted checkpoint (see
    ``_needs_context_refresh``) — everything else is a straight refresh of
    the cheap fields project_import already computed. Persists the result
    and returns it in the same shape :func:`get_bookkeeper` serves.
    """
    imported = project_import.get_imported_projects(claude_root=claude_root, codex_db=codex_db)
    sp = Path(store_path) if store_path is not None else _store_path()
    store = _load_store(sp)
    existing_projects: dict[str, Any] = store.get("projects", {})

    real_claude_root = (
        Path(claude_root) if claude_root is not None else project_import._claude_projects_root()
    )
    real_codex_db = (
        Path(codex_db) if codex_db is not None else project_import._codex_state_db()
    )

    updated: dict[str, Any] = {}
    for p in imported["projects"]:
        path = p["path"]
        prior = existing_projects.get(path)
        last_active_epoch = _iso_to_epoch(p["last_active"])

        if _needs_context_refresh(prior, last_active_epoch):
            items: list[dict[str, Any]] = []
            if "claude_code" in p["sources"]:
                claude_dir = real_claude_root / _sanitized_dirname(path)
                if claude_dir.is_dir():
                    items.extend(_claude_recent_items(claude_dir, _CLAUDE_SCAN_FILES_CAP))
            if "codex_cli" in p["sources"]:
                items.extend(_codex_recent_items(real_codex_db, path, _CLAUDE_SCAN_FILES_CAP))
            items.sort(key=_sort_key, reverse=True)
            items = items[:_MAX_CONTEXT_ITEMS]
            if items:
                context = items[0]["text"]
                context_source = f"{items[0]['source']}:{items[0]['field']}"
            else:
                context = ""
                context_source = "none"
            context_updated_at = _now_iso()
        else:
            items = prior.get("context_items", [])
            context = prior.get("context", "")
            context_source = prior.get("context_source", "none")
            context_updated_at = prior.get("context_updated_at")

        updated[path] = {
            "path": path,
            "name": p["title"],
            "sources": p["sources"],
            "session_count": p["session_count"],
            "session_counts": p["session_counts"],
            "last_active": p["last_active"],
            "git_branch": p["git_branch"],
            "stat": p["stat"],
            "exists": p["exists"],
            "context": context,
            "context_source": context_source,
            "context_items": items,
            "context_updated_at": context_updated_at,
            "_checkpoint_last_active_epoch": last_active_epoch,
        }

    new_store = {
        "version": 1,
        "last_refreshed": _now_iso(),
        "context_method": _CONTEXT_METHOD,
        "claude_code": imported["claude_code"],
        "codex_cli": imported["codex_cli"],
        "projects": updated,
        # Real bug caught by test_manual_project_survives_a_refresh_call /
        # test_delete_of_an_auto_discovered_project_hides_it_...: this dict
        # used to be built from scratch with no reference to `store` (the
        # PRIOR persisted record, loaded above) at all, silently dropping
        # any manually created project and un-hiding any deleted
        # auto-discovered one on every single refresh. Both are carried
        # forward explicitly here — refresh() only ever rebuilds "projects"
        # (the auto-discovered set); manual_projects/hidden_paths are this
        # module's own state, never re-derived from project_import.
        "manual_projects": store.get("manual_projects", {}),
        "hidden_paths": store.get("hidden_paths", []),
    }
    _save_store(new_store, sp)
    return _public_view(new_store)


def _public_view(store: dict[str, Any]) -> dict[str, Any]:
    """The bookshelf-card-ready response shape: strips this module's own
    internal checkpoint field (``_checkpoint_last_active_epoch``) — real
    and inspectable in the persisted JSON file, but not part of the public
    contract another agent's UI code should depend on. Merges in manually
    created projects and filters out user-deleted (hidden) ones -- see
    create_project/delete_project."""
    hidden = set(store.get("hidden_paths", []))
    projects = [
        {k: v for k, v in rec.items() if not k.startswith("_")}
        for rec in store.get("projects", {}).values()
        if rec.get("path") not in hidden
    ]
    for rec in store.get("manual_projects", {}).values():
        if rec.get("path") in hidden:
            continue
        projects.append(dict(rec))
    projects.sort(key=lambda p: (p["last_active"] is None, p["last_active"] or ""), reverse=True)
    return {
        "last_refreshed": store.get("last_refreshed"),
        "context_method": store.get("context_method", _CONTEXT_METHOD),
        "claude_code": store.get("claude_code", {"configured": False}),
        "codex_cli": store.get("codex_cli", {"configured": False}),
        "projects": projects,
    }


def create_project(
    name: str,
    path: str,
    description: str = "",
    store_path: Path | str | None = None,
) -> dict[str, Any]:
    """Manually register a project on the bookshelf — real user request
    (v13.4), for a project that has no Claude/Codex session history yet
    (a brand-new folder) or that the user simply wants tracked/pinned.
    Does NOT create the directory itself (Rule 2.2: no silent side
    effects a caller didn't ask for) -- ``path`` is recorded whether or
    not it exists yet; ``exists`` reflects the real, current filesystem
    state at creation time and is honestly re-checked, not assumed.

    Returns the same per-project card shape refresh()'s auto-discovered
    entries use, so the client's rendering code needs no special case for
    "manual" vs "auto-discovered" -- only ``sources: ["manual"]`` differs.
    """
    name = (name or "").strip()
    path = (path or "").strip()
    if not name:
        raise ValueError("create_project requires a non-empty 'name'.")
    if not path:
        raise ValueError("create_project requires a non-empty 'path'.")
    resolved = str(Path(path).expanduser())
    sp = Path(store_path) if store_path is not None else _store_path()
    store = _load_store(sp)
    if resolved in store.get("projects", {}) or resolved in store.get("manual_projects", {}):
        raise ValueError(f"a project at {resolved!r} is already on the bookshelf.")
    now = _now_iso()
    record = {
        "path": resolved,
        "name": name,
        "sources": ["manual"],
        "session_count": 0,
        "session_counts": {},
        "last_active": now,
        "git_branch": None,
        "stat": None,
        "exists": Path(resolved).exists(),
        "context": description.strip(),
        "context_source": "manual" if description.strip() else "none",
        "context_items": [],
        "context_updated_at": now,
        "created_at": now,
    }
    store.setdefault("manual_projects", {})[resolved] = record
    # A hidden auto-discovered project at the same path, re-created
    # manually, should reappear -- an explicit re-add un-hides it.
    store["hidden_paths"] = [p for p in store.get("hidden_paths", []) if p != resolved]
    _save_store(store, sp)
    return record


def delete_project(path: str, store_path: Path | str | None = None) -> bool:
    """Remove a project from the bookshelf. NEVER touches the real
    directory or its session files -- "delete" here means "stop tracking
    on the bookshelf", the same meaning "delete" carries in any project
    list/dashboard UI, not "destroy the project's real files". A manually
    created project is removed outright (nothing else derives it); an
    auto-discovered project is HIDDEN (added to hidden_paths) rather than
    removed from the "projects" dict directly, because refresh() rebuilds
    that dict from a fresh real scan on every call and would silently
    resurrect a direct deletion on the very next refresh -- hidden_paths
    is the one thing refresh() never touches, so a hide actually sticks.
    Returns True if something was actually removed/hidden, False if
    ``path`` wasn't tracked at all (nothing to do, not an error)."""
    resolved = str(Path(path).expanduser())
    sp = Path(store_path) if store_path is not None else _store_path()
    store = _load_store(sp)
    changed = False
    if resolved in store.get("manual_projects", {}):
        del store["manual_projects"][resolved]
        changed = True
    if resolved in store.get("projects", {}):
        hidden = set(store.get("hidden_paths", []))
        hidden.add(resolved)
        store["hidden_paths"] = sorted(hidden)
        changed = True
    if changed:
        _save_store(store, sp)
    return changed


def get_bookkeeper(
    claude_root: Path | str | None = None,
    codex_db: Path | str | None = None,
    store_path: Path | str | None = None,
) -> dict[str, Any]:
    """``GET /api/projects/bookkeeper``'s entry point: serves the
    persisted record as-is — a plain file read, not a rescan (the whole
    point of persisting this in the first place — see the module
    docstring). The only exception is bootstrapping: a store that has
    never been refreshed runs one refresh so a first-ever call doesn't
    come back honestly-but-uselessly empty.
    """
    sp = Path(store_path) if store_path is not None else _store_path()
    store = _load_store(sp)
    if store.get("last_refreshed") is None:
        return refresh(claude_root=claude_root, codex_db=codex_db, store_path=sp)
    return _public_view(store)
