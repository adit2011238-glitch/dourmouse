"""General Dispatch Agent engine (RUN:GENERAL mode) — NVIDIA NIM backed.

Design ancestry: the user-supplied dourmouse-ai-assistant reference project
routes every query through a central command processor into capability
modules (system control, phone, files, AI) with JSON state files and a
dual-AI fallback. This module is Dourmouse's equivalent, adapted to the
v2.0 architecture contract: a REGISTRY of subagents, each exposing a set of
plain tool specs + handlers, driven by an NVIDIA-NIM-backed orchestrator
(the same OpenAI-compatible tool-calling loop pattern already proven in
orchestrator.py, Phase 1).

Open-ended by design (user requirement: the final ATLAS version arrives
later): any subagent — the General roster here, or the Trading roster
(Research, Monitoring, Risk/Guardrail, Execution) later — is just a
registered group of tools. Adding the trading roster later must not require
touching this engine or the existing General roster. A later session simply
calls ``registry.register_subagent(...)`` with the trading agents.

Permission model (v2.0 Section 2.9), enforced deterministically in the loop
(Rule 2.8 — never an LLM judgment call):
- REGULAR: tool executes immediately.
- REQUIRES_CONFIRMATION: the loop calls ``confirmation_gate`` with a plain
  text description of the exact action. The gate is the human (a prompt in
  the CLI, a Slack round-trip later). No gate => the tool is NOT executed;
  the model gets "CONFIRMATION REQUIRED ..." and must surface it.
- PROHIBITED: never executes, returns a refusal string.

No silent stubs (Rule 2.2): unbuilt backends return NOT CONFIGURED / REFUSED
honestly; unknown tools and malformed arguments produce error text, not
crashes and not fabricated success.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from openai import OpenAI

from dourmouse.config import NvidiaConfig, OllamaConfig, load_llm_config
from dourmouse.governance import (
    BudgetTracker,
    DlpFilter,
    RbacPolicy,
    validate_against_schema,
    validate_tool_arguments,
)
from dourmouse.planner import build_plan


# Transient API failures worth retrying (institutional self-correction):
# rate limits and 5xx/connection errors. Anything else (auth, malformed
# requests) must fail loudly — never masked by a retry loop.
def _is_transient_error(exc: Exception) -> bool:
    import openai as _openai

    for cls in (
        _openai.RateLimitError,
        _openai.APIConnectionError,
        _openai.APITimeoutError,
        _openai.InternalServerError,
    ):
        if isinstance(exc, cls):
            return True
    return False


# Hard cap on a single LLM response. qwen3 without a cap can ramble for
# hundreds of tokens at local speeds; 1400 tokens covers any answer and any
# tool-call JSON with room to spare.
_DEFAULT_MAX_TOKENS = 1400


def _call_with_retry(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    config: NvidiaConfig | None,
    call_log: list[dict[str, Any]] | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> Any:
    """LLM call with bounded retry + backoff, and optional model fallback.

    Deterministic resilience (spec: self-correction & exception handling):
    transient errors retry up to ``max_retries`` with exponential backoff;
    if still failing and a ``fallback_model`` is configured, ONE fallback
    attempt runs against the second model before giving up. Never retries
    non-transient errors. The real response object is returned.
    """
    retries = max(0, int(config.max_retries)) if config else 0
    backoff = float(config.retry_backoff) if config else 0.5
    fallback = (config.fallback_model or "").strip() if config else ""
    # v4.1: the Ollama path uses the native client, which disables thinking
    # (think=False — the compat endpoint ignores it), pins keep_alive, and
    # raises num_ctx past the 4096 truncation default. NVIDIA needs no extra
    # body. (v4.0 history: thinking-tuned models emit reasoning tokens before
    # content and hit max_tokens empty.)
    extra_body = None

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if call_log is not None:
                call_log.append({"model": model, "attempt": attempt + 1})
            if on_delta is not None:
                return _stream_completion(
                    client, model, messages, tools, extra_body, on_delta
                )
            return client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                extra_body=extra_body,
                max_tokens=_DEFAULT_MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001 - inspect then decide
            last_exc = exc
            if not _is_transient_error(exc):
                raise
            if attempt < retries:
                time.sleep(backoff * (2**attempt))
    if fallback and fallback != model:
        if call_log is not None:
            call_log.append({"model": fallback, "attempt": "fallback"})
        return client.chat.completions.create(
            model=fallback,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            extra_body=extra_body,
            max_tokens=_DEFAULT_MAX_TOKENS,
        )
    assert last_exc is not None
    raise last_exc


def _stream_completion(
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    extra_body: dict[str, Any] | None,
    on_delta: Callable[[str], None],
) -> _OllamaResponse:
    """Stream one completion, emitting text deltas to ``on_delta`` as they arrive.

    Tool calls are accumulated from the OpenAI-format ``delta.tool_calls``
    stream chunks (id + name + concatenated argument fragments). Returns an
    OpenAI-shaped response (``choices[0].message``) so the dispatch loop is
    unchanged. This is what gives the UI a Claude-like feel: the first tokens
    appear in ~1s instead of the whole answer landing at once.
    """
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        extra_body=extra_body,
        max_tokens=_DEFAULT_MAX_TOKENS,
        stream=True,
    )
    content_parts: list[str] = []
    tool_acc: dict[int, dict[str, str]] = {}
    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        text = getattr(delta, "content", None)
        if text:
            content_parts.append(text)
            on_delta(text)
        for tc in getattr(delta, "tool_calls", None) or []:
            idx = tc.index if getattr(tc, "index", None) is not None else 0
            acc = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
            if getattr(tc, "id", None):
                acc["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    acc["name"] = fn.name
                if getattr(fn, "arguments", None):
                    acc["args"] += fn.arguments
    if tool_acc:
        tool_calls = [
            _OllamaTc(a["id"], a["name"], a["args"])
            for _, a in sorted(tool_acc.items())
        ]
        return _OllamaResponse(_OllamaMessage("".join(content_parts), tool_calls))
    return _OllamaResponse(_OllamaMessage("".join(content_parts), None))


class Permission(str, Enum):
    REGULAR = "regular"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    PROHIBITED = "prohibited"


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool exposed by a subagent.

    ``handler`` receives the parsed JSON arguments dict and returns plain
    text to feed back to the model. ``confirm_prompt`` (required for
    confirmation-gated tools) renders a plain-text description of the exact
    action for a human to approve.

    ``output_schema`` (optional, institutional contract enforcement): when a
    tool's result is JSON, declare the expected object schema here and the
    engine validates the handler's output against it — violations are
    surfaced honestly in the result (never silently dropped) so agent
    handoffs can't break downstream integrations.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]
    permission: Permission = Permission.REGULAR
    confirm_prompt: Callable[[dict[str, Any]], str] | None = None
    output_schema: dict[str, Any] | None = None

    def openai_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class Subagent:
    """A named capability group (v2.0 Section 4 roster row)."""

    name: str
    domain: str
    description: str
    tools: tuple[ToolSpec, ...] = field(default_factory=tuple)

    def roster_line(self) -> str:
        tool_names = ", ".join(
            f"{t.name}[{t.permission.value}]" for t in self.tools
        )
        return f"- {self.name} ({self.domain}): {self.description}\n    tools: {tool_names}"


class DispatchRegistry:
    """Holds subagents and their tools; the single extension point.

    Open-ended: registering a new subagent (e.g. the future Trading roster)
    is the ONLY step needed to extend the dispatcher. Tool names must be
    unique across the whole registry so later additions can never silently
    shadow an existing tool.
    """

    def __init__(self) -> None:
        self._subagents: dict[str, Subagent] = {}
        self._tools: dict[str, ToolSpec] = {}

    def register_subagent(self, subagent: Subagent) -> None:
        if subagent.name in self._subagents:
            raise ValueError(f"subagent already registered: {subagent.name}")
        for tool in subagent.tools:
            if tool.name in self._tools:
                raise ValueError(
                    f"tool name collision across registry: {tool.name!r} "
                    f"(from {subagent.name})"
                )
        for tool in subagent.tools:
            self._tools[tool.name] = tool
        self._subagents[subagent.name] = subagent

    def lookup(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def get_subagent(self, name: str) -> Subagent | None:
        return self._subagents.get(name)

    def all_subagents(self) -> tuple[Subagent, ...]:
        """All registered subagents (stable insertion order)."""
        return tuple(self._subagents.values())

    @property
    def subagent_names(self) -> set[str]:
        return set(self._subagents)

    @property
    def tool_names(self) -> set[str]:
        return set(self._tools)

    @property
    def gated_tool_names(self) -> set[str]:
        """Names of tools that require human confirmation before executing."""
        return {
            name
            for name, spec in self._tools.items()
            if spec.permission is Permission.REQUIRES_CONFIRMATION
        }

    def tool_specs(self) -> list[dict[str, Any]]:
        return [t.openai_spec() for t in self._tools.values()]

    def describe_roster(self) -> str:
        lines = [s.roster_line() for s in self._subagents.values()]
        return "\n".join(lines) if lines else "(empty roster)"


def _scoped_tool_specs(
    registry: DispatchRegistry, agent_names: set[str]
) -> list[dict[str, Any]]:
    """Full tool schemas ONLY for the named agents (plus the orchestrator's
    delegate tool, so mid-task delegation stays possible).

    The roster description in the system message still names every agent and
    tool, so planning is unaffected — this only shrinks the schema payload.
    Sending all 60 schemas costs ~80s of cold prefill (measured live: 5,457
    tokens @ 67 t/s = 81s before the first token) and dwarfs the actual
    conversation; scoped, a plain question prefills in ~18s cold / ~1s warm.
    """
    names = set(agent_names)
    names.add("orchestrator")
    out: list[dict[str, Any]] = []
    for sub in registry.all_subagents():
        if sub.name in names:
            out.extend(t.openai_spec() for t in sub.tools)
    return out


_SYSTEM_PROMPT = (
    "You are the Dourmouse Lead Orchestrator, operating the general "
    "dispatch roster. You interpret requests and delegate to subagent tools "
    "registered below. Rules:\n"
    "1. Default to drafting, not sending. A finished draft is a deliverable; "
    "a sent/executed action requires human confirmation.\n"
    "2. If a tool result says 'CONFIRMATION REQUIRED', report the exact "
    "proposed action to the user and stop — never claim it was done.\n"
    "3. If a tool result says 'NOT CONFIGURED' or 'REFUSED', report that "
    "honestly — never invent output, results, or confirmations.\n"
    "4. Anything that could touch money or send external messages gets "
    "confirmation-gated handling even if the wording of the request is "
    "urgent.\n"
    "5. You never fabricate facts; use the research tools for real facts.\n"
    "6. run_command has a deterministic safety guard: if it returns "
    "'REFUSED by deterministic safety guard', do NOT rephrase the same "
    "command to sneak past it — either respect the refusal or use "
    "run_privileged_command, which surfaces the exact command for the "
    "human to approve in the UI. Never attempt workarounds for a refused "
    "action (Rule 2.9/2.10).\n"
    "7. For large or clearly separable work you may spawn a NESTED agent run "
    "with delegate_task (the orchestrator's own tool) — a fresh sub-dispatch "
    "against the same roster, depth-bounded and audit-logged as a job. Only "
    "delegate when the sub-task is genuinely self-contained; otherwise use "
    "the roster tools directly.\n"
    "8. RESPONSE STYLE (always): answer the question FIRST — one or two clear "
    "sentences for the headline, then detail. Use short paragraphs, headers "
    "and bullet lists for anything multi-part. Summarize tool results in "
    "plain language with the key facts; never dump raw tool output or JSON. "
    "No preamble, no meta-commentary, no emojis unless asked. Be warm, "
    "direct, and concise."
)


# Native Ollama adapter (v4.1). The OpenAI-compat endpoint on this Ollama
# build IGNORES think/enable_thinking (measured live: 57-73s thinking traces
# per answer, content empty at any token cap), while the native /api/chat
# honors think=False (measured: 2+2 in 3.2s/9 tokens vs 39.6s/188). So the
# local path talks to the native API directly: real streaming, a warm model,
# and a proper context window instead of the 4096-token truncation default.
_OLLAMA_NUM_CTX = 8192
_OLLAMA_KEEP_ALIVE = "30m"


class _OllamaTcFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _OllamaTc:
    def __init__(self, tc_id: str, name: str, arguments: str) -> None:
        self.id = tc_id
        self.function = _OllamaTcFunction(name, arguments)


class _OllamaMessage:
    def __init__(self, content: str, tool_calls: list[_OllamaTc] | None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _OllamaResponse:
    def __init__(self, message: _OllamaMessage) -> None:
        self.choices = [type("_Choice", (), {"message": message})()]


class _OllamaDelta:
    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _OllamaChunk:
    def __init__(self, delta: _OllamaDelta) -> None:
        self.choices = [type("_Choice", (), {"delta": delta})()]


class _OllamaCompletions:
    """OpenAI-shaped ``chat.completions`` surface over the native API."""

    def __init__(self, client: "OllamaNativeClient") -> None:
        self._client = client

    def create(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        extra_body: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> Any:
        return self._client._create(
            model=model, messages=messages, tools=tools or [],
            max_tokens=max_tokens, stream=stream,
        )


class OllamaNativeClient:
    """Keyless local client that calls Ollama's native /api/chat.

    Exposes the same call surface the dispatch loop already uses
    (``chat.completions.create``) and returns OpenAI-shaped messages and
    stream chunks, so nothing else in the engine changes. ``_post`` is
    injectable for hermetic tests.
    """

    def __init__(
        self,
        config: OllamaConfig,
        model: str | None = None,
        _post: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        base = (config.base_url or "http://127.0.0.1:11434/v1").strip()
        self._root = base[:-3] if base.endswith("/v1") else base
        self._model = model or config.model
        self._post = _post or self._default_post
        self.chat = type("_Chat", (), {"completions": _OllamaCompletions(self)})()

    def _default_post(self, payload: dict[str, Any]) -> str:
        import urllib.request as _urllib

        req = _urllib.Request(
            self._root + "/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with _urllib.urlopen(req, timeout=300) as resp:
            return resp.read().decode()

    def _default_post_lines(self, payload: dict[str, Any]):
        """Incremental NDJSON reader — this is what makes streaming real.

        A buffered ``read()`` would only "stream" after the whole answer
        finished; reading the response line by line forwards each token the
        moment Ollama emits it.
        """
        import urllib.request as _urllib

        req = _urllib.Request(
            self._root + "/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with _urllib.urlopen(req, timeout=300) as resp:
            for raw_line in resp:
                yield raw_line.decode()

    def _translate_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rewrite OpenAI-format history into Ollama's native shape.

        The dispatch loop stores assistant tool_calls in OpenAI format
        (stringified arguments + id/type); Ollama's native decoder REJECTS
        that (measured: "Value looks like object, but can't find closing '}'
        symbol" — it wants arguments as a parsed object, no id/type). Tool
        result messages keep just role+content.
        """
        out: list[dict[str, Any]] = []
        for msg in messages:
            m = dict(msg)
            tcs = m.get("tool_calls")
            if tcs:
                native_tcs = []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    native_tcs.append(
                        {"function": {"name": fn.get("name", ""), "arguments": args}}
                    )
                m["tool_calls"] = native_tcs
            if m.get("role") == "tool":
                m.pop("tool_call_id", None)
            out.append(m)
        return out

    def _create(
        self,
        *,
        model: str | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int | None,
        stream: bool,
    ) -> Any:
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": self._translate_messages(messages),
            "stream": bool(stream),
            "think": False,
            "enable_thinking": False,
            "keep_alive": _OLLAMA_KEEP_ALIVE,
            "options": {
                "num_predict": int(max_tokens or _DEFAULT_MAX_TOKENS),
                "num_ctx": _OLLAMA_NUM_CTX,
            },
        }
        if tools:
            payload["tools"] = tools
        if stream:
            return self._stream(payload)
        return self._complete(payload)

    def _complete(self, payload: dict[str, Any]) -> _OllamaResponse:
        data = json.loads(self._post(payload))
        msg = data.get("message") or {}
        tool_calls = self._native_tool_calls(msg.get("tool_calls") or [])
        return _OllamaResponse(_OllamaMessage(msg.get("content") or "", tool_calls))

    def _stream(self, payload: dict[str, Any]):
        if self._post is self._default_post:
            lines = self._default_post_lines(payload)
        else:
            lines = iter((self._post(payload) or "").splitlines())
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = chunk.get("message") or {}
            content = msg.get("content") or None
            delta_tcs = None
            tcs = msg.get("tool_calls") or []
            if tcs:
                delta_tcs = []
                for i, tc in enumerate(tcs):
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    delta_tcs.append(
                        type("_T", (), {
                            "index": i,
                            "id": tc.get("id"),
                            "function": type("_F", (), {
                                "name": fn.get("name"),
                                "arguments": None if args is None else json.dumps(args),
                            })(),
                        })()
                    )
            if content is None and not delta_tcs:
                continue
            yield _OllamaChunk(_OllamaDelta(content=content, tool_calls=delta_tcs))

    @staticmethod
    def _native_tool_calls(tcs: list[dict[str, Any]]) -> list[_OllamaTc] | None:
        if not tcs:
            return None
        out: list[_OllamaTc] = []
        for i, tc in enumerate(tcs):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            out.append(
                _OllamaTc(
                    tc.get("id") or f"call_{i}",
                    fn.get("name") or "",
                    "" if args is None else (args if isinstance(args, str) else json.dumps(args)),
                )
            )
        return out


def _build_client(config: NvidiaConfig) -> Any:
    # Ollama: talk to the native API (fast, streaming, think disabled).
    # NVIDIA: the OpenAI SDK, with a non-empty sentinel key (Ollama ignores
    # key values, but the SDK rejects empty strings — reviewer-caught).
    if isinstance(config, OllamaConfig):
        return OllamaNativeClient(config)
    key = config.api_key or "local-keyless"
    return OpenAI(api_key=key, base_url=config.base_url)


def _emit_event(
    event_sink: Callable[[dict[str, Any]], None] | None, entry: dict[str, Any]
) -> None:
    """Call the optional event_sink without letting it break execution.

    The sink is a pure observer (Rule: UI streaming must never alter or abort
    dispatch), so a raising sink is swallowed.
    """
    if event_sink is None:
        return
    try:
        event_sink(entry)
    except Exception:
        pass


def _execute_tool(
    spec: ToolSpec,
    arguments: dict[str, Any],
    confirmation_gate: Callable[[str], bool] | None,
    ledger: list[dict[str, Any]] | None = None,
) -> str:
    """Permission-enforced tool execution (deterministic, Rule 2.8).

    ``ledger`` (optional) receives immutable-audit events for every human
    intervention: a ``confirmation_requested`` entry when a gated tool waits
    on the human, and a ``confirmation_resolved`` entry with the approval
    decision. This is how the audit trail logs WHO decided WHAT, even though
    the gate itself is a black box to the engine.
    """
    if spec.permission is Permission.PROHIBITED:
        return (
            f"REFUSED: tool '{spec.name}' is prohibited by policy and will "
            "never execute."
        )
    if spec.permission is Permission.REQUIRES_CONFIRMATION:
        prompt_text = (
            spec.confirm_prompt(arguments)
            if spec.confirm_prompt
            else f"Execute {spec.name} with {json.dumps(arguments)}?"
        )
        if ledger is not None:
            ledger.append(
                {
                    "type": "confirmation_requested",
                    "tool": spec.name,
                    "prompt": prompt_text,
                }
            )
        if confirmation_gate is None:
            return (
                f"CONFIRMATION REQUIRED: {prompt_text} "
                "(no confirmation channel attached; NOT executed)"
            )
        approved = bool(confirmation_gate(prompt_text))
        if ledger is not None:
            ledger.append(
                {
                    "type": "confirmation_resolved",
                    "tool": spec.name,
                    "approved": approved,
                }
            )
        if not approved:
            return f"DECLINED BY USER: {prompt_text}"
    result = spec.handler(arguments)
    # Institutional contract enforcement (spec: structured output): when the
    # tool declares an output_schema, validate the REAL result and surface a
    # violation honestly — never silently pass a malformed handoff.
    if spec.output_schema is not None:
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            result += (
                "\n[OUTPUT CONTRACT: tool declares output_schema but returned "
                "non-JSON — downstream may break]"
            )
        else:
            violation = validate_against_schema(parsed, spec.output_schema)
            if violation is not None:
                result += f"\n[OUTPUT CONTRACT VIOLATION: {violation}]"
    return result


class JobTracker:
    """Bounded, thread-safe audit log of delegated (nested) agent runs.

    Every delegate_task spawns a job: id, parent, task, target subagent,
    depth, status, timestamps, and the REAL result/error text. This is the
    institutional audit tree — the UI renders it as the DELEGATED TASKS
    panel and it is how a multi-agent run can be traced afterwards.
    Statuses: running -> done | error | refused.
    """

    _MAX_JOBS = 500

    def __init__(self) -> None:
        self._jobs: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next = 0

    def spawn(self, *, task: str, subagent: str | None, depth: int,
              parent_id: str | None = None) -> str:
        with self._lock:
            self._next += 1
            job_id = f"job-{self._next}"
            self._jobs.append(
                {
                    "id": job_id,
                    "parent_id": parent_id,
                    "task": task[:400],
                    "subagent": subagent,
                    "depth": depth,
                    "status": "running",
                    "created_at": _now_iso(),
                    "finished_at": None,
                    "result": "",
                    "error": "",
                }
            )
            if len(self._jobs) > self._MAX_JOBS:
                del self._jobs[: len(self._jobs) - self._MAX_JOBS]
            return job_id

    def finish(self, job_id: str, result: str = "", error: str = "") -> None:
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    job["status"] = "error" if error else "done"
                    job["finished_at"] = _now_iso()
                    job["result"] = (result or "")[:800]
                    job["error"] = (error or "")[:800]
                    return

    def refuse(self, job_id: str, reason: str) -> None:
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    job["status"] = "refused"
                    job["finished_at"] = _now_iso()
                    job["error"] = reason[:800]
                    return

    def snapshot(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first view (stable; caller may not mutate the dicts)."""
        with self._lock:
            return list(reversed(self._jobs[-limit:]))

    def count(self) -> int:
        with self._lock:
            return len(self._jobs)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


@dataclass
class DispatchContext:
    """Context for a dispatch run, used by the orchestrator's delegate_task.

    Carries everything a NESTED run needs to be spawned with the same client,
    config, confirmation gate, and event sink as the parent, plus the
    deterministic recursion guards (Rule 2.8): depth is bounded by
    ``max_depth`` and the total number of delegates across the whole tree is
    bounded by ``max_delegates`` via the shared mutable ``budget`` list.
    """

    registry: DispatchRegistry
    client: Any
    config: NvidiaConfig | None
    confirmation_gate: Callable[[str], bool] | None
    event_sink: Callable[[dict[str, Any]], None] | None
    jobs: JobTracker | None = None
    depth: int = 0
    max_depth: int = 3
    budget: list[int] = field(default_factory=lambda: [0])  # shared across tree
    max_delegates: int = 25
    current_job_id: str | None = None
    model: str = "test-model"
    # Institutional governance, threaded through the whole tree so nested runs
    # inherit the SAME budget tracker, DLP filter, and RBAC role (spec:
    # cost-capping, data-loss prevention, role-based access control).
    cost_budget: BudgetTracker | None = None
    dlp: DlpFilter | None = None
    rbac: RbacPolicy | None = None
    # Shared truth: the parent run's recent context, passed into nested runs
    # so delegated agents see what the parent already learned/decided.
    parent_context: str = ""

    def delegates_used(self) -> int:
        return self.budget[0]

    def consume_delegate(self) -> bool:
        """Atomically claim one delegation budget slot."""
        if self.budget[0] >= self.max_delegates:
            return False
        self.budget[0] += 1
        return True


def current_dispatch_context(registry: DispatchRegistry) -> DispatchContext | None:
    """The active dispatch context for a registry, or None outside a run.

    run_dispatch_messages pushes one context per (possibly nested) run; the
    delegate_task handler reads the top of the stack to spawn its nested run
    with the parent's client/gate/sink. Synchronous nesting means the stack
    is exactly one per in-flight run.
    """
    stack = getattr(registry, "_ctx_stack", None)
    if not stack:
        return None
    return stack[-1]


def system_message(registry: DispatchRegistry) -> str:
    """The immutable system prompt for a registry (persona + roster).

    Shared by run_dispatch and chat.ChatSession so a conversation always
    carries the same instructions and tool list.
    """
    return _SYSTEM_PROMPT + "\n\nROSTER:\n" + registry.describe_roster()


def run_dispatch_messages(
    messages: list[dict[str, Any]],
    registry: DispatchRegistry,
    max_turns: int = 8,
    client: Any | None = None,
    config: NvidiaConfig | None = None,
    confirmation_gate: Callable[[str], bool] | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    job_tracker: JobTracker | None = None,
    depth: int = 0,
    max_depth: int = 3,
    budget: list[int] | None = None,
    max_delegates: int = 25,
    current_job_id: str | None = None,
    cost_budget: BudgetTracker | None = None,
    dlp: DlpFilter | None = None,
    rbac: RbacPolicy | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Run the tool loop over an existing message list (conversation-aware).

    The caller owns ``messages`` (system + history); this appends the
    assistant's final text / tool exchanges to it in place so a multi-turn
    conversation (chat.ChatSession) keeps full context. Returns
    {"final_text", "transcript", "messages"}.

    ``event_sink`` (optional) receives each transcript event as it is
    produced ("assistant_text", "tool_use", "tool_result", "result") so a
    UI can stream progress over SSE. It is a pure observer: never affects
    execution.

    Recursive dispatch (the orchestrator's delegate_task tool): ``depth`` is
    the current nesting level, ``max_depth`` caps it, ``budget`` is the
    SHARED mutable delegate budget across the whole tree, and
    ``max_delegates`` caps total nested spawns. These are the deterministic
    recursion guards (Rule 2.8) — an agent can never spawn unboundedly.

    Institutional governance (deterministic, never an LLM judgment):
    ``cost_budget`` (defaults to a fresh BudgetTracker per run) caps LLM
    calls / estimated cost / wall time and stops the run with an honest
    BUDGET EXHAUSTED event; ``dlp`` redacts credential-shaped text from tool
    results and model output before it reaches the API boundary or the
    transcript; ``rbac`` refuses tools outside the role's allow-set before
    they execute. All three are threaded through delegated nested runs.
    """
    # v3.1 per-agent models: an explicit ``model`` override (e.g. a nested
    # delegate resolved to its target subagent's model) wins over the
    # config default. Deterministic (Rule 2.8) — the caller picks the model;
    # the engine never guesses.
    if client is None:
        config = config or load_llm_config()
        client = _build_client(config)
        model = model or config.model
    else:
        model = model or (config.model if config is not None else "test-model")

    # Compulsory governance defaults: cost-capping and DLP are ON by default
    # (institutional baseline); RBAC is off unless a role is supplied, so the
    # engine's existing behavior is unchanged for callers that opt out.
    cost_budget = cost_budget if cost_budget is not None else BudgetTracker()
    dlp = dlp if dlp is not None else DlpFilter()

    # Push a dispatch context so the delegate_task tool can spawn nested runs
    # with the same client/gate/sink and the same recursion guards. The stack
    # is exactly one per in-flight run because nesting is synchronous.
    ctx = DispatchContext(
        registry=registry,
        client=client,
        config=config,
        confirmation_gate=confirmation_gate,
        event_sink=event_sink,
        jobs=job_tracker,
        depth=depth,
        max_depth=max_depth,
        budget=budget if budget is not None else [0],
        max_delegates=max_delegates,
        current_job_id=current_job_id,
        model=model,
        cost_budget=cost_budget,
        dlp=dlp,
        rbac=rbac,
        parent_context=_build_parent_context(messages),
    )
    # INVARIANT: at most ONE in-flight run per registry at any instant. The
    # webui guarantees this by serializing chat under session_lock, and
    # nesting is synchronous (a delegate runs to completion inside the
    # parent's loop), so stack[-1] is always the in-flight run's context.
    stack: list[DispatchContext] = getattr(registry, "_ctx_stack", None)
    if stack is None:
        stack = []
        registry._ctx_stack = stack
    stack.append(ctx)
    try:
        return _run_dispatch_loop(messages, registry, max_turns, ctx)
    finally:
        stack.pop()
        if not stack:
            # Leave no stale stack on the registry for a later fresh run.
            delattr(registry, "_ctx_stack")


def _build_parent_context(
    messages: list[dict[str, Any]], limit: int = 6, max_len: int = 600
) -> str:
    """Compact recent user/assistant text for a nested run's shared truth.

    Deterministic and bounded: the last ``limit`` user/assistant turns, each
    truncated to ``max_len`` chars, system/tool noise excluded. Delegated
    agents see what the parent conversation already learned/decided instead
    of starting from a blank slate (spec: consistent truth across multi-turn
    workflows).
    """
    parts: list[str] = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"{m['role']}: {content[:max_len]}")
    return "\n".join(parts[-limit:])


# A short text-only model message immediately after a tool result, while the
# plan still has unexecuted steps, is usually a transitional note ("let me try
# a more targeted search") rather than a final answer. The loop nudges the
# model to keep going a bounded number of times before ending honestly.
_MAX_TEXT_ONLY_NUDGES = 2
_MAX_TEXT_ONLY_NUDGE_CHARS = 240
# If the model spends a plan's worth of tool calls without touching every
# step (e.g. it fixates on re-searching), inject ONE deterministic checkpoint
# reminder listing the unexecuted steps, so multi-step chains cannot silently
# end half-finished.
_MAX_PLAN_REMINDERS = 1


def _run_dispatch_loop(
    messages: list[dict[str, Any]],
    registry: DispatchRegistry,
    max_turns: int,
    ctx: DispatchContext,
) -> dict[str, Any]:
    """The actual tool-calling loop (called with a pushed context)."""
    transcript: list[dict[str, Any]] = []
    client = ctx.client
    model = ctx.model
    event_sink = ctx.event_sink
    confirmation_gate = ctx.confirmation_gate
    cost_budget = ctx.cost_budget
    dlp = ctx.dlp
    rbac = ctx.rbac
    nudges = 0
    plan_reminders = 0
    # Deterministic tool->owner map: plans name SUBAGENTS but the model calls
    # TOOLS, so the loop ties them together to tell when a plan step has
    # actually been executed.
    tool_owner: dict[str, str] = {}
    for _sub in registry.all_subagents():
        for _t in _sub.tools:
            tool_owner[_t.name] = _sub.name

    def _budget_entry(reason: str) -> dict[str, Any]:
        return {"type": "budget_exhausted", "reason": reason}

    # v2.0 Phase 2.1: for a multi-step prompt, emit a visible PLAN event
    # before executing (deterministic heuristic + subagent mapping, no extra
    # LLM call). It rides the transcript, so the UI renders it as a [PLAN]
    # block and chat.py persists it to the session JSONL — audit trails for
    # arbitrary sessions, not just dev sessions.
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    plan = build_plan(str(last_user), registry)
    if plan:
        plan_entry: dict[str, Any] = {"type": "plan", "steps": plan, "total": len(plan)}
        transcript.append(plan_entry)
        _emit_event(event_sink, plan_entry)
    # v4.1: scope the tool schemas to the plan's agents instead of shipping
    # all 60. Plain questions send no tools at all — the biggest single
    # latency lever (see _scoped_tool_specs).
    plan_agents = {step["subagent"] for step in (plan or [])}
    scoped_tools = _scoped_tool_specs(registry, plan_agents) if plan_agents else []

    for _ in range(max_turns):
        # Deterministic cost cap BEFORE each LLM call (spec: prevent runaway
        # execution loop costs). A tripped budget ends the run honestly.
        if cost_budget is not None:
            reason = cost_budget.check()
            if reason is not None:
                entry = _budget_entry(reason)
                transcript.append(entry)
                _emit_event(event_sink, entry)
                messages.append({"role": "assistant", "content": ""})
                return {"final_text": "", "transcript": transcript, "messages": messages}

        # v4.2 plan checkpoint: if the model has used a plan's worth of tool
        # calls but some plan step's agent has never run, it is fixating (e.g.
        # re-searching instead of moving to the write step). Inject ONE
        # deterministic reminder listing the unexecuted steps so the run does
        # not end half-finished. Bounded: at most _MAX_PLAN_REMINDERS per run.
        def _missing_plan_steps() -> list[dict[str, Any]]:
            used_tools = {e["name"] for e in transcript if e.get("type") == "tool_use"}
            touched_steps = {
                s["n"]
                for s in plan
                if any(tool_owner.get(u) == s["subagent"] for u in used_tools)
            }
            return [s for s in plan if s["n"] not in touched_steps]

        def _inject_plan_reminder(missing: list[dict[str, Any]]) -> None:
            nonlocal plan_reminders
            reminder = (
                "[PLAN CHECKPOINT] The following plan step(s) have not been "
                "executed yet: "
                + "; ".join(
                    f"STEP {s['n']}/{len(plan)} ({s['subagent']}): {s['task']}"
                    for s in missing
                )
                + ". Execute them now with the appropriate tools. If a step "
                "is genuinely impossible, say so explicitly and finish."
            )
            messages.append({"role": "system", "content": reminder})
            entry = {"type": "plan_reminder", "steps": [s["n"] for s in missing]}
            transcript.append(entry)
            _emit_event(event_sink, entry)
            plan_reminders += 1

        if plan and plan_reminders < _MAX_PLAN_REMINDERS:
            tool_use_count = sum(1 for e in transcript if e.get("type") == "tool_use")
            missing = _missing_plan_steps()
            # Fixation case: the model spends a plan's worth of tool calls
            # without touching every step (e.g. re-searching instead of
            # writing). Fire the reminder BEFORE the next LLM call.
            if missing and tool_use_count >= len(plan):
                _inject_plan_reminder(missing)

        # v4.1: stream text tokens to the UI as they arrive (first token in
        # ~1s instead of the whole answer landing at once). Only for the real
        # OpenAI client at the top of the tree: engine-test fakes keep the
        # non-streaming path, and nested delegate runs render via their own
        # assistant_text events rather than hijacking the parent's stream.
        on_delta = None
        if ctx.depth == 0 and ctx.event_sink is not None and isinstance(
            client, (OpenAI, OllamaNativeClient)
        ):
            def on_delta(text: str) -> None:
                _emit_event(ctx.event_sink, {"type": "assistant_delta", "text": text})

        response = _call_with_retry(
            client,
            model=model,
            messages=messages,
            tools=scoped_tools,
            config=ctx.config,
            on_delta=on_delta,
        )
        message = response.choices[0].message

        # Record the call against the budget AFTER it succeeded, using real
        # request + response sizes (token estimate ~4 chars/token).
        if cost_budget is not None:
            resp_text = message.content or ""
            if getattr(message, "tool_calls", None):
                resp_text += json.dumps(
                    [tc.function.arguments for tc in message.tool_calls], default=str
                )
            cost_budget.record_call(messages, resp_text)

        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            text = message.content or ""
            if dlp is not None:
                text, hits = dlp.redact(text)
                if hits:
                    text += f"\n[DLP: {len(hits)} secret pattern(s) redacted from model text]"
            entry = {"type": "assistant_text", "text": text}
            transcript.append(entry)
            _emit_event(event_sink, entry)
            messages.append({"role": "assistant", "content": text})
            # Orchestration robustness: a text-only message right after a
            # tool result while the plan still has unexecuted steps is often
            # the model thinking aloud ("let me try a more targeted search")
            # instead of actually calling the next tool. Treat it as context
            # and keep the loop going a bounded number of times, so multi-
            # step chains don't silently die mid-plan. Ends honestly after
            # the nudge budget, exactly as before.
            tools_used = sum(1 for e in transcript if e.get("type") == "tool_use")
            # "Steps pending" means plan steps whose agent has NOT run, not
            # merely fewer raw tool calls than plan steps: the model may burn
            # its whole budget re-running ONE step's tools (live: three
            # atlas_* calls for step 1 while steps 2-3 never ran) and still
            # emit a transitional note.
            steps_pending = plan is not None and (
                tools_used < len(plan) or bool(_missing_plan_steps())
            )
            prev_was_tool_result = any(
                e.get("type") == "tool_result" for e in transcript[-3:]
            )
            if (
                steps_pending
                and prev_was_tool_result
                and len(text) <= _MAX_TEXT_ONLY_NUDGE_CHARS
                and nudges < _MAX_TEXT_ONLY_NUDGES
            ):
                nudges += 1
                continue
            # Fabrication case (exit path): a text-only message ends the run
            # even when the plan still has unexecuted steps. The most common
            # failure is the model CLAIMING a step is done without ever
            # calling its tool (live: "saved to .../outlook_brief.txt" with
            # zero write_file calls). Fire the same bounded checkpoint here
            # so a long "final" answer cannot silently skip plan steps.
            if plan and plan_reminders < _MAX_PLAN_REMINDERS:
                missing = _missing_plan_steps()
                if missing:
                    _inject_plan_reminder(missing)
                    continue
            # Reminder budget spent and steps STILL unexecuted: the model has
            # ignored the checkpoint. Never let a claimed completion of those
            # steps pass silently — append an honest caveat to the final text
            # AND the already-emitted transcript entry + persisted message
            # (Rule 2.2: no fabricated success).
            if plan:
                missing = _missing_plan_steps()
                if missing:
                    # Soft wording: "not executed via tools" — a knowledge
                    # step answered without a tool (e.g. "tell me the file
                    # path") is still flagged, but reads as an honest note
                    # rather than a failure verdict.
                    text += "\n\n[DOURMOUSE: plan step(s) not executed via tools — " + "; ".join(
                        f"STEP {s['n']}/{len(plan)} ({s['subagent']}): {s['task']}"
                        for s in missing
                    ) + "]"
                    if transcript and transcript[-1].get("type") == "assistant_text":
                        transcript[-1]["text"] = text
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["content"] = text
            return {"final_text": text, "transcript": transcript, "messages": messages}

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        }
        messages.append(assistant_msg)

        for tool_call in tool_calls:
            name = tool_call.function.name
            use_entry = {
                "type": "tool_use",
                "name": name,
                "raw_arguments": tool_call.function.arguments,
            }
            transcript.append(use_entry)
            _emit_event(event_sink, use_entry)
            spec = registry.lookup(name)
            if spec is None:
                result_text = (
                    f"ERROR: unknown tool '{name}' — not in the registered roster."
                )
            elif rbac is not None and not rbac.allows(name):
                # Deterministic RBAC refusal BEFORE anything executes (spec:
                # role-based access control).
                result_text = rbac.refusal_text(name)
            else:
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as exc:
                    result_text = f"ERROR: model returned invalid JSON tool arguments: {exc}"
                else:
                    # Contract enforcement: validate args against the declared
                    # schema BEFORE any handler runs (spec: rigid JSON schemas).
                    validation_error = validate_tool_arguments(spec.parameters, arguments)
                    if validation_error is not None:
                        result_text = (
                            f"ERROR: invalid arguments for '{name}': {validation_error}"
                        )
                    else:
                        try:
                            result_text = _execute_tool(
                                spec, arguments, confirmation_gate, ledger=transcript
                            )
                        except Exception as exc:  # surface handler errors honestly
                            result_text = f"ERROR: tool '{name}' failed: {exc}"

            # DLP at the API boundary: redact credential-shaped text from tool
            # results before they reach the model or the audit transcript.
            if (
                dlp is not None
                and result_text
                and not result_text.startswith(("REFUSED", "ERROR"))
            ):
                result_text, hits = dlp.redact(result_text)
                if hits:
                    result_text += f"\n[DLP: {len(hits)} secret pattern(s) redacted from tool result]"

            result_entry = {"type": "tool_result", "name": name, "text": result_text}
            transcript.append(result_entry)
            _emit_event(event_sink, result_entry)
            messages.append(
                {"role": "tool", "tool_call_id": tool_call.id, "content": result_text}
            )

    exhausted_entry = {
        "type": "result",
        "is_error": True,
        "reason": "max_turns exceeded",
    }
    transcript.append(exhausted_entry)
    _emit_event(event_sink, exhausted_entry)
    # Keep the history well-formed for multi-turn chat: after a tool exchange
    # the next turn must NOT begin with a "user" message (OpenAI-compatible
    # APIs reject "tool" then "user" without an intervening assistant).
    messages.append({"role": "assistant", "content": ""})
    return {"final_text": "", "transcript": transcript, "messages": messages}


def run_dispatch(
    prompt: str,
    registry: DispatchRegistry,
    max_turns: int = 8,
    client: Any | None = None,
    config: NvidiaConfig | None = None,
    confirmation_gate: Callable[[str], bool] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Send one request through the NVIDIA-backed general dispatcher.

    Single-shot convenience wrapper over run_dispatch_messages: builds a
    fresh system+user message list, runs the loop, and returns
    {"final_text", "transcript"}. ``client``/``config`` injectable for
    isolated testing; ``confirmation_gate`` is the human-in-the-loop hook.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_message(registry)},
        {"role": "user", "content": prompt},
    ]
    report = run_dispatch_messages(
        messages,
        registry,
        max_turns=max_turns,
        client=client,
        config=config,
        confirmation_gate=confirmation_gate,
        model=model,
    )
    return {"final_text": report["final_text"], "transcript": report["transcript"]}


def _cli_confirmation_gate(prompt_text: str) -> bool:
    print(f"\n[CONFIRMATION REQUIRED] {prompt_text}")
    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


if __name__ == "__main__":
    # Delayed import so the engine module stays dependency-light for tests.
    from dourmouse.general_roster import build_general_registry

    _registry = build_general_registry()
    user_prompt = " ".join(sys.argv[1:]) or "What subagents are available?"
    report = run_dispatch(user_prompt, _registry, confirmation_gate=_cli_confirmation_gate)
    print(json.dumps(report, indent=2, default=str))
