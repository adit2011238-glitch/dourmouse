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

import base64
import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
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

    def reset_run(self) -> None:
        """Restart the FULL budget window for a new top-level request tree.

        Calls, estimated cost and the wall clock all reset together. The
        caps are per-request-tree (see :class:`BudgetLimits` — "one dispatch
        run"), so every new directive gets a fresh 40-call/$1/600s envelope;
        a pathological single run is still bounded, but a long-lived session
        can never be bricked by cumulative usage. The web UI keeps ONE
        ChatSession (and thus one tracker) for the whole life of the server,
        so without this the call/cost caps permanently reject every new
        request once the session crosses them — observed live: the app
        stopped answering anything after ~14 directives in a row.
        """
        with self._lock:
            self._started = time.monotonic()
            self._calls = 0
            self._in_tokens = 0
            self._out_tokens = 0

    def reset_wall_clock(self) -> None:
        """Restart ONLY the wall-time window (back-compat; use ``reset_run``
        for a full per-request-tree reset).
        """
        with self._lock:
            self._started = time.monotonic()

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
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"), "ANTHROPIC_API_KEY"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "OPENAI_API_KEY"),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "GOOGLE_API_KEY"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "SLACK_TOKEN"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "GITHUB_TOKEN"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS_ACCESS_KEY_ID"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"), "PRIVATE_KEY_BLOCK"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "JWT"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "GITHUB_TOKEN"),
    # Ollama Cloud key shape: 32 hex characters, a dot, then a token.
    (re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9_-]{16,}"), "OLLAMA_API_KEY"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), "BEARER_TOKEN"),
    # Phase H, R2B-08: Google OAuth access and refresh tokens, Stripe live keys.
    (re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"), "GOOGLE_OAUTH_TOKEN"),
    (re.compile(r"(?<![A-Za-z0-9/])1//0[A-Za-z0-9_-]{20,}"), "GOOGLE_REFRESH_TOKEN"),
    (re.compile(r"\b(?:sk|rk|pk)_live_[A-Za-z0-9]{16,}"), "STRIPE_LIVE_KEY"),
)

# Phase H, R2B-08: an AWS secret access key has no prefix of its own (40
# characters of base64), so on its own it is indistinguishable from other
# data. Next to an access key id (AKIA...) it is not: when a text holds an
# AKIA id, every standalone 40-character run with upper case, lower case and
# a digit is treated as its secret.
_AWS_KEY_ID = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_AWS_SECRET = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])")

# Phase H, R2B-08: a password manager or browser export row:
# ``bank.com|bob|Tr0ub4dor&3`` or ``https://bank.com,bob,Tr0ub4dor&3``.
_CREDENTIAL_ROWS = (
    re.compile(r"(?<![\w.-])(?P<head>https?://[^\s,|]{3,200},[^\s,|]{1,120},)(?P<pw>[^\s,|]{6,128})"),
    re.compile(r"(?<![\w.-])(?P<head>(?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,24}\|[^\s|]{1,120}\|)(?P<pw>[^\s|]{6,128})"),
)

# Characters that render as nothing. Inside a key they defeat every exact
# and shape match while the key still works once a page strips them.
_INVISIBLE = re.compile("[­​‌‍⁠﻿]")
_INVISIBLE_OR_SPACE = "[\\s­​‌‍⁠﻿]*"

# A secret assigned to a name: GEMINI_API_KEY=..., "db_password": "...",
# clientSecret: .... Found by locating the assignment first (a bounded name, an
# = or :, a value) and judging name and value in code. A single pattern with a
# variable-length name in front of the marker backtracks on any long run of
# letters and digits (a base64 blob in a tool result froze the process for
# minutes; finding #136), and matching the marker anywhere in a word redacted
# "monkey" and "keyboard_layout".
_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<name>[A-Za-z][A-Za-z0-9_.-]{0,79})(?P<q1>['\"]?)(?P<sep>[ \t]*[=:][ \t]*)"
    # Phase H, R2B-08: a quoted value runs to its closing quote, so symbols
    # and spaces inside it ("P@ssw0rd!2024", "correct horse battery staple")
    # no longer cut the match short. Bounded, so it cannot backtrack far.
    r"(?:(?P<q2>['\"])(?P<qvalue>[^'\"\r\n]{8,256})(?P=q2)"
    r"|(?P<value>[A-Za-z0-9._~+/=\-!@#$%^*]{8,}))"
)
_STRONG_NAME_WORDS = frozenset({"secret", "secrets", "password", "passwd", "credential", "credentials", "apikey"})
_WEAK_NAME_WORDS = frozenset({"key", "keys", "token", "tokens"})
_STRONG_NAME_RE = re.compile(r"api[_-]?key|access[_-]?token|auth[_-]?token")


def _name_words(name: str) -> list[str]:
    return [w for w in re.split(r"[_.\-]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()) if w]


def _is_secret_assignment(name: str, value: str) -> bool:
    words = _name_words(name)
    if _STRONG_NAME_RE.search("_".join(words)) or _STRONG_NAME_WORDS.intersection(words):
        return True  # a name that says "secret": any value of this length
    if _WEAK_NAME_WORDS.intersection(words):
        if any(c.isspace() for c in value):
            return False  # "key": "press the 2 keys" is prose, not a key
        # key and token also name harmless things (token_count, max_tokens,
        # key_binding), so those need a value that looks random: letters and
        # digits together, and long enough.
        return len(value) >= 12 and any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
    return False


def _redact_assignments(text: str) -> tuple[str, bool]:
    if "=" not in text and ":" not in text:
        return text, False
    hit = False

    def sub(m: re.Match[str]) -> str:
        nonlocal hit
        quoted = m.group("qvalue") is not None
        value = m.group("qvalue") if quoted else m.group("value")
        if not _is_secret_assignment(m.group("name"), value):
            return m.group(0)
        hit = True
        quote = m.group("q2") or ""
        return f"{m.group('name')}{m.group('q1')}{m.group('sep')}{quote}[REDACTED:SECRET_ASSIGNMENT]{quote}"

    return _ASSIGNMENT.sub(sub, text), hit

_ENV_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
_ENV_MIN_VALUE_LEN = 8


def _value_forms(value: str) -> set[str]:
    """The raw value plus its URL-encoded and base64 spellings."""
    raw = value.encode("utf-8")
    forms = {
        value,
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value),
    }
    for encoder in (base64.b64encode, base64.urlsafe_b64encode):
        b64 = encoder(raw).decode("ascii")
        forms.add(b64)
        forms.add(b64.rstrip("="))
    # Phase H, R2B-08: hex, as a byte dump or a URL path would carry it.
    forms.add(raw.hex())
    forms.add(raw.hex().upper())
    return {f for f in forms if len(f) >= _ENV_MIN_VALUE_LEN}


def _spaced_pattern(value: str) -> re.Pattern[str]:
    """The raw value with any run of white space or invisible characters
    allowed between its characters (R2B-08: a key split by spaces, line
    breaks or a zero-width character). Every element is a literal, so the
    match is linear in the text."""
    tokens = [t for t in re.split(r"\s+", value) if t]
    gap = _INVISIBLE_OR_SPACE[:-1] + "]+"  # white space inside the secret is one run, not one per space
    return re.compile(gap.join(_INVISIBLE_OR_SPACE.join(re.escape(c) for c in t) for t in tokens))


class _ExactValues:
    """Exact secret strings taken from the user's own .env.

    Loaded on first use and reloaded whenever the file changes (mtime or
    size) or ``refresh()`` is called, so a key the owner adds in Settings is
    covered without a restart. The values are never logged or returned.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamp: tuple[str, int, int] | None = None
        self._forms: tuple[str, ...] = ()
        self._spaced: tuple[re.Pattern[str], ...] = ()

    def _load(self, path: Any) -> tuple[tuple[str, ...], tuple[re.Pattern[str], ...]]:
        from dotenv import dotenv_values

        forms: set[str] = set()
        raws: set[str] = set()
        for name, value in dotenv_values(path).items():
            if value and len(value) >= _ENV_MIN_VALUE_LEN and any(m in name.upper() for m in _ENV_NAME_MARKERS):
                forms |= _value_forms(value)
                raws.add(value)
        # Longest first so a form containing another is replaced whole.
        ordered = tuple(sorted(forms, key=len, reverse=True))
        spaced = tuple(_spaced_pattern(v) for v in sorted(raws, key=len, reverse=True))
        return ordered, spaced

    def _refresh(self, force: bool) -> None:
        from .config import user_env_path

        path = user_env_path()
        try:
            st = path.stat()
            stamp = (str(path), st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = (str(path), -1, -1)
        if force or stamp != self._stamp:
            self._forms, self._spaced = self._load(path) if stamp[1] != -1 else ((), ())
            self._stamp = stamp

    def get(self, force: bool = False) -> tuple[str, ...]:
        with self._lock:
            self._refresh(force)
            return self._forms

    def spaced(self) -> tuple[re.Pattern[str], ...]:
        """One pattern per raw secret that tolerates white space and invisible
        characters between its characters."""
        with self._lock:
            self._refresh(False)
            return self._spaced


_EXACT_VALUES = _ExactValues()


def refresh_exact_values() -> None:
    """Re-read the user's .env now (normally it is re-read when it changes)."""
    _EXACT_VALUES.get(force=True)


class DlpFilter:
    """Redacts credential-shaped content before it leaves the boundary.

    Applied to tool results and model text in the dispatch loop BEFORE they
    are appended to the message list (which is what gets sent to the model)
    and BEFORE they are written to the transcript/ledger. A matched secret is
    replaced with ``[REDACTED:<LABEL>]``; the caller is told which labels
    fired so it can note the event honestly.

    Besides the shape patterns, every secret-named value in the user's own
    .env is redacted by exact match (raw, URL-encoded and base64 forms).
    """

    def redact(self, text: str) -> tuple[str, list[str]]:
        result, matched = self._redact(text)
        if _INVISIBLE.search(text):
            # Phase H, R2B-08: a zero-width character inside a key hides it
            # from every pattern. When removing the invisible characters
            # reveals a secret, the text without them is what is passed on
            # (otherwise the original, so an emoji joiner is never touched).
            stripped, stripped_hits = self._redact(_INVISIBLE.sub("", text))
            if len(stripped_hits) > len(matched):
                return stripped, stripped_hits
        return result, matched

    def _redact(self, text: str) -> tuple[str, list[str]]:
        matched: list[str] = []
        result = text
        for form in _EXACT_VALUES.get():
            if form in result:
                if "ENV_SECRET" not in matched:
                    matched.append("ENV_SECRET")
                result = result.replace(form, "[REDACTED:ENV_SECRET]")
        for spaced in _EXACT_VALUES.spaced():
            result, n = spaced.subn("[REDACTED:ENV_SECRET]", result)
            if n and "ENV_SECRET" not in matched:
                matched.append("ENV_SECRET")
        aws_context = bool(_AWS_KEY_ID.search(result))
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(result):
                matched.append(label)
                result = pattern.sub(f"[REDACTED:{label}]", result)
        if aws_context:
            def _aws(m: re.Match[str]) -> str:
                v = m.group(0)
                if any(c.isupper() for c in v) and any(c.islower() for c in v) and any(c.isdigit() for c in v):
                    return "[REDACTED:AWS_SECRET_ACCESS_KEY]"
                return v

            replaced = _AWS_SECRET.sub(_aws, result)
            if replaced != result:
                matched.append("AWS_SECRET_ACCESS_KEY")
                result = replaced
        result, rows = _redact_credential_rows(result)
        if rows:
            matched.append("CREDENTIAL_ROW")
        result, assigned = _redact_assignments(result)
        if assigned:
            matched.append("SECRET_ASSIGNMENT")
        return result, matched

    def secrets_in_arguments(self, arguments: Any) -> list[str]:
        """Phase H, R2B-08: the labels of any secret inside a tool call's
        arguments (every string, at any depth, also URL-decoded), or an empty
        list. Used to refuse a call that would send a secret off the machine
        (a URL, a search query, text typed into a page, a message)."""
        labels: list[str] = []
        for value in _strings_in(arguments):
            for candidate in {value, urllib.parse.unquote_plus(value)}:
                for label in self.redact(candidate)[1]:
                    if label not in labels:
                        labels.append(label)
        return labels


def _strings_in(value: Any, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.extend(_strings_in(item, depth + 1))
            if isinstance(key, str):
                out.append(key)
        return out
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in _strings_in(item, depth + 1)]
    return []


def _redact_credential_rows(text: str) -> tuple[str, bool]:
    """``site|user|password`` and ``https://site,user,password`` rows: the
    third field is replaced when it mixes at least two kinds of character."""
    if "|" not in text and "," not in text:
        return text, False
    hit = False

    def sub(m: re.Match[str]) -> str:
        nonlocal hit
        pw = m.group("pw")
        kinds = sum((any(c.islower() for c in pw), any(c.isupper() for c in pw),
                     any(c.isdigit() for c in pw), any(not c.isalnum() for c in pw)))
        if kinds < 2 or pw.startswith("[REDACTED"):
            return m.group(0)
        hit = True
        return m.group("head") + "[REDACTED:CREDENTIAL_ROW]"

    for pattern in _CREDENTIAL_ROWS:
        text = pattern.sub(sub, text)
    return text, hit


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
    rejected BEFORE any handler runs. Mutates ``arguments`` IN PLACE when it
    coerces a numeric string (see ``_coerce_numeric_string`` below) — the
    caller passes the same dict on to the handler, so the coercion is what
    the handler actually sees.
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
        if expected is None:
            continue
        if not _matches_type(expected, value):
            # v8.12: a JSON-string number ("10" for an integer field) is a
            # known tool-calling quirk, not malformed input — observed live,
            # web_search's max_results as a string, rejected 3x verbatim
            # before the model gave up and dropped the argument. Coerce and
            # re-check rather than reject outright; anything that still
            # fails after coercion (a real non-numeric string, "abc") stays
            # rejected exactly as before.
            coerced = _coerce_numeric_string(expected, value)
            if coerced is not _UNCOERCIBLE:
                arguments[name] = coerced
                continue
            return f"argument {name!r} must be {expected}, got {type(value).__name__}"
    return None


_UNCOERCIBLE = object()  # sentinel: distinguishes "coerced to None" from "couldn't coerce"


def _coerce_numeric_string(expected: str, value: Any) -> Any:
    """A numeric-looking JSON string for an "integer"/"number" field ->
    the real int/float, or ``_UNCOERCIBLE`` if it isn't one.

    Deliberately narrow: only ``str`` values, only the two numeric
    types, and integer coercion demands a plain sign+digits string ("10",
    "-3") — never "3.5" ('integer' must stay a real integer, not silently
    truncated float text).
    """
    if not isinstance(value, str) or expected not in ("integer", "number"):
        return _UNCOERCIBLE
    text = value.strip()
    if expected == "integer":
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        return _UNCOERCIBLE
    try:
        return float(text)
    except ValueError:
        return _UNCOERCIBLE


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
