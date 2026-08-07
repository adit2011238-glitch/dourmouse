"""Institutional governance layer — deterministic, Rule 2.8, no LLM anywhere.

The enterprise spec's COMPULSORY foundation, implemented as pure Python:

- Cost-capping budgets: ``BudgetTracker`` counts LLM calls, estimates tokens
  and USD cost, and caps calls / cost / wall-time across the WHOLE
  delegation tree (shared by reference, like the delegate budget). Runaway
  execution loops are stopped deterministically, never by an LLM judgment.
- DLP filters: ``DlpFilter`` redacts credential-shaped strings (API keys,
  PEM blocks, JWTs, AWS keys, secret assignments...) at the API boundary —
  tool results and model text are redacted BEFORE they are appended to the
  message list, so secrets never reach the model or the audit transcript.
- RBAC: ``RbacPolicy`` maps a human role to an allowed tool set. The default
  role ``operator`` preserves existing behavior (all tools); ``readonly``
  allows only read-only research/read tools. Anything outside the role is
  REFUSED before execution.
- Contract enforcement: ``validate_tool_arguments`` checks a tool call's JSON
  against its declared schema (required fields + types) before any handler
  runs, and ``validate_against_schema`` enforces a declared ``output_schema``
  on a tool's result — malformed calls/results never execute/flow downstream.

All functions are deterministic and import-safe (stdlib only) so the engine
stays dependency-light.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------- #
# Cost-capping budget — deterministic runaway-loop protection
# --------------------------------------------------------------------------- #

@dataclass
class BudgetLimits:
    """Hard caps for one dispatch run (shared across the whole tree)."""

    max_calls: int = 40
    max_est_cost_usd: float = 1.00
    max_wall_seconds: float = 600.0
    input_rate_per_m: float = 0.15   # USD per 1M input tokens (NVIDIA NIM)
    output_rate_per_m: float = 0.60  # USD per 1M output tokens


class BudgetTracker:
    """Thread-safe counter + cap for LLM calls, estimated cost, and wall time.

    ``record_call`` is called after every LLM response with the request
    messages and the response text; ``check`` returns None while within
    budget or a plain-text reason string once a cap is exceeded. Nested
    (delegated) runs SHARE the parent's tracker, so the cap is per top-level
    request, not per sub-run.
    """

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self.limits = limits or BudgetLimits()
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._calls = 0
        self._in_tokens = 0
        self._out_tokens = 0

    # -- recording --------------------------------------------------------- #

    def record_call(self, request_messages: list[dict[str, Any]], response_text: str) -> None:
        with self._lock:
            self._calls += 1
            self._in_tokens += _estimate_tokens(request_messages)
            self._out_tokens += max(0, len(response_text) // 4)

    def check(self) -> str | None:
        """None if within budget, else an honest plain-text reason."""
        with self._lock:
            calls = self._calls
            est_cost = self._est_cost_usd()
            elapsed = time.monotonic() - self._started
        if calls >= self.limits.max_calls:
            return (
                f"BUDGET EXHAUSTED: {calls} LLM calls reached the cap of "
                f"{self.limits.max_calls} per request tree."
            )
        if est_cost >= self.limits.max_est_cost_usd:
            return (
                f"BUDGET EXHAUSTED: estimated cost ${est_cost:.4f} reached "
                f"the cap of ${self.limits.max_est_cost_usd:.2f}."
            )
        if elapsed >= self.limits.max_wall_seconds:
            return (
                f"BUDGET EXHAUSTED: {elapsed:.0f}s elapsed reached the cap of "
                f"{self.limits.max_wall_seconds}s per request tree."
            )
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": self._calls,
                "est_input_tokens": self._in_tokens,
                "est_output_tokens": self._out_tokens,
                "est_cost_usd": round(self._est_cost_usd(), 6),
                "elapsed_seconds": round(time.monotonic() - self._started, 2),
                "limits": {
                    "max_calls": self.limits.max_calls,
                    "max_est_cost_usd": self.limits.max_est_cost_usd,
                    "max_wall_seconds": self.limits.max_wall_seconds,
                    "input_rate_per_m": self.limits.input_rate_per_m,
                    "output_rate_per_m": self.limits.output_rate_per_m,
                },
            }

    def _est_cost_usd(self) -> float:
        return (
            self._in_tokens / 1_000_000 * self.limits.input_rate_per_m
            + self._out_tokens / 1_000_000 * self.limits.output_rate_per_m
        )


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap deterministic token estimate (~4 chars/token). Good enough to
    cap runaway loops — the spec asks for a budget, not an LLM tokenizer."""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += len(content) // 4
        # tool_calls / tool results are also serialized into the request.
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += len(json.dumps(tool_calls, default=str)) // 4
    return max(1, total)


# --------------------------------------------------------------------------- #
# DLP filter — secret redaction at the API boundary
# --------------------------------------------------------------------------- #

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"nvapi-[A-Za-z0-9._-]{8,}"), "NVIDIA_API_KEY"),
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "OPENAI_API_KEY"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS_ACCESS_KEY_ID"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"), "PRIVATE_KEY_BLOCK"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "JWT"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "GITHUB_TOKEN"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|auth[_-]?token)"
            r"\b\s*[=:]\s*['\"]?[A-Za-z0-9._\-]{12,}"
        ),
        "SECRET_ASSIGNMENT",
    ),
)


class DlpFilter:
    """Redacts credential-shaped content before it leaves the boundary.

    Applied to tool results and model text in the dispatch loop BEFORE they
    are appended to the message list (which is what gets sent to the model)
    and BEFORE they are written to the transcript/ledger. A matched secret is
    replaced with ``[REDACTED:<LABEL>]``; the caller is told which labels
    fired so it can note the event honestly.
    """

    def redact(self, text: str) -> tuple[str, list[str]]:
        matched: list[str] = []
        result = text
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(result):
                matched.append(label)
                result = pattern.sub(f"[REDACTED:{label}]", result)
        return result, matched


# --------------------------------------------------------------------------- #
# RBAC — role -> allowed tool set (deterministic refusal)
# --------------------------------------------------------------------------- #

_READONLY_TOOLS = {
    "web_search",
    "fetch_url",
    "open_url",
    "propose_time_slots",
    "list_calendar_events",
    "read_file",
    "search_files",
    "diff_preview",
    "list_files",
    "search_vault",
    "read_note",
    "system_info",
    "list_path",
    "read_path",
}


class RbacPolicy:
    """Maps a human role to an allowed tool set.

    ``operator`` (default) allows every registered tool — identical to today's
    behavior. ``readonly`` allows only read-only research/read tools. A custom
    role can be supplied with an explicit allow-list. Enforcement happens in
    the dispatch loop: a tool outside the role returns a REFUSED string and
    NEVER executes.
    """

    def __init__(self, role: str = "operator", allow: set[str] | None = None) -> None:
        self.role = role
        if role == "operator":
            self._allowed: set[str] | None = None
        elif role == "readonly":
            self._allowed = set(_READONLY_TOOLS)
        elif allow is not None:
            self._allowed = set(allow)
        else:
            raise ValueError(
                f"unknown RBAC role {role!r} — use 'operator', 'readonly', "
                "or pass an explicit allow set."
            )

    def allows(self, tool_name: str) -> bool:
        return self._allowed is None or tool_name in self._allowed

    def refusal_text(self, tool_name: str) -> str:
        return (
            f"REFUSED by RBAC policy: tool '{tool_name}' is not permitted "
            f"for role '{self.role}'. Nothing was executed."
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "allowed": sorted(self._allowed) if self._allowed is not None else None,
        }


# --------------------------------------------------------------------------- #
# Contract enforcement — JSON-schema validation of tool calls and results
# --------------------------------------------------------------------------- #

def validate_tool_arguments(parameters: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Check a tool call's arguments against its declared schema.

    Returns None when valid, else a human-readable error. Extra arguments
    (model noise) are tolerated; missing required fields and wrong types are
    rejected BEFORE any handler runs.
    """
    props = parameters.get("properties", {})
    for required in parameters.get("required", []):
        if required not in arguments:
            return f"missing required argument {required!r}"
    for name, value in arguments.items():
        declared = props.get(name)
        if declared is None:
            continue  # not in the schema — tolerated, handler decides
        expected = declared.get("type")
        if expected is not None and not _matches_type(expected, value):
            return f"argument {name!r} must be {expected}, got {type(value).__name__}"
    return None


def validate_against_schema(value: Any, schema: dict[str, Any]) -> str | None:
    """Validate a structured value (e.g. a tool result) against an object
    schema: {"type": "object", "properties": {...}, "required": [...]}.
    Returns None when valid, else a human-readable error."""
    if schema.get("type") == "object":
        if not isinstance(value, dict):
            return f"expected object, got {type(value).__name__}"
        props = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                return f"missing required output field {required!r}"
        for name, item in value.items():
            declared = props.get(name)
            if declared and declared.get("type") is not None:
                if not _matches_type(declared["type"], item):
                    return f"output field {name!r} must be {declared['type']}, got {type(item).__name__}"
        return None
    if schema.get("type") is not None and not _matches_type(schema["type"], value):
        return f"expected {schema['type']}, got {type(value).__name__}"
    return None


def _matches_type(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True  # unknown declared types are not enforced (lenient)
