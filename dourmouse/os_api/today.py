"""Backend for the Home screen (plan 1.5, finding 200).

``GET /api/os/today`` answers everything Home draws in one read, from real stores only:

* ``greeting``: the part of the day, today's date and the owner's name when one is set
  (``DOURMOUSE_OWNER_NAME``; with none set the screen greets without a name).
* ``needs_you``: jobs parked for the owner (waiting for approval, for a sign-in, or blocked) and the
  cross-screen attention feed. Nothing is invented: an empty list means nothing needs the owner.
* ``office``: the fourteen agent identities, each ``working`` (it owns a running job), ``waiting`` (it owns
  a job that needs the owner) or ``idle``, and one line naming what the busiest one is doing.
* ``calendar``: today's events from the signed-in Google account. ``state`` says why there are none
  (``signed_out``, ``error``, ``empty``); an error is reported, never turned into an empty day.
* ``projects``: Continue tiles, newest activity first, pinned first, at most ``TILE_MAX``.

Reads only. It starts nothing and changes nothing.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from . import Request, route

TILE_MAX = 4
NEEDS_MAX = 6
EVENTS_MAX = 6


def part_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _initials(name: str) -> str:
    words = [w for w in name.replace("-", " ").split() if w and w.lower() not in ("and", "of")]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].capitalize()
    return (words[0][0] + words[1][0]).upper()


def greeting(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    return {
        "part": part_of_day(now.hour),
        "date": f"{now.strftime('%A')} {now.day} {now.strftime('%B')}",
        "name": os.environ.get("DOURMOUSE_OWNER_NAME", "").strip()[:40],
    }


def _jobs_board() -> dict[str, list[dict[str, Any]]]:
    from dourmouse import jobs
    from dourmouse.goals import get_goal_store

    board = jobs.board(get_goal_store())
    return {col["key"]: col["jobs"] for col in board["columns"]}


def needs_you(columns: dict[str, list[dict[str, Any]]], attention: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for job in columns.get("needs_you", []):
        items.append({
            "kind": "job", "id": job["id"], "title": job["title"], "owner": job.get("owner") or "",
            "note": job.get("reason") or "Waiting for you", "since": job.get("updated_at"), "target": "goals",
        })
    for a in attention:
        items.append({
            "kind": "attention", "id": f"a{a.get('id')}", "title": a.get("summary") or "", "owner": "",
            "note": a.get("detail") or "", "since": a.get("at"), "target": str(a.get("screen") or "HOME").lower(),
            "dismiss_id": a.get("id"),
        })
    return items


def office(columns: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    from dourmouse.agent_profiles import PROFILES

    running = {j["owner"]: j for j in columns.get("running", []) if j.get("owner")}
    waiting = {j["owner"]: j for j in columns.get("needs_you", []) if j.get("owner")}
    agents = []
    for p in PROFILES:
        status = "working" if p.id in running else "waiting" if p.id in waiting else "idle"
        agents.append({"id": p.id, "name": p.name, "initials": _initials(p.name), "status": status})
    busy = [a for a in agents if a["status"] == "working"]
    line = None
    if busy:
        first = busy[0]
        line = {"agent": first["name"], "text": f"is working on {running[first['id']]['title']}"}
    return {"agents": agents, "working": len(busy), "waiting": sum(1 for a in agents if a["status"] == "waiting"), "line": line}


def _events_today(token: str, now: datetime) -> list[dict[str, Any]]:
    from dourmouse import google_services as gs

    local = now.astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    params = urllib.parse.urlencode({
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 25,
        "timeMin": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeMax": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    data = gs._http_json("GET", f"{gs._CALENDAR_API}/calendars/primary/events?{params}", token)
    out = []
    for ev in data.get("items") or []:
        s = ev.get("start") or {}
        stamp = s.get("dateTime")
        if stamp:
            try:
                time_text = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
            except ValueError:
                time_text = stamp[11:16]
        else:
            time_text = "All day"
        out.append({"time": time_text, "title": str(ev.get("summary") or "(no title)")[:80]})
    return out


def calendar(req: Request, now: datetime | None = None) -> dict[str, Any]:
    from dourmouse import google_auth
    from dourmouse import google_services as gs

    now = now or datetime.now(timezone.utc)
    google_auth.set_current_user(req.user)
    try:
        try:
            token = gs._oauth_access_token()
        except Exception as exc:  # noqa: BLE001 -- a broken token store is reported, not hidden
            return {"state": "error", "events": [], "note": f"{type(exc).__name__}: {str(exc)[:160]}"}
        if not token:
            return {"state": "signed_out", "events": [], "note": "Sign in with Google to see your day."}
        try:
            events = _events_today(token, now)
        except RuntimeError as exc:
            return {"state": "error", "events": [], "note": str(exc)[:200]}
    finally:
        google_auth.set_current_user(None)
    if not events:
        return {"state": "empty", "events": [], "note": "Nothing on your calendar today."}
    return {"state": "ok", "events": events[:EVENTS_MAX], "more": max(0, len(events) - EVENTS_MAX), "note": ""}


def tiles() -> list[dict[str, Any]]:
    from dourmouse.projects_store import get_project_store

    out = []
    for p in get_project_store().list()[:TILE_MAX]:
        bits = []
        if p["open_work"]:
            bits.append(f"{p['open_work']} open" if p["open_work"] != 1 else "1 open item")
        if p["threads"]:
            bits.append(f"{p['threads']} thread" + ("s" if p["threads"] != 1 else ""))
        out.append({
            "id": p["id"], "name": p["name"], "goal": p["goal"], "cover": p["cover"], "pinned": p["pinned"],
            "line": p["goal"] or ", ".join(bits) or "No activity yet", "last_active": p["last_active"],
        })
    return out


@route("GET", "/api/os/today")
def today(req: Request) -> tuple[int, dict[str, Any]]:
    columns = _jobs_board()
    attention = []
    queue = getattr(req.server, "attention", None)
    if queue is not None:
        attention = queue.snapshot()
    needs = needs_you(columns, attention)
    off = office(columns)
    return 200, {
        "ok": True, "at": time.time(), "greeting": greeting(),
        "needs_you": needs[:NEEDS_MAX], "needs_total": len(needs),
        "office": off, "calendar": calendar(req), "projects": tiles(),
    }
