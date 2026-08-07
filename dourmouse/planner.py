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
    " first ",
    " second ",
    " third ",
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


def looks_multi_step(prompt: str) -> bool:
    """Cheap deterministic heuristic: sequencing language or 2+ outcomes."""
    lower = " " + prompt.lower().strip() + " "
    if any(m in lower for m in _SEQUENCE_MARKERS):
        return True
    clauses = re.split(r"[,.!?;]", lower)
    verbs_hit = sum(len(_OUTCOME_RE.findall(c)) for c in clauses)
    return verbs_hit >= 2


def find_agents_for_query(
    registry: Any, query: str, limit: int = 3
) -> list[dict[str, Any]]:
    """Deterministically rank subagents by how well their capabilities match.

    Scores each agent by token overlap between the query and its name +
    description + every tool name/description. Pure string matching (Rule
    2.8 style: no LLM judgment in the lookup path). Returns
    [{name, score, tools: [...]}] sorted best-first.
    """
    tokens = {
        t for t in re.findall(r"[a-z0-9]{2,}", query.lower()) if t not in _STOP_WORDS
    }
    if not tokens:
        return []
    scored = []
    for sub in registry.all_subagents():
        haystack = " ".join(
            [sub.name, sub.description]
            + [t.name + " " + t.description for t in sub.tools]
        ).lower()
        name_hits = sum(1 for t in tokens if t in sub.name)
        hay_hits = sum(1 for t in tokens if t in haystack)
        score = name_hits * 3 + hay_hits
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
    prompt = prompt.strip()
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
