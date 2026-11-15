"""Backend for the OS shell's OFFICE screen (finding #147).

``GET /api/os/office/floors`` groups the real agent roster into the building's
floors. The server had no team field, so the grouping rule lives here, in one
place, and is applied to the roster that ``/api/roster`` reports:

* Each floor lists agent names. An agent on the roster that no floor names is
  NOT dropped: it is placed on an extra floor called "Unassigned" and listed
  under ``unassigned``, so a new agent shows up in the building the day it is
  registered instead of silently going missing (the mockup's own comment warns
  about exactly that 42 of 43 undercount).
* A name a floor lists that is not on the roster is reported under ``unknown``
  and not drawn. The screen shows that count, so a stale table is visible.
* ``mail`` is an always-on poller that belongs to no team, so it sits in the
  Lounge, which the screen shows regardless of the selected floor.

The grouping is a design decision, not data the registry carries; the response
says so (``rule``). Statuses are NOT here: they come from ``/api/activity`` and
its event stream.
"""

from __future__ import annotations

from typing import Any

from . import Request, route

FLOORS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("exec", "Executive & Ops", (
        "orchestrator", "comms", "companion", "admin_ops", "scheduling", "tasks",
        "goals", "system", "memory", "messenger", "docs")),
    ("research", "Research & Intel", (
        "research_info", "research_mesh", "evidence_pipeline", "device_wiki", "study",
        "worldmonitor", "globe", "news", "rnd")),
    ("trust", "Trust & Security", ("security", "reviewer", "agent_smith")),
    ("eng", "Engineering", (
        "dev_coding", "code_claude", "code_codex", "code_deepseek", "code_nvidia",
        "code_ollama", "compute", "browser", "panel_control", "apps", "app_driver", "design_3d")),
    ("fin", "Finance & Media", (
        "forex", "freebuff", "google_workspace", "markets", "media", "music", "mt5", "t212")),
    ("lounge", "Lounge", ("mail",)),
)

RULE = (
    "Floors group agents by what they are for: operations, research, security, engineering, "
    "finance and media. The agent registry has no team field, so the grouping is chosen by hand. "
    "An agent that no floor names goes to an Unassigned floor and is counted."
)


def build_floors(subagents: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {s["name"]: s for s in subagents}
    named: set[str] = set()
    floors: list[dict[str, Any]] = []
    unknown: list[str] = []
    for fid, title, names in FLOORS:
        agents = []
        for name in names:
            named.add(name)
            sub = by_name.get(name)
            if sub is None:
                unknown.append(name)
                continue
            agents.append(_agent(sub))
        floors.append({"id": fid, "name": title, "agents": agents})
    leftover = sorted(n for n in by_name if n not in named)
    if leftover:
        floors.append({"id": "unassigned", "name": "Unassigned", "agents": [_agent(by_name[n]) for n in leftover]})
    return {
        "floors": floors,
        "total": len(by_name),
        "unassigned": leftover,
        "unknown": unknown,
        "rule": RULE,
    }


def _agent(sub: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": sub["name"],
        "domain": sub.get("domain"),
        "description": (sub.get("description") or "")[:240],
        "model": sub.get("model"),
        "tool_count": len(sub.get("tools") or []),
    }


@route("GET", "/api/os/office/floors")
def floors(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse.webui import build_roster_payload

    roster = build_roster_payload(req.server.registry, getattr(req.server, "config", None))
    return 200, {"ok": True, **build_floors(roster["subagents"])}


# ---------------------------------------------------------------------------
# Floor, Team and Activity from real events (plan 1.7, finding 203)
#
# The fourteen agent identities (agent_profiles) sit in eight rooms. A desk's
# state is what the tracker, the jobs board and the running fan-outs really
# report, nothing else:
#   working   a skill of the agent is computing, the agent owns a running job, or it is a branch of a running split task
#   needs_you a skill is waiting for a sign-in or approval, or a job it owns waits for you
#   meeting   the agent is a running branch of a fan-out (it sits in the Meeting room)
#   idle      none of the above
# The tracker also reports "live" for the model-free feed polls (news, markets, rnd, mail, tasks). A poll is
# not an agent run, so it never makes a desk work and never opens a lane (CC-1). It is reported once, as
# `feeds`, so the screen can say so in one plain line.
# There is no source for "rate limited", so no desk is ever drawn as rate limited.
# ---------------------------------------------------------------------------

ROOMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("front", "Front desk", ("orchestrator", "planner")),
    ("research", "Research wing", ("researcher", "librarian")),
    ("comms", "Comms", ("mail_calendar", "docs")),
    ("workshop", "Workshop", ("coder", "mac_operator", "browser", "agent_smith")),
    ("watch", "Watch room", ("security", "news_world")),
    ("trading", "Trading floor", ("markets",)),
    ("studio", "Studio", ("media",)),
)
MEETING_ROOM = ("meeting", "Meeting room")
MESSAGE_WINDOW = 12


def _room_of(agent_id: str) -> str:
    for rid, _name, ids in ROOMS:
        if agent_id in ids:
            return rid
    return "other"


def _skills(profile: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*profile.skills, profile.id)))


def _agent_of(name: str) -> str | None:
    from dourmouse.agent_profiles import resolve

    found = resolve(name or "")
    return found.agent_id if found is not None else None


def _jobs_columns() -> dict[str, list[dict[str, Any]]]:
    from dourmouse import jobs
    from dourmouse.goals import get_goal_store

    board = jobs.board(get_goal_store())
    return {col["key"]: col["jobs"] for col in board["columns"]}


def meeting_agents(snapshot: dict[str, Any]) -> dict[str, str]:
    """agent id -> run id, for every fan-out branch that has not reported back."""
    out: dict[str, str] = {}
    for run_id, run in (snapshot.get("fanouts") or {}).items():
        for branch in (run.get("branches") or {}).values():
            if branch.get("phase") == "result":
                continue
            agent = _agent_of(branch.get("agent") or "")
            if agent:
                out[agent] = run_id
    return out


def _lines(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agent-to-agent messages that name two different agents. A broadcast or an unknown sender is a
    message but not a line between two desks, so it is left out of the picture."""
    out = []
    for m in messages:
        a, b = _agent_of(m.get("from") or ""), _agent_of(m.get("to") or "")
        if not a or not b or a == b:
            continue
        out.append({"from": a, "to": b, "subject": (m.get("subject") or m.get("body") or "")[:120], "at": m.get("ts")})
        if len(out) >= MESSAGE_WINDOW:
            break
    return out


def build_floor(snapshot: dict[str, Any], columns: dict[str, list[dict[str, Any]]],
                messages: list[dict[str, Any]]) -> dict[str, Any]:
    from dourmouse.agent_profiles import PROFILES

    tracked = snapshot.get("agents") or {}
    running = {j["owner"]: j for j in columns.get("running", []) if j.get("owner")}
    waiting = {j["owner"]: j for j in columns.get("needs_you", []) if j.get("owner")}
    meeting = meeting_agents(snapshot)
    agents: list[dict[str, Any]] = []
    for p in PROFILES:
        states = [(s, tracked.get(s) or {}) for s in _skills(p)]
        auth = any(t.get("status") == "auth" for _s, t in states)
        computing = [(s, t) for s, t in states if t.get("status") == "computing"]
        calls = sum(len(t.get("concurrent_call_ids") or []) for _s, t in states)
        if auth or p.id in waiting:
            status = "needs_you"
        elif computing or p.id in running or p.id in meeting:
            status = "working"
        else:
            status = "idle"
        doing = None
        if p.id in running:
            doing = {"kind": "job", "text": running[p.id].get("title") or "", "job": running[p.id].get("id")}
        elif computing:
            last = max((t.get("last") or {} for _s, t in computing), key=lambda x: x.get("at") or "")
            doing = {"kind": "tool", "tool": last.get("tool"), "at": last.get("at")}
        elif p.id in waiting:
            doing = {"kind": "job", "text": waiting[p.id].get("title") or "", "job": waiting[p.id].get("id")}
        agents.append({
            "id": p.id, "name": p.name, "job": p.job, "room": "meeting" if p.id in meeting else _room_of(p.id),
            "home": _room_of(p.id), "status": status, "calls": calls, "meeting": meeting.get(p.id), "doing": doing,
        })
    rooms = [{"id": rid, "name": name, "agents": [a["id"] for a in agents if a["home"] == rid and a["room"] != "meeting"]}
             for rid, name, _ids in ROOMS]
    stray = [a["id"] for a in agents if a["home"] == "other" and a["room"] != "meeting"]
    if stray:
        rooms.append({"id": "other", "name": "Other", "agents": stray})
    rooms.append({"id": MEETING_ROOM[0], "name": MEETING_ROOM[1], "agents": [a["id"] for a in agents if a["room"] == "meeting"]})
    return {
        "rooms": rooms, "agents": agents, "lines": _lines(messages), "feeds": _feeds(tracked),
        "counts": {k: sum(1 for a in agents if a["status"] == k) for k in ("working", "needs_you", "idle")},
    }


def _feeds(tracked: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for skill, t in tracked.items():
        if t.get("status") == "live" and t.get("last"):
            out.append({"skill": skill, "tool": t["last"].get("tool"), "at": t["last"].get("at")})
    return sorted(out, key=lambda f: f.get("at") or "", reverse=True)


def build_activity(snapshot: dict[str, Any], meetings: list[dict[str, Any]]) -> dict[str, Any]:
    """Lanes only for real runs: a skill that is computing or waiting for approval and holds a call id."""
    from dourmouse.agent_profiles import PROFILES, SKILL_TO_AGENT, get_profile

    lanes: list[dict[str, Any]] = []
    tracked = snapshot.get("agents") or {}
    for skill, t in tracked.items():
        if t.get("status") not in ("computing", "auth"):
            continue
        agent = SKILL_TO_AGENT.get(skill) or _agent_of(skill)
        profile = get_profile(agent) if agent else None
        last = t.get("last") or {}
        for call_id in t.get("concurrent_call_ids") or []:
            mine = last.get("call_id") == call_id
            lanes.append({
                "call_id": call_id, "agent": agent or skill, "agent_name": profile.name if profile else skill, "skill": skill,
                "status": t["status"], "tool": last.get("tool") if mine else None, "at": last.get("at") if mine else None,
            })
    names: dict[str, str] = {}
    for profile in PROFILES:
        for skill in _skills(profile):
            names[skill] = profile.name
    return {"lanes": lanes, "feeds": _feeds(tracked), "meetings": meetings, "names": names}


@route("GET", "/api/os/office/floor")
def floor(req: Request) -> tuple[int, dict[str, Any]]:
    log = getattr(req.server, "office_log", None)
    messages = log.recent_messages(60) if log is not None else []
    return 200, {"ok": True, **build_floor(req.server.tracker.snapshot(), _jobs_columns(), messages)}


@route("GET", "/api/os/office/activity")
def activity(req: Request) -> tuple[int, dict[str, Any]]:
    log = getattr(req.server, "office_log", None)
    meetings = log.recent_meetings(8) if log is not None else []
    return 200, {"ok": True, **build_activity(req.server.tracker.snapshot(), meetings)}
