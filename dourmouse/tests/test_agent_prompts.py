"""Validation tests for dourmouse/agent_prompts.py.

AGENT_SYSTEM_PROMPTS is wired into dispatch.py as of v8.31 (commit
3ae24a3) -- dispatch.py splices a bespoke prompt in alongside the generic
roster prose whenever a turn resolves to exactly one agent that has an
entry here. This file guards the dict's own integrity so a bad entry
can't quietly ship:

- every key is a real subagent actually registered in the live registry
  (build_general_registry() + the ``system`` subagent it adds), so a typo'd
  or since-removed agent name can never sit in the dict unnoticed.
- every value is non-empty (a real prompt, not a placeholder).
- every value actually looks like one of the house-style prompts ("You are
  the Dourmouse/DOURMOUSE [<name>] Agent, ...") and names the same agent as
  its own dict key, catching a block copied under the wrong key.
- design_3d (the one hand-written, non-PDF entry) is checked a bit more
  specifically: it must stay honest about NOT being a renderer/mesh
  generator, matching design_3d_ops.py's own scope disclaimer.
"""

from __future__ import annotations

import re

from dourmouse.agent_prompts import AGENT_SYSTEM_PROMPTS
from dourmouse.general_roster import build_general_registry

_HEADER_RE = re.compile(r"^You are the DOURMOUSE \[([\w.]+)\] Agent,", re.IGNORECASE)


def _real_agent_names() -> set[str]:
    registry = build_general_registry()
    return {sub.name for sub in registry.all_subagents()}


def test_every_key_is_a_real_registered_agent() -> None:
    real_names = _real_agent_names()
    unmatched = set(AGENT_SYSTEM_PROMPTS) - real_names
    assert not unmatched, (
        f"AGENT_SYSTEM_PROMPTS has keys with no matching registered "
        f"subagent: {sorted(unmatched)}"
    )


def test_no_value_is_empty() -> None:
    for name, prompt in AGENT_SYSTEM_PROMPTS.items():
        assert isinstance(prompt, str), name
        assert prompt.strip(), f"{name}: prompt text is empty"


def test_every_prompt_opens_with_its_own_agent_header() -> None:
    for name, prompt in AGENT_SYSTEM_PROMPTS.items():
        match = _HEADER_RE.match(prompt.strip())
        assert match, (
            f"{name}: prompt does not open with the expected "
            f'"You are the Dourmouse [{name}] Agent," header'
        )
        assert match.group(1) == name, (
            f"{name}: prompt header names a different agent "
            f"({match.group(1)!r})"
        )


def test_dict_covers_a_real_subset_of_the_31_agent_roster() -> None:
    real_names = _real_agent_names()
    # v8.30's per-agent-model comment and dispatch.py's own "all-31-agent
    # roster" comment both describe the current roster size. Not asserting
    # an exact "31" here (that would break the moment the roster grows) --
    # asserting the PDF's extracted subset is non-trivial and really is a
    # subset of whatever is registered today.
    assert AGENT_SYSTEM_PROMPTS
    assert set(AGENT_SYSTEM_PROMPTS).issubset(real_names)


def test_design_3d_is_registered_and_covered() -> None:
    # design_3d is the one hand-written (non-PDF) entry -- guard that it's
    # actually a real registered agent and actually has a prompt, same as
    # every other key, so this doesn't silently regress if the roster
    # wiring in general_roster.py ever changes.
    assert "design_3d" in _real_agent_names()
    assert "design_3d" in AGENT_SYSTEM_PROMPTS


def test_design_3d_prompt_is_honest_about_scope() -> None:
    # Real feedback from a live demo: the generic roster prose alone routed
    # correctly but framed responses weakly. The bespoke prompt must stay
    # consistent with design_3d_ops.py's own "not a renderer/mesh
    # generator" scope disclaimer rather than contradicting or softening it.
    prompt = AGENT_SYSTEM_PROMPTS["design_3d"]
    lowered = prompt.lower()
    assert "not a 3d renderer" in lowered or "not a renderer" in lowered
    assert "mesh" in lowered and "cad" in lowered
    assert ".obj" in lowered and ".glb" in lowered
    assert "confirmation" in lowered
    assert "write_manifest_entry" in prompt


def test_research_info_scopes_effort_to_the_request() -> None:
    # backlog #11 asked for effort scaled to the request; the 2026-10-19 owner
    # feedback (answers clear but not researched) removed the "answer from
    # knowledge" escape: depth scales, the check does not.
    prompt = AGENT_SYSTEM_PROMPTS["research_info"]
    assert "EFFORT SCALING" in prompt
    lowered = prompt.lower()
    assert "at least one retrieved source" in lowered
    assert "do not answer a factual question from memory" in lowered
    assert "check the premise first" in lowered
    assert "diverge" not in lowered or "multi-step research" in lowered


def test_research_first_is_in_the_base_and_orchestrator_prompts() -> None:
    from dourmouse.dispatch import _SYSTEM_PROMPT

    assert "RESEARCH FIRST" in _SYSTEM_PROMPT
    assert "from memory, not verified" in _SYSTEM_PROMPT
    # the old escape hatches that let a fluent unchecked answer through
    assert "your own knowledge is a real answer" not in _SYSTEM_PROMPT
    assert "offer the nearest tool" not in _SYSTEM_PROMPT
    assert "RESEARCH FIRST" in AGENT_SYSTEM_PROMPTS["orchestrator"]


def test_rnd_prompt_treats_fetched_content_as_untrusted() -> None:
    # Real gap found in a security audit (Phase 1, docs/GODSPEED_ROADMAP.md):
    # research_web_search/research_fetch_url retrieve genuinely adversarial-
    # controlled internet content, but this prompt had zero instruction-
    # hierarchy language, unlike the "mail" agent's established "untrusted
    # data" framing for email/Drive content. Regression lock for the fix.
    prompt = AGENT_SYSTEM_PROMPTS["rnd"]
    assert "UNTRUSTED CONTENT" in prompt
    lowered = prompt.lower()
    assert "untrusted data" in lowered
    assert "ignore previous instructions" in lowered
    assert "prompt-injection attempt" in lowered


def test_browser_prompt_treats_page_content_as_untrusted() -> None:
    # Same audit, same gap. browser_agent.py itself has no prompt constant of
    # its own -- it's pure tool implementation (browser_open, browser_click,
    # browser_extract, ...). The real per-agent prompt for those tools is
    # AGENT_SYSTEM_PROMPTS["browser"], spliced in by dispatch.py exactly like
    # rnd and mail. It drives a real browser rendering arbitrary third-party
    # pages, so it gets the same untrusted-content framing.
    prompt = AGENT_SYSTEM_PROMPTS["browser"]
    assert "UNTRUSTED CONTENT" in prompt
    lowered = prompt.lower()
    assert "untrusted data" in lowered
    assert "ignore previous instructions" in lowered
    assert "prompt-injection attempt" in lowered
