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


def _call_with_retry(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    config: NvidiaConfig | None,
    call_log: list[dict[str, Any]] | None = None,
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
    # v4.0 (reviewer-caught live): thinking-tuned local models (qwen3, deepseek
    # r1) emit reasoning tokens BEFORE content and hit max_tokens empty. Ollama
    # honours enable_thinking=False for a direct answer; NVIDIA ignores it.
    extra_body = {"enable_thinking": False} if isinstance(config, OllamaConfig) else None

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if call_log is not None:
                call_log.append({"model": model, "attempt": attempt + 1})
            return client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                extra_body=extra_body,
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
        )
    assert last_exc is not None
    raise last_exc


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
    "the roster tools directly."
)


def _build_client(config: NvidiaConfig) -> OpenAI:
    # Ollama (and other keyless local backends) carry an EMPTY api_key by
    # design, but the OpenAI SDK rejects empty strings. Ollama ignores the
    # key value, so substitute a non-empty sentinel for keyless configs
    # (reviewer-caught: the live local path crashed with Missing credentials).
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

        response = _call_with_retry(
            client,
            model=model,
            messages=messages,
            tools=registry.tool_specs(),
            config=ctx.config,
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
