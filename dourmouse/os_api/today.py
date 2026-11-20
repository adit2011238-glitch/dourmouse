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
* ``strip`` (plan 4.12, PL-08): six cards, Mail, Calendar, Goals, Overnight, Security and Usage, each
  ``{key, title, state, number, line, action, at}``. Each is read on its own with its own time limit, so one slow
  or broken source shows as that card's ``error`` and never blanks or delays the others.
* ``continue``: up to three threads to pick up, newest first, and ``privacy``: what left this Mac in the last
  24 hours (cloud requests, sources shared, redactions), read from the egress ledger.

Reads only. It starts nothing and changes nothing.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Any

from . import Request, route

TILE_MAX = 4
NEEDS_MAX = 6
EVENTS_MAX = 6
CONTINUE_MAX = 3
CARD_SECONDS = 8.0
CARD_ORDER = ("mail", "calendar", "goals", "overnight", "security", "usage")
CARD_TITLES = {"mail": "Mail", "calendar": "Calendar", "goals": "Goals", "overnight": "Overnight",
               "security": "Security", "usage": "Usage"}


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


def feed_needs_you() -> list[dict[str, Any]]:
    """Items from the attention feed (plan 1.8) that wait on the owner. Jobs are skipped: the board already lists them.
    ``target`` carries the item's own link, arguments included, so Review opens exactly the place it is about."""
    from dourmouse import attention

    out = []
    for it in attention.get_attention_store().items():
        if it["kind"] != "needs_you" or it["source"] == "jobs":
            continue
        out.append({
            "kind": "feed", "id": f"f{it['id']}", "title": it["title"], "owner": "", "note": it["detail"],
            "since": it["created"], "target": it["href"][2:],
        })
    return out


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


# ---------------------------------------------------------------------------------------------------------------
# The Today strip (plan 4.12). A card is {key, title, state, number, line, action, at}. ``state`` is one of
# ``ok``, ``empty`` (a real read that found nothing), ``unavailable`` (not connected) or ``error``. ``number`` is the
# one figure the card stands for, or None when there is no honest figure.

def _card(key: str, state: str, line: str, number: int | float | None = None, target: str = "", label: str = "Open",
          at: float | None = None) -> dict[str, Any]:
    return {
        "key": key, "title": CARD_TITLES[key], "state": state, "number": number, "line": line,
        "action": {"label": label, "target": target} if target else None, "at": at if at is not None else time.time(),
    }


def mail_card(req: Request) -> dict[str, Any]:
    from . import ApiError, comms

    try:
        _status, data = comms.inbox(Request(server=req.server, query={"limit": ["25"]}, user=req.user))
    except ApiError as exc:
        return _card("mail", "error", str(exc)[:160], target="comms", label="Open mail")
    at = data.get("cached_at") or time.time()
    if data.get("state") == "unavailable":
        return _card("mail", "unavailable", "Mail is not connected.", target="comms", label="Open mail", at=at)
    rows = data.get("rows") or []
    if not rows:
        return _card("mail", "empty", "Your inbox is empty.", 0, "comms", "Open mail", at)
    flags = [r.get("unread") for r in rows]
    if all(f is None for f in flags):  # the App Password path cannot say which are unread; say what is known
        return _card("mail", "ok", f"{len(rows)} recent messages.", None, "comms", "Open mail", at)
    unread = sum(1 for f in flags if f)
    if not unread:
        return _card("mail", "empty", "Nothing unread.", 0, "comms", "Open mail", at)
    first = next(r for r in rows if r.get("unread"))
    sender = str(first.get("from") or "").split("<")[0].strip().strip('"')[:40]
    return _card("mail", "ok", f"Latest from {sender}: {str(first.get('subject') or '')[:60]}" if sender else f"{unread} unread.",
                 unread, "comms", "Open mail", at)


def calendar_card(req: Request, now: datetime | None = None) -> dict[str, Any]:
    cal = calendar(req, now)
    card = _calendar_card(cal, now)
    card["raw"] = cal  # the route lifts this into its own ``calendar`` key so Google is asked once
    return card


def _calendar_card(cal: dict[str, Any], now: datetime | None) -> dict[str, Any]:
    state = cal["state"]
    target = "comms"
    if state == "signed_out":
        return _card("calendar", "unavailable", cal["note"], target=target)
    if state == "error":
        return _card("calendar", "error", cal["note"], target=target)
    if state == "empty":
        return _card("calendar", "empty", cal["note"], 0, target)
    local = (now or datetime.now()).astimezone().strftime("%H:%M")
    events = cal["events"]
    nxt = next((e for e in events if e["time"] != "All day" and e["time"] >= local), None) or events[0]
    all_day = nxt["time"] == "All day"
    total = len(events) + cal.get("more", 0)
    return _card("calendar", "ok", (f"All day: {nxt['title']}" if all_day else f"Next at {nxt['time']}: {nxt['title']}"), total, target)


def goals_card(columns: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    running, waiting = len(columns.get("running", [])), len(columns.get("needs_you", []))
    if not running and not waiting:
        return _card("goals", "empty", "Nothing running.", 0, "goals")
    bits = []
    if running:
        bits.append(f"{running} running")
    if waiting:
        bits.append(f"{waiting} waiting for you")
    return _card("goals", "ok", ", ".join(bits) + ".", running + waiting, "goals")


def overnight_window(now: datetime) -> float:
    """Epoch seconds of the last 22:00 (the start of quiet hours) before ``now``, at most 16 hours back."""
    local = now.astimezone()
    start = local.replace(hour=22, minute=0, second=0, microsecond=0)
    if start > local:
        start -= timedelta(days=1)
    return max(start.timestamp(), local.timestamp() - 16 * 3600)


def overnight_card(req: Request, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = overnight_window(now)
    runs = failed = 0
    note = ""
    try:
        from dourmouse.automation_runs import RunHistory

        for r in RunHistory()._load():
            if float(r.get("ts") or 0) >= since and r.get("status") not in ("running", "parked"):
                runs += 1
                failed += 1 if r.get("status") in ("failed", "error") else 0
    except Exception as exc:  # noqa: BLE001 -- reported on the card, not hidden
        return _card("overnight", "error", f"{type(exc).__name__}: {str(exc)[:140]}", target="timetable")
    news = 0
    watcher = getattr(req.server, "news_watcher", None)
    if watcher is not None:
        try:
            for item in watcher.recent(limit=200, important_only=True):
                stamp = item.get("ts") or item.get("seen_at") or item.get("published") or 0
                if isinstance(stamp, (int, float)) and stamp >= since:
                    news += 1
        except Exception as exc:  # noqa: BLE001
            note = f" News not read: {type(exc).__name__}."
    if not runs and not news:
        return _card("overnight", "empty", "Quiet overnight." + note, 0, "timetable")
    bits = []
    if runs:
        bits.append(f"{runs} automation run{'s' if runs != 1 else ''}" + (f", {failed} failed" if failed else ""))
    if news:
        bits.append(f"{news} important news item{'s' if news != 1 else ''}")
    return _card("overnight", "ok", "; ".join(bits) + "." + note, runs + news, "timetable" if runs else "news")


def security_card(req: Request) -> dict[str, Any]:
    runtime = getattr(req.server, "security_sentry", None)
    result = runtime.last_result if runtime else None
    if result is None:
        return _card("security", "empty", "No scan has finished yet.", None, "security", "Open security")
    at = getattr(runtime, "last_scan_at", None)
    if getattr(runtime, "consecutive_failed_scans", 0):
        return _card("security", "error", "The last scan failed: " + str(getattr(runtime, "last_scan_error", "") or "unknown")[:120],
                     None, "security", "Open security", at)
    high = med = 0
    for f in result.all_findings:
        high += 1 if f.severity == "high" else 0
        med += 1 if f.severity == "med" else 0
    if high:
        return _card("security", "ok", f"{high} high finding{'s' if high != 1 else ''}" + (f", {med} medium." if med else "."),
                     high, "security", "Open security", at)
    if med:
        return _card("security", "ok", f"No high findings, {med} medium.", 0, "security", "Open security", at)
    return _card("security", "ok", "No findings on the last scan.", 0, "security", "Open security", at)


def usage_card() -> dict[str, Any]:
    from dourmouse.usage_tracker import get_totals

    t = get_totals()
    claude, ollama = t.get("claude") or {}, t.get("ollama") or {}
    requests = int(claude.get("requests") or 0) + int(ollama.get("requests") or 0)
    if not requests:
        return _card("usage", "empty", "No model use recorded yet.", 0, "settings")
    bits = []
    if claude.get("requests"):
        bits.append(f"Claude ${float(claude.get('cost_usd') or 0):.2f}, {int(claude['requests'])} requests")
    if ollama.get("requests"):
        tokens = int(ollama.get("prompt_tokens") or 0) + int(ollama.get("completion_tokens") or 0)
        bits.append(f"Ollama {int(ollama['requests'])} requests, {tokens:,} tokens")
    return _card("usage", "ok", "; ".join(bits) + ", all time.", requests, "settings")


def strip(req: Request, columns: dict[str, list[dict[str, Any]]] | None, now: datetime | None = None,
          seconds: float = CARD_SECONDS) -> list[dict[str, Any]]:
    """The six cards, each read on its own thread under one shared deadline. A card that raises or runs out of time
    is reported as that card's own ``error``; the rest are unaffected."""
    jobs = {
        "mail": lambda: mail_card(req),
        "calendar": lambda: calendar_card(req, now),
        "goals": lambda: goals_card(columns) if columns is not None else _card("goals", "error", "Jobs could not be read.", target="goals"),
        "overnight": lambda: overnight_card(req, now),
        "security": lambda: security_card(req),
        "usage": usage_card,
    }
    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="today-card")
    futures = {key: pool.submit(fn) for key, fn in jobs.items()}
    wait(list(futures.values()), timeout=seconds)
    out = []
    for key in CARD_ORDER:
        fut = futures[key]
        if not fut.done():
            out.append(_card(key, "error", "Took too long to read."))
            continue
        try:
            out.append(fut.result())
        except Exception as exc:  # noqa: BLE001 -- one failing source is one card, never the page
            out.append(_card(key, "error", f"{type(exc).__name__}: {str(exc)[:140]}"))
    pool.shutdown(wait=False, cancel_futures=True)
    return out


def continue_items() -> list[dict[str, Any]]:
    """Up to three threads to pick up, newest first, across projects."""
    from dourmouse.projects_store import get_project_store

    store = get_project_store()
    rows = []
    for p in store.list():
        for th in store.get(p["id"])["threads"][:CONTINUE_MAX]:
            rows.append((th["updated_at"], p, th))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [{
        "project": p["id"], "project_name": p["name"], "thread": th["id"], "title": th["title"],
        "line": (th.get("summary") or "")[:120], "at": at, "target": f"projects/{p['id']}/{th['id']}",
    } for at, p, th in rows[:CONTINUE_MAX]]


def privacy_line(now: float | None = None) -> dict[str, Any]:
    """What left this Mac in the last 24 hours: cloud requests, sources shared, redactions."""
    from dourmouse import privacy

    now = time.time() if now is None else now
    cutoff = now - 86400
    requests = sources = redactions = 0
    for row in privacy.read_egress(500):
        try:
            ts = datetime.strptime(str(row.get("ts")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            continue
        requests += 1
        sources += len(row.get("sources") or [])
        redactions += sum(int(v) for v in (row.get("redactions") or {}).values() if isinstance(v, (int, float)))
    return {"hours": 24, "requests": requests, "sources": sources, "redactions": redactions}


@route("GET", "/api/os/today")
def today(req: Request) -> tuple[int, dict[str, Any]]:
    try:
        columns: dict[str, list[dict[str, Any]]] | None = _jobs_board()
    except Exception:  # noqa: BLE001 -- the Goals card reports it; the rest of Home still draws
        columns = None
    attention = []
    queue = getattr(req.server, "attention", None)
    if queue is not None:
        attention = queue.snapshot()
    needs = needs_you(columns or {}, attention) + feed_needs_you()
    off = office(columns or {})
    cards = strip(req, columns)
    cal_card = next(c for c in cards if c["key"] == "calendar")
    cal = cal_card.pop("raw", None) or {"state": "error", "events": [], "note": cal_card["line"]}
    try:
        priv: dict[str, Any] | None = privacy_line()
    except Exception:  # noqa: BLE001
        priv = None
    try:
        again = continue_items()
    except Exception:  # noqa: BLE001
        again = []
    return 200, {
        "ok": True, "at": time.time(), "greeting": greeting(),
        "needs_you": needs[:NEEDS_MAX], "needs_total": len(needs),
        "office": off, "calendar": cal, "projects": tiles(),
        "strip": cards, "continue": again, "privacy": priv,
    }
