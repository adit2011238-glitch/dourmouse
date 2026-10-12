"""Claude-as-orchestrator: fan work out to other cloud models concurrently.

The architecture this implements, per the user's instruction: Claude Code is
the brain the user talks to in every tab. It does not do all the work itself.
It decides what needs doing, hands each piece to whichever model is the right
one for that piece, runs them CONCURRENTLY, and reports back. That is what
makes rapid-fire multi-prompt work feel fast -- five delegated questions take
about as long as the slowest one, not the sum of all five.

Two ways a delegated task can run:

  The roster (ROSTER)         The real dispatch loop with the named agent's
                              real tools, on this machine's configured
                              backend (Ollama Cloud with a key). Every tool
                              result passes the privacy gate (privacy.py):
                              tiers, the owner's consent per source, Private
                              mode, placeholders for identifiers, the egress
                              ledger.

  Gemini (GEMINI)             One tool-less Gemini call for work whose input
                              is public: research, summarisation, news and
                              world-monitor analysis. Large context, fast,
                              cheap.

-----------------------------------------------------------------------------
The routing rule (PRIV, 2026-10-12)
-----------------------------------------------------------------------------
Every model Dourmouse uses runs in the cloud (the owner's rule), so routing
cannot keep anything on this machine and no longer claims to. It used to: a
"local only" agent list promised that mail, memory, files and money never
left the laptop, which was false the moment a cloud key was set (A-13).

What routing still decides is whether a task gets real tools. An agent whose
input is public may use tool-less Gemini. Every other agent, the sensitive
ones above all (privacy.SENSITIVE_AGENTS), runs through the roster, where the
privacy gate applies to whatever it reads. A model-supplied ``model`` cannot
move a sensitive agent's task to Gemini (P5-31): that would hand the
model-written prompt, possibly holding content it already read, to another
provider without the gate.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import contextvars
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Callable

#: Agents whose inputs are public by nature, where Gemini's large context and
#: low cost are a real win. Every agent not listed runs through the roster.
_PUBLIC_INPUT_AGENTS = frozenset({
    "research_info",
    "news",
    "worldmonitor",
    "globe",
    "rnd",
    "music",
    # 2026-09-20: real public-web research (Domain G's chat-reachable
    # wiring) -- the same class of work research_info/rnd already do,
    # reached through the same forced_agent mechanism.
    "evidence_pipeline",
    # 2026-09-14: real image generation (generate_image, image_gen.py).
    # The tool itself always calls Gemini's cloud API regardless of this
    # setting (there is no local image-gen backend at all) -- this only
    # controls whether the CONVERSATIONAL/routing layer around a request
    # aimed at this agent may also use cloud Gemini, and an image prompt
    # is not the personal/financial data class privacy.SENSITIVE_AGENTS
    # covers.
    "media",
    # 2026-09-19: field-specialist qualification (research_mesh_tools.py).
    # The real corpus is public academic exam papers scraped from public
    # university archive pages, same public-web class as research_info --
    # never the user's own private data.
    "research_mesh",
})

ROSTER = "roster"
GEMINI = "gemini"


def route_for(agent: str | None, *, allow_gemini: bool = True) -> str:
    """How a task for ``agent`` runs: ``GEMINI`` (one tool-less call) only
    for a public-input agent while Gemini is configured and allowed, else
    ``ROSTER`` (the dispatch loop with the agent's tools, behind the privacy
    gate). Unknown agents run through the roster."""
    from dourmouse.privacy import SENSITIVE_AGENTS

    name = (agent or "").strip().lower()
    if not allow_gemini or name in SENSITIVE_AGENTS or name not in _PUBLIC_INPUT_AGENTS:
        return ROSTER
    return GEMINI if gemini_available() else ROSTER


def _backend_for(task: "DelegationTask", allow_gemini: bool) -> str:
    """The task's own ``model`` may pick Gemini only for an agent that is
    not sensitive, and no task goes to Gemini once the request has read a
    sensitive source (P5-31); ``model`` naming anything else runs the roster."""
    from dourmouse.privacy import SENSITIVE_AGENTS

    from dourmouse.privacy import current_turn

    turn = current_turn()
    if turn is not None and turn.sources:
        # This request already read a sensitive source; the model-written
        # prompt may quote it, so it stays on the request's own backend.
        return ROSTER
    asked = (task.model or "").strip().lower()
    name = (task.agent or "").strip().lower()
    if asked == GEMINI and allow_gemini and name not in SENSITIVE_AGENTS and gemini_available():
        return GEMINI
    if asked:
        return ROSTER
    return route_for(task.agent, allow_gemini=allow_gemini)


def gemini_available() -> bool:
    """True only if the Gemini backend is present AND configured.

    Imported lazily and defensively: this module must keep working, routing
    everything through the roster, on a machine where Gemini was never set up.
    """
    try:
        from dourmouse import gemini_backend
    except Exception:  # noqa: BLE001 - module genuinely absent
        return False
    try:
        return bool(gemini_backend.gemini_configured())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Tasks and results
# --------------------------------------------------------------------------- #


@dataclass
class DelegationTask:
    """One unit of delegated work."""

    prompt: str
    #: Which roster agent should handle it. None lets the orchestrator route.
    agent: str | None = None
    #: Ask for a backend ("gemini"). Honoured only for an agent that is not
    #: sensitive; None means: apply the policy above.
    model: str | None = None
    #: Free-form label so a caller can match results back to intent.
    label: str = ""


@dataclass
class DelegationResult:
    task: DelegationTask
    ok: bool
    text: str = ""
    error: str = ""
    model_used: str = ""
    seconds: float = 0.0
    #: Real token/cost usage when the backend reported any.
    usage: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

#: Concurrency ceiling: one fan-out is bounded so it cannot flood the
#: provider's rate limits or starve the machine of threads.
_DEFAULT_MAX_WORKERS = int(os.environ.get("DOURMOUSE_DELEGATE_WORKERS", "4"))

_pool_lock = threading.Lock()

#: Extra seconds the fan-out waits beyond ``timeout`` before reporting the
#: unfinished tasks as timed out.
_DELEGATE_GRACE_S = 30.0

# Finding #134 (AGENT-3): the dispatch run that called delegate_to_models,
# handed to the worker threads through the context (they copy it), so the
# delegated turns inherit its backend instead of rebuilding one.
_caller_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar("dourmouse_delegation_caller", default=None)


@contextlib.contextmanager
def inherit_caller(ctx: Any) -> Iterator[None]:
    token = _caller_ctx.set(ctx)
    try:
        yield
    finally:
        _caller_ctx.reset(token)


def _run_roster(task: DelegationTask, timeout: float) -> DelegationResult:
    """One turn through the real dispatch loop on the configured backend.

    Goes through run_dispatch_messages rather than a bare completion call so
    the delegated turn gets the SAME real tools the user would get -- a
    delegated "check my inbox" must actually be able to check the inbox.
    """
    import time

    from dourmouse.dispatch import is_backend_failure_text, run_dispatch_messages
    from dourmouse.general_roster import build_general_registry

    started = time.monotonic()
    caller = _caller_ctx.get()
    job_id = None
    if caller is not None and caller.jobs is not None:
        job_id = caller.jobs.spawn(
            task=task.prompt, subagent=task.agent, depth=caller.depth + 1, parent_id=caller.current_job_id
        )
    try:
        registry = build_general_registry()
        # A delegated turn gets a short capability note of its own. It already
        # has its tools attached; what it lacks is the knowledge that they are
        # REAL and that an honest failure beats a plausible guess.
        messages: list[dict[str, Any]] = []
        try:
            from dourmouse.model_context import agent_context

            messages.append({"role": "system", "content": agent_context(task.agent)})
        except Exception:  # noqa: BLE001 - context must never break a turn
            pass
        messages.append({"role": "user", "content": task.prompt})
        # With no calling run (the MCP bridge), client is left None on
        # purpose: run_dispatch_messages builds the right one itself,
        # honouring the per-agent backend split. Inside a run, the delegated
        # turn inherits that run's backend and governance (finding #134).
        inherit: dict[str, Any] = {}
        if caller is not None:
            inherit = {
                "client": caller.client, "config": caller.config, "confirmation_gate": caller.confirmation_gate,
                "event_sink": caller.event_sink, "job_tracker": caller.jobs, "depth": caller.depth + 1,
                "max_depth": caller.max_depth, "budget": caller.budget, "max_delegates": caller.max_delegates,
                "current_job_id": job_id, "cost_budget": caller.cost_budget, "dlp": caller.dlp,
                "rbac": caller.rbac, "fanout_branch": True, "policy": caller.policy,
            }
            if task.agent and caller.config is not None and hasattr(caller.config, "model_for_agent"):
                inherit["model"] = caller.config.model_for_agent(task.agent)
        # A real deadline: the dispatch loop checks should_stop between model
        # turns and before each tool call, so a delegated turn stops soon
        # after ``timeout`` instead of running on unseen (finding P5-30).
        # The calling run's own STOP still applies.
        deadline = started + max(1.0, float(timeout))
        caller_stop = getattr(caller, "should_stop", None) if caller is not None else None

        def _should_stop() -> bool:
            return time.monotonic() > deadline or bool(caller_stop is not None and caller_stop())

        report = run_dispatch_messages(
            messages,
            registry,
            forced_agent=task.agent or None,
            should_stop=_should_stop,
            **inherit,
        )
        if time.monotonic() > deadline and not str(report.get("final_text") or "").strip():
            raise TimeoutError(f"the delegated turn did not finish within {timeout:.0f}s and was stopped")
        text = str(report.get("final_text") or "")
        if is_backend_failure_text(text):
            # A backend that could not answer says so as the reply; that is a
            # failure to report, never a success.
            raise RuntimeError(text.strip())
        if caller is not None and caller.jobs is not None and job_id:
            caller.jobs.finish(job_id, result=text)
        return DelegationResult(
            task=task,
            ok=True,
            text=text,
            model_used=ROSTER,
            seconds=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 - one failed branch must not kill the fan-out
        if caller is not None and caller.jobs is not None and job_id:
            caller.jobs.finish(job_id, error=str(exc))
        return DelegationResult(
            task=task,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            model_used=ROSTER,
            seconds=time.monotonic() - started,
        )


def _run_gemini(task: DelegationTask, timeout: float) -> DelegationResult:
    """One tool-less turn on Gemini, for a public-input task.

    The prompt is written by the calling model and may quote what it read,
    so it gets the same placeholder swap as a tool result (PRIV-2), and the
    answer is restored on this Mac. ``call_gemini`` records the egress row.
    """
    import time

    started = time.monotonic()
    try:
        from dourmouse import gemini_backend
    except Exception as exc:  # noqa: BLE001
        return DelegationResult(
            task=task,
            ok=False,
            error=f"NOT CONFIGURED: the Gemini backend is unavailable ({exc}).",
            model_used=GEMINI,
        )
    try:
        usage: dict[str, Any] = {}

        def _on_usage(u: dict[str, Any]) -> None:
            usage.update(u or {})

        from dourmouse import privacy
        from dourmouse.governance import PiiRedactor

        with privacy.turn_scope() as turn:
            prompt, counts = PiiRedactor(privacy.owner_emails()).redact(task.prompt, turn.placeholders)
            turn.redactions.update(counts)
            text = gemini_backend.call_gemini(prompt, timeout=timeout, on_usage=_on_usage)
            text = turn.placeholders.restore_text(str(text or ""))
        return DelegationResult(
            task=task,
            ok=True,
            text=text,
            model_used=GEMINI,
            seconds=time.monotonic() - started,
            usage=usage,
        )
    except Exception as exc:  # noqa: BLE001
        return DelegationResult(
            task=task,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            model_used=GEMINI,
            seconds=time.monotonic() - started,
        )


def delegate(
    tasks: list[DelegationTask],
    *,
    max_workers: int | None = None,
    timeout: float = 300.0,
    allow_gemini: bool = True,
    on_result: Callable[[DelegationResult], None] | None = None,
) -> list[DelegationResult]:
    """Run every task concurrently and return results in the ORIGINAL order.

    Order matters: the orchestrator asked N questions and needs to match N
    answers back to them. Completion order is meaningless to the caller, so
    results are re-sorted to the order the tasks were given in.

    A task that fails comes back as a result with ``ok=False`` and the real
    error text. It never raises, because one bad branch of a fan-out must not
    discard the other four answers that succeeded.
    """
    if not tasks:
        return []

    workers = max(1, min(int(max_workers or _DEFAULT_MAX_WORKERS), 8))
    results: list[DelegationResult | None] = [None] * len(tasks)

    def _one(index: int, task: DelegationTask) -> None:
        try:
            backend = _backend_for(task, allow_gemini)
            runner = _run_gemini if backend == GEMINI else _run_roster
            result = runner(task, timeout)
        except BaseException as exc:  # noqa: BLE001
            # The runners catch their own errors, but a raise from routing --
            # or from a runner swapped in by a caller or a test -- must not
            # leave results[index] as None. That would surface downstream as
            # "timed out after Ns", which is a different and misleading
            # diagnosis than the real failure.
            result = DelegationResult(
                task=task,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                model_used=(task.model or "").strip().lower(),
            )
        results[index] = result
        if on_result is not None:
            try:
                on_result(result)
            except Exception:  # noqa: BLE001 - an observer must never break the run
                pass

    # Not a ``with`` block: leaving one calls shutdown(wait=True), which waits
    # for every worker, so the timeout below could never take effect and one
    # stuck turn hung the whole fan-out (finding P5-30). Finished answers are
    # returned; a still-running worker is left behind and its task reports a
    # timeout.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(contextvars.copy_context().run, _one, i, t) for i, t in enumerate(tasks)]
        concurrent.futures.wait(futures, timeout=timeout + _DELEGATE_GRACE_S)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    out: list[DelegationResult] = []
    for i, r in enumerate(results):
        if r is None:
            out.append(
                DelegationResult(
                    task=tasks[i],
                    ok=False,
                    error=f"timed out after {timeout:.0f}s with no result",
                )
            )
        else:
            out.append(r)
    return out


def format_results(results: list[DelegationResult]) -> str:
    """Render a fan-out for the orchestrator to read back.

    Deliberately plain text with explicit success/failure per item: the
    orchestrator has to be able to tell which of its five questions actually
    got answered, and a failure that reads like an answer is the worst
    possible outcome here.
    """
    if not results:
        return "No tasks were delegated."

    lines: list[str] = []
    ok_count = sum(1 for r in results if r.ok)
    lines.append(f"DELEGATED {len(results)} task(s) — {ok_count} succeeded, "
                 f"{len(results) - ok_count} failed.")
    lines.append("")
    for i, r in enumerate(results, 1):
        label = r.task.label or r.task.agent or f"task {i}"
        head = f"[{i}] {label} · {r.model_used or 'unrouted'} · {r.seconds:.1f}s"
        lines.append(head)
        lines.append("-" * len(head))
        if r.ok:
            lines.append(r.text.strip() or "(empty answer)")
        else:
            lines.append(f"FAILED: {r.error}")
        lines.append("")
    return "\n".join(lines).rstrip()
