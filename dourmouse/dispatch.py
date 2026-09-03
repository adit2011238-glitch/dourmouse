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
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from openai import OpenAI

from dourmouse.config import (
    NvidiaConfig,
    OllamaConfig,
    OmniRouteConfig,
    backend_identity,
    brief_mode_enabled,
    fast_lane_enabled,
    fast_lane_model,
    fast_lane_model_swap_enabled,
    fast_lane_server_enabled,
    load_llm_config,
)
from dourmouse import model_router
from dourmouse.backend_fallback import load_llm_config_with_fallback
from dourmouse.governance import (
    BudgetTracker,
    DlpFilter,
    RbacPolicy,
    validate_against_schema,
    validate_tool_arguments,
)
from dourmouse.planner import build_plan, looks_multi_step


# Transient API failures worth retrying (institutional self-correction):
# rate limits and 5xx/connection errors. Anything else (auth, malformed
# requests) must fail loudly — never masked by a retry loop.
#
# v13.2 (live-caught, real bug): this only ever recognized the `openai` SDK's
# own exception hierarchy. _OllamaNativeClient (the "cloud"/native Ollama
# path — see its own module docstring on why it talks to /api/chat directly
# instead of through the openai client) uses bare urllib.request, which
# raises urllib.error.HTTPError/URLError and socket timeouts — a completely
# different class hierarchy that isinstance() against openai.* exceptions
# will never match. Live-reproduced: a real transient 500 from Ollama Cloud
# mid-turn (after the model had already streamed a full answer and called a
# tool) hit this function, got classified as non-transient, and `raise`d
# immediately on the FIRST attempt with zero retries — crashing the whole
# turn (empty persisted transcript, a raw "HTTP Error 500: Internal Server
# Error" surfaced to the user) for exactly the kind of hiccup the retry loop
# exists to absorb.
def _is_transient_error(exc: Exception) -> bool:
    import socket
    import urllib.error

    import openai as _openai

    for cls in (
        _openai.RateLimitError,
        _openai.APIConnectionError,
        _openai.APITimeoutError,
        _openai.InternalServerError,
    ):
        if isinstance(exc, cls):
            return True
    if isinstance(exc, TimeoutError | socket.timeout | ConnectionError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        # A real 5xx or rate-limit response from the far end is worth
        # retrying; a 4xx (bad request, auth, not found) never gets better
        # on retry and must fail loudly instead of masking a real problem.
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        # No HTTP status at all (DNS failure, connection refused, a plain
        # socket timeout wrapped by urllib) — network-level, transient.
        return True
    return False


# Hard cap on a single LLM response. qwen3 without a cap can ramble for
# hundreds of tokens at local speeds. Measured dispatch outputs (tool-call
# JSON and chat answers) run 120-300 tokens, so 800 covers any answer and
# any tool-call JSON with 2.7x headroom while halving worst-case generation
# latency on this hardware (the old 1400 cap meant up to ~80-140s at the
# 10-17 tok/s thermal ceiling; 800 bounds it to ~45-80s). Code completions
# pass their own 4000 cap via code_backends.
_DEFAULT_MAX_TOKENS = 800

# ---- LLM context bounding (v4.2 speed) ------------------------------- #
# Measured on the user's M3 Air: prefill runs ~46 tok/s under sustained
# load, so every token re-sent to the model costs ~20ms. Sessions used to
# grow unbounded — every turn re-prefilled the ENTIRE conversation, so later
# turns took minutes. The LLM now sees a bounded rolling window: the system
# message + the full in-flight exchange + as many complete older exchanges
# as fit the budget. The authoritative ``messages`` list is untouched (still
# persisted, resumable, and returned to callers) — only the API boundary is
# bounded.
#
# v13.2 (live-caught, real bug — explicit user report: "the model easily
# loses the plot and doesn't retain context"): this constant's own comment
# justified 4600 against an 8192 num_ctx ceiling that no longer exists —
# _OLLAMA_NUM_CTX was raised to 16384 (see its own comment, a SEPARATE
# earlier fix) and this was simply never revisited, so every real turn was
# still being trimmed to less than a THIRD of what the model can now
# actually hold (system prompt ~3,700 tokens, not charged against this
# budget — see _bounded_context — + this history budget + the 800-token
# response cap, against a 16,384 window). 9000 leaves system(~3700) +
# history(9000) + response(800) = ~13,500, still ~2,900 tokens of real
# headroom under 16,384 even when the chars/4 estimate undercounts —
# proportionally the same safety margin the original 4600/8192 sizing had,
# just correctly re-derived against the window that is actually in use.
# Overridable via DOURMOUSE_MAX_CONTEXT_TOKENS for a smaller-context model
# where even this would overflow — never hardcode a second constant for
# that case.
_MAX_LLM_TOKENS = 9000
_MAX_TOOL_RESULT_CHARS = 800  # OLD tool results re-read by the model get cut


def _max_llm_tokens() -> int:
    raw = os.environ.get("DOURMOUSE_MAX_CONTEXT_TOKENS", "").strip()
    if raw:
        try:
            return max(500, int(raw))
        except ValueError:
            pass
    return _MAX_LLM_TOKENS


def _est_tokens(message: dict[str, Any]) -> int:
    """Rough per-message token estimate (repo convention ~4 chars/token)."""
    content = message.get("content") or ""
    if message.get("role") == "tool":
        return len(content) // 4
    cost = 4 + len(content) // 4
    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            fn = tc.get("function") or {}
            cost += len(fn.get("arguments") or "") // 4
    return cost


def _bounded_context(
    messages: list[dict[str, Any]],
    max_tokens: int = _MAX_LLM_TOKENS,
    max_tool_chars: int = _MAX_TOOL_RESULT_CHARS,
) -> list[dict[str, Any]]:
    """Bounded copy of ``messages`` for the LLM API boundary.

    Always keeps the system prompt (messages[0]) and the ENTIRE in-flight
    exchange — everything from the most recent ``user`` message onward — so
    the current directive and its tool trail are never truncated. Older
    complete exchanges are added most-recent-first while the token budget
    allows; anything beyond the budget is dropped at a clean ``user``
    boundary (never mid-exchange, which some backends reject). Tool-result
    messages OLDER than the in-flight exchange are truncated to
    ``max_tool_chars`` in the copy: they were already seen in full when
    produced, so later turns only need the gist.
    """
    if not messages:
        return []
    system = messages[0] if messages[0].get("role") == "system" else None
    user_idx = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    tail_start = user_idx[-1] if user_idx else (0 if system is None else 1)
    # Walk backward from the in-flight tail while the budget allows.
    keep_from = tail_start
    budget = max_tokens
    i = tail_start
    while i > 0 and budget > 0:
        cost = _est_tokens(messages[i - 1])
        if cost <= budget:
            budget -= cost
            i -= 1
            keep_from = i
        else:
            break
    # Roll forward to a clean user boundary so the window never starts
    # mid-exchange (a dangling tool/assistant-tool_calls message would be
    # rejected by OpenAI-compatible backends).
    j = keep_from
    while j < len(messages) and messages[j].get("role") != "user":
        j += 1
    keep_from = j
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        # The system prompt is ALWAYS kept, even when the boundary roll
        # below lands past it (a bounded window without the system prompt is
        # a different conversation to the model).
        if i == 0 and m.get("role") == "system":
            out.append(m)
            continue
        if i < keep_from:
            continue
        if (
            m.get("role") == "tool"
            and i < tail_start
            and len(m.get("content") or "") > max_tool_chars
        ):
            m = {**m, "content": m["content"][:max_tool_chars] + "\n...[truncated]"}
        out.append(m)
    return out


def _usage_of(response: Any) -> dict[str, int]:
    """Pull token counts off a completion, tolerating shapes that lack them.

    Streaming responses and some local backends omit usage entirely, so this
    returns whatever is present rather than assuming the field exists.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    out: dict[str, int] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, field, None)
        if isinstance(value, int):
            out[field] = value
    return out


#: How often the "still working" heartbeat fires while a model call is
#: blocked with zero visible output — see _call_with_retry's own
#: heartbeat thread below. 3s: frequent enough that a user watching the
#: UI never wonders if the tab died, infrequent enough it's not visual
#: noise on a call that finishes fast anyway (nothing fires before the
#: first interval elapses).
_HEARTBEAT_INTERVAL_S = 3.0


def _call_with_retry(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    config: NvidiaConfig | None,
    call_log: list[dict[str, Any]] | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> Any:
    """LLM call with bounded retry + backoff, optional fallback, and timing.

    Every model call in the process funnels through here, so this is the one
    place that can measure inference latency without touching call sites. The
    timing wraps the whole retry sequence deliberately: what a user waits for
    is the total, including backoff and any fallback attempt, not the duration
    of the one attempt that happened to succeed.

    v13 (live-caught, real bug): a slow local-model call gives the UI
    ZERO signal for the entire PREFILL phase — no tokens exist yet to
    stream, so even the streaming path (_stream_completion's on_delta)
    sits silent. Live-measured: a real turn's prefill alone ran 60+
    seconds with nothing visible, indistinguishable from a hung/dead
    request — exactly what got reported as "no reply". When ``event_sink``
    is given, a background thread emits a real elapsed-time "brain_thinking"
    event every _HEARTBEAT_INTERVAL_S while the (blocking) call runs, so
    the UI can show real progress ("still thinking — 45s") instead of a
    frozen screen. Purely additive: the heartbeat thread never touches
    the actual model call or its result, and the LAST heartbeat is
    always followed by a genuine response or a genuine error — never a
    fabricated one.
    """
    start = time.perf_counter()
    ok = True
    stop_heartbeat = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    if event_sink is not None:
        def _beat() -> None:
            while not stop_heartbeat.wait(_HEARTBEAT_INTERVAL_S):
                _emit_event(
                    event_sink,
                    {
                        "type": "brain_thinking",
                        "model": model,
                        "elapsed_s": round(time.perf_counter() - start, 1),
                    },
                )

        heartbeat_thread = threading.Thread(target=_beat, daemon=True)
        heartbeat_thread.start()
    try:
        response = _call_with_retry_inner(
            client,
            model=model,
            messages=messages,
            tools=tools,
            config=config,
            call_log=call_log,
            on_delta=on_delta,
            on_thinking=on_thinking,
            client_factory=client_factory,
        )
        return response
    except BaseException:
        ok = False
        response = None
        raise
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        real_usage = _usage_of(response) if response is not None else {}
        try:
            from dourmouse import obs

            obs.log_perf(
                op="inference",
                duration_ms=(time.perf_counter() - start) * 1000.0,
                extra={
                    "model": model,
                    "ok": ok,
                    "streamed": on_delta is not None,
                    "n_messages": len(messages),
                    "n_tools": len(tools),
                    "attempts": len(call_log) if call_log is not None else None,
                    **real_usage,
                },
            )
        except Exception:  # noqa: BLE001 - measurement must never break a call
            pass
        # v13.6: real usage bar ("how much usage you have used on...
        # ollama api key") -- this is the one real choke point every
        # non-Claude-CLI backend call (Ollama/NVIDIA/OmniRoute) already
        # passes through, so it's also the one place to record real
        # token counts without duplicating _usage_of's own extraction.
        if real_usage:
            try:
                from dourmouse import usage_tracker

                usage_tracker.record_ollama_usage(real_usage)
            except Exception:  # noqa: BLE001 - usage tracking must never break a call
                pass


def _call_with_retry_inner(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    config: NvidiaConfig | None,
    call_log: list[dict[str, Any]] | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> Any:
    """LLM call with bounded retry + backoff, and optional model fallback.

    Deterministic resilience (spec: self-correction & exception handling):
    transient errors retry up to ``max_retries`` with exponential backoff;
    if still failing and a ``fallback_model`` is configured, ONE fallback
    attempt runs against the second model before giving up. Never retries
    non-transient errors. The real response object is returned.

    v13.1 (Aider port part 4/4, dourmouse/model_router.py): ``client_factory``
    is purely additive — omitted (the default), behavior is byte-for-byte
    unchanged from before this parameter existed. When given, a
    rate-limit-shaped failure calls it again for the NEXT attempt instead
    of retrying the SAME exhausted client — the caller is expected to have
    already marked the current account's cooldown (see _build_client's
    multi-account wiring) so the factory naturally returns a DIFFERENT
    account's client when one is configured and available.
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
                    client, model, messages, tools, extra_body, on_delta, on_thinking
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
                if client_factory is not None and model_router.is_rate_limit_error(exc):
                    try:
                        client = client_factory()
                    except Exception:  # noqa: BLE001 - a bad factory must not break the retry itself
                        pass
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
    on_thinking: Callable[[str], None] | None = None,
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
        thinking_text = getattr(delta, "thinking", None)
        if thinking_text and on_thinking is not None:
            on_thinking(thinking_text)
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
            if tool.name in self._tools and self._tools[tool.name] is not tool:
                raise ValueError(
                    f"tool name collision across registry: {tool.name!r} "
                    f"(from {subagent.name})"
                )
        for tool in subagent.tools:
            self._tools[tool.name] = tool
        self._subagents[subagent.name] = subagent

    def extend_subagent(self, name: str, tool: ToolSpec) -> None:
        """Attach an existing tool to an already-registered subagent.

        The single extension point for cross-agent capabilities (v5.8
        artifacts): a tool like publish_artifact belongs on every agent that
        produces reports, but tool names must stay globally unique so a
        later addition can never silently shadow an existing tool. Sharing
        the SAME ToolSpec object (``is``-identity) satisfies both: the
        registry keeps one slot under that name, every subagent carries the
        same handler, and a DIFFERENT object claiming the name still raises.
        """
        sub = self._subagents.get(name)
        if sub is None:
            raise ValueError(f"no subagent registered: {name}")
        if any(t is tool for t in sub.tools):
            return  # idempotent — already shared with this agent
        if tool.name not in self._tools:
            self._tools[tool.name] = tool
        elif self._tools[tool.name] is not tool:
            raise ValueError(
                f"tool name collision across registry: {tool.name!r} "
                f"(from {name})"
            )
        self._subagents[name] = Subagent(
            name=sub.name,
            domain=sub.domain,
            description=sub.description,
            tools=sub.tools + (tool,),
        )

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

    def describe_roster(self, focus: set[str] | None = None) -> str:
        """Roster text for the system message.

        With ``focus`` unset every agent gets its full line (name, domain,
        description, every tool) — the historical behaviour, unchanged.

        With ``focus`` set, only the named agents get the full line; the rest
        collapse to a single comma-joined name list. The lead still knows every
        agent exists (so it can delegate or ask), but the 31-agent/161-tool
        dump costs ~12.3k chars (~3.1k tokens) on EVERY turn while a typical
        directive touches one or two agents. Prefill is the dominant cost on
        fanless hardware, so this is the cheapest remaining latency lever
        after ``_scoped_tool_specs`` (which already trims the schemas but not
        this prose).
        """
        if not focus:
            lines = [s.roster_line() for s in self._subagents.values()]
            return "\n".join(lines) if lines else "(empty roster)"

        # Never drop the orchestrator: its delegate_task tool is how the lead
        # reaches anything that was collapsed.
        keep = set(focus) | {"orchestrator"}
        detailed: list[str] = []
        collapsed: list[str] = []
        for sub in self._subagents.values():
            if sub.name in keep:
                detailed.append(sub.roster_line())
            else:
                collapsed.append(sub.name)

        if not detailed:
            # A focus that matched nothing must not yield an empty roster.
            lines = [s.roster_line() for s in self._subagents.values()]
            return "\n".join(lines) if lines else "(empty roster)"

        out = "\n".join(detailed)
        if collapsed:
            out += (
                "\n- also available (ask or delegate_task to expand): "
                + ", ".join(collapsed)
            )
        return out


def _scoped_tool_specs(
    registry: DispatchRegistry, agent_names: set[str], *, include_delegate: bool = True
) -> list[dict[str, Any]]:
    """Full tool schemas ONLY for the named agents (plus, by default, the
    orchestrator's delegate tool, so mid-task delegation stays possible).

    The roster description in the system message still names every agent and
    tool, so planning is unaffected — this only shrinks the schema payload.
    Sending all 60 schemas costs ~80s of cold prefill (measured live: 5,457
    tokens @ 67 t/s = 81s before the first token) and dwarfs the actual
    conversation; scoped, a plain question prefills in ~18s cold / ~1s warm.

    ``include_delegate=False`` (used for a forced_agent run — see this
    function's own call site) drops the orchestrator's delegate_task/
    delegate_parallel from the scoped set entirely. Real bug this fixes,
    live-reproduced: a forced_agent run scoped to e.g. code_claude (a
    subagent whose ONLY real tool happens to be a tool of the SAME name)
    still exposed delegate_task pointing at a target that ALSO named
    "code_claude" — a weak local orchestrator model, given a choice between
    calling the tool directly and delegating to "the code_claude subagent"
    by name, picked delegate_task, which opened a NESTED forced_agent run
    scoped to code_claude again... recursing to the hard depth cap (3)
    without ever calling the real tool once. forced_agent's own docstring
    already promises a "hard-scoped to exactly one subagent's tools" run;
    dropping delegate access here is what actually makes that true.
    """
    names = set(agent_names)
    if include_delegate:
        names.add("orchestrator")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sub in registry.all_subagents():
        if sub.name not in names:
            continue
        for t in sub.tools:
            # Shared tools (extend_subagent) appear on several agents — emit
            # the schema once so the model never sees duplicate names.
            if t.name in seen:
                continue
            seen.add(t.name)
            out.append(t.openai_spec())
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
    "direct, and concise.\n"
    "9. NEVER narrate your own reasoning. Never start with 'Okay,', 'Hmm,', "
    "'Let me', 'I think', or meta-talk about the conversation ('the user "
    "asked', 'as I said before'). Do not restate the question. Just deliver "
    "the answer. If you do not know, say so in one sentence and offer the "
    "nearest tool that could find out.\n"
    "10. AGENT ROUTING — use the right agent/tool for the task:\n"
    "  - Coding, building, debugging → dev_coding (run_python, edit_file, "
    "claude_code, codex_code). Coding via an external LLM CLI → code_claude, "
    "code_codex, code_deepseek, code_nvidia, code_ollama.\n"
    "  - Full laptop access (files anywhere, shell) → system (dangerous "
    "commands are confirmation-gated); sandboxed workspace file cleanup → "
    "admin_ops (deletion is always per-item confirmed).\n"
    "  - Stock quotes / market movers → markets; web research & synthesis → "
    "research_info (web_search, fetch_url); live R&D intel → rnd.\n"
    "  - Email / Gmail / Drive → mail; drafting messages → comms (draft "
    "only, sending confirmed); calendar → scheduling (read-only + proposed "
    "times, booking confirmed).\n"
    "  - Storing or recalling knowledge → memory (remember, recall, "
    "memory_search_semantic); long-term chat recall is ALSO injected "
    "automatically into your context when relevant — use it when it appears.\n"
    "  - News headlines → news; local task list → tasks; Spotify → music; "
    "ATLAS quant repo → atlas; Freebuff → freebuff; global intelligence → "
    "worldmonitor; inter-agent messages → messenger; nested subtasks → "
    "delegate_task (depth-bounded).\n"
    "11. DAILY TASKS — your standing duties (the live agents run these on a "
    "fixed cadence and you operate them):\n"
    "  - Live polls: news headlines every 2 min; market gainers + losers "
    "every 2 min; rnd news + movers every 3 min; inbox (top 5) every 5 min; "
    "task list every 1 min. Results are broadcast on the inter-agent bus and "
    "keep each LIVE agent current — never present a poll result as new "
    "research or as the operator's data.\n"
    "  - When asked to summarize the day or review what happened, run the "
    "honest daily self-review via the memory agent's daily_digest tool — "
    "never fabricate a digest from memory alone.\n"
    "  - Surface anything time-sensitive the live feeds expose; do not "
    "silently drop a poll result that matters.\n"
    "12. NEVER proactively call write_note, remember, or any other "
    "persistence tool to save your OWN answer just because it was long or "
    "seemed useful. A finished answer is a deliverable in the chat, not "
    "something to file away on its own — only persist when the user "
    "explicitly asked you to save/remember/note it, or when the request "
    "was itself a request to store something."
)


# Native Ollama adapter (v4.1). The OpenAI-compat endpoint on this Ollama
# build IGNORES think/enable_thinking (measured live: 57-73s thinking traces
# per answer, content empty at any token cap), while the native /api/chat
# honors think=False (measured: 2+2 in 3.2s/9 tokens vs 39.6s/188). So the
# local path talks to the native API directly: real streaming, a warm model,
# and a proper context window instead of the 4096-token truncation default.
#
# v13 (live-caught, real bug): 8192 was only ever "double Ollama's own
# 4096 default" — never derived from this roster's real prompt sizes.
# Live-measured on a real multi-turn conversation: the always-sent system
# prompt alone (33-agent roster description) is ~3,700 tokens, and a
# single real turn (one gmail_search + a summarize pass, ordinary
# COMMS-screen usage) reached 6,719 tokens of prompt — 82% of the OLD
# 8192 ceiling. The observed failure mode at that size was not a clean
# truncation error: the model silently produced a fully unrelated
# hallucinated answer (a linear-programming script, asked to summarize
# an inbox) after 323s. qwen2.5:7b's own real context length (`ollama
# show qwen2.5:7b`) is 32768 — Dourmouse was capping it to a quarter of
# what it actually supports. Raised to give real headroom against this
# exact failure; still well under the model's own ceiling, so KV-cache
# memory cost stays bounded rather than jumping straight to 32768.
_OLLAMA_NUM_CTX = 16384
_OLLAMA_KEEP_ALIVE = "30m"

#: Models whose chat template either (a) IGNORES the `think` /
#: `enable_thinking` request flags and reasons anyway — the reasoning then
#: lands untagged in the user-visible answer AND consumes the num_predict
#: budget (qwen3:4b, the original case this list was built for) — or
#: (b) actively REJECTS the request outright when the flags are present at
#: all, a stricter failure live-caught 2026-08-30 against the companion
#: agent's real workspace_ui/delegate_task tool-calling turns: every
#: request 400'd with body {"error":"\"qwen2.5:7b\" does not support
#: thinking"} the instant `think`/`enable_thinking` were sent, regardless
#: of true/false — Ollama here treats an unsupported model even ASKING is
#: an error, not something to silently ignore. Both failure modes get the
#: exact same fix (drop the flags before sending), so one list serves
#: both. Substring match on the model name, so tags (":4b",
#: ":7b-instruct-q4_0") all hit. Override with DOURMOUSE_NO_THINK_MODELS
#: (comma-separated) when a future build fixes or breaks a model — no
#: code change needed to re-tune this.
_THINK_FLAG_IGNORED_DEFAULT = "qwen3:4b,qwen2.5"


def _no_think_models() -> tuple[str, ...]:
    raw = os.environ.get("DOURMOUSE_NO_THINK_MODELS")
    if raw is None:
        raw = _THINK_FLAG_IGNORED_DEFAULT
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def _ignores_think_flag(model: str) -> bool:
    name = (model or "").strip().lower()
    return any(pat in name for pat in _no_think_models())


#: v13.1: visible chain-of-thought, on by default per explicit repeated user
#: request ("enable visible chain of thought for all models"). Set
#: DOURMOUSE_SHOW_THINKING=0 to go back to the old silent-reasoning
#: behavior (e.g. for a model/account where reasoning tokens are billed and
#: unwanted).
_SHOW_THINKING_ENV = "DOURMOUSE_SHOW_THINKING"


def _show_thinking_enabled() -> bool:
    import os

    raw = os.environ.get(_SHOW_THINKING_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


#: The documented qwen3 soft switch. Appended to the LAST user message because
#: the template only honours it on the active turn — a system-message
#: placement was measured as NOT working (367 tok, reasoning still leaked).
_NO_THINK_TOKEN = "/no_think"


def _append_no_think(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(m) for m in messages]
    for msg in reversed(out):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and _NO_THINK_TOKEN not in content:
            msg["content"] = f"{content} {_NO_THINK_TOKEN}"
        break
    return out


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
    def __init__(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
        thinking: str | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        # v13.1: visible chain-of-thought — Ollama's native /api/chat, when
        # sent think:true, streams reasoning tokens in a SEPARATE
        # message.thinking field, never mixed into message.content. Kept as
        # its own delta attribute (not concatenated into content) so the UI
        # can render it in its own "THINKING" block instead of leaking into
        # the visible answer — the exact leak Rule "9. NEVER narrate your
        # own reasoning" above was written to prevent when thinking WAS
        # mixed into content on models that ignored think:False.
        self.thinking = thinking


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
        # v13: Ollama Cloud (ollama.com) — the SAME native /api/chat shape,
        # a real GPU-hosted account, auth via a real API key the local
        # daemon never needs. OllamaConfig already carried an api_key
        # field (kept keyless/unused for the local case); this is the one
        # real wiring point: a non-empty key adds the Bearer header,
        # nothing else about this client changes. Verified live against
        # https://ollama.com/api/chat: gpt-oss:20b answered correctly in
        # 677ms — real GPU compute, not this machine's.
        self._headers = {"Content-Type": "application/json"}
        if config.api_key:
            self._headers["Authorization"] = f"Bearer {config.api_key}"
        # Real bug found and fixed here (2026-08-30, live-caught while
        # debugging an unrelated 400): `self._post is self._default_post`
        # in _stream() below ALWAYS evaluates False, even when this exact
        # branch (_post is None) ran and _post really is self._default_post
        # — Python creates a NEW bound-method wrapper object on every
        # attribute access, so two separate `self._default_post` reads are
        # never `is`-identical to each other, only equal. The real effect:
        # every real (non-test) streaming call silently fell through to the
        # buffered `self._post(payload)` + splitlines() branch instead of
        # the genuinely-incremental `_default_post_lines()` — true
        # token-by-token delivery to the browser was never actually
        # happening for the default client, only for injected test
        # doubles that happened to differ by identity. A plain bool
        # recorded once here, instead of an unreliable method-identity
        # check later, is the fix.
        self._is_default_post = _post is None
        self._post = _post or self._default_post
        self.chat = type("_Chat", (), {"completions": _OllamaCompletions(self)})()

    def _default_post(self, payload: dict[str, Any]) -> str:
        import urllib.request as _urllib

        req = _urllib.Request(
            self._root + "/api/chat",
            data=json.dumps(payload).encode(),
            headers=self._headers,
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
            headers=self._headers,
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
        chosen = model or self._model
        sent = self._translate_messages(messages)
        show_thinking = _show_thinking_enabled()
        payload: dict[str, Any] = {
            "model": chosen,
            "messages": sent,
            "stream": bool(stream),
            "think": show_thinking,
            "enable_thinking": show_thinking,
            "keep_alive": _OLLAMA_KEEP_ALIVE,
            "options": {
                "num_predict": int(max_tokens or _DEFAULT_MAX_TOKENS),
                "num_ctx": _OLLAMA_NUM_CTX,
            },
        }
        # v5.32: `think: False` is NOT honoured by every qwen3 build. Measured
        # on Ollama 0.32.9 (4 prompts, temperature 0, this machine):
        #
        #   qwen3:8b  think:False      4.4s median,  25 tok  — honoured
        #   qwen3:4b  think:False     45.1s median, 360 tok  — IGNORED, and the
        #                                                      reasoning is
        #                                                      emitted as the
        #                                                      visible answer
        #   qwen3:4b  /no_think       21.5s median, 178 tok  — honoured
        #
        # Worse, sending think:False to qwen3:4b was consistently WORSE than
        # sending no flag at all (461 vs 434 tokens). So for models in the
        # ignore-list we drop the ineffective flags and use the documented
        # `/no_think` soft switch instead, which the template does respect.
        # v13.1: the /no_think soft switch only makes sense when we WANT
        # thinking suppressed. Now that show_thinking defaults True (visible
        # CoT), an ignore-list model's leaked reasoning is exactly what the
        # user asked to see — it just lands in .content instead of the
        # clean .thinking field these models don't honour, same as before
        # think:False existed. Only force /no_think when thinking is
        # explicitly turned off.
        if _ignores_think_flag(chosen):
            payload.pop("think", None)
            payload.pop("enable_thinking", None)
            if not show_thinking:
                payload["messages"] = _append_no_think(sent)
        if tools:
            payload["tools"] = tools
        if stream:
            return self._stream(payload)
        return self._complete(payload)

    def _complete(self, payload: dict[str, Any]) -> _OllamaResponse:
        data = json.loads(self._post(payload))
        msg = data.get("message") or {}
        tool_calls = self._native_tool_calls(msg.get("tool_calls") or [])
        message = _OllamaMessage(msg.get("content") or "", tool_calls)
        message.thinking = msg.get("thinking") or None
        return _OllamaResponse(message)

    def _stream(self, payload: dict[str, Any]):
        if self._is_default_post:
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
            # v13.1: Ollama's native streaming puts reasoning tokens in their
            # OWN field when think:true — never mixed into .content, so this
            # is a clean separation, not a heuristic parse.
            thinking = msg.get("thinking") or None
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
            if content is None and thinking is None and not delta_tcs:
                continue
            yield _OllamaChunk(_OllamaDelta(content=content, tool_calls=delta_tcs, thinking=thinking))

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


#: v13.1 (Aider port part 4/4, dourmouse/model_router.py): the NVIDIA
#: account pool lives for the process lifetime — cooldown state MUST
#: survive across calls (that's the entire point: skip an account that
#: just rate-limited on the NEXT call, not just within one retry loop).
#: Built lazily so a process that never sets NVIDIA_API_KEY_2 never pays
#: for it, and reset()-able for tests that need a fresh one per case.
_nvidia_account_pool: model_router.AccountPool | None = None


def _get_nvidia_account_pool() -> model_router.AccountPool:
    global _nvidia_account_pool
    if _nvidia_account_pool is None:
        _nvidia_account_pool = model_router.AccountPool(
            model_router.accounts_from_env("nvidia", "NVIDIA_API_KEY")
        )
    return _nvidia_account_pool


def _reset_account_pools_for_testing() -> None:
    """Test-only: forces the next _get_nvidia_account_pool() call to
    rebuild from the CURRENT environment instead of a stale cached one."""
    global _nvidia_account_pool
    _nvidia_account_pool = None


def _nvidia_rotation_factory(
    initial_client: Any, config: NvidiaConfig | OllamaConfig | OmniRouteConfig | None
) -> Callable[[], Any] | None:
    """None (no rotation) unless 2+ NVIDIA accounts are actually
    configured — a single-account setup (the overwhelmingly common case)
    is completely untouched by this: _call_with_retry_inner's
    client_factory stays unused and behavior is byte-for-byte what it was
    before multi-account routing existed.

    When 2+ accounts ARE configured: returns a closure that, each time
    _call_with_retry_inner calls it after a rate-limit error, marks the
    account that just failed into cooldown and builds a fresh OpenAI
    client against the next available one.
    """
    if not isinstance(config, NvidiaConfig):
        return None
    pool = _get_nvidia_account_pool()
    if len(pool) < 2:
        return None
    state: dict[str, model_router.Account | None] = {"current": None}

    def factory() -> Any:
        previous = state["current"]
        if previous is not None:
            pool.mark_rate_limited(previous.name)
        account = pool.select(exclude=previous.name if previous else None)
        if account is None:
            # every account is cooling down — keep serving on the one we
            # already have rather than raising here; the caller's own
            # retry/fallback machinery still runs against it and surfaces
            # the real error if it genuinely can't succeed.
            return initial_client
        state["current"] = account
        return OpenAI(api_key=account.api_key or "local-keyless", base_url=config.base_url)

    return factory


def _build_client(
    config: NvidiaConfig | OllamaConfig | OmniRouteConfig,
    forced_agent: str | None = None,
) -> Any:
    # v13 (opt-in experiment, the user's own explicit ask): route through
    # a real strong backend — Claude Code CLI, or a real Ollama Cloud
    # account — INSTEAD of the local model, for every feature. See
    # _orchestrator_backend_mode()'s own docstring for the accepted
    # values and why "split" exists.
    mode = _orchestrator_backend_mode()
    if mode == "split":
        mode = _agent_split_backend(forced_agent)
    if mode in ("claude", "claude_cli"):
        return ClaudeCliClient()
    if mode in ("ollama_cloud", "cloud"):
        return OllamaNativeClient(_ollama_cloud_config())
    # Ollama: talk to the native API (fast, streaming, think disabled).
    # NVIDIA / OmniRoute: the OpenAI SDK, with a non-empty sentinel key
    # (Ollama/OmniRoute ignore key values, but the SDK rejects empty strings
    # — reviewer-caught). OmniRoute is OpenAI-compatible and keyless.
    if isinstance(config, OllamaConfig):
        return OllamaNativeClient(config)
    key = config.api_key or "local-keyless"
    return OpenAI(api_key=key, base_url=config.base_url)


#: v5.22.5: domains where a hallucinated answer is worse than a slow one.
#: The fast orchestrator brain is great at chat but fabricates on
#: tool-critical single-step prompts (observed: invented Spotify playlist
#: URIs, "the playlist is empty" without calling any tool). These
#: deterministic keywords force the heavy brain — cheap, never an LLM
#: judgment (Rule 2.8).
_TOOL_CRITICAL_RE = re.compile(
    r"(spotify|playlist|play a song|play music|play my|play track|"
    r"currently playing|top tracks|now playing|music)",
    re.IGNORECASE,
)


def _resolve_brain_model(
    *,
    fast: str,
    default: str,
    prompt: str,
    explicit: str | None,
) -> tuple[str, bool]:
    """v5.5: choose the dispatch brain deterministically — (model, escalated).

    ``explicit`` (a focus-agent model override) always wins. Otherwise a
    MULTI-STEP prompt escalates to the full default brain (the model heavy
    agents already use) instead of the fast orchestrator brain, so hard
    multi-step work gets the stronger model while simple chat stays fast.
    Deterministic (Rule 2.8): the planner's cheap multi-step heuristic,
    never an LLM judgment. ``escalated`` is True when the heavy brain was
    chosen and differs from the fast one — the UI surfaces it honestly.
    """
    if explicit:
        return explicit, False
    if looks_multi_step(prompt):
        return default, default != fast
    # v5.22.5: TOOL-CRITICAL domains also escalate to the heavy brain.
    # Music/Spotify is the poster child: the fast brain (qwen2.5:7b)
    # FABRICATES playlist URIs and even hallucinates "the playlist is
    # empty" without calling a tool — the stronger model follows the
    # spotify_playlists-lookup instruction and routes honestly. Cheap
    # deterministic keyword test (Rule 2.8), never an LLM judgment.
    if _TOOL_CRITICAL_RE.search(prompt):
        return default, default != fast
    return fast, False


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
    # Tool-boundary containment. A handler is the seam between the model and
    # real infrastructure, and anything can come back through it: a 404, a
    # dead socket, a parser hitting an unexpected shape, an outright bug. An
    # exception escaping here aborts the whole dispatch turn, so the user
    # loses the conversation over one failed tool. Catch broadly, record the
    # traceback where a developer can find it, and hand the model a sentence
    # it can reason about and relay. KeyboardInterrupt/SystemExit are not
    # caught — those must still stop the process.
    start = time.perf_counter()
    try:
        result = spec.handler(arguments)
    except Exception as exc:  # noqa: BLE001 - deliberate boundary catch
        from dourmouse import net_errors, obs

        obs.log_error(
            source=f"tool:{spec.name}",
            kind=net_errors.classify(exc).value,
            what=spec.name,
            detail=traceback.format_exc(),
            status=net_errors.http_status(exc),
            extra={"arguments": arguments},
        )
        obs.log_agent_call(
            tool=spec.name,
            ok=False,
            duration_ms=(time.perf_counter() - start) * 1000.0,
            detail=f"{type(exc).__name__}: {exc}",
        )
        # Keep the long-standing "ERROR: tool 'x' failed:" prefix — callers
        # and the DLP boundary below key off an ERROR prefix, and the model
        # is trained on it. What changes is the tail: transport noise is
        # replaced by a sentence, while a genuine diagnostic survives.
        return (
            f"ERROR: tool '{spec.name}' failed: "
            + net_errors.friendly(exc, what=f"a result from {spec.name}")
        )
    obs_duration_ms = (time.perf_counter() - start) * 1000.0
    try:
        from dourmouse import obs

        obs.log_agent_call(tool=spec.name, ok=True, duration_ms=obs_duration_ms)
    except Exception:  # noqa: BLE001 - observability must never break dispatch
        pass
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


# -- Claude CLI as the orchestrator brain (v13, opt-in experiment) --------- #
# The user's own explicit ask: wire the real Claude Code CLI in as the
# model behind EVERY feature (not just the CODE screen's code_claude
# tool), to test whether a single strong model with real MCP tool access
# genuinely replicates — and improves on — how Claude Code itself acts as
# a harness. Architecturally this is the CLEANEST possible integration
# point: _build_client() below is the ONE place a fresh client is
# resolved for every dispatch call (chat.py never caches one), so gating
# there means every screen, every tool-scoping path, every existing
# piece of dispatch.py's plumbing (budget tracking, RBAC, DLP, transcript,
# heartbeat, persistence) keeps working completely unchanged — only WHICH
# model answers changes.
#
# Claude runs its OWN complete agentic loop via the real Claude Code CLI
# (code_backends.run_code_task, which already wires --mcp-config +
# --allowedTools "mcp__dourmouse__*" — see that module's own docstring),
# so it can call Dourmouse's real tools directly over MCP rather than
# through Dourmouse's own Python tool-calling loop. That means every
# response reports NO tool_calls to dispatch.py — Claude already did
# whatever tool work was needed internally — so a turn through this
# client is exactly ONE call, not several.
_CLAUDE_ORCHESTRATOR_ENV = "DOURMOUSE_ORCHESTRATOR_BACKEND"
_OLLAMA_CLOUD_BASE_URL = "https://ollama.com"
#: Ollama Cloud model verified live against the real API tonight
#: (https://ollama.com/api/chat, real key, 677ms real response). A real,
#: large, cloud-GPU-hosted model — not this machine's compute.
_OLLAMA_CLOUD_DEFAULT_MODEL = "gpt-oss:20b"


def _orchestrator_backend_mode() -> str:
    """DOURMOUSE_ORCHESTRATOR_BACKEND — the opt-in experiment switch:
    unset/'' -> unchanged local/configured behavior; 'claude'/'claude_cli'
    -> every feature routed through the real Claude Code CLI (see
    ClaudeCliClient); 'ollama_cloud'/'cloud' -> every feature routed
    through a real Ollama Cloud account (real API key, real GPU compute,
    NOT this machine's); 'split' -> deterministically divide the roster
    between the two (see _agent_split_backend) rather than picking one
    for everything — the user's own explicit ask, to compare both at once
    rather than testing them one at a time.
    """
    return os.environ.get(_CLAUDE_ORCHESTRATOR_ENV, "").strip().lower()


def claude_orchestrator_enabled() -> bool:
    return _orchestrator_backend_mode() in ("claude", "claude_cli")


_agent_split_cache: dict[str, str] | None = None


def _agent_split_map() -> dict[str, str]:
    """Real, verifiably even split of the ACTUAL registered roster —
    computed once (module-lifetime cache, same pattern as
    code_backends.py's own _CLAUDE_SESSIONS): sort every real subagent
    name alphabetically and alternate, so a 34-agent roster splits
    17/17, not whatever a per-name hash happens to land on (measured: a
    sha256-parity split on this real roster came out 12/22 — nowhere
    near "evenly split", the user's own explicit ask). Alphabetical sort
    keeps it deterministic across restarts without needing to persist
    anything.
    """
    global _agent_split_cache
    if _agent_split_cache is not None:
        return _agent_split_cache
    from dourmouse.general_roster import build_general_registry

    names = sorted(s.name for s in build_general_registry().all_subagents())
    _agent_split_cache = {
        name: ("claude" if i % 2 == 0 else "ollama_cloud") for i, name in enumerate(names)
    }
    return _agent_split_cache


def _effective_split_agent(
    forced_agent: str | None, last_user: str, registry: DispatchRegistry
) -> str | None:
    """Which agent name the orchestrator-backend split should key off of
    for THIS turn — real bug this fixes, live-caught: for an ordinary
    (non-forced_agent) conversational query, _build_client() is called
    BEFORE the planner resolves plan_agents (planning happens later,
    inside _run_dispatch_loop), so a plain "how many unread emails do I
    have" always fell through to _agent_split_backend(None)'s "claude by
    default" case — the split NEVER actually applied to natural,
    planner-routed queries, only to forced_agent screens (CODE's
    toolchain picker). Since real planning here (find_agents_for_query)
    is pure Python/deterministic — no LLM call needed — it's safe to
    peek at it early, purely to pick a backend, without duplicating or
    fighting the loop's own later (identical) resolution."""
    if forced_agent:
        return forced_agent
    if not last_user:
        return None
    try:
        from dourmouse.planner import find_agents_for_query

        matches = find_agents_for_query(registry, str(last_user), limit=1)
    except Exception:  # noqa: BLE001 - a peek for routing must never break the real turn
        return None
    if matches and matches[0].get("score", 0) >= 3:
        return matches[0]["name"]
    return None


def _agent_split_backend(agent_name: str | None) -> str:
    """Deterministic (Rule 2.8) even split of the roster across the two
    real backends — see _agent_split_map's own docstring. An agent name
    outside the current registry (a stale reference, a test double)
    falls back to a stable hash of the name so it never crashes and
    still lands the same side every time."""
    if not agent_name:
        return "claude"  # no single agent resolved (a free top-level chat) — Claude by default
    mapped = _agent_split_map().get(agent_name)
    if mapped is not None:
        return mapped
    import hashlib

    digest = hashlib.sha256(agent_name.encode("utf-8")).hexdigest()
    return "claude" if int(digest, 16) % 2 == 0 else "ollama_cloud"


def _ollama_cloud_config() -> OllamaConfig:
    """A real OllamaConfig pointed at ollama.com instead of localhost —
    OllamaNativeClient already supports a Bearer-auth base_url (see its
    own __init__ comment); this just supplies the real endpoint + key.
    Honest degrade if the key isn't set: OllamaNativeClient still builds
    (no exception here — Rule 2.1's "never fabricate" concern is about
    the ANSWER, not the client construction), and the real request then
    fails with ollama.com's own real 401, surfaced through the same
    "reported honestly" path every other backend failure already uses.
    """
    return OllamaConfig(
        api_key=os.environ.get("OLLAMA_API_KEY", "").strip(),
        base_url=_OLLAMA_CLOUD_BASE_URL,
        model=os.environ.get("OLLAMA_CLOUD_MODEL", "").strip() or _OLLAMA_CLOUD_DEFAULT_MODEL,
    )


def _claude_orchestrator_cwd() -> str:
    """A REAL, STABLE directory, deliberately separate from the CODE
    screen's own code_claude session (which uses the project root) —
    Claude CLI session continuity is keyed by cwd (code_backends.py's own
    _claude_session_key), and mixing "answer general Dourmouse
    directives" history with "help me code" history under one
    conversation would confuse both."""
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent
    path = project_root / "workspace" / "claude_orchestrator"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


_CLAUDE_ORCHESTRATOR_FRAMING = (
    "[Dourmouse assistant — you have a live MCP server called 'dourmouse' "
    "connected with real tools: mail, tasks, world/news data, code "
    "execution, and more (tool names start with mcp__dourmouse__). Use "
    "ONLY those dourmouse tools for this request — you may have other "
    "MCP integrations (Gmail, etc.) configured on this account from "
    "unrelated contexts; ignore them here, they are not what this "
    "session is asking about. Use a real dourmouse tool whenever the "
    "request needs real data or a real action. Never fabricate a "
    "result. Be direct — answer first, skip preamble.]\n\n"
)


class ClaudeCliClient:
    """chat.completions.create()-shaped adapter routing through the real
    Claude Code CLI. See the module comment above this class for the
    full rationale."""

    def __init__(self, cwd: str | None = None, timeout: int = 180) -> None:
        self.cwd = cwd or _claude_orchestrator_cwd()
        self.timeout = timeout
        self.chat = type("_Chat", (), {"completions": _ClaudeCliCompletions(self)})()

    def _create(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> Any:
        from dourmouse import code_backends

        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        prompt = _CLAUDE_ORCHESTRATOR_FRAMING + str(last_user)
        try:
            text = code_backends.run_code_task("claude", prompt, cwd=self.cwd, timeout=self.timeout)
        except RuntimeError as exc:
            # Honest failure, not a fabricated reply (Rule 2.1/2.2) — the
            # SAME "reported honestly" contract every code_* tool already
            # uses, surfaced as the model's own answer since there is no
            # tool_result slot to put it in at this layer.
            text = f"CLAUDE ORCHESTRATOR (reported honestly): {exc}"
        message = _OllamaMessage(text, None)  # Claude used MCP internally; no Dourmouse-side tool_calls
        if stream:
            return iter([_OllamaChunk(_OllamaDelta(content=text))])
        return _OllamaResponse(message)


class _ClaudeCliCompletions:
    def __init__(self, client: "ClaudeCliClient") -> None:
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
        return self._client._create(model=model, messages=messages, tools=tools, max_tokens=max_tokens, stream=stream)


class JobTracker:
    """Bounded, thread-safe audit log of delegated (nested) agent runs.

    Every delegate_task spawns a job: id, parent, task, target subagent,
    depth, status, timestamps, and the REAL result/error text. This is the
    institutional audit tree — the UI renders it as the DELEGATED TASKS
    panel and it is how a multi-agent run can be traced afterwards.
    Statuses: running -> done | error | refused.
    """

    _MAX_JOBS = 500

    def __init__(self, chime_fn: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._jobs: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next = 0
        # v13.5 (Vision OS checklist item 6, "proactive audio interruption
        # & contextual chimes"): an optional real hook, called with the
        # finished job's own snapshot dict whenever a TOP-LEVEL
        # (depth == 0) delegated job finishes/errors/is refused — the
        # real "a background automation pipeline finished or failed"
        # event this codebase actually has. depth == 0 only (not every
        # nested sub-branch a delegate_parallel fan-out spawns) so this
        # never turns into a chime storm for one real background task.
        # Same pure-observer discipline as event_sink/_emit_event
        # elsewhere: wrapped so a raising chime_fn can never break the
        # real job bookkeeping — see the call sites below.
        self._chime_fn = chime_fn

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
        finished: dict[str, Any] | None = None
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    job["status"] = "error" if error else "done"
                    job["finished_at"] = _now_iso()
                    job["result"] = (result or "")[:800]
                    job["error"] = (error or "")[:800]
                    finished = dict(job)
                    break
        self._maybe_chime(finished)

    def refuse(self, job_id: str, reason: str) -> None:
        finished: dict[str, Any] | None = None
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    job["status"] = "refused"
                    job["finished_at"] = _now_iso()
                    job["error"] = reason[:800]
                    finished = dict(job)
                    break
        self._maybe_chime(finished)

    def _maybe_chime(self, job: dict[str, Any] | None) -> None:
        if job is None or self._chime_fn is None or job.get("depth") != 0:
            return
        try:
            self._chime_fn(job)
        except Exception:  # noqa: BLE001 - a chime must never break job bookkeeping
            pass

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
    # v13.5 "stop/directive bug" fix: threaded alongside event_sink so the
    # inner turn loop (which reads everything off ctx, not the outer
    # function's own parameters) can actually see it — see
    # run_dispatch_messages' should_stop docstring paragraph.
    should_stop: Callable[[], bool] | None = None
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
    # v5.30: server fast lane. True when this run's first completion should
    # go to the compute node (Dell) with automatic local fallback;
    # ``server_fallback_model`` is the local fast model used when the node
    # is down. The brain label stays honest: it reports the Dell model.
    server_lane: bool = False
    server_fallback_model: str = ""
    # v8.10: this turn is a LOOKUP — the API boundary marks the user turn
    # with the brevity rule. Set once in dispatch() from the deterministic
    # prompt shape, and inherited by nested runs so a delegate answering a
    # lookup does not write the essay on the parent's behalf.
    brief: bool = False
    # v8.18: this turn arrived on the VOICE channel (spoken, not typed) — the
    # API boundary marks the user turn with the voice-reply rule (no
    # markdown structure, spoken-plain confirmations, one question instead
    # of an enumerated list). Unlike ``brief`` this is never inferred from
    # the prompt text itself (nothing about the words distinguishes a
    # spoken request from a typed one) — it is set once from the caller's
    # explicit channel flag and inherited by nested runs for the same
    # reason ``brief`` is: a delegate answering on behalf of a voice turn
    # must not hand back a table the parent cannot speak.
    voice: bool = False
    # v8.12: hard-scopes this run to exactly one subagent — see the
    # forced_agent docstring on run_dispatch_messages for why. NOT
    # inherited by further nesting (unlike brief): a forced-agent run's
    # own delegate_task is already absent from its scoped tools (it only
    # owns whichever ONE agent it was forced to), so there is nothing to
    # propagate.
    forced_agent: str | None = None
    # v13: Grounded Mode (config.grounded_mode_enabled()) for THIS run — a
    # user-controllable setting, not inferred from the prompt. See
    # _MAX_GROUNDED_NUDGES's own comment for the mechanism and the live bug
    # this exists to catch (an agent with real tools available answering
    # with zero of them, presented as if it had researched). Inherited by
    # nested delegate runs the same way ``voice``/``brief`` are — a
    # delegate answering on grounded mode's behalf must honor it too.
    grounded: bool = False
    # v8.30: True once a model has been deliberately chosen for this run —
    # an explicit caller override, the fast lane's own cheap/fast pick, or
    # brain escalation — and must NOT be second-guessed further downstream.
    # False only for the genuinely generic default-model case, which is
    # exactly when the per-agent refinement below (once plan_agents is
    # known) is allowed to act. Two of the three per-agent-model paths
    # already existed before this: an explicit focus_agent route
    # (webui.py) and a delegate_task nested run both already resolve
    # model_for_agent(target) BEFORE calling run_dispatch_messages, so
    # ctx.model_pinned is True for both and this flag correctly leaves them
    # alone. The one gap was the plain auto-routed top-level call, where
    # the target agent literally is not known until AFTER build_plan /
    # find_agents_for_query run — this field is what lets that case get
    # refined too, without touching the two paths already working.
    model_pinned: bool = False

    def delegates_used(self) -> int:
        return self.budget[0]

    def consume_delegate(self) -> bool:
        """Atomically claim one delegation budget slot."""
        if self.budget[0] >= self.max_delegates:
            return False
        self.budget[0] += 1
        return True


def _registry_ctx_stack(registry: DispatchRegistry) -> list[DispatchContext]:
    """The nesting stack of in-flight DispatchContexts for this registry —
    always THIS THREAD's own stack, never shared with any other thread.

    v8.30 and earlier: this was a single plain list (``registry._ctx_stack``)
    under the documented INVARIANT "at most ONE in-flight run per registry
    at any instant" — true as long as nesting was purely synchronous (a
    delegate_task run runs to completion inside the parent's own call
    stack before the parent's loop resumes). v8.31's delegate_parallel
    breaks that invariant on purpose: several nested run_dispatch_messages
    calls are now genuinely in flight AT ONCE, on separate threads, against
    the SAME registry object. A single shared list would let one thread's
    push/pop interleave with another's, so ``current_dispatch_context``
    could hand a tool call in branch A the context that actually belongs
    to branch B — the delegate_task/delegate_parallel handler inside a
    parallel branch would then read the WRONG budget/depth/client. A
    ``threading.local`` gives each thread its own private stack instead:
    within one thread the old synchronous-nesting invariant still holds
    exactly as before (that thread's own stack is still "at most one
    in-flight run's worth of nesting"), and different threads simply never
    see each other's stacks.
    """
    local = getattr(registry, "_ctx_stack_local", None)
    if local is None:
        local = threading.local()
        registry._ctx_stack_local = local
    stack = getattr(local, "stack", None)
    if stack is None:
        stack = []
        local.stack = stack
    return stack


def current_dispatch_context(registry: DispatchRegistry) -> DispatchContext | None:
    """The active dispatch context for a registry, or None outside a run
    ON THIS THREAD (see ``_registry_ctx_stack`` for why this is now
    per-thread rather than per-registry).

    run_dispatch_messages pushes one context per (possibly nested) run; the
    delegate_task/delegate_parallel handlers read the top of the calling
    thread's stack to spawn their nested run(s) with the parent's
    client/gate/sink. Synchronous nesting within one thread means that
    thread's stack top is always its own in-flight run's context.
    """
    stack = _registry_ctx_stack(registry)
    if not stack:
        return None
    return stack[-1]


def system_message(
    registry: DispatchRegistry, focus: set[str] | None = None
) -> str:
    """The immutable system prompt for a registry (persona + roster).

    Shared by run_dispatch and chat.ChatSession so a conversation always
    carries the same instructions and tool list.

    ``focus`` (optional) names the agents this turn actually plans to use.
    When given, the roster shows those agents in full and collapses the rest
    to a name list — same contract, far less prefill. Omitted, the prompt is
    byte-identical to every earlier version.
    """
    return _SYSTEM_PROMPT + "\n\nROSTER:\n" + registry.describe_roster(focus)


def _fast_lane_model_is_servable(client: Any) -> bool:
    """Whether DOURMOUSE_FAST_MODEL can actually be served by `client`.

    The fast lane swaps in a small *local* model name (default qwen3:4b) to
    get the first token out sooner. That is only meaningful when the client
    is the local Ollama daemon. Against a hosted backend the name is simply
    unknown, and the request comes back "404 page not found" — which is what
    every short question did on a machine configured for NVIDIA, because the
    swap happened unconditionally.

    A hosted backend does not need the lane anyway: the measured p50 there is
    ~1.1s, faster than the local small model. So when the client is not
    local, keep the primary model and let the lane's other savings (the
    compact system prompt) still apply.
    """
    if isinstance(client, OllamaNativeClient):
        return True
    base_url = str(getattr(client, "base_url", "") or "").lower()
    if not base_url:
        # An unrecognised or test double: assume the historical behaviour so
        # engine tests that assert the swap keep passing.
        return True
    return "127.0.0.1" in base_url or "localhost" in base_url or ":11434" in base_url


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
    experience_sink: Callable[[dict[str, Any]], None] | None = None,
    session_stem: str | None = None,
    forced_agent: str | None = None,
    voice: bool = False,
    should_stop: Callable[[], bool] | None = None,
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

    v13.5 (live-caught, real bug — "the stop/directive bug"): ``should_stop``
    is a real cancellation check, same house pattern as ``cost_budget`` —
    called at the top of every turn AND before every individual tool call
    within a turn, so a user hitting STOP mid-multi-tool-call turn (not just
    between turns) still lands honestly on a ``stopped_by_user`` event
    instead of the loop running to completion regardless. Before this
    existed, there was NO channel from the SSE stream ending back into this
    loop at all: webui.py's ``_SSEStream.emit()`` already detected a dead
    client socket (BrokenPipeError/ConnectionResetError on write, which is
    exactly what a clicked STOP produces via the aborted fetch) but
    silently discarded that fact ("client went away; loop continues
    harmlessly") — the CLIENT stopped rendering, but the SERVER thread kept
    holding session_lock and running the full remaining ``max_turns``,
    burning real tool calls/tokens/cost and blocking every other queued
    request on the same session lock the whole time. STOP only ever
    actually interrupted a run that happened to be blocked inside
    WebConfirmationGate.wait() (the v13.2 fix) — an ordinary tool-calling
    run with no pending approval had no way to be told to stop at all. This
    is that channel: ``event_sink`` stays a pure, never-raising observer
    (unchanged, ``_emit_event`` still swallows everything a sink raises) —
    cancellation is its own explicit, polled predicate, not repurposed
    exception plumbing through the observer. Not threaded into delegate_task
    / delegate_parallel's own nested recursive runs (real, stated scope
    limit, not silently assumed away): those already don't stream events at
    depth>0 and are separately bounded by ``max_delegates``/``max_depth``.

    v5.6 neural orchestration: ``experience_sink`` (optional, called ONCE per
    TOP-LEVEL run with a self-supervised experience record — the prompt, the
    agents whose tools were actually used, and how cleanly the run ended) is
    how the system learns from its own orchestration. ``session_stem`` ties
    the record to a session so operator 👍/👎 ratings can reweight it. The
    sink is a pure observer like ``event_sink``: it must never break
    execution, and nested delegate runs never log (only depth 0 does).

    v8.12: ``forced_agent`` hard-scopes this run to exactly one subagent's
    tools, bypassing build_plan/find_agents_for_query entirely. Only
    delegate_task's own ROUTING DIRECTIVE nested runs pass it — the
    directive text already says "using ONLY the 'X' subagent", so asking
    the general planner to re-derive that from the sentence is redundant
    and, worse, fragile: a task description with commas in it (a plain
    list of features to cover) can fool build_plan's multi-step fallback
    splitter into cutting the directive into nonsense fragments routed to
    the WRONG agents — traced live, "using ONLY the 'research_info'
    subagent... covering features, performance, ease of use, hardware
    requirements..." got split on those commas into fragments scored
    against 'tasks' and 'dev_coding', so research_info's own web_search
    tool was never even offered. The nested run then had nothing but
    delegate_task available (orchestrator's own tool, never scoped out)
    and recursed into itself until the depth-3 guard refused it, returning
    no answer after 145s. forced_agent makes the explicit directive
    authoritative instead of re-guessed.

    v8.18: ``voice`` marks this turn as arriving on the voice channel (the
    caller transcribed it from speech and will speak the reply back), so
    the API boundary appends the voice-reply rule alongside (not instead
    of) the brevity rule. Explicit and caller-supplied, unlike ``brief``,
    because nothing in the prompt's own words says whether it was typed or
    spoken — see the DispatchContext.voice docstring for why it still
    inherits down through nested runs the same way ``brief`` does.
    """
    # v3.1 per-agent models: an explicit ``model`` override (e.g. a nested
    # delegate resolved to its target subagent's model) wins over the
    # config default. Deterministic (Rule 2.8) — the caller picks the model;
    # the engine never guesses.
    escalated_brain = False
    _explicit_model = model  # the caller's override, captured before resolution
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    if client is None:
        config = config or load_llm_config_with_fallback()
        client = _build_client(config, forced_agent=_effective_split_agent(forced_agent, last_user, registry))
        # v5.0 fast dispatch: the orchestrator (looping dispatch brain)
        # defaults to its per-agent model (qwen3:4b on the local backend) so
        # every turn is fast; explicit ``model`` overrides still win.
        # v5.5 brain escalation: multi-step prompts use the full default
        # brain; simple chat stays on the fast orchestrator brain.
        fast = (
            config.model_for_agent("orchestrator")
            if hasattr(config, "model_for_agent")
            else config.model
        )
        # ``config`` is typed NvidiaConfig | None at the parameter boundary;
        # getattr avoids the narrowing mypy can't do after the pre-existing
        # ``config = config or load_llm_config()`` reassignment above.
        default_brain = str(getattr(config, "model", "test-model"))
        model, escalated_brain = _resolve_brain_model(
            fast=fast, default=default_brain, prompt=str(last_user), explicit=model
        )
    else:
        model = model or (config.model if config is not None else "test-model")
    # Fast lane (v5.x): a PURE-CHAT turn — no plan and no agent match (the
    # loop's exact scoped-tools condition, deterministic and LLM-free) — is
    # the "simple response" case. It answers in ONE completion with a compact
    # system prompt (no 21-agent roster) instead of looping, so "2+2" and
    # knowledge questions return in seconds. Anything agentic keeps the
    # resolved brain and the full loop; an explicit model override (v3.1
    # per-agent) always wins. Opt out with DOURMOUSE_FAST_LANE=0, pick the
    # model with DOURMOUSE_FAST_MODEL.
    fast_lane = (
        _explicit_model is None
        and not escalated_brain
        and fast_lane_enabled()
        and _is_pure_chat(str(last_user), registry)
    )
    # v5.30: when the compute node (Dell) is EXPLICITLY configured and a
    # FRESH cached probe says online, the fast lane's single completion goes
    # to the Dell (Qwen3 1.7B — smaller than the local fast model, so the
    # first token lands sooner and the M3 stays free). server_online_cached
    # NEVER probes, so a dead node costs zero extra latency: the lane just
    # stays local. Any failure falls back to the local fast model inside the
    # loop, and the brain label reports the Dell model only when the lane
    # actually engaged. Only pure chat reaches the lane; agentic turns keep
    # the resolved brain + full loop.
    server_lane = False
    if fast_lane and _fast_lane_model_is_servable(client) and fast_lane_model_swap_enabled():
        model = fast_lane_model()
        from dourmouse.remote_server import server_model, server_online_cached, server_url_configured

        server_lane = (
            fast_lane_server_enabled()
            and server_url_configured()
            and server_online_cached()
        )
        if server_lane:
            model = f"server:{server_model()}"
    # world-monitor-expansion (UX pass item 1): real backend identity for
    # the console's per-response model/local indicator. The Dell compute
    # node (server_lane) is a special case backend_identity() can't see
    # from ``config`` alone — the config object is whatever the MAIN
    # backend is (often NVIDIA), but the Dell literally runs Ollama (see
    # remote_server.py's own docstring: "MAIN DOURMOUSE -> ... -> Ollama"),
    # so it is reported local exactly like any other Ollama call.
    if server_lane:
        backend_name, backend_local = "ollama", True
    else:
        backend_name, backend_local = backend_identity(config)
    # v13 (real bug, self-caught): the orchestrator-backend experiment
    # swaps the CLIENT in _build_client() but backend_identity() above
    # only ever looks at `config`, which never changes — so the "brain"
    # event kept reporting "ollama/qwen2.5:7b" even while a request was
    # genuinely answered by Claude (verified live: a real ~/.claude
    # session file was written at the exact request timestamp, and the
    # answer arrived in ~7s — far faster than qwen2.5:7b's real ~20-70s
    # floor for the same prompt). Report the REAL answering backend
    # honestly instead of the stale config-derived guess.
    _orch_mode = _orchestrator_backend_mode()
    if _orch_mode == "split":
        # Same effective-agent peek _build_client() used above to pick
        # the REAL client — must agree, or this event would report a
        # different backend than the one actually built.
        _orch_mode = _agent_split_backend(_effective_split_agent(forced_agent, last_user, registry))
    if _orch_mode in ("claude", "claude_cli"):
        model, backend_name, backend_local = "claude-sonnet-5 (CLI)", "claude_cli", False
    elif _orch_mode in ("ollama_cloud", "cloud"):
        model, backend_name, backend_local = _OLLAMA_CLOUD_DEFAULT_MODEL, "ollama_cloud", False
    # The chosen brain is surfaced honestly so the UI can show which model
    # actually answered (Rule 2.1) — fast vs heavy per run. Only at the top
    # of the tree: nested delegate runs ride the parent's event sink, so a
    # delegate's model must never clobber the top-level brain indicator
    # (reviewer-caught; the loop already streams assistant_delta only at
    # depth 0 for the same reason).
    if event_sink is not None and depth == 0:
        _emit_event(
            event_sink,
            {
                "type": "brain",
                "model": model,
                "escalated": escalated_brain,
                "backend": backend_name,
                "local": backend_local,
            },
        )

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
        should_stop=should_stop,
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
        forced_agent=forced_agent,
        # v8.30: pinned whenever anything more specific than the plain
        # generic default already claimed this model — an explicit caller
        # override, brain escalation, or the fast lane's own deliberate
        # cheap/fast pick. Only the genuinely generic case is left open for
        # the per-agent refinement further down the loop.
        model_pinned=not (
            _explicit_model is None and not escalated_brain and not fast_lane
        ),
    )
    # INVARIANT: at most ONE in-flight run per registry PER THREAD at any
    # instant. The webui guarantees this for the top-level chat path by
    # serializing chat under session_lock, and nesting via delegate_task is
    # synchronous (a delegate runs to completion inside the parent's loop)
    # — so within any ONE thread, stack[-1] is always that thread's
    # in-flight run's context. v8.31's delegate_parallel deliberately runs
    # several such (synchronous-within-themselves) nested runs concurrently
    # on DIFFERENT threads, which is exactly why the stack itself is now
    # thread-local (see _registry_ctx_stack) rather than shared.
    stack: list[DispatchContext] = _registry_ctx_stack(registry)
    # v8.10 brevity: a lookup-shaped prompt answers short. A nested run also
    # inherits the parent's brief flag — a delegate writing three paragraphs
    # back into a brief parent turn just moves the essay one level down.
    if brief_mode_enabled():
        ctx.brief = _is_brief_intent(str(last_user)) or (
            bool(stack) and stack[-1].brief
        )
    # v8.18: voice is an explicit channel flag (never inferred from the
    # prompt), but still inherits down the delegate stack like brief does —
    # a nested run started by delegate_task has no ``voice=`` of its own to
    # pass, so it picks up the parent's.
    ctx.voice = voice or (bool(stack) and stack[-1].voice)
    # v13: Grounded Mode is a persisted user SETTING, not a per-call param —
    # unlike voice/brief there is no caller-supplied override to OR against;
    # every top-level turn reads the current setting fresh (live, no
    # restart, same contract as orchestrator_model_setting()), and a nested
    # delegate run inherits it from its parent on the stack exactly like
    # voice/brief do.
    from dourmouse.config import grounded_mode_enabled

    ctx.grounded = grounded_mode_enabled() or (bool(stack) and stack[-1].grounded)
    if fast_lane:
        # The lane still runs the loop (pure-chat exits after ONE call since
        # tools are empty), but the API boundary sees the compact system
        # prompt instead of the 2.2k-token roster — the dominant prefill
        # cost on a fanless M3. The authoritative ``messages`` (persisted)
        # keeps the full prompt so an agentic turn later in the session
        # still routes tools correctly.
        ctx.compact_system = True
        if server_lane:
            ctx.server_lane = True
            ctx.server_fallback_model = fast_lane_model()
    stack.append(ctx)
    try:
        report = _run_dispatch_loop(messages, registry, max_turns, ctx)
    finally:
        stack.pop()
        # Note: unlike the old shared-list version, there is no
        # delattr/cleanup needed here — an emptied THREAD-LOCAL stack just
        # sits there as an empty list for that thread (cheap, and a worker
        # thread reused by a later ThreadPoolExecutor submission starts
        # from that same clean empty list, same as a brand new thread
        # would via threading.local()'s own per-thread default).
    # v5.6: one experience per TOP-LEVEL run feeds the neural orchestrator.
    # A raising sink must never abort the run — it is a pure observer.
    if depth == 0 and experience_sink is not None:
        try:
            record = _build_experience(messages, ctx, report, session_stem)
            if record is not None:
                experience_sink(record)
        except Exception:  # noqa: BLE001, S110 - a raising observer never breaks dispatch
            pass
    return report


def _build_experience(
    messages: list[dict[str, Any]],
    ctx: DispatchContext,
    report: dict[str, Any],
    session_stem: str | None,
) -> dict[str, Any] | None:
    """Derive one self-supervised orchestration experience from a finished run.

    Labels come from OUTCOMES, not opinions: the agents whose tools were
    actually used (``agents_used``), whether a plan existed, whether the run
    ended cleanly (no plan caveat, no tool errors, no max-turns / budget
    exhaustion, and a real final answer). Pure-chat turns log as multi-step
    negatives — the net learns that most prompts need no plan. Returns None
    only when there is no user prompt to learn from.
    """
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    prompt = str(last_user or "").strip()
    if not prompt:
        return None
    transcript: list[dict[str, Any]] = report.get("transcript") or []
    owner: dict[str, str] = {}
    for sub in ctx.registry.all_subagents():
        for tool in sub.tools:
            owner[tool.name] = sub.name
    tools_used = [e["name"] for e in transcript if e.get("type") == "tool_use"]
    agents_used: list[str] = []
    for t in tools_used:
        o = owner.get(t)
        if o and o not in agents_used:
            agents_used.append(o)
    agents_scoped: list[str] = []
    for e in transcript:
        if e.get("type") == "plan":
            for step in e.get("steps") or []:
                a = step.get("subagent")
                if a and a not in agents_scoped:
                    agents_scoped.append(a)
    has_caveat = any(
        e.get("type") == "plan_reminder"
        or (
            e.get("type") == "assistant_text"
            and "not executed via tools" in str(e.get("text") or "")
        )
        for e in transcript
    )
    tool_errors = any(
        e.get("type") == "tool_result"
        and str(e.get("text") or "").startswith(("ERROR", "REFUSED"))
        for e in transcript
    )
    exhausted = any(
        e.get("type") == "result" and e.get("is_error") for e in transcript
    )
    budget_hit = any(e.get("type") == "budget_exhausted" for e in transcript)
    final_text = str(report.get("final_text") or "").strip()
    return {
        "prompt": prompt,
        "ts": _now_iso(),
        "session_stem": session_stem,
        "plan_given": any(e.get("type") == "plan" for e in transcript),
        "tools_used": tools_used,
        "agents_used": agents_used,
        "agents_scoped": agents_scoped,
        "outcome_ok": bool(
            not has_caveat and not tool_errors and not exhausted
            and not budget_hit and final_text
        ),
        "model": ctx.model,
    }


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

# v13 Grounded Mode (config.grounded_mode_enabled(), off by default — see
# that function's own docstring for the live-reproduced bug this exists to
# catch): when a turn had real tools available (scoped_tools non-empty —
# some agent genuinely matched) but the model's first final answer used
# ZERO of them, give it exactly ONE honest chance to correct itself before
# accepting the answer as-is with a caveat. Deliberately only one, unlike
# the plan-checkpoint's own budget — this is a lighter-touch nudge for a
# turn that was never a multi-step plan in the first place, so an
# unresponsive model shouldn't burn multiple round-trips on it.
_MAX_GROUNDED_NUDGES = 1


# Knowledge questions the local model can answer directly from its weights.
# The fast lane takes them EVEN when a research/info agent matches, because
# routing a stable fact through web research adds seconds without improving
# the answer (the model knows it).
_KNOWLEDGE_CUES = (
    "what is", "what's", "who is", "who's", "when was", "when did",
    "where is", "where's", "why is", "why does", "how does", "how many",
    "capital of", "largest", "smallest", "tallest", "deepest", "oldest",
    "meaning of", "definition", "difference between", "explain",
    "example of", "synonym", "spell", "first letter",
)
# Live-data / action words that force the agentic path even inside a
# knowledge-shaped question (weather today, latest news, prices, email...).
_LIVE_DATA_WORDS = (
    "news", "market", "stock", "price", "forex", "crypto", "email",
    "inbox", "mail", "weather", "forecast", "temperature", "today",
    "latest", "breaking", "play", "track", "song", "music", "task",
    "todo", "code", "file", "folder", "scan", "backup", "schedule",
    "meeting", "calendar", "send", "draft", "status", "report",
    "digest", "score", "result", "election", "match", "game",
)


# Compact system prompt for PURE-CHAT fast-lane turns. The full orchestrator
# prompt carries a ~2.2k-token roster of 21 agents that a no-tools answer
# never uses — prefill of that prompt is most of the latency on a fanless
# M3 (~160 tok/s). The fast lane sends this style-only prompt instead,
# keeping the response-quality rules that actually govern chat output.
_FAST_LANE_SYSTEM = (
    "You are Dourmouse, a concise personal assistant. Answer directly and "
    "warmly: the headline in one or two sentences, then detail if the "
    "question needs it. No preamble, no meta-commentary, no emojis unless "
    "asked. Never start with 'Okay,', 'Hmm,', 'Let me', or 'I think'. Do not "
    "restate the question. If you do not know, say so in one sentence. Never "
    "claim to have done something you did not do. Long-term memory appears "
    "below as REMEMBERED CONTEXT when relevant."
)


# v8.10 "stop the essays". Measured on the live desktop against the 120B
# brain: tool lookups already answer in 16-18 words, but a QUESTION-shaped
# turn pads a correct one-line answer out to an article — "how do I list
# files in a folder on windows" returned 187 words across four headed
# sections (File Explorer / cmd / PowerShell / the list_path tool), and
# "explain what a virtual environment is" returned 113. Both wanted two
# sentences. The system prompt's rule 8 already says "concise" -- buried in
# a 2.2k-token prompt, that reads as advice and produced those numbers.
#
# The rule rides the LAST USER MESSAGE, not the system prompt — the same placement
# and the same reason as _NO_THINK_TOKEN above, measured the same way. Two
# earlier placements were tried and rejected on this box:
#   * appended to the system prompt: obeyed only sometimes (two identical
#     runs of six lookups gave medians of 32 and 94 words);
#   * as its own trailing system message: the model treated the rule as a
#     TASK and deliberated about it in the answer — "We need to answer: what
#     is a REST API. Follow constraints: LOOKUP, at most 3 sentences..."
#     shipped as 171 words of visible reasoning.
# Hence one short clause in parentheses: a constraint on the reply, with
# nothing in it worth planning about.
# NO NUMBERS IN THIS STRING. A word/sentence budget reads as a puzzle to a
# reasoning-tuned model: given "under 60 words" this brain wrote the answer,
# then counted it out loud in the reply -- "Word count: Let's count. A(1)
# REST2 API3 ..." -- 202 words of visible arithmetic. Qualitative wording
# asks for the same thing with nothing to verify.
_BRIEF_MARKER = "(Be brief: answer directly in a sentence or two, no headings or lists.)"


def _append_brief(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of ``messages`` with the brevity marker on the last user turn.

    Mirrors ``_append_no_think`` exactly, including the copy-don't-mutate
    discipline: this runs at the API boundary, and the authoritative list
    stays clean so the marker is never persisted into the session history.
    """
    out = [dict(m) for m in messages]
    for msg in reversed(out):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and _BRIEF_MARKER not in content:
            msg["content"] = f"{content}\n\n{_BRIEF_MARKER}"
        break
    return out


# v8.18: the voice-channel reply rule. Placed on the last user turn for the
# exact reason documented on _BRIEF_MARKER above and pinned by
# TestBoundaryPlacement in test_brief_intent.py: appended to the system
# prompt instead, this brain treats it as an optional style note and
# follows it inconsistently; on the last user turn (the same spot
# _NO_THINK_TOKEN and _BRIEF_MARKER already use) it holds turn over turn.
# So although the task that motivated this is "a channel-aware
# system-prompt addendum", it is implemented with the mechanism already
# proven to work on this backend rather than the one proven not to.
# NO NUMBERS IN THIS STRING for the same reason _BRIEF_MARKER has none: a
# reasoning-tuned model asked for "1-2 sentences" has been observed
# counting the sentences out loud in the reply instead of just giving one.
_VOICE_MARKER = (
    "(This reply will be spoken aloud, not read: no markdown, no headings, "
    "no tables, no code blocks, no bullet or numbered lists -- plain "
    "spoken sentences only, code read out as plain words not symbols. Keep "
    "it to a sentence or two unless the content genuinely needs more. "
    "State a confirmation plainly, like done or sent or found three, want "
    "me to read them, instead of showing it silently. If something is "
    "unclear, ask one plain question instead of listing options.)"
)


def _append_voice(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of ``messages`` with the voice-reply marker on the last user turn.

    Mirrors ``_append_brief`` exactly (same copy-don't-mutate boundary
    discipline), and the two stack: a brief AND spoken turn gets both
    markers appended to the same copy, never persisted into history.
    """
    out = [dict(m) for m in messages]
    for msg in reversed(out):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and _VOICE_MARKER not in content:
            msg["content"] = f"{content}\n\n{_VOICE_MARKER}"
        break
    return out


def _maybe_ingest_memory(
    depth: int, question: str, answer: str, plan_agents: set[str]
) -> None:
    """Real ingestion into global memory — the other half of retrieval
    above. OFF by default (see global_memory.global_memory_enabled), and
    only at depth 0: a nested delegate_task run answering on behalf of the
    parent would otherwise double-store the same real exchange twice.
    Tags each stored turn with the resolved plan_agents (which real agent
    actually handled it) rather than a UI screen name — dispatch.py has no
    notion of which tab was open, but it DOES know which agent ran, which
    is a more meaningful tag for later retrieval anyway. Swallows its own
    failures (Rule: an observer must never break the turn it's observing)
    and never stores an empty answer — nothing to recall from silence."""
    if depth != 0 or not answer.strip():
        return
    from dourmouse.global_memory import global_memory_enabled

    if not global_memory_enabled():
        return
    try:
        from dourmouse.global_memory import get_default_memory

        screen = next(iter(plan_agents), "")
        get_default_memory().add(f"Q: {question}\nA: {answer}", screen=screen)
    except Exception:  # noqa: BLE001 - ingestion must never break a turn
        pass


def _append_memory_context(messages: list[dict[str, Any]], context_block: str) -> list[dict[str, Any]]:
    """Copy of ``messages`` with real retrieved memory prepended to the
    last user turn — same copy-don't-mutate, last-user-turn-not-system-
    prompt boundary discipline as ``_append_brief``/``_append_voice``
    (this backend follows a short instruction on the last user turn
    reliably; the same instruction in the system prompt gets followed only
    inconsistently, already proven and pinned by this codebase's own
    tests). ``context_block`` is real, retrieved text from
    global_memory.GlobalMemory.retrieve_context_for_prompt() — this
    function never fabricates or pads it; an empty block is simply not
    injected (see the caller)."""
    out = [dict(m) for m in messages]
    for msg in reversed(out):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and context_block not in content:
            msg["content"] = f"{context_block}\n\n{content}"
        break
    return out


# Prompt shapes that ARE lookups: a question wanting one fact or one method.
_BRIEF_CUES = (
    "what is", "what's", "whats", "what are", "who is", "who's",
    "when is", "when was", "when did", "where is", "where's", "which",
    "how do i", "how do you", "how to", "how does", "how much", "how many",
    "how long", "is it", "are there", "can i", "do i", "does it",
    "define", "meaning of", "definition of", "capital of",
    # Bare "explain X" is a lookup; "explain in detail" / "explain why" keep
    # their room via _VERBOSE_CUES, which is checked first.
    "explain", "what does", "what do",
)

# Shapes that want ROOM, checked first — they override the cues above so a
# request that asks for length is never squeezed. "explain" is deliberately
# NOT here: an unqualified "explain X" is a lookup; "explain X in detail"
# matches "in detail" below and keeps its room.
_VERBOSE_CUES = (
    "write", "draft", "compose", "essay", "article", "blog", "post",
    "report", "summary of the", "in detail", "detailed", "thorough",
    "comprehensive", "step by step", "step-by-step", "walk me through",
    "tutorial", "guide me", "compare", "comparison", "pros and cons",
    "trade-off", "tradeoff", "analyse", "analyze", "analysis", "review",
    "brainstorm", "ideas for", "options for", "plan for", "outline",
    "list all", "list every", "everything about", "deep dive",
    "research", "investigate", "explain in", "why did", "why does",
    "elaborate", "expand on", "more detail", "long", "full",
)


def _is_brief_intent(prompt: str) -> bool:
    """Deterministic lookup-shape test (Rule 2.8 — keywords, never an LLM
    judgement, so the same prompt classifies the same way twice).

    True only for a SHORT question that asks for one fact or one method.
    Anything that asks for length, or any prompt long enough to be carrying
    real detail of its own, is left alone — the failure mode to avoid is
    truncating work the user actually wanted, not missing one essay.
    """
    text = str(prompt or "").strip().lower()
    if not text:
        return False
    words = text.split()
    # A long prompt carries its own detail and usually wants a real answer.
    if len(words) > 25:
        return False
    if any(cue in text for cue in _VERBOSE_CUES):
        return False
    return any(cue in text for cue in _BRIEF_CUES)


def _is_pure_chat(prompt: str, registry: Any) -> bool:
    """Deterministic pure-chat check: no plan AND no agent match (score >= 3),
    plus a knowledge-question exemption so stable facts answer on the fast
    lane even when a research agent nominally matches.

    Mirrors the loop's scoped-tools condition (build_plan + the same
    find_agents_for_query threshold); the knowledge exemption only ever
    widens the fast lane, and when the fast lane is off the loop's own
    scoped-tools logic is unchanged.
    """
    prompt_l = str(prompt).lower()
    if build_plan(str(prompt), registry):
        return False
    from dourmouse.planner import find_agents_for_query

    agentic = any(
        m.get("score", 0) >= 3
        for m in find_agents_for_query(registry, str(prompt), limit=2)
    )
    if not agentic:
        return True
    # Agent matched, but a pure knowledge question with no live-data intent
    # answers faster (and just as well) on the local model.
    return any(cue in prompt_l for cue in _KNOWLEDGE_CUES) and not any(
        w in prompt_l for w in _LIVE_DATA_WORDS
    )


# v13 (real architecture fix): tools whose own result IS ALREADY a
# complete, real, natural-language answer from a genuine separate agent —
# code_claude/code_codex/code_deepseek/code_nvidia/code_ollama each shell
# out to (or API-call) a real coding backend and return its actual final
# text, not raw data needing interpretation. Handing that to the LOCAL
# ORCHESTRATOR MODEL for a second "final answer" pass can only re-narrate
# an already-complete answer through a weaker model — see the short-
# circuit below (forced_agent only) for where this is used and why.
_COMPLETE_ANSWER_TOOLS = {
    "code_claude", "code_codex", "code_deepseek", "code_nvidia", "code_ollama",
}


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
    should_stop = ctx.should_stop
    confirmation_gate = ctx.confirmation_gate
    cost_budget = ctx.cost_budget
    dlp = ctx.dlp
    rbac = ctx.rbac
    nudges = 0
    plan_reminders = 0
    grounded_nudges = 0
    # v13: repeat-call guard for expensive, session-stateful CLI delegates.
    # Live bug this fixes: a weak local orchestrator model (e.g. qwen2.5:7b,
    # the fallback once NVIDIA broke) regularly can't tell a completed
    # single-shot task apart from one still pending, and re-issues the exact
    # same claude_code/codex_code call a second time in the same turn. Each
    # of those tools now carries session continuity (--session-id/--resume),
    # so a blind re-run doesn't just waste ~40-80s re-spawning a real CLI —
    # it replays the SAME instruction into the SAME live conversation a
    # second time, and the model's own final answer then glues both
    # returned payloads together with no separator (observed live:
    # "OK-CLAUDEOK-CLAUDE" from one "say OK-CLAUDE" task). Only these two
    # tools are guarded — they're the only ones where identical (name,
    # arguments) is both detectable AND unambiguously wasteful to repeat;
    # arbitrary tools (email, search, clock) can legitimately want a second
    # real call with the same arguments.
    _DEDUP_GUARDED_TOOLS = {"claude_code", "codex_code"}
    _seen_tool_calls: dict[tuple[str, str], str] = {}
    # Deterministic tool->owner map: plans name SUBAGENTS but the model calls
    # TOOLS, so the loop ties them together to tell when a plan step has
    # actually been executed.
    tool_owner: dict[str, str] = {}
    for _sub in registry.all_subagents():
        for _t in _sub.tools:
            tool_owner[_t.name] = _sub.name

    def _budget_entry(reason: str) -> dict[str, Any]:
        return {"type": "budget_exhausted", "reason": reason}

    def _stop_entry() -> dict[str, Any]:
        return {"type": "stopped_by_user", "reason": "cancelled by the user"}

    # v2.0 Phase 2.1: for a multi-step prompt, emit a visible PLAN event
    # before executing (deterministic heuristic + subagent mapping, no extra
    # LLM call). It rides the transcript, so the UI renders it as a [PLAN]
    # block and chat.py persists it to the session JSONL — audit trails for
    # arbitrary sessions, not just dev sessions.
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    # v8.12: forced_agent (delegate_task's own ROUTING DIRECTIVE nested runs)
    # bypasses build_plan entirely — the directive already says "using ONLY
    # the 'X' subagent", so re-deriving that from the sentence is redundant
    # and, worse, fragile against punctuation in the task description (see
    # the forced_agent docstring on run_dispatch_messages for the traced
    # live failure). No PLAN event either: a one-agent forced scope has
    # nothing plan-shaped to show.
    plan: list[dict[str, Any]] | None
    if ctx.forced_agent:
        plan = None
        plan_agents = {ctx.forced_agent}
    else:
        plan = build_plan(str(last_user), registry)
        if plan:
            plan_entry: dict[str, Any] = {"type": "plan", "steps": plan, "total": len(plan)}
            transcript.append(plan_entry)
            _emit_event(event_sink, plan_entry)
        # v4.1: scope the tool schemas to the plan's agents instead of
        # shipping all 60. Plain questions send no tools at all — the
        # biggest single latency lever (see _scoped_tool_specs).
        plan_agents = {step["subagent"] for step in (plan or [])}
        # v5.2: a SINGLE-step directive ("how much is BTC worth", "check my
        # inbox", "draft an email") has no plan, but it is still an AGENTIC
        # request — the model must see the target agent's tools or it
        # answers blind. When no plan exists, scope the tools of the
        # best-matching subagent(s) instead of sending zero schemas, so real
        # directives can actually execute. Pure chat (no agent match) still
        # sends nothing.
        if not plan_agents:
            from dourmouse.planner import find_agents_for_query

            matches = find_agents_for_query(registry, last_user, limit=2)
            plan_agents = {
                m["name"] for m in matches if m["score"] >= 3
            }
    scoped_tools = (
        _scoped_tool_specs(registry, plan_agents, include_delegate=not ctx.forced_agent)
        if plan_agents
        else []
    )

    # v8.30: per-agent model routing for the ONE case it was never wired
    # for. An explicit focus_agent route and a delegate_task nested run
    # both already resolve model_for_agent(target) before ever reaching
    # here (see DispatchContext.model_pinned's docstring) — this is
    # specifically the plain auto-routed top-level call, where the target
    # agent genuinely isn't known until build_plan/find_agents_for_query
    # run, which is exactly what just happened above. Deliberately
    # conservative: only acts on a SINGLE, unambiguous plan_agents match —
    # a multi-agent plan keeps the already-resolved general-purpose model
    # rather than guessing which step's agent should own the WHOLE run.
    if (
        not ctx.model_pinned
        and ctx.config is not None
        and len(plan_agents) == 1
        and hasattr(ctx.config, "model_for_agent")
    ):
        routed_model = ctx.config.model_for_agent(next(iter(plan_agents)))
        if routed_model and routed_model != model:
            model = routed_model
            if event_sink is not None and ctx.depth == 0:
                # Same backend as the run's original "brain" event (the
                # config object doesn't change, only the model string
                # model_for_agent resolves within it) — backend_identity()
                # again, not re-guessed.
                _routed_backend, _routed_local = backend_identity(ctx.config)
                # v13 (self-caught, same real bug as the original "brain"
                # event fix above): this per-agent routing override ALSO
                # only ever looks at ctx.config, so it clobbered an
                # already-honest "claude_cli" brain event back to
                # "ollama"/the per-agent model string — purely cosmetic
                # (the actual `client` object, and therefore which
                # backend genuinely answers, is untouched by this block;
                # `model` here is just a label ClaudeCliClient/the
                # ollama_cloud client both ignore) but confusing and
                # dishonest about what's actually happening. Re-apply the
                # SAME orchestrator-mode override so this second event
                # can't un-say what the first one correctly reported.
                _orch_mode2 = _orchestrator_backend_mode()
                if _orch_mode2 == "split":
                    # ctx.forced_agent stays None for an ordinary
                    # planner-routed query (see _effective_split_agent's
                    # own docstring) — plan_agents (exactly one entry,
                    # this block's own guard condition) is what the
                    # CLIENT was actually built against in that case, so
                    # match it here rather than re-falling-back to "no
                    # agent" and reporting the wrong side of the split.
                    _orch_mode2 = _agent_split_backend(ctx.forced_agent or next(iter(plan_agents)))
                if _orch_mode2 in ("claude", "claude_cli"):
                    model, _routed_backend, _routed_local = "claude-sonnet-5 (CLI)", "claude_cli", False
                elif _orch_mode2 in ("ollama_cloud", "cloud"):
                    model, _routed_backend, _routed_local = _OLLAMA_CLOUD_DEFAULT_MODEL, "ollama_cloud", False
                _emit_event(
                    event_sink,
                    {
                        "type": "brain",
                        "model": model,
                        "escalated": False,
                        "backend": _routed_backend,
                        "local": _routed_local,
                    },
                )

    # v5.32: the schemas are scoped above, but the ROSTER PROSE in the system
    # message still described all 31 agents / 161 tools (~12.3k chars) on every
    # turn, while a typical directive touches one or two. Now that plan_agents
    # is known, precompute a focused system prompt — applied AT THE API
    # BOUNDARY below, next to the fast lane, so the authoritative messages stay
    # byte-identical for persistence and later turns.
    #
    # v8.31: bespoke per-agent system prompts (dourmouse/agent_prompts.py,
    # commit 3ae24a3) ride the SAME "resolves to exactly ONE agent"
    # detection v8.30's per-agent model routing already uses above — when
    # plan_agents is a single, unambiguous match AND that agent has a
    # hand-written prompt, splice it in ALONGSIDE (not instead of)
    # _SYSTEM_PROMPT: the base orchestrator rules carry real governance
    # (confirmation-gating, honest NOT CONFIGURED/REFUSED reporting, no
    # fabrication, response style) that every turn must keep regardless of
    # which agent is doing the work — dropping them for a bespoke persona
    # would be a regression, not a refinement. No bespoke prompt for the
    # agent, or more than one agent in play (nothing single to own the
    # prompt): fall back to the existing generic roster prompt exactly as
    # v8.30 left it.
    bespoke_agent_prompt = None
    if len(plan_agents) == 1:
        from dourmouse.agent_prompts import AGENT_SYSTEM_PROMPTS

        bespoke_agent_prompt = AGENT_SYSTEM_PROMPTS.get(next(iter(plan_agents)))
    if bespoke_agent_prompt:
        focused_system = (
            _SYSTEM_PROMPT
            + "\n\nAGENT-SPECIFIC INSTRUCTIONS (bespoke, extracted from "
            "agent prompts.pdf for this turn's single resolved agent):\n\n"
            + bespoke_agent_prompt
        )
    elif plan_agents:
        focused_system = system_message(registry, plan_agents)
    else:
        focused_system = None

    # v8.30: unified, embedding-based memory across every screen — real
    # retrieved past context auto-injected into the prompt, not a tool the
    # model has to remember to call (the same reason JARVIS's own per-agent
    # memory is injected rather than callable). OFF by default
    # (DOURMOUSE_GLOBAL_MEMORY unset) — see global_memory.py's own
    # docstring for why: this adds a real embedding call before every
    # top-level turn, and the specific Ollama embedding model it expects
    # has not been confirmed pulled on every deployment. Retrieved ONCE
    # here (not per loop iteration) since the question doesn't change
    # turn to turn within one run; only depth 0 (top-level), matching the
    # same reasoning ctx.compact_system/assistant_delta streaming already
    # uses — a nested delegate run gets the parent's own context, not a
    # second independent retrieval.
    memory_context = ""
    if ctx.depth == 0:
        from dourmouse.global_memory import global_memory_enabled

        if global_memory_enabled():
            try:
                from dourmouse.global_memory import get_default_memory

                memory_context = get_default_memory().retrieve_context_for_prompt(str(last_user))
            except Exception:  # noqa: BLE001 - memory retrieval must never break a turn
                memory_context = ""

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

        # v13.5 "stop/directive bug" fix — see run_dispatch_messages'
        # should_stop docstring paragraph above for the full diagnosis.
        # Checked here (between turns) AND again before each individual
        # tool call below (a turn can request several).
        if should_stop is not None and should_stop():
            entry = _stop_entry()
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
        on_thinking = None
        if ctx.depth == 0 and ctx.event_sink is not None and isinstance(
            client, (OpenAI, OllamaNativeClient)
        ):
            def on_delta(text: str) -> None:
                _emit_event(ctx.event_sink, {"type": "assistant_delta", "text": text})

            # v13.1: visible chain-of-thought — a real, separate SSE channel
            # (never concatenated into assistant_delta/buf) so the UI can
            # render reasoning tokens in their own block instead of them
            # either vanishing (old think:False) or leaking into the
            # answer text (the exact bug think:False existed to prevent).
            def on_thinking(text: str) -> None:
                _emit_event(ctx.event_sink, {"type": "thinking_delta", "text": text})

        # v13.1 (Aider port part 4/4): None unless 2+ NVIDIA accounts are
        # actually configured — see _nvidia_rotation_factory's own
        # docstring for why a single-account setup is completely
        # unaffected by this existing at all.
        client_factory = _nvidia_rotation_factory(client, ctx.config)

        # v4.2 speed: the LLM sees a bounded rolling window (system +
        # in-flight exchange + recent history), never the unbounded
        # conversation. The full list stays authoritative for persistence.
        bounded = _bounded_context(messages, _max_llm_tokens())
        # Fast lane (v5.x): pure-chat turns swap the 2.2k-token orchestrator
        # roster for the compact style-only prompt AT THE API BOUNDARY only.
        # The authoritative messages are untouched, so a later agentic turn
        # in the same session still routes tools from the full prompt.
        if (
            getattr(ctx, "compact_system", False)
            and bounded
            and bounded[0].get("role") == "system"
        ):
            bounded = [
                {"role": "system", "content": _FAST_LANE_SYSTEM}
            ] + bounded[1:]
        # v5.32 roster focus: same boundary trick for AGENTIC turns. The fast
        # lane above wins when both apply (it is the cheaper prompt), so this
        # only fires for real directives, swapping the all-31-agent roster for
        # one scoped to the planned agents (~60% fewer prompt chars).
        elif (
            focused_system
            and bounded
            and bounded[0].get("role") == "system"
            and bounded[0].get("content") == system_message(registry)
        ):
            bounded = [
                {"role": "system", "content": focused_system}
            ] + bounded[1:]
        # v8.10 brevity: mark the last user turn at the API boundary only —
        # the authoritative persisted ``messages`` never sees the marker.
        # Prompt only, deliberately NO token cap: this brain spends tokens on
        # reasoning before it emits content (the same property _NO_THINK_TOKEN
        # exists for), so a tight max_tokens does not shorten the answer, it
        # truncates it — measured as a reply cut mid-clause at "using standard
        # HTTP verbs (GET," and, on a tool turn, as raw deliberation shipped as
        # the answer. The standard cap still applies as it always did.
        if getattr(ctx, "brief", False) and bounded:
            bounded = _append_brief(bounded)
        # v8.18: same API-boundary trick for the voice channel — applied
        # after brief so a spoken lookup carries both markers. Text-channel
        # turns never see this (ctx.voice defaults False), so typed
        # behavior is unchanged.
        if getattr(ctx, "voice", False) and bounded:
            bounded = _append_voice(bounded)
        # v8.30: real retrieved memory, prepended after brief/voice so all
        # three can stack on the same turn without clobbering each other.
        if memory_context and bounded:
            bounded = _append_memory_context(bounded, memory_context)
        # v5.30: the server fast lane tries the Dell first; ANY failure
        # (unreachable, timeout, 500, malformed) falls back to the local
        # fast model — the node can never take the reply down.
        if getattr(ctx, "server_lane", False):
            try:
                from dourmouse.remote_server import DourmouseServerClient

                response = DourmouseServerClient().chat_completions_create(
                    messages=bounded
                )
            except Exception:  # noqa: BLE001 - any Dell failure -> local
                ctx.server_lane = False  # keep the rest of this run local
                if event_sink is not None:
                    _emit_event(
                        event_sink,
                        {
                            "type": "assistant_text",
                            "text": "[compute node offline — answered by the local fast model]",
                        },
                    )
                response = _call_with_retry(
                    client,
                    model=ctx.server_fallback_model or model,
                    messages=bounded,
                    tools=scoped_tools,
                    config=ctx.config,
                    on_delta=on_delta,
                    on_thinking=on_thinking,
                    event_sink=event_sink,
                    client_factory=client_factory,
                )
        else:
            response = _call_with_retry(
                client,
                model=model,
                messages=bounded,
                tools=scoped_tools,
                config=ctx.config,
                on_delta=on_delta,
                on_thinking=on_thinking,
                event_sink=event_sink,
                client_factory=client_factory,
            )
        message = response.choices[0].message

        # Record the call against the budget AFTER it succeeded, using real
        # request + response sizes (token estimate ~4 chars/token). The
        # model saw the BOUNDED copy, so account against that — the full
        # list would over-count in long sessions.
        if cost_budget is not None:
            resp_text = message.content or ""
            if getattr(message, "tool_calls", None):
                resp_text += json.dumps(
                    [tc.function.arguments for tc in message.tool_calls], default=str
                )
            cost_budget.record_call(bounded, resp_text)

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
            # v13 Grounded Mode (see _MAX_GROUNDED_NUDGES's own comment):
            # real tools were genuinely offered this turn (scoped_tools
            # non-empty — some agent actually matched) but the model never
            # called a single one. Live-reproduced root cause this guards:
            # a RESEARCH-routed turn answering a factual question from
            # stale parametric memory in zero tool calls, presented with no
            # indication it wasn't grounded. Off by default; only engages
            # when the user has explicitly turned Grounded Mode on.
            grounded_violation = (
                ctx.grounded and tools_used == 0 and bool(scoped_tools)
            )
            if grounded_violation and grounded_nudges < _MAX_GROUNDED_NUDGES:
                grounded_nudges += 1
                reminder = (
                    "[GROUNDED MODE] You answered with zero tool calls, but "
                    "real tools were available this turn. If this task "
                    "genuinely needs one (e.g. a live fact, current data, a "
                    "file, or a search) call it now. If no tool was "
                    "actually needed, say so explicitly and explain briefly "
                    "why, so the user can tell a deliberate no-tool answer "
                    "apart from one that skipped grounding by mistake."
                )
                messages.append({"role": "system", "content": reminder})
                reminder_entry = {"type": "grounded_reminder"}
                transcript.append(reminder_entry)
                _emit_event(event_sink, reminder_entry)
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
            # v13 Grounded Mode: the nudge budget above is spent and the
            # model STILL used zero tools despite real ones being offered.
            # Never let this pass silently as if it had been researched —
            # append the same kind of honest caveat the plan-based check
            # above uses, so the user can see this specific answer wasn't
            # grounded rather than trusting it at face value.
            if ctx.grounded and tools_used == 0 and bool(scoped_tools):
                # v13.2 (live-caught, real bug): when the grounded-mode nudge
                # above forced a SECOND completion call and that follow-up
                # answers with nothing new (common — the model already gave
                # its real answer the first time and has nothing to add),
                # `text` here is that follow-up's own EMPTY content.
                # Appending the disclaimer to it and returning would drop
                # the actual answer entirely — live-reproduced: a real
                # ~900-char essay's OWN assistant_text transcript entry
                # survived correctly, but final_text (what the session
                # ledger persists, and the ONLY thing a page reload uses to
                # rebuild the answer bubble — see restoreSession's own
                # comment on why it never replays assistant_text from the
                # transcript) ended up as JUST the 161-char disclaimer.
                # Recover the real answer from the transcript before this
                # empty follow-up overwrites it. Scoped tightly to this
                # exact case (a round-trip happened AND this call's own text
                # is empty) so the unrelated fabrication-correction nudges
                # above — which deliberately DISCARD an earlier wrong claim
                # — are never touched by this.
                if not text.strip() and grounded_nudges > 0:
                    prior = next(
                        (
                            e.get("text", "")
                            for e in reversed(transcript)
                            if e.get("type") == "assistant_text" and e.get("text", "").strip()
                        ),
                        "",
                    )
                    if prior:
                        text = prior
                text += (
                    "\n\n[DOURMOUSE: Grounded Mode was on and this answer used "
                    "zero tool calls despite real tools being available — "
                    "treat it as unverified, not as a live lookup result]"
                )
                if transcript and transcript[-1].get("type") == "assistant_text":
                    transcript[-1]["text"] = text
                if messages and messages[-1].get("role") == "assistant":
                    messages[-1]["content"] = text
            _maybe_ingest_memory(ctx.depth, str(last_user), text, plan_agents)
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
            # v13.5 "stop/directive bug" fix: re-checked before EACH tool
            # call, not just between turns — a single turn can request
            # several tool calls back to back, and STOP should not have to
            # wait for all of them to finish first.
            if should_stop is not None and should_stop():
                entry = _stop_entry()
                transcript.append(entry)
                _emit_event(event_sink, entry)
                return {"final_text": "", "transcript": transcript, "messages": messages}
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
                # v8.11 capability-denial guard: rule 10 of the system
                # prompt names AGENTS ("system", "dev_coding", ...) in the
                # same voice as tools, so an under-scoped turn regularly
                # calls the agent name itself as a bare tool. Observed live:
                # the model tried `system {"command":"df -h"}`, got the
                # generic unknown-tool error three times unchanged, then
                # gave up and told the user it lacked a capability it had —
                # the tool just was not the one it typed. Naming the real
                # tools inline lets it self-correct in the SAME turn instead
                # of retrying the same wrong name or surrendering.
                agent = registry.get_subagent(name)
                if agent is not None:
                    real_tools = ", ".join(t.name for t in agent.tools) or "none"
                    result_text = (
                        f"ERROR: '{name}' is an AGENT, not a tool — you cannot "
                        f"call it directly. Either call one of its real tools "
                        f"({real_tools}), or use delegate_task with "
                        f"subagent='{name}'."
                    )
                else:
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
                        dedup_key = (
                            (name, json.dumps(arguments, sort_keys=True, default=str))
                            if name in _DEDUP_GUARDED_TOOLS
                            else None
                        )
                        prior_result = (
                            _seen_tool_calls.get(dedup_key) if dedup_key else None
                        )
                        if prior_result is not None:
                            # Same tool + identical arguments already ran this
                            # turn — reuse the real prior result instead of
                            # replaying the instruction into the same live
                            # CLI session a second time (see the guard's
                            # docstring above the loop for the bug this
                            # prevents).
                            result_text = (
                                "[DOURMOUSE: identical call already made this "
                                f"turn — not re-running '{name}' a second time "
                                "with the same arguments; reusing that result]\n"
                                + prior_result
                            )
                        else:
                            try:
                                result_text = _execute_tool(
                                    spec, arguments, confirmation_gate, ledger=transcript
                                )
                            except Exception as exc:  # surface handler errors honestly
                                result_text = f"ERROR: tool '{name}' failed: {exc}"
                            else:
                                if dedup_key:
                                    _seen_tool_calls[dedup_key] = result_text

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

        # v13 (real architecture fix, see _COMPLETE_ANSWER_TOOLS's own
        # comment): a single forced_agent call to one of the CLI-backed
        # "complete answer" tools already returned a full, real answer
        # from a genuine separate agent (e.g. Claude Sonnet 5 via the
        # actual Claude Code CLI) — return it directly instead of paying
        # for a second full prefill+generation round trip just to have
        # the LOCAL orchestrator model re-narrate it, weaker, slower.
        # Live-measured: Claude's own correct answer got flattened into a
        # blander local-model paraphrase, +12s for strictly negative
        # value. Scoped tightly: forced_agent only (a CODE-screen
        # toolchain pick, never a general multi-tool orchestration where
        # the model may still need to combine several results), exactly
        # one tool call this turn, that exact tool, and a real result (an
        # honest NOT CONFIGURED/ERROR/REFUSED still goes back to the
        # model — those aren't answers, they're situations the model may
        # want to explain or react to).
        if (
            ctx.forced_agent
            and len(tool_calls) == 1
            and name in _COMPLETE_ANSWER_TOOLS
            and not result_text.startswith(("ERROR", "REFUSED", "NOT CONFIGURED"))
        ):
            _maybe_ingest_memory(ctx.depth, str(last_user), result_text, plan_agents)
            return {"final_text": result_text, "transcript": transcript, "messages": messages}

    # v8.28: the loop ran out of tool-call turns (max_turns) while the model
    # was still mid-research — observed live on a completely reasonable
    # question ("latest stable PyTorch version"): 7 web_search calls + 1
    # fetch_url, never once emitting text, then the old code below returned
    # final_text="" and the UI rendered a bare "No reply." after burning the
    # user's whole wait on real tool work with nothing to show for it. A
    # tool-budget cap must never be allowed to produce an EMPTY answer when
    # real research already happened — it has to force a synthesis instead.
    #
    # Fix: one last LLM call with NO tools available (tools=[] — the model
    # physically cannot call another one, so it is forced to answer in
    # text), plus an explicit instruction to use only what has already been
    # gathered. This reuses the same system-message-injection mechanism the
    # plan-checkpoint reminder above already uses, just for a different
    # trigger (turn exhaustion, not plan fixation).
    forced_entry = {
        "type": "budget_exhausted",
        "reason": "max_turns exceeded — forcing a synthesis answer from what was already gathered",
    }
    transcript.append(forced_entry)
    _emit_event(event_sink, forced_entry)
    forced_messages = messages + [
        {
            "role": "system",
            "content": (
                "[OUT OF TOOL BUDGET] You have used every tool call available "
                "for this turn. Do not attempt to call any more tools — none "
                "are available. Answer the user's original question RIGHT "
                "NOW using only what you already found above. If what you "
                "gathered is incomplete, give your best answer from it and "
                "say plainly what remains uncertain — never return an empty "
                "reply."
            ),
        }
    ]
    try:
        forced_response = _call_with_retry(
            client,
            model=model,
            messages=_bounded_context(forced_messages, _max_llm_tokens()),
            tools=[],  # no tools offered: the model cannot keep stalling on search
            config=ctx.config,
            event_sink=event_sink,
        )
        forced_text = forced_response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - the forced call itself must never crash the turn
        forced_text = ""
        _emit_event(
            event_sink,
            {"type": "assistant_text", "text": f"[forced synthesis call failed: {exc}]"},
        )

    if not forced_text.strip():
        # Even the forced, tool-free call came back empty (rare) — say so
        # honestly instead of a silent blank reply (Rule 2.2: no fabricated
        # success, but also no silent failure the user can't see).
        tool_names = [e["name"] for e in transcript if e.get("type") == "tool_use"]
        forced_text = (
            "I wasn't able to reach a complete answer within my tool budget "
            f"({len(tool_names)} tool call(s): {', '.join(tool_names) or 'none'}). "
            "Try rephrasing the question more narrowly, or ask again — a "
            "second attempt often converges faster."
        )

    if dlp is not None:
        forced_text, hits = dlp.redact(forced_text)
        if hits:
            forced_text += f"\n[DLP: {len(hits)} secret pattern(s) redacted from model text]"

    entry = {"type": "assistant_text", "text": forced_text}
    transcript.append(entry)
    _emit_event(event_sink, entry)
    # Keep the history well-formed for multi-turn chat: after a tool exchange
    # the next turn must NOT begin with a "user" message (OpenAI-compatible
    # APIs reject "tool" then "user" without an intervening assistant).
    messages.append({"role": "assistant", "content": forced_text})
    _maybe_ingest_memory(ctx.depth, str(last_user), forced_text, plan_agents)
    return {"final_text": forced_text, "transcript": transcript, "messages": messages}


def run_dispatch(
    prompt: str,
    registry: DispatchRegistry,
    max_turns: int = 8,
    client: Any | None = None,
    config: NvidiaConfig | None = None,
    confirmation_gate: Callable[[str], bool] | None = None,
    model: str | None = None,
    voice: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Send one request through the NVIDIA-backed general dispatcher.

    Single-shot convenience wrapper over run_dispatch_messages: builds a
    fresh system+user message list, runs the loop, and returns
    {"final_text", "transcript"}. ``client``/``config`` injectable for
    isolated testing; ``confirmation_gate`` is the human-in-the-loop hook.
    ``voice`` (v8.18) marks the turn as arriving on the voice channel — see
    run_dispatch_messages for what that changes. ``should_stop`` (v13.5)
    is the real cancellation predicate — see run_dispatch_messages' own
    docstring paragraph for the full "stop/directive bug" diagnosis.
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
        voice=voice,
        should_stop=should_stop,
        # The CLI is a learning surface too: log the single-shot run so the
        # neural orchestrator learns from it.
        experience_sink=(
            None if not orch_net_enabled() else _cli_experience_sink(registry)
        ),
    )
    return {"final_text": report["final_text"], "transcript": report["transcript"]}


def orch_net_enabled() -> bool:
    """Delayed gate so dispatch never imports numpy unless learning is on."""
    from dourmouse.orch_net import orch_enabled

    return orch_enabled()


def _cli_experience_sink(registry: DispatchRegistry) -> Callable[[dict[str, Any]], None]:
    """CLI experience sink: log with the full roster as the vocabulary hint."""
    from dourmouse.orch_net import log_experience

    names = [s.name for s in registry.all_subagents()]

    def _sink(record: dict[str, Any]) -> None:
        log_experience(record, agent_names=names)

    return _sink


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
