"""Bounded lead authority (R7, finding #133): the model proposes, the
runtime decides and executes.

Every tool call already passes through one choke point
(``dispatch._execute_tool``: permission, hooks, required arguments, the
human approval gate). What was missing was a run-level policy above the
per-call checks, and a durable record of what the model proposed versus
what the runtime did. This module adds both:

- **A run policy.** Per dispatch run, two hard bounds the model cannot
  talk its way past: the same tool with the same arguments may run at
  most ``max_identical`` times (a loop breaker), and at most
  ``max_consequential`` approval-gated actions may be requested (so a
  runaway run cannot bury the owner in approval prompts). Both return a
  plain refusal the model can relay.
- **An action ledger.** Each call becomes ``action.proposed``, then one of
  ``action.denied`` (policy), ``action.declined`` (the human said no),
  ``action.executed`` or ``action.failed``, in the append-only event log
  (R6). Arguments are never logged raw (they can hold credentials): only
  their names and a hash, so two identical calls can be recognised without
  revealing either.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_sink: Callable[[str, str, str, str, dict[str, Any]], Any] | None = None


def set_action_sink(fn: Callable[[str, str, str, str, dict[str, Any]], Any] | None) -> None:
    """The server wires the event log in here (append_event's signature)."""
    global _sink
    _sink = fn


def _args_fingerprint(arguments: dict[str, Any]) -> tuple[list[str], str]:
    blob = json.dumps(arguments, sort_keys=True, default=str)
    return sorted(arguments), hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def record(kind: str, tool: str, arguments: dict[str, Any], actor: str = "", **extra: Any) -> None:
    if _sink is None:
        return
    keys, digest = _args_fingerprint(arguments)
    with contextlib.suppress(Exception):  # the ledger must never break a real call
        _sink(f"action.{kind}", "tool", tool, actor, {"argument_names": keys, "arguments_sha": digest, **extra})


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


@dataclass
class RunPolicy:
    max_identical: int = field(default_factory=lambda: _env_int("DOURMOUSE_MAX_IDENTICAL_CALLS", 3))
    max_consequential: int = field(default_factory=lambda: _env_int("DOURMOUSE_MAX_CONSEQUENTIAL_PER_RUN", 8))
    actor: str = ""
    consequential: int = 0
    calls: Counter[str] = field(default_factory=Counter)

    def decide(self, name: str, arguments: dict[str, Any], *, consequential: bool) -> str | None:
        """None to allow, or the refusal reason."""
        _, digest = _args_fingerprint(arguments)
        key = f"{name}:{digest}"
        if self.calls[key] >= self.max_identical:
            return (f"'{name}' was already called {self.calls[key]} times with exactly these arguments in this "
                    "run; the runtime will not repeat it again. Use the earlier result, or change the approach.")
        if consequential and self.consequential >= self.max_consequential:
            return (f"this run has already asked for {self.consequential} actions that need approval, the most "
                    "one run may ask for. Finish with what is done, or have the user start a new request.")
        self.calls[key] += 1
        if consequential:
            self.consequential += 1
        return None
