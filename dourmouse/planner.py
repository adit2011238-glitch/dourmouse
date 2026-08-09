"""Multi-step task planning for the dispatch loop (v2.0 Phase 2.1).

The flat tool-call loop in dispatch.py has no explicit plan state — for
anything beyond 2-3 steps the model improvises turn by turn. This module
adds a structured plan WITHOUT a separate planning LLM call (that would
double latency/cost for simple requests): a cheap deterministic heuristic
decides whether a prompt is multi-step, and if so, builds a numbered list of
subtasks with the subagent each maps to (token-overlap scoring — pure
string matching, Rule 2.8 style; no LLM judgment in the plan path).

The orchestrator emits the plan as a visible ``plan`` transcript event
BEFORE executing (never a silent internal monologue), the UI renders it as
a [PLAN] block, and each later tool_use ties back to a step number so
multi-step delegation is checkable rather than opaque. Because the plan
event rides the same transcript that chat.py persists to session JSONL, the
plan is auditable for arbitrary sessions too.

``find_agents_for_query`` lives here (moved from webui.py, which re-exports
it) so planner.py is self-contained and imports nothing from the project
except the registry type it scores — no import cycles.
"""

from __future__ import annotations

import re
from typing import Any

_STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "what", "when",
    "where", "who", "which", "are", "can", "you", "your", "me", "my",
    "a", "an", "of", "to", "in", "on", "it", "is", "do", "does",
    "please", "use", "using", "help", "need", "want", "like", "how",
}

# Cheap multi-step markers: explicit sequencing language ("then", "after
# that") or 2+ distinct outcome verbs. Deliberately conservative — a false
# negative just means the model improvises as before; a false positive adds
# a plan block that is still honest and useful.
_SEQUENCE_MARKERS = (
    " then ",
    " after that",
    " and also",
    " next",
    " afterwards",
    " finally",
    ", and ",
    " also ",
    " as well as",
)
_OUTCOME_VERBS = (
    "search", "find", "write", "create", "draft", "run", "check",
    "list", "summarize", "fetch", "open", "delete", "read", "propose",
    "compare", "build", "test", "fix", "debug", "convert", "send",
)
# Word-boundary verb matcher (counts OCCURRENCES, so "search and summarize"
# in ONE clause still counts as two outcomes).
_OUTCOME_RE = re.compile(r"\b(?:search|find|write|create|draft|run|check|list|summarize|fetch|open|delete|read|propose|compare|build|test|fix|debug|convert|send)\b")
_SUBTASK_SPLIT = re.compile(
    r",\s*(?:then|and|also|after that)\s+|;\s+|\.\s+then\s+| and also | after that |\s+then\s+"
)

# Intent verbs -> the tool-name stems that satisfy that intent. When the query
# expresses an intent with a verb that is NOT the tool's literal name (e.g.
# "save" for write_file), the agent owning a tool whose stems overlap the
# intent's stems earns a capability bonus. This is what stops "save it to a
# file" from routing to an agent that can only list/delete (live failure:
# admin_ops, which owns no write tool, won the step by alphabet tie-break and
# the chain silently degraded to a hallucinated "saved").
_VERB_CAPABILITY: dict[str, set[str]] = {
    "write": {"write", "save", "create", "append", "put", "store", "edit"},
    "save": {"write", "save", "create", "append", "put", "store"},
    "create": {"write", "create", "append", "put", "store", "add", "new"},
    "edit": {"edit", "write", "change", "modify", "update"},
    "delete": {"delete", "remove", "erase"},
    "remove": {"delete", "remove", "erase"},
    "search": {"search", "find", "lookup", "query", "recall"},
    "find": {"search", "find", "lookup", "query", "recall"},
    "lookup": {"search", "find", "lookup", "query", "recall"},
    "read": {"read", "open", "view", "list", "show"},
    "list": {"list", "read", "show", "view"},
    "open": {"open", "read", "view"},
    "send": {"send", "message", "draft", "notify"},
    "draft": {"draft", "write", "compose", "message"},
    "build": {"write", "create", "build", "make", "code", "implement"},
    "make": {"write", "create", "build", "make", "code", "implement"},
    "summarize": {"summarize", "digest", "report", "brief"},
    "check": {"check", "status", "list", "inspect", "monitor"},
    "fetch": {"fetch", "get", "download", "search"},
    "run": {"run", "execute", "command"},
    "remember": {"remember", "store", "save", "memorize", "note"},
    "recall": {"recall", "search", "find", "lookup", "query"},
}

# High-confidence domain words that must route to ONE specific agent, stronger
# than any description overlap. These are the live misroutes the generic
# scorer got wrong ("check my inbox" -> admin_ops/atlas tie-break; "summarize
# new emails" -> news because 'new' matches 'news'). The boost is applied as a
# flat score addition to the owning agent (Rule 2.8: deterministic, no LLM).
_DOMAIN_ROUTE: dict[str, str] = {
    "inbox": "mail",
    "email": "mail",
    "emails": "mail",
    "gmail": "mail",
    "mail": "mail",
    "draft": "comms",  # drafting is comms; sending/reading is mail
    "news": "news",
    "headline": "news",
    "headlines": "news",
    "weather": "research_info",
    "btc": "markets",
    "bitcoin": "markets",
    "stock": "markets",
    "stocks": "markets",
    "quote": "markets",
    "quotes": "markets",
    "price": "markets",
    "prices": "markets",
    "forex": "markets",
    "task": "tasks",
    "tasks": "tasks",
    "todo": "tasks",
    "calendar": "scheduling",
    "schedule": "scheduling",
    "meeting": "scheduling",
    # NOT here: "web" — too broad. "build a web app" would steal a coding
    # request to research_info; "search the web" already routes via the
    # search verb + research_info's web_search tool stem (reviewer-caught).
    "wikipedia": "research_info",
}

# Stop words that are ALSO strong domain words must not be stripped — the
# generic stop-word filter would delete them before _DOMAIN_ROUTE sees them.
_DOMAIN_WORDS = set(_DOMAIN_ROUTE)



def _task_only(prompt: str) -> str:
    """Drop the delegated-run boilerplate before any heuristic runs.

    Nested delegate prompts append "[PARENT CONTEXT — read this; ...]"
    background to the task. The word 'read' plus the semicolons inside that
    boilerplate trips the multi-step heuristic (live: a one-word task "B"
    got a 3-step garbage plan and an extra LLM call). Background context is
    not the task, so planning must never see it.
    """
    # Anchor on the exact boilerplate prefix ("[PARENT CONTEXT — read this;
    # ...]") so a user prompt that merely MENTIONS the feature is not
    # truncated.
    return re.split(r"\[PARENT CONTEXT \u2014", prompt)[0]


def looks_multi_step(prompt: str) -> bool:
    """Is this prompt multi-step? Deterministic heuristic OR the learned net.

    The heuristic (sequencing language or 2+ outcome verbs) is the floor;
    the neural orchestrator (orch_net) adds an OR-branch only when it is
    trained AND confident (p > 0.65) from real outcomes. Delayed import so
    planner stays dependency-light for tests and the net's absence changes
    nothing (Rule 2.8: the decision is a deterministic function either way).
    """
    prompt = _task_only(prompt)
    lower = " " + prompt.lower().strip() + " "
    if any(m in lower for m in _SEQUENCE_MARKERS):
        return True
    clauses = re.split(r"[,.!?;]", lower)
    verbs_hit = sum(len(_OUTCOME_RE.findall(c)) for c in clauses)
    if verbs_hit >= 2:
        return True
    from dourmouse.orch_net import neural_is_multi_step

    return neural_is_multi_step(prompt)


def find_agents_for_query(
    registry: Any, query: str, limit: int = 3
) -> list[dict[str, Any]]:
    """Deterministically rank subagents by how well their capabilities match.

    Pure string matching (Rule 2.8 style: no LLM judgment in the lookup
    path), with two deliberately weighted signals on top of the plain
    token-overlap score:

    1. The query names a TOOL outright (e.g. ``write_file``) — the agent
       that owns that tool gets a strong bonus (+5 per named tool). Naming
       the tool is the most explicit intent a user can express, and it is
       what makes tool scoping safe: a step naming ``write_file`` must be
       planned at the agent that actually owns it.
    2. Path-like fragments (``/tmp/dm_orch_test.txt``) are stripped before
       tokenizing, so they cannot inject junk tokens (dm/tmp/txt/orch) that
       substring-match agent names like ``admin_ops`` and steal the step.

    Name overlap (a query token inside the subagent name) scores +3, other
    description/tool overlap +1. Returns [{name, score, tools: [...]}]
    sorted best-first; ties break on name, so results are deterministic
    for a given query + registry.
    """
    # 1) Strip path fragments first — a file path is not intent.
    cleaned = re.sub(r"\S*/\S+", " ", query.lower())
    tokens = {
        t
        for t in re.findall(r"[a-z0-9]{2,}", cleaned)
        if t not in _STOP_WORDS or t in _DOMAIN_WORDS
    }
    if not tokens:
        return []
    # 2) Explicit tool-name mentions in the RAW query (underscores intact):
    #    "write_file" is a tool name; "write" is a generic verb.
    mentions = set(re.findall(r"[a-z0-9_]{3,}", query.lower()))
    # Intent verbs in the query expand to the tool stems that satisfy them
    # ("save" -> write_* tools), so capability matches score even when the
    # user never names the tool literally.
    verb_stems: set[str] = set()
    for _t in tokens:
        if _t in _VERB_CAPABILITY:
            verb_stems |= _VERB_CAPABILITY[_t]
    # 3) High-confidence domain words (inbox/email/news/weather/btc/…) — a
    # flat strong boost for EVERY agent owning a matched domain word, so
    # "check my inbox" can never tie-break to admin_ops and "new emails"
    # never routes to news. Multiple domain words boost multiple agents
    # ("draft an email" boosts comms AND mail); normal scoring then decides
    # the winner. Deterministic: no iteration-order dependence.
    domain_targets = {_DOMAIN_ROUTE[w] for w in tokens if w in _DOMAIN_ROUTE}
    # Learned evidence (v5.6), computed ONCE per query (not per agent): the
    # neural orchestrator's routing head adds positive evidence only —
    # 0.5 * max(0, logit). Its max boost (~2) sits BELOW the deterministic
    # tool-mention (+5), domain (+4) and name (+3) bonuses, so the net
    # refines ties and near-misses but can never overturn a strong
    # deterministic match. Unknown/untrained agents score 0 and are
    # unaffected. Delayed import keeps planner dependency-light.
    from dourmouse.orch_net import _ROUTE_LAMBDA, neural_agent_scores

    agent_names = [s.name for s in registry.all_subagents()]
    nn = neural_agent_scores(query, agent_names)
    scored = []
    for sub in registry.all_subagents():
        haystack = " ".join(
            [sub.name, sub.description]
            + [t.name + " " + t.description for t in sub.tools]
        ).lower()
        name_hits = sum(1 for t in tokens if t in sub.name)
        hay_hits = sum(1 for t in tokens if t in haystack)
        tool_hits = sum(1 for t in sub.tools if t.name in mentions)
        # Capability credit: +1 per DISTINCT intent verb the agent's tools
        # satisfy, never per matching tool — a kitchen-sink agent (system's
        # read_path/list_path/open_path; memory's search_vault/recall) must
        # not compound points over a focused agent for the same single
        # intent. Pure tie-breaker: it resolves a write-intent step toward
        # the agent that OWNS write_* tools without ever outscoring an
        # explicit tool mention (+5) or a real name hit (+3).
        tool_stems = {
            stem
            for _tool in sub.tools
            for stem in re.split(r"[^a-z0-9]+", _tool.name.lower())
        }
        cap_hits = sum(
            1
            for _t in tokens
            if _t in _VERB_CAPABILITY and tool_stems & _VERB_CAPABILITY[_t]
        )
        score = name_hits * 3 + tool_hits * 5 + hay_hits + cap_hits
        # High-confidence domain boost: the owning agent gets a flat +4 —
        # enough to beat hay overlap (+1) and name hits (+3), never an
        # explicit tool mention (+5), so "use gmail_search" can't be stolen
        # by a domain word (reviewer-caught).
        if sub.name in domain_targets:
            score += 4
        if nn is not None and sub.name in nn:
            score += _ROUTE_LAMBDA * max(0.0, nn[sub.name])
        if score > 0:
            scored.append(
                {
                    "name": sub.name,
                    "score": score,
                    "tools": [t.name for t in sub.tools],
                }
            )
    scored.sort(key=lambda r: (-r["score"], r["name"]))
    return scored[:limit]


def build_plan(prompt: str, registry: Any, max_steps: int = 6) -> list[dict[str, Any]] | None:
    """Build a numbered subtask plan for a multi-step prompt, or None.

    Returns [{"n": 1, "task": "...", "subagent": "..."}, ...]. ``task`` is
    the raw subtask clause (honest — not rephrased by an LLM); ``subagent``
    is the best-matching registered subagent, or "orchestrator" when no
    agent scores (the orchestrator still carries it conversationally).
    Deterministic for a given prompt + registry.
    """
    prompt = _task_only(prompt).strip()
    if not prompt or not looks_multi_step(prompt):
        return None

    # Split on sequencing connectors first, then on plain sentence breaks.
    pieces = [p.strip() for p in _SUBTASK_SPLIT.split(prompt) if p.strip()]
    if len(pieces) < 2:
        pieces = [p.strip() for p in re.split(r"[.,;]", prompt) if p.strip()]
    if len(pieces) < 2:
        # Single clause but multi-step language ("search X then summarize"):
        # treat the whole prompt as one planned step for visibility.
        pieces = [prompt]

    steps: list[dict[str, Any]] = []
    for i, piece in enumerate(pieces[:max_steps], 1):
        matches = find_agents_for_query(registry, piece, limit=1)
        subagent = matches[0]["name"] if matches else "orchestrator"
        steps.append({"n": i, "task": piece, "subagent": subagent})
    return steps
