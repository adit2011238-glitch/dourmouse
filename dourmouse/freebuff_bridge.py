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
report NOT CONFIGURED / the real error — never a fabricated status. Reads
are read-only (Rule 2.10 — nothing sent, changed, or deleted). The ONE
write surface (v5.11) is ``freebuff_dispatch``: it creates a NEW thread in
a project the user chose and posts ONE prompt to it — the explicit
"dispatch work into Freebuff" action, never an edit/delete of existing
state. Secrets are never returned — only the account email/name the app
itself exposes, and never tokens/keys.

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

def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST one JSON endpoint; raises FreebuffNotAvailable on any failure.

    Same determinism contract as _get: a dead app, a timeout, a non-JSON
    body, or a non-2xx status all raise the SAME typed error with a useful
    reason — the caller renders it honestly. Never returns fabricated data.
    """
    url = _FREEBUFF_BASE + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "dourmouse/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            body = resp.read(64_000_000).decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:  # app answered but rejected the write
        raise FreebuffNotAvailable(
            f"Freebuff API {path} returned HTTP {exc.code} (app-side rejection)"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FreebuffNotAvailable(
            f"Freebuff app not reachable at {_FREEBUFF_BASE}: {exc}"
        ) from exc
    if not (200 <= status < 300):
        raise FreebuffNotAvailable(f"Freebuff API {path} returned HTTP {status}")
    try:
        payload_out = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FreebuffNotAvailable(f"Freebuff API {path} returned non-JSON") from exc
    if not isinstance(payload_out, dict):
        raise FreebuffNotAvailable(f"Freebuff API {path} returned an unexpected shape")
    return payload_out


# --------------------------------------------------------------------------- #
# v5.11 — Dispatch (write) bridge
# --------------------------------------------------------------------------- #

# The app enforces MAX_USER_PROMPT_CHARS = 200_000 server-side; we cap the
# tool input far lower so a runaway model can never flood a thread (the
# bounded-window guard already protects the model's own context, this
# protects the app + the user's thread list).
_MAX_DISPATCH_CHARS = 8_000
_MAX_TITLE_CHARS = 120


class FreebuffDispatchError(RuntimeError):
    """A write failed (validation, app error, or app unreachable)."""


# Thread ids from the app are UUID-ish; anything path-like is refused so a
# dispatch can never target an arbitrary path (same guard as reads).
def _validate_thread_id(thread_id: str) -> str:
    tid = (thread_id or "").strip()
    if not tid or not _THREAD_ID_RE.match(tid):
        raise FreebuffDispatchError(
            f"Freebuff thread 'id' must be an id from freebuff_threads "
            f"(got {thread_id!r}) — refusing a path-like id (honest)."
        )
    return tid


def _validate_project_path(project_path: str) -> str:
    p = (project_path or "").strip()
    if not p.startswith("/"):
        raise FreebuffDispatchError(
            f"Freebuff 'project_path' must be an absolute path from "
            f"freebuff_projects (got {project_path!r})."
        )
    return p


def freebuff_create_thread(
    project_path: str,
    title: str = "",
) -> dict[str, Any]:
    """Create ONE thread in a project via POST /api/threads (v5.11).

    Returns the real thread object the app returns (id, status, turnState,
    ...). ``project_path`` must be an absolute path the app already knows
    (from freebuff_projects / freebuff_threads) — the app opens it. A
    relative path is refused. No message is posted here — call
    freebuff_post_message next, or use freebuff_dispatch for the two-step
    in one call.
    """
    p = _validate_project_path(project_path)
    payload = {"projectPath": p}
    if title and title.strip():
        payload["title"] = (title or "").strip()[:_MAX_TITLE_CHARS]
    try:
        return _post("/api/threads", payload)
    except FreebuffNotAvailable as exc:
        raise FreebuffDispatchError(str(exc)) from exc


def freebuff_post_message(
    thread_id: str,
    text: str,
    project_path: str,
) -> dict[str, Any]:
    """Post + run ONE prompt in a thread via POST /api/thread/:id/message.

    Returns the real app response ({ok: true, ...}). ``text`` is capped at
    _MAX_DISPATCH_CHARS; the thread id must come from freebuff_threads.
    """
    tid = _validate_thread_id(thread_id)
    text = (text or "").strip()
    if not text:
        raise FreebuffDispatchError("freebuff dispatch requires a non-empty 'prompt'.")
    if len(text) > _MAX_DISPATCH_CHARS:
        raise FreebuffDispatchError(
            f"freebuff prompt too long ({len(text)} chars; max {_MAX_DISPATCH_CHARS}). "
            "Shorten the task before dispatching — honest, nothing was sent."
        )
    payload: dict[str, Any] = {
        "text": text,
        "projectPath": _validate_project_path(project_path),
    }
    try:
        return _post(f"/api/thread/{tid}/message", payload)
    except FreebuffNotAvailable as exc:
        raise FreebuffDispatchError(str(exc)) from exc


def freebuff_dispatch(
    prompt: str,
    project_path: str,
    title: str = "",
) -> dict[str, Any]:
    """Dispatch ONE task into a REAL Freebuff thread (create + post, v5.11).

    Creates a new thread in ``project_path`` with the prompt as its first
    message, so a real Freebuff agent picks it up and runs it (the app's
    own harness — the same one the user's threads run on). Returns the
    thread object + the message post result:

    ``{thread: {...}, posted: {...}}``

    The caller can then poll ``freebuff_read_thread`` for the answer, and
    the live events watcher surfaces the thread's turn transitions in the
    HUD feed. Honest error (FreebuffDispatchError) on any failure — a
    dead app, a bad path, or an app-side rejection is reported, never
    fabricated as success (Rule 2.2).

    No-orphan guarantee (reviewer fix): the prompt is validated (non-empty,
    within the cap) BEFORE the thread is created, so a predictably-bad
    prompt never leaves an empty thread behind. If the post fails AFTER
    creation, the error carries the created thread id so the caller can
    report it honestly ("thread X created, prompt failed").
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise FreebuffDispatchError("freebuff dispatch requires a non-empty 'prompt'.")
    if len(prompt) > _MAX_DISPATCH_CHARS:
        raise FreebuffDispatchError(
            f"freebuff prompt too long ({len(prompt)} chars; max {_MAX_DISPATCH_CHARS}). "
            "Shorten the task before dispatching — honest, nothing was sent."
        )
    title = (title or "").strip().replace("\n", " ")
    thread = freebuff_create_thread(project_path, title or prompt[:_MAX_TITLE_CHARS])
    tid = str(thread.get("id", "")).strip()
    if not tid or not _THREAD_ID_RE.match(tid):
        raise FreebuffDispatchError(
            "Freebuff created a thread without a usable id (honest)."
        )
    try:
        posted = freebuff_post_message(tid, prompt, project_path)
    except FreebuffDispatchError as exc:
        raise FreebuffDispatchError(
            f"thread {tid} was created but the prompt failed to post: {exc}"
        ) from exc
    return {"thread": thread, "posted": posted}


def _freebuff_dispatch_tool(arguments: dict[str, Any]) -> str:
    """Tool handler: dispatch a task into a real Freebuff thread."""
    prompt = (arguments.get("prompt") or "").strip()
    project_path = (arguments.get("project_path") or "").strip()
    title = (arguments.get("title") or "").strip()
    if not prompt:
        return "ERROR: freebuff_dispatch requires a non-empty 'prompt'."
    if not project_path:
        return (
            "ERROR: freebuff_dispatch requires 'project_path' (an absolute "
            "path from freebuff_projects / freebuff_threads)."
        )
    try:
        out = freebuff_dispatch(prompt, project_path, title)
    except FreebuffDispatchError as exc:
        return f"FREEBUFF DISPATCH (reported honestly): FAILED — {exc}"
    thread = out["thread"]
    posted = out["posted"]
    tid = str(thread.get("id", ""))
    status = thread.get("status", "")
    turn = thread.get("turnState", "")
    ok = bool(posted.get("ok"))
    title_t = (thread.get("title") or "").replace("\n", " ")[:100]
    return (
        f"FREEBUFF DISPATCH (live): thread {tid} created in "
        f"{thread.get('projectPath', project_path)} ({title_t}). "
        f"Prompt posted: {'accepted (running)' if ok else 'FAILED to post — ' + str(posted)}. "
        f"Thread state: {status}/{turn}. Use freebuff_read_thread with "
        f"thread_id={tid} to fetch the agent's answer when the turn finishes."
    )


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


def _spec(
    name: str,
    description: str,
    handler: Any,
    props: dict[str, Any],
    required: list[str] | None = None,
    *,
    permission: Any = None,
    confirm_prompt: Any = None,
) -> Any:
    from dourmouse.dispatch import Permission, ToolSpec

    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": props, "required": required or []},
        handler=handler,
        permission=permission if permission is not None else Permission.REGULAR,
        confirm_prompt=confirm_prompt,
    )


def build_freebuff_tool_specs() -> list[Any]:
    """The v5.5 read + v5.11 dispatch ToolSpecs for the ``freebuff`` subagent.

    Read tools are read-only by design (Rule 2.10 — nothing sent, changed,
    or deleted). The single write tool, ``freebuff_dispatch``, creates ONE
    thread in the user's chosen project and posts ONE prompt to it — the
    explicit dispatch action the user asked for (v5.11). It never deletes
    or edits existing threads, and every write is reported honestly.

    ``freebuff_dispatch`` is REQUIRES_CONFIRMATION: it doesn't just write
    data, it hands a real autonomous agent in another live app a prompt to
    act on against a real project path — closer to deploy/send_draft than
    to a file write. Every other tool here is a read.
    """
    from dourmouse.dispatch import Permission

    return [
        _spec(
            "freebuff_dispatch",
            "Dispatch a task into a REAL Freebuff thread (v5.11 write): "
            "creates a new thread in the given project and posts the prompt "
            "as its first message, so a real Freebuff agent runs it there. "
            "Requires 'prompt' and 'project_path' (an absolute path from "
            "freebuff_projects/freebuff_threads). Returns the new thread id "
            "+ post status; fetch the agent's answer later with "
            "freebuff_read_thread. Use this when the user wants work done "
            "inside Freebuff (their AI workspace), not just read. REQUIRES "
            "human confirmation before it dispatches anything.",
            _freebuff_dispatch_tool,
            {
                "prompt": {
                    "type": "string",
                    "description": "the task/prompt to dispatch to a Freebuff agent (max 8000 chars)",
                },
                "project_path": {
                    "type": "string",
                    "description": "absolute project path from freebuff_projects/freebuff_threads",
                },
                "title": {
                    "type": "string",
                    "description": "optional thread title (defaults to a prefix of the prompt)",
                },
            },
            ["prompt", "project_path"],
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: (
                f"Dispatch to Freebuff project {a.get('project_path', '?')!r}: "
                f"{(a.get('prompt') or '')[:160]!r}"
                f"{'...' if len(a.get('prompt') or '') > 160 else ''}? "
                "A real Freebuff agent will start acting on this."
            ),
        ),
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
