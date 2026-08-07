"""Self-improvement loop (v4.0, Phase 13) — the system reviews itself.

Pure, deterministic statistics over the inter-agent bus plus honest,
heuristic-driven improvement suggestions. The digest is computed from REAL
bus traffic (``get_message_bus``), never fabricated: if an agent has posted
nothing, the digest says so. Suggestions are conservative rules, not magic:

- an agent that has never spoken may be misconfigured (flag it)
- the most prolific agents are doing the real work (surface them)
- a silent agent with a critical role deserves a check-in

Nothing here is written back to the bus and nothing raises: a self-review
that can crash the system would be worse than no review at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from dourmouse.message_bus import get_message_bus


def digest_from_messages(
    messages: list[dict[str, Any]],
    agents: list[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reduce bus traffic into a per-agent digest + improvement suggestions.

    ``messages`` is the JSON-safe snapshot shape from ``MessageBus.snapshot``
    (newest first). Pure function — no I/O, no global state — so tests can
    feed it an arbitrary history and assert the arithmetic exactly.
    """
    clock = now or datetime.now(timezone.utc)
    stats: dict[str, dict[str, Any]] = {}
    for name in agents:
        stats[name] = {
            "sent": 0,
            "received": 0,
            "last_sent_at": None,
            "last_subject": None,
            "subjects": {},  # subject -> count, to find the tool most used
        }

    for msg in messages:
        sender = stats.get(msg.get("from", ""))
        if sender is not None:
            sender["sent"] += 1
            subject = str(msg.get("subject") or "")[:80]
            sender["subjects"][subject] = sender["subjects"].get(subject, 0) + 1
            sender["last_sent_at"] = msg.get("at")
            sender["last_subject"] = subject
        receiver = stats.get(msg.get("to", ""))
        if receiver is not None:
            receiver["received"] += 1

    # Roll up: top tool per agent + idle detection + suggestions.
    suggestions: list[str] = []
    per_agent: dict[str, dict[str, Any]] = {}
    for name, s in stats.items():
        top = sorted(s["subjects"].items(), key=lambda kv: (-kv[1], kv[0]))
        top_tool = top[0][0] if top else None
        per_agent[name] = {
            "sent": s["sent"],
            "received": s["received"],
            "last_sent_at": s["last_sent_at"],
            "top_activity": top_tool,
            "activity_count": top[0][1] if top else 0,
        }
        if s["sent"] == 0:
            suggestions.append(
                f"{name}: no bus traffic in the digest window — check configuration."
            )
        elif top_tool and s["subjects"][top_tool] >= 3:
            suggestions.append(
                f"{name}: most active via '{top_tool}' "
                f"({s['subjects'][top_tool]} posts) — the current workload driver."
            )

    if not suggestions:
        suggestions.append("No anomalies detected — all agents are quiet or idle.")

    return {
        "generated_at": clock.isoformat(timespec="seconds"),
        "message_count": len(messages),
        "agents": per_agent,
        "suggestions": suggestions,
    }


def build_daily_digest(registry: Any) -> dict[str, Any]:
    """Live digest over the process bus + the registry's roster.

    Deliberately defensive: a missing bus or a bad snapshot degrades to an
    honest error report instead of a traceback (Rule 2.2).
    """
    try:
        bus = get_message_bus()
        messages = bus.snapshot(limit=500)
        agents = [sub.name for sub in registry.all_subagents()]
        return digest_from_messages(messages, agents)
    except Exception as exc:  # pragma: no cover - defensive, no crash allowed
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "message_count": 0,
            "agents": {},
            "suggestions": [f"digest unavailable: {type(exc).__name__}: {exc}"],
        }
