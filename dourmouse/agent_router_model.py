"""User-directed (2026-09-15): "use the local agent router model as your
router for choosing tools" — a real, user-fine-tuned local Ollama model
(``agent-router:latest``, 3.1B, Qwen2.5-based) trained specifically to
pick which registered agent a query belongs to.

Real, evidence-based background (a real 15-query comparison run earlier
this session against the deterministic ``planner.find_agents_for_query``
scorer, using the real 36-agent production registry): 8/8 clean
agreement on mail/news/apps/docs/browser/markets/memory-shaped queries,
but 4 of 5 coding-shaped queries got NO tool call at all (the model just
answered directly instead of routing), and the one call it did produce
was garbled (``agent='is_prime(17)'`` — a function call, not an agent
name). Two real, separate deterministic-scorer bugs surfaced the SAME
day this was requested ("documents" keyword-colliding with the `docs`
Google-Workspace agent for a plain local-folder question; a `system`
agent that scored below the routing threshold for an obviously
file-shaped query) are exactly the class of mistake this model doesn't
make — it isn't doing substring/keyword matching at all.

Given both real, on-the-record failure modes, this is wired as the
PRIMARY router with the deterministic scorer strictly as the fallback,
never the other way — a None return here must never be read as "no
agent needed", only "ask find_agents_for_query instead". This keeps the
real upside (correct routing on the class of query the deterministic
scorer just got wrong twice) without the real downside (a coding-shaped
query silently getting zero tools) ever reaching the user: any failure
shape (network error, timeout, no tool call, an unknown/garbled agent
name) returns None and the caller falls back exactly as before this
model existed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

# Always the LOCAL daemon, never Ollama Cloud — this is a small, user-
# fine-tuned model that only exists on this machine; there is no cloud
# equivalent to route to, unlike the general Ollama backend config.
_ROUTER_BASE_URL = "http://127.0.0.1:11434"
_ROUTER_MODEL_ENV = "DOURMOUSE_AGENT_ROUTER_MODEL"
_DEFAULT_ROUTER_MODEL = "agent-router:latest"

# Real, live-tested request timeout — this sits on the critical path of
# every plain single-agent turn, so a slow/hung local daemon must never
# stall a real directive. Short enough to fail fast into the
# deterministic fallback; long enough for a real small-model local
# inference (measured live: well under this during this session's own
# comparison testing).
_ROUTER_TIMEOUT_S = 6.0

_ROUTE_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "route_to_agent",
        "description": "Choose which registered agent should handle this query.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "The exact name of the best-matching agent.",
                }
            },
            "required": ["agent"],
        },
    },
}


def _router_model() -> str:
    return os.environ.get(_ROUTER_MODEL_ENV, "").strip() or _DEFAULT_ROUTER_MODEL


def route_via_local_model(
    query: str, agent_names: list[str] | set[str], timeout: float = _ROUTER_TIMEOUT_S
) -> str | None:
    """Ask the local fine-tuned router model which agent should handle
    ``query``. Returns a real, currently-registered agent name, or None
    on ANY failure — a query the model didn't route, routed to an agent
    that doesn't exist (a stale/renamed name, or garbled output), or a
    real network/daemon failure. Never raises; the whole point is a
    caller can unconditionally fall back to the deterministic scorer on
    None without a try/except of its own.
    """
    query = (query or "").strip()
    if not query:
        return None
    valid = {str(n).strip() for n in agent_names if str(n).strip()}
    if not valid:
        return None
    payload = {
        "model": _router_model(),
        "messages": [{"role": "user", "content": query}],
        "tools": [_ROUTE_TOOL_SPEC],
        "stream": False,
    }
    req = urllib.request.Request(
        f"{_ROUTER_BASE_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed localhost host
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None
    tool_calls = (data.get("message") or {}).get("tool_calls") or []
    if not tool_calls:
        # The model answered directly instead of routing — the exact
        # real failure mode this module's own docstring documents for
        # coding-shaped queries. Honest None, not a guess.
        return None
    args = (tool_calls[0].get("function") or {}).get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    agent = str(args.get("agent") or "").strip()
    return agent if agent in valid else None
