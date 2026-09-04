"""What each model is told about itself, its tools, and the other models.

The problem this solves, reported directly by the user: "the models don't
know what tools or agents they can use". That was literally true. The Claude
route talks to the CLI through an MCP bridge, and MCP tools are *discoverable*
but not *announced* -- the model has to go looking, which it did, burning a
ToolSearch round trip on every single turn before it could act, and sometimes
concluding it had no relevant tool when it plainly did. Drive was reachable
for weeks by an agent that never knew to look.

So: tell it, once, up front.

Why once and not every turn: the Claude CLI is invoked with --session-id on
the first call for a working directory and --resume on every call after, so
the CLI itself holds the conversation. Re-sending a two-thousand-token
capability briefing on every turn would pay for it every time and tell the
model nothing it was not already told. It is prepended to the first prompt of
a session and never again.

The roster is read from the REAL registry rather than hardcoded, so this can
never drift out of sync with the tools that actually exist -- which is the
failure mode the briefing is meant to prevent in the first place.
"""

from __future__ import annotations

import threading
from typing import Any

_cache_lock = threading.Lock()
_preamble_cache: str | None = None


def _roster_lines() -> list[str]:
    """One line per agent: its name, and what it can actually do."""
    try:
        from dourmouse.general_roster import build_general_registry
    except Exception:  # noqa: BLE001 - a briefing must never break a turn
        return []

    try:
        registry = build_general_registry()
    except Exception:  # noqa: BLE001
        return []

    lines: list[str] = []
    for sub in sorted(registry.all_subagents(), key=lambda s: s.name):
        tools = sorted(t.name for t in getattr(sub, "tools", []))
        if not tools:
            continue
        # The description is often several sentences; the first is the part
        # that identifies the agent, and the rest is caveat detail the model
        # does not need in a roster summary.
        desc = " ".join(str(getattr(sub, "description", "")).split())
        first = desc.split(". ")[0].rstrip(".") if desc else ""
        shown = ", ".join(tools[:10])
        if len(tools) > 10:
            shown += f", +{len(tools) - 10} more"
        lines.append(f"  {sub.name} — {first}\n      tools: {shown}")
    return lines


def _rag_lines() -> list[str]:
    """What long-term knowledge is available, and its real current state.

    Reports live status rather than a static claim: a briefing that says a
    store is available when it is not is worse than saying nothing, because
    the model will confidently try and then report a failure to the user.
    """
    lines: list[str] = []

    try:
        from dourmouse import config

        store_desc = "local SQLite memory store on this machine"
        try:
            import os

            remote = (os.environ.get("DOURMOUSE_MEMORY_REMOTE_URL") or "").strip()
            if remote:
                store_desc = f"remote memory store at {remote}"
        except Exception:  # noqa: BLE001
            pass
        lines.append(
            f"  remember / recall / query_shared_memory — long-term memory ({store_desc}). "
            "Facts persist across sessions and across every model here."
        )
        del config
    except Exception:  # noqa: BLE001
        pass

    try:
        from dourmouse import desktop_rag  # type: ignore[attr-defined]

        status = desktop_rag.desktop_rag_status()
        state = "reachable" if status.get("ok") else f"UNAVAILABLE — {status.get('detail')}"
        lines.append(
            f"  desktop vault — ~1,023,765 chunks of reference text on the "
            f"Windows desktop over SSH. Currently {state}."
        )
    except Exception:  # noqa: BLE001 - module may not exist yet
        pass

    return lines


def claude_orchestrator_preamble() -> str:
    """The briefing prepended to the first Claude turn of a session.

    Cached: the roster does not change within a process, and rebuilding the
    registry on every session start would be real work for an identical
    string.
    """
    global _preamble_cache
    with _cache_lock:
        if _preamble_cache is not None:
            return _preamble_cache

        parts: list[str] = []
        parts.append(
            "You are the orchestrator inside Dourmouse, a desktop assistant "
            "running on this user's own machine. You are the only model the "
            "user talks to. You are not working alone: a local model (Ollama) "
            "and, when configured, a cloud model (Gemini) are available to do "
            "work you hand them."
        )
        parts.append(
            "HOW TO WORK\n"
            "- You have Dourmouse's real tools, exposed to you as "
            "mcp__dourmouse__<name>. They act on this user's actual accounts "
            "and machine: real inbox, real Drive, real files, real money. "
            "Treat their output as fact and never invent a result.\n"
            "- When a request has independent parts, call "
            "mcp__dourmouse__delegate_to_models ONCE with all of them rather "
            "than working through them one at a time. They run concurrently, "
            "so a batch costs about as long as its slowest item. This is the "
            "main reason you are fast.\n"
            "- Delegation routing is privacy-first and automatic: anything "
            "touching mail, memory, files, money or the user's repositories "
            "stays on the local model; public research may go to the cloud "
            "one. You can override per task, but do not move private data to "
            "the cloud.\n"
            "- If a tool reports NOT CONFIGURED or an error, say so plainly "
            "and say what would fix it. Never present a failure as an answer."
        )

        roster = _roster_lines()
        if roster:
            parts.append(
                "AGENTS YOU CAN DELEGATE TO (pass the name as 'agent' to "
                "delegate_to_models to give that task these tools):\n"
                + "\n".join(roster)
            )

        rag = _rag_lines()
        if rag:
            parts.append("LONG-TERM KNOWLEDGE:\n" + "\n".join(rag))

        parts.append(
            "This briefing is sent once, at the start of this session. You "
            "keep it for every following turn — you do not need to search for "
            "your own tools again."
        )

        _preamble_cache = "\n\n".join(parts)
        return _preamble_cache


def agent_context(agent: str | None) -> str:
    """A short capability note for a delegated local-model turn.

    Deliberately much shorter than the orchestrator briefing. A delegated
    turn is already scoped to one agent and its tools are already attached to
    the request; what it lacks is the knowledge that its tools are REAL and
    that honest failure beats a plausible guess.
    """
    name = (agent or "").strip()
    who = f"the {name} specialist" if name else "a general assistant"
    return (
        f"You are {who} inside Dourmouse, running on the user's own machine. "
        "The tools attached to this request are real and act on the user's "
        "actual accounts, files and data — use them rather than answering "
        "from memory, and never invent a result. If a tool reports NOT "
        "CONFIGURED or fails, report that plainly instead of guessing. Answer "
        "the question directly and briefly; another model is assembling your "
        "answer together with others."
    )


def reset_cache() -> None:
    """Drop the cached briefing. For tests, and for a live roster reload."""
    global _preamble_cache
    with _cache_lock:
        _preamble_cache = None
