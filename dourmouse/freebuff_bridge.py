"""Freebuff Desktop read bridge (v5.5) — real access to the user's Freebuff.

The Freebuff Desktop app (an Electron app) serves its own renderer from a
loopback-only HTTP API on ``127.0.0.1:51819`` (the UI server) with NO token:
it is the app's own internal API, and the app refuses non-loopback hosts.
This module reads it (read-only, deterministic, Rule 2.8) so Dourmouse can
answer "what is my AI working on", "summarize my recent Freebuff threads",
"which skills do I have installed", etc. with REAL data.

Endpoints used (all GET, all loopback, all read-only):
- ``/api/auth/status``        — account identity (authed, email, name)
- ``/api/projects``           — projects, each with its threads (title,
                                status, turn state, last prompt/outcome)
- ``/api/thread/<id>``        — ONE thread's full conversation (messages)
- ``/api/notes``              — the user's notes
- ``/api/skills``             — installed skills
- ``/api/project/changes``    — git changes for a project path (?path=)
- ``/api/project/recents``    — recent project paths

Honesty (Rule 2.2): when the app is not running or an endpoint fails, tools
report NOT CONFIGURED / the real error — never a fabricated status. NO
writes are ever issued through this bridge: it reads the app's state, it
does not mutate it (Rule 2.10 — nothing is sent, changed, or deleted).
Secrets are never returned — only the account email/name the app itself
exposes, and never tokens/keys.

The token-gated bridge on 51820 (``Authorization: Bearer <per-launch UUID>``)
is the app's internal debugger/preview API and is NOT needed for these
reads; the renderer-facing 51819 API is the right surface for read-only
integration.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# The Freebuff app's own UI server. Overridable so tests point at a fake
# server (FREEBUFF_API_URL) — never hardcoded to a live expectation.
_FREEBUFF_BASE = os.environ.get("FREEBUFF_API_URL", "http://127.0.0.1:51819").strip().rstrip("/")
_REQUEST_TIMEOUT = 5.0

# Output caps so a huge thread/project list can never blow the model's
# context (the bounded-window guard would truncate anyway; cap at the source).
_MAX_PROJECTS = 12
_MAX_THREADS = 40
_MAX_MESSAGES = 60
_MAX_MESSAGE_CHARS = 2000
_MAX_NOTES = 30
_MAX_SKILLS = 40
_MAX_CHANGES = 50


class FreebuffNotAvailable(RuntimeError):
    """The Freebuff app (or the requested surface) is not reachable."""


def _get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """GET one JSON endpoint; raises FreebuffNotAvailable on any failure.

    Deterministic and failure-safe (Rule 2.2): a dead app, a timeout, a
    non-JSON body, or a non-200 status all raise the SAME typed error with a
    useful reason — the caller renders it honestly. Never returns fabricated
    data.
    """
    url = _FREEBUFF_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "dourmouse/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            # A thread conversation can be HUGE (measured live: >8 MB for a
            # long thread — it embeds full tool outputs, diffs, and previews).
            # Read to EOF so the JSON is never truncated mid-string; the
            # safety ceiling only guards against a runaway endpoint, and the
            # output caps are applied LATER on the parsed structure, never on
            # the raw read.
            body = resp.read(64_000_000).decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FreebuffNotAvailable(f"Freebuff app not reachable at {_FREEBUFF_BASE}: {exc}") from exc
    if status != 200:
        raise FreebuffNotAvailable(f"Freebuff API {path} returned HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FreebuffNotAvailable(f"Freebuff API {path} returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise FreebuffNotAvailable(f"Freebuff API {path} returned an unexpected shape")
    return payload


# --------------------------------------------------------------------------- #
# Account / reachability
# --------------------------------------------------------------------------- #

def freebuff_account() -> dict[str, Any] | None:
    """The user's Freebuff account identity, or None honestly when the app
    is not running / not authed. Never raises (Rule 2.2)."""
    try:
        payload = _get("/api/auth/status")
    except FreebuffNotAvailable:
        return None
    if not payload.get("authed"):
        return None
    user = payload.get("user") or {}
    return {
        "email": user.get("email", ""),
        "name": user.get("name", ""),
    }


def freebuff_status() -> dict[str, Any]:
    """Honest connection status for connections.py / the SETUP panel.

    ``ok`` is True only when the app answers auth/status AND reports an
    authenticated account — that is what makes the reads actually usable.
    """
    account = freebuff_account()
    if account is None:
        return {
            "ok": False,
            "detail": "Freebuff Desktop not running or not authed",
            "hint": "start the Freebuff app and sign in",
            "account": None,
        }
    return {
        "ok": True,
        "detail": f"app running · {account.get('name') or account.get('email')}",
        "hint": "",
        "account": account,
    }


# --------------------------------------------------------------------------- #
# Projects & threads
# --------------------------------------------------------------------------- #

def freebuff_projects() -> list[dict[str, Any]]:
    """Projects with their threads (title/status/turn state), capped."""
    payload = _get("/api/projects")
    out: list[dict[str, Any]] = []
    for proj in (payload.get("projects") or [])[:_MAX_PROJECTS]:
        threads: list[dict[str, Any]] = []
        for t in (proj.get("threads") or [])[:_MAX_THREADS]:
            threads.append(
                {
                    "id": t.get("id", ""),
                    "title": (t.get("title") or "").strip(),
                    "status": t.get("status", ""),
                    "turnState": t.get("turnState", ""),
                    "lastTurnOutcome": t.get("lastTurnOutcome", ""),
                    "updatedAt": t.get("updatedAt"),
                }
            )
        out.append(
            {
                "path": proj.get("path", ""),
                "thread_count": len(proj.get("threads") or []),
                "threads": threads,
            }
        )
    return out


_THREAD_ID_RE = re.compile(r"^[0-9a-fA-F-]{1,64}$")


def freebuff_thread_messages(thread_id: str) -> list[dict[str, Any]]:
    """ONE thread's full conversation, capped, newest-first-ordered as given.

    ``thread_id`` must be the id string from freebuff_projects (UUID-ish);
    anything path-like is refused so this read can never escape into files
    (path-traversal guard, same rule as ATLAS report reads).
    """
    thread_id = thread_id.strip()
    if not thread_id or not _THREAD_ID_RE.match(thread_id):
        raise ValueError(
            f"Freebuff thread 'id' must be an id from freebuff_projects "
            f"(got {thread_id!r}) — refusing a path-like id (honest)."
        )
    payload = _get(f"/api/thread/{thread_id}")
    out: list[dict[str, Any]] = []
    for m in (payload.get("messages") or [])[:_MAX_MESSAGES]:
        parts = m.get("parts") or []
        texts = [
            (p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"
        ]
        text = "\n".join(t for t in texts if t)[:_MAX_MESSAGE_CHARS]
        out.append({"role": m.get("role", ""), "text": text})
    return out


# --------------------------------------------------------------------------- #
# Notes, skills, changes
# --------------------------------------------------------------------------- #

def freebuff_notes() -> list[dict[str, Any]]:
    payload = _get("/api/notes")
    return (payload.get("notes") or [])[:_MAX_NOTES]


def freebuff_skills() -> list[dict[str, Any]]:
    payload = _get("/api/skills")
    out = []
    for s in (payload.get("skills") or [])[:_MAX_SKILLS]:
        out.append(
            {
                "name": s.get("name", ""),
                "description": (s.get("prompt") or "").strip().splitlines()[0][:160]
                if isinstance(s.get("prompt"), str)
                else "",
            }
        )
    return out


def freebuff_project_changes(project_path: str) -> list[dict[str, Any]]:
    """Git changes for a project path (?path=). The path must be an absolute
    path the app itself exposes (from freebuff_projects / recents) — we only
    relay it as a query parameter, and a relative/garbage path is refused."""
    project_path = project_path.strip()
    if not project_path.startswith("/"):
        raise ValueError(
            f"Freebuff project 'path' must be an absolute path from "
            f"freebuff_projects (got {project_path!r})."
        )
    payload = _get("/api/project/changes", {"path": project_path})
    out = []
    for f in (payload.get("files") or [])[:_MAX_CHANGES]:
        out.append(
            {
                "path": f.get("path", ""),
                "status": f.get("status", ""),
                "adds": f.get("adds", 0),
                "dels": f.get("dels", 0),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Tool handlers (plain text for the model)
# --------------------------------------------------------------------------- #

def _freebuff_status_tool(arguments: dict[str, Any]) -> str:
    st = freebuff_status()
    if not st["ok"]:
        return f"FREEBUFF (reported honestly): NOT CONFIGURED — {st['detail']} ({st['hint']})"
    acc = st["account"] or {}
    name = acc.get("name") or acc.get("email") or "unknown"
    return (
        f"FREEBUFF ACCOUNT: authed as {name} ({acc.get('email', '')}) "
        f"— local read API live. Ask for projects, threads, notes, or skills."
    )


def _freebuff_projects_tool(arguments: dict[str, Any]) -> str:
    try:
        projects = freebuff_projects()
    except FreebuffNotAvailable as exc:
        return f"FREEBUFF PROJECTS (reported honestly): NOT CONFIGURED — {exc}"
    if not projects:
        return "FREEBUFF PROJECTS: none (honest — the app reports no projects)."
    lines = []
    for p in projects:
        lines.append(f"- {p['path']} ({p['thread_count']} threads)")
        for t in p["threads"][:5]:
            title = (t["title"] or "").replace("\n", " ")[:90]
            lines.append(f"    • [{t['status']}/{t['turnState']}] {t['id'][:8]} — {title}")
    return "FREEBUFF PROJECTS (live):\n" + "\n".join(lines)


def _freebuff_threads_tool(arguments: dict[str, Any]) -> str:
    """All threads across projects, flattened (title + status + last outcome)."""
    try:
        projects = freebuff_projects()
    except FreebuffNotAvailable as exc:
        return f"FREEBUFF THREADS (reported honestly): NOT CONFIGURED — {exc}"
    rows = []
    for p in projects:
        for t in p["threads"]:
            rows.append((t, p["path"]))
    if not rows:
        return "FREEBUFF THREADS: none (honest)."
    lines = []
    for t, project in rows[:_MAX_THREADS]:
        title = (t["title"] or "").replace("\n", " ")[:100]
        lines.append(
            f"- [{t['status']}/{t['turnState']}] {t['id']} "
            f"({project.rsplit('/', 1)[-1]}) — {title}"
        )
    return f"FREEBUFF THREADS ({len(rows)} total, live):\n" + "\n".join(lines)


def _freebuff_read_thread_tool(arguments: dict[str, Any]) -> str:
    thread_id = (arguments.get("thread_id") or "").strip()
    if not thread_id:
        return "ERROR: freebuff_read_thread requires 'thread_id' (from freebuff_threads)."
    try:
        messages = freebuff_thread_messages(thread_id)
    except ValueError as exc:
        return f"ERROR: {exc}"
    except FreebuffNotAvailable as exc:
        return f"FREEBUFF THREAD (reported honestly): NOT CONFIGURED — {exc}"
    if not messages:
        return f"FREEBUFF THREAD {thread_id}: no messages (honest)."
    lines = []
    for m in messages:
        role = m["role"]
        text = (m["text"] or "").strip()
        if not text:
            continue
        prefix = "USER" if role == "user" else "AI  "
        lines.append(f"[{prefix}] {text[:800]}")
    return f"FREEBUFF THREAD {thread_id} ({len(lines)} messages, live):\n" + "\n".join(lines)


def _freebuff_notes_tool(arguments: dict[str, Any]) -> str:
    try:
        notes = freebuff_notes()
    except FreebuffNotAvailable as exc:
        return f"FREEBUFF NOTES (reported honestly): NOT CONFIGURED — {exc}"
    if not notes:
        return "FREEBUFF NOTES: none (honest)."
    lines = []
    for n in notes:
        if isinstance(n, dict):
            lines.append(
                f"- {n.get('title') or n.get('id') or '(untitled)'}"
                + (f" — {str(n.get('body') or n.get('text') or '')[:140]}" if n.get("body") or n.get("text") else "")
            )
        else:
            lines.append(f"- {str(n)[:140]}")
    return "FREEBUFF NOTES (live):\n" + "\n".join(lines)


def _freebuff_skills_tool(arguments: dict[str, Any]) -> str:
    try:
        skills = freebuff_skills()
    except FreebuffNotAvailable as exc:
        return f"FREEBUFF SKILLS (reported honestly): NOT CONFIGURED — {exc}"
    if not skills:
        return "FREEBUFF SKILLS: none (honest)."
    lines = [f"- {s['name']}: {s['description']}" for s in skills]
    return "FREEBUFF SKILLS (live):\n" + "\n".join(lines)


def _freebuff_changes_tool(arguments: dict[str, Any]) -> str:
    path = (arguments.get("path") or "").strip()
    if not path:
        return "ERROR: freebuff_changes requires 'path' (an absolute project path from freebuff_projects)."
    try:
        changes = freebuff_project_changes(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    except FreebuffNotAvailable as exc:
        return f"FREEBUFF CHANGES (reported honestly): NOT CONFIGURED — {exc}"
    if not changes:
        return f"FREEBUFF CHANGES for {path}: none (clean working tree — honest)."
    lines = []
    for c in changes:
        lines.append(f"- {c['status']:>10} {c['path']} (+{c['adds']}/-{c['dels']})")
    return f"FREEBUFF CHANGES for {path} ({len(changes)} files, live):\n" + "\n".join(lines)


def _spec(name: str, description: str, handler: Any, props: dict[str, Any], required: list[str] | None = None) -> Any:
    from dourmouse.dispatch import ToolSpec

    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": props, "required": required or []},
        handler=handler,
    )


def build_freebuff_tool_specs() -> list[Any]:
    """The v5.5 Freebuff read ToolSpecs for the ``freebuff`` subagent."""
    return [
        _spec(
            "freebuff_status",
            "Freebuff Desktop account status: authed account name/email and "
            "whether the local read API is live. Use first to check the "
            "connection.",
            _freebuff_status_tool,
            {},
        ),
        _spec(
            "freebuff_projects",
            "List the real Freebuff projects, each with its threads (title, "
            "status, turn state). Live read of the app's project surface.",
            _freebuff_projects_tool,
            {},
        ),
        _spec(
            "freebuff_threads",
            "List ALL Freebuff threads across projects (title, status, turn "
            "state, last outcome) — the 'what is my AI working on' view.",
            _freebuff_threads_tool,
            {},
        ),
        _spec(
            "freebuff_read_thread",
            "Read ONE Freebuff thread's full conversation (user/AI messages) "
            "by its id from freebuff_threads. Returns the real thread text.",
            _freebuff_read_thread_tool,
            {"thread_id": {"type": "string", "description": "thread id from freebuff_threads"}},
            ["thread_id"],
        ),
        _spec(
            "freebuff_notes",
            "List the user's Freebuff notes (titles + snippets).",
            _freebuff_notes_tool,
            {},
        ),
        _spec(
            "freebuff_skills",
            "List the skills installed in Freebuff (name + one-line purpose).",
            _freebuff_skills_tool,
            {},
        ),
        _spec(
            "freebuff_changes",
            "Git changes (uncommitted) for a Freebuff project path — what "
            "work is in flight in that project.",
            _freebuff_changes_tool,
            {"path": {"type": "string", "description": "absolute project path from freebuff_projects"}},
            ["path"],
        ),
    ]


# --------------------------------------------------------------------------- #
# HUD panel payload (pure function — unit-testable without a web server)
# --------------------------------------------------------------------------- #

def freebuff_panel_snapshot() -> dict[str, Any]:
    """The complete GET /api/freebuff payload.

    ``configured: False`` honestly when the app is not running/authed. When
    live: account identity, project/thread counts, the newest thread titles,
    note/skill counts, and the last few projects. Every section is
    independently failure-safe — one dead endpoint never kills the panel.
    """
    st = freebuff_status()
    if not st["ok"]:
        return {"configured": False, "detail": st["detail"], "hint": st["hint"]}
    payload: dict[str, Any] = {"configured": True, "account": st["account"]}
    try:
        projects = freebuff_projects()
        payload["projects"] = [
            {
                "path": p["path"],
                "thread_count": p["thread_count"],
                "threads": [
                    {
                        "id": t["id"],
                        "title": (t["title"] or "").replace("\n", " ")[:100],
                        "status": t["status"],
                        "turnState": t["turnState"],
                    }
                    for t in p["threads"][:4]
                ],
            }
            for p in projects[:6]
        ]
        payload["project_count"] = len(projects)
        payload["thread_count"] = sum(p["thread_count"] for p in projects)
    except Exception as exc:  # noqa: BLE001 -- one section never kills the panel
        payload["projects_error"] = str(exc)
    for key, fn in (("notes_count", lambda: len(freebuff_notes())), ("skills_count", lambda: len(freebuff_skills()))):
        try:
            payload[key] = fn()
        except Exception:  # noqa: BLE001
            payload[key] = None
    return payload
