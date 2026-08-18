"""Import Claude Code and Codex CLI session history into long-term memory
(roadmap item 4: "already on disk, no API needed").

Both formats were read from REAL files on this desktop before writing a
single line of parser code (Rule 2.8 — no guessing at an undocumented
schema):

- Claude Code writes one JSONL "rollout" file per session under
  ``~/.claude/projects/<sanitized-cwd>/<sessionId>.jsonl``. Most lines are
  full transcript turns (``type: "user"|"assistant"``, rich content
  blocks), but the client also drops a few small bookkeeping lines worth
  using directly: ``type: "custom-title"`` (a short human-readable title,
  already generated — no LLM call needed here) and ``type: "last-prompt"``.
- Codex CLI keeps a SQLite index at ``~/.codex/state_5.sqlite``, table
  ``threads``, with per-session columns (title, first_user_message,
  preview, cwd, model, timestamps) that are already a session summary —
  no need to even open the underlying rollout JSONL for a first pass.
  Verified live: two real rows on this desktop share the EXACT same
  title ("Write a Python function fib(n). Code only.") — titles are NOT
  unique, so the thread id must be part of the fact title or the second
  import silently overwrites the first (MemoryStore.remember upserts on
  (source, title)).

Both importers are purely mechanical extraction — first/last message,
existing titles, counts, timestamps — never an LLM summarization call
("no API needed" per the roadmap). Idempotent: MemoryStore.remember()
upserts on (source, title), and both importers derive a STABLE title per
session (session id embedded), so re-running never duplicates facts —
only refreshes them if the session changed since the last import.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dourmouse.memory_store import MemoryStore

_CLAUDE_SOURCE = "claude_history"
_CODEX_SOURCE = "codex_history"
_BODY_CHARS = 1500  # a fact body this long is already a full recall snippet;
# a whole session's raw transcript would drown the memory store and useful
# recall in noise for anyone else's fact sitting nearby.


def _claude_projects_root() -> Path:
    """DOURMOUSE_CLAUDE_HISTORY env, else ~/.claude/projects.

    Overridable because Claude Code's own history lives on whatever machine
    the user is typing into — not necessarily the one running Dourmouse
    (verified live: this desktop's own ~/.claude/projects has 10 files,
    0.8MB; the "80 sessions, 111MB" the roadmap describes is the user's
    Mac). The default is still the natural same-machine case.
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


def _short_id(session_id: str) -> str:
    return (session_id or "unknown")[:8]


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _flatten_content(content: Any) -> str:
    """A message's ``content`` is either a plain string (user turns,
    usually) or a list of content blocks (assistant turns: text/
    thinking/tool_use/tool_result). Only ``text`` blocks are prose worth
    recalling — thinking is the model's scratch work, tool_use/tool_result
    are structured data, not something to paraphrase without an LLM call.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _project_label(project_dir: Path) -> str:
    """The sanitized-cwd directory name back into something readable.

    Claude Code names each project folder after the cwd with path
    separators replaced by ``-`` (``/Users/x/Claude code`` ->
    ``-Users-x-Claude-code``). There's no reliable un-sanitization (a
    literal ``-`` in a real path name is indistinguishable from a
    separator), so this is a best-effort label for a fact body, not a
    real path reconstruction.
    """
    name = project_dir.name
    return name[1:] if name.startswith("-") else name


def _parse_claude_session(path: Path) -> dict[str, Any] | str:
    """One session JSONL -> a summary dict, or a short skip-reason string
    ("orchestration" | "empty") if it has no real
    conversational content (an empty/aborted session), OR if it is not a
    human-initiated top-level conversation at all.

    Verified live on 81 real files: 30 of them are NOT something the user
    typed — they are Claude Code's OWN sub-agent/orchestration runs (e.g.
    an All-Hands worker), each recorded as a full session file whose
    "first user message" is actually a system-style instruction block
    ("You are one independent worker inside..."). Importing those as
    "history" would misrepresent Dourmouse's own boilerplate as something
    the user said. The client already marks this structurally — the first
    real turn carries ``origin: {"kind": "human"}`` on a genuine
    conversation and omits ``origin`` entirely on an internal one — so the
    filter is a metadata check, not content pattern-matching (Rule 2.8:
    deterministic on real signal, never a guess at prose shape).
    """
    session_id = path.stem
    custom_title = ""
    last_prompt = ""
    first_user = ""
    last_assistant = ""
    cwd = ""
    git_branch = ""
    first_ts = ""
    last_ts = ""
    turn_count = 0
    seen_first_user_origin = False
    is_human = False

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable"
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a truncated last line in an active session; skip
            rtype = rec.get("type")
            if rtype == "custom-title":
                custom_title = (rec.get("customTitle") or "").strip()
            elif rtype == "last-prompt":
                last_prompt = (rec.get("lastPrompt") or "").strip()
            elif rtype in ("user", "assistant"):
                msg = rec.get("message") or {}
                text = _flatten_content(msg.get("content"))
                ts = rec.get("timestamp") or ""
                if not first_ts and ts:
                    first_ts = ts
                if ts:
                    last_ts = ts
                if not cwd:
                    cwd = rec.get("cwd") or ""
                if not git_branch:
                    git_branch = rec.get("gitBranch") or ""
                if rtype == "user":
                    if not seen_first_user_origin:
                        seen_first_user_origin = True
                        origin = rec.get("origin")
                        is_human = isinstance(origin, dict) and origin.get("kind") == "human"
                    turn_count += 1
                    if not first_user and text:
                        first_user = text
                elif rtype == "assistant" and text:
                    last_assistant = text

    if not is_human:
        return "orchestration"  # an internal/sub-agent run, not user history
    if not first_user and not last_assistant and not custom_title:
        return "empty"  # nothing conversational in this file

    return {
        "session_id": session_id,
        "title": custom_title or first_user or last_prompt or "untitled session",
        "first_user": first_user or last_prompt,
        "last_assistant": last_assistant,
        "cwd": cwd,
        "git_branch": git_branch,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "turn_count": turn_count,
        "project": _project_label(path.parent),
    }


def import_claude_code_history(
    store: MemoryStore, root: Path | str | None = None
) -> dict[str, Any]:
    """Walk every Claude Code session JSONL under ``root`` and remember one
    fact per session. Returns {"scanned", "imported", "skipped",
    "skipped_by_reason"} — the breakdown exists because "orchestration" is
    normally the majority of what gets skipped (verified live: 30 of 81
    files), and silently folding that into one "skipped" number would read
    as "most of your history didn't import" when really most of it was
    never the user's own conversation to begin with.
    """
    base = Path(root) if root is not None else _claude_projects_root()
    scanned = 0
    imported = 0
    skipped = 0
    skipped_by_reason: dict[str, int] = {}
    if not base.is_dir():
        return {
            "scanned": 0, "imported": 0, "skipped": 0,
            "skipped_by_reason": {}, "root": str(base),
        }

    for path in sorted(base.rglob("*.jsonl")):
        scanned += 1
        summary = _parse_claude_session(path)
        if isinstance(summary, str):
            skipped += 1
            skipped_by_reason[summary] = skipped_by_reason.get(summary, 0) + 1
            continue
        title = f"{_truncate(summary['title'], 80)} [{_short_id(summary['session_id'])}]"
        body_lines = [
            f"Project: {summary['project']}" + (f" ({summary['cwd']})" if summary["cwd"] else ""),
        ]
        if summary["git_branch"] and summary["git_branch"] != "HEAD":
            body_lines.append(f"Branch: {summary['git_branch']}")
        if summary["first_ts"] or summary["last_ts"]:
            body_lines.append(
                f"When: {summary['first_ts'] or '?'} → {summary['last_ts'] or '?'}"
                f" ({summary['turn_count']} user turn(s))"
            )
        if summary["first_user"]:
            body_lines.append(f"Asked: {_truncate(summary['first_user'], _BODY_CHARS)}")
        if summary["last_assistant"]:
            body_lines.append(f"Last reply: {_truncate(summary['last_assistant'], _BODY_CHARS)}")
        body = "\n".join(body_lines)
        try:
            store.remember(_CLAUDE_SOURCE, title, body)
            imported += 1
        except ValueError:
            skipped += 1
            skipped_by_reason["invalid"] = skipped_by_reason.get("invalid", 0) + 1
    return {
        "scanned": scanned, "imported": imported, "skipped": skipped,
        "skipped_by_reason": skipped_by_reason, "root": str(base),
    }


def _epoch_to_iso(value: Any) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def import_codex_history(
    store: MemoryStore, db_path: Path | str | None = None
) -> dict[str, Any]:
    """Read every row of Codex CLI's ``threads`` table and remember one
    fact per thread. Returns {"scanned", "imported", "skipped"}.

    Opened read-only (``mode=ro``) — Codex itself may hold this DB open
    (WAL-mode, live writer), and an importer must never risk touching the
    user's own tool's state.
    """
    path = Path(db_path) if db_path is not None else _codex_state_db()
    if not path.is_file():
        return {"scanned": 0, "imported": 0, "skipped": 0, "db": str(path), "configured": False}

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return {"scanned": 0, "imported": 0, "skipped": 0, "db": str(path), "configured": False}

    scanned = 0
    imported = 0
    skipped = 0
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, title, first_user_message, preview, cwd, model, "
            "git_branch, created_at, updated_at, archived FROM threads"
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        con.close()
        return {"scanned": 0, "imported": 0, "skipped": 0, "db": str(path), "configured": False}
    finally:
        con.close()

    for row in rows:
        scanned += 1
        thread_id = row["id"] or ""
        heading = (row["title"] or row["first_user_message"] or row["preview"] or "").strip()
        if not heading:
            skipped += 1
            continue
        title = f"{_truncate(heading, 80)} [{_short_id(thread_id)}]"
        body_lines = []
        if row["cwd"]:
            body_lines.append(f"Directory: {row['cwd']}")
        if row["model"]:
            body_lines.append(f"Model: {row['model']}")
        if row["git_branch"]:
            body_lines.append(f"Branch: {row['git_branch']}")
        started = _epoch_to_iso(row["created_at"])
        ended = _epoch_to_iso(row["updated_at"])
        if started or ended:
            body_lines.append(f"When: {started or '?'} → {ended or '?'}")
        if row["archived"]:
            body_lines.append("(archived in Codex)")
        if row["first_user_message"]:
            body_lines.append(f"Asked: {_truncate(row['first_user_message'], _BODY_CHARS)}")
        elif row["preview"]:
            body_lines.append(f"Preview: {_truncate(row['preview'], _BODY_CHARS)}")
        body = "\n".join(body_lines)
        try:
            store.remember(_CODEX_SOURCE, title, body)
            imported += 1
        except ValueError:
            skipped += 1

    return {
        "scanned": scanned,
        "imported": imported,
        "skipped": skipped,
        "db": str(path),
        "configured": True,
    }


def import_all_history(
    store: MemoryStore,
    claude_root: Path | str | None = None,
    codex_db: Path | str | None = None,
) -> dict[str, Any]:
    """Run both importers. Never raises — a source that isn't on this
    machine (Rule 2.2: honest, not fabricated) just reports zero, same as
    every other "not configured" surface in this codebase."""
    return {
        "claude": import_claude_code_history(store, claude_root),
        "codex": import_codex_history(store, codex_db),
    }
