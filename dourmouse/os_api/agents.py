"""Agent profiles for the UI (PL-10 thread T3, finding #193).

``GET /api/os/agents`` lists the thirteen agents plus Agent Smith with their
job, contract, skills (the old agent names), tools, model, memory namespace,
workspace folder, permission summary, step budget and capability-test
standing. ``GET /api/os/agents/profile?id=<agent id or old name>`` returns one
profile; an old agent name answers with the agent that now owns it.

The data comes from ``dourmouse.agent_profiles`` and the live registry. An
agent with no recorded capability test is reported as ``untested``, never as
available.
"""

from __future__ import annotations

from typing import Any

from . import ApiError, Request, route


def _results(server: Any) -> dict[str, dict[str, Any]]:
    """Capability-test summaries per agent id (PL-10 T3 step 5). None are
    recorded yet, so every agent reads as untested."""
    getter = getattr(server, "capability_results", None)
    if callable(getter):
        return getter() or {}
    return {}


@route("GET", "/api/os/agents")
def agents_index(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse.agent_profiles import build_agents_payload

    payload = build_agents_payload(req.server.registry, getattr(req.server, "config", None), _results(req.server))
    return 200, {"ok": True, **payload}


@route("GET", "/api/os/agents/profile")
def agent_profile(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse.agent_profiles import build_profile, get_profile, resolve

    name = req.need("id")
    found = resolve(name)
    profile = get_profile(found.agent_id) if found is not None else None
    if found is None or profile is None:
        raise ApiError(404, f"unknown agent {name!r}")
    data = build_profile(profile, req.server.registry, getattr(req.server, "config", None),
                         _results(req.server).get(profile.id))
    return 200, {"ok": True, "asked_for": name, "via": found.via, "profile": data}
