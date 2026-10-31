"""Peer tools: any agent can see who else exists and ask one of them for help.

Until now only the orchestrator and the companion could reach another agent
(delegate_task). Every chat screen pins ONE agent's tools and drops
delegate_task on purpose (dispatch._scoped_tool_specs, v8.12: a pinned agent
that could delegate to itself recursed to the depth cap), so the browser, the
mail agent, the research agent and the rest were walled in: none could hand a
job to another agent. These two tools are offered to every run that gets tools
(dispatch.PEER_TOOLS), whatever its pin:

- ``list_agents``: deterministic, no model. Who exists and what each does.
- ``ask_agent``: hand one job to one OTHER agent and get its answer back.

ask_agent adds no second nested-run engine. After its own checks it calls the
delegate_task handler, so the depth limit, the shared delegate budget, the job
audit log, the run policy and the privacy and DLP settings are exactly the
ones delegate_task already has. What it adds is the peer rules:

- it never asks the agent that is asking (the v8.12 recursion bug);
- it never asks an agent already in the chain (browser asks research_info,
  which asks browser again);
- the owner is asked first when something in the conversation reads text
  written by other people and the target can act (dispatch._argument_gate).

Owned by the messenger agent (the agent whose job is agents talking to each
other) and found by name across the registry, like the shared desk.
"""

from __future__ import annotations

from typing import Any

from dourmouse.dispatch import (
    DispatchRegistry,
    Permission,
    ToolSpec,
    current_dispatch_context,
)

#: Longest agent description list_agents prints per agent.
_LINE_LIMIT = 120


def _first(arguments: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(arguments.get(key) or "").strip()
        if value:
            return value
    return ""


def _one_line(text: str, limit: int) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[: limit - 3].rstrip() + "..."


def _first_sentence(text: str, limit: int) -> str:
    one = " ".join(str(text or "").split())
    cut = one.find(". ")
    if 0 < cut < limit:
        one = one[: cut + 1]
    return _one_line(one, limit)


def build_list_agents_tool(registry: DispatchRegistry) -> ToolSpec:
    def _list_agents(arguments: dict[str, Any]) -> str:
        which = _first(arguments, "agent", "name", "subagent")
        if which:
            sub = registry.get_subagent(which)
            if sub is None:
                return f"ERROR: unknown agent {which!r}. Call list_agents with no argument to see every agent."
            lines = [f"{sub.name} ({sub.domain}): {_one_line(sub.description, 400)}", "tools:"]
            for tool in sub.tools:
                asks = " [asks the owner first]" if tool.permission is Permission.REQUIRES_CONFIRMATION else ""
                lines.append(f"- {tool.name}{asks}: {_first_sentence(tool.description, 160)}")
            return "\n".join(lines)
        lines = []
        for sub in registry.all_subagents():
            lines.append(
                f"- {sub.name} ({sub.domain}, {len(sub.tools)} tools): "
                f"{_first_sentence(sub.description, _LINE_LIMIT)}"
            )
        return (
            f"{len(lines)} agents you can ask with ask_agent "
            "(list_agents with 'agent' set shows one agent's tools):\n" + "\n".join(lines)
        )

    return ToolSpec(
        name="list_agents",
        description=(
            "List the other agents and what each one does. Pass 'agent' to see one agent's tools in "
            "detail. Use it before ask_agent when you are not sure who can do a job."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "default": "", "description": "optional: one agent to describe"},
            },
        },
        handler=_list_agents,
    )


def build_ask_agent_tool(registry: DispatchRegistry, delegate_tool: ToolSpec) -> ToolSpec:
    """``delegate_tool`` is the registry's delegate_task ToolSpec: the one
    nested-run implementation, reused as it is."""

    def _ask_agent(arguments: dict[str, Any]) -> str:
        ctx = current_dispatch_context(registry)
        if ctx is None:
            return (
                "ERROR: ask_agent requires an active dispatch context "
                "(it can only be called from inside a dispatch run)."
            )
        target = _first(arguments, "agent", "subagent", "to_agent", "agent_name")
        question = _first(arguments, "question", "task", "prompt", "instructions", "request")
        if not target:
            return "ERROR: ask_agent requires 'agent', the agent to ask. list_agents shows who exists."
        if not question:
            return "ERROR: ask_agent requires a non-empty 'question'."
        if target not in registry.subagent_names:
            return (
                f"ERROR: unknown agent {target!r}. Known: "
                f"{', '.join(sorted(registry.subagent_names))}"
            )
        # Who is asking: the one pinned agent, else the agents the planner scoped this turn to.
        asking = {ctx.forced_agent} if ctx.forced_agent else set(ctx.routed_agents)
        if target in asking:
            return (
                f"REFUSED: you are already working as {target!r}. Use its tools directly "
                "instead of asking it; ask_agent is for a DIFFERENT agent."
            )
        chain = tuple(ctx.agent_chain) + ((ctx.forced_agent,) if ctx.forced_agent else ())
        if target in chain:
            return (
                f"REFUSED: {target!r} is already part of this conversation ({' -> '.join(chain)}). "
                "Asking it again would loop. Use what it already told you."
            )
        delegated: dict[str, Any] = {"task": question, "subagent": target}
        if arguments.get("max_turns") is not None:
            delegated["max_turns"] = arguments["max_turns"]
        result = delegate_tool.handler(delegated)
        if str(result).startswith(("ERROR", "REFUSED")):
            return result
        who = ctx.forced_agent or ", ".join(sorted(ctx.routed_agents)) or "orchestrator"
        return f"ANSWER FROM {target} (asked by {who}):\n{result}"

    return ToolSpec(
        name="ask_agent",
        description=(
            "Ask ONE other agent to do a piece of work with ITS tools and get its answer back. Use it "
            "when the job needs something your own tools cannot do, for example the browser agent asking "
            "research_info to look something up, or asking mail about a message. Name the agent exactly "
            "as list_agents shows it. Ask for the one thing you need, not for a long project. Never ask "
            "your own agent (call your own tools directly) or an agent already in this conversation. "
            "Anything that sends, deletes or changes things still asks the owner first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "the agent to ask, as list_agents names it"},
                "question": {"type": "string", "description": "what you need from that agent, with the details it needs"},
                "max_turns": {
                    "type": "integer", "default": 5,
                    "description": "tool-call turns the asked agent gets; leave the default, a small number cuts its work off",
                },
            },
            "required": ["agent", "question"],
        },
        handler=_ask_agent,
    )


def build_peer_tools(registry: DispatchRegistry, delegate_tool: ToolSpec) -> list[ToolSpec]:
    return [build_ask_agent_tool(registry, delegate_tool), build_list_agents_tool(registry)]
