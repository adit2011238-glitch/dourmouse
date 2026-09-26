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
        "code_ollama", "compute", "browser", "panel_control", "apps", "design_3d")),
    ("fin", "Finance & Media", (
        "forex", "freebuff", "google_workspace", "markets", "media", "music", "mt5", "t212")),
    ("lounge", "Lounge", ("mail",)),
)

RULE = (
    "Floors are a fixed table in dourmouse/os_api/office.py, chosen by what an agent is for "
    "(operations, research, security, engineering, finance and media). The registry carries no "
    "team field. An agent the table does not name goes to an Unassigned floor and is counted."
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
