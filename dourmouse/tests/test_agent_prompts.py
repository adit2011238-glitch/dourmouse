"""Validation tests for dourmouse/agent_prompts.py.

AGENT_SYSTEM_PROMPTS is NOT wired into dispatch.py yet (that is a separate,
later step) -- this file only guards the dict's own integrity so a bad
extraction can't quietly ship:

- every key is a real subagent actually registered in the live registry
  (build_general_registry() + the ``system`` subagent it adds), so a typo'd
  or since-removed agent name can never sit in the dict unnoticed.
- every value is non-empty (a real extracted prompt, not a placeholder).
- every value actually looks like one of the PDF's prompts ("You are the
  Dourmouse/DOURMOUSE [<name>] Agent, ...") and names the same agent as its
  own dict key, catching a block copied under the wrong key.
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
