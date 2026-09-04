"""Claude-as-orchestrator: fan work out to the cheaper local and cloud models.

The architecture this implements, per the user's instruction: Claude Code is
the brain the user talks to in every tab. It does not do all the work itself.
It decides what needs doing, hands each piece to whichever model is the right
one for that piece, runs them CONCURRENTLY, and reports back. That is what
makes rapid-fire multi-prompt work feel fast -- five delegated questions take
about as long as the slowest one, not the sum of all five.

Three backends, three jobs:

  Claude (the orchestrator)   Planning, decomposition, code, multi-step tool
                              use, and assembling the final answer. It is the
                              only model that talks to the user.

  Ollama (local)              Anything touching the user's private data. Free,
                              private, already holds the whole agent roster.

  Gemini (Google AI Studio)   Bulk work over PUBLIC data: research,
                              summarisation, long-document reading, news and
                              world-monitor analysis. Large context, fast,
                              cheap.

-----------------------------------------------------------------------------
The routing rule, and why it is what it is
-----------------------------------------------------------------------------
The split is drawn on PRIVACY first and capability second, because that is the
line that actually matters and the one that is expensive to get wrong.

Anything that reads the user's mail, memory, files, calendar, Drive, finances
or personal documents routes to Ollama, which runs on this machine. Sending
that content to a cloud API would mean the user's inbox leaving their laptop
to answer a question they thought was local. No amount of speed justifies it,
so those agents are pinned local and the policy below is explicit rather than
heuristic -- a heuristic would eventually guess wrong on a new agent, and the
failure would be silent.

Everything whose input is already public -- a news article, a web page, a
research question, a world-monitor feed -- can go to Gemini, where the large
context window and low cost are a genuine win.

Agents not named in the policy default to LOCAL. That default is deliberate:
if a future agent is added and nobody thinks about its routing, the safe
outcome is that it stays on the machine.
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

#: Agents pinned to the local model because their inputs are the user's own
#: private data. Not a performance judgement -- a privacy one.
#: Agents pinned to the local model because their inputs are the user's own
#: private data. Not a performance judgement -- a privacy one. Names checked
#: against the real registry (35 agents), not guessed.
_LOCAL_ONLY_AGENTS = frozenset({
    # Personal correspondence, documents and memory.
    "mail", "comms", "messenger", "docs", "memory", "companion",
    # The user's machine, files, schedule and running processes.
    "admin_ops", "system", "tasks", "scheduling", "compute",
    "browser", "panel_control", "freebuff", "design_3d",
    # Money. Positions, balances and brokerage credentials.
    "markets", "forex", "mt5", "t212",
    # The user's own private repositories and research pipeline.
    "dev_coding", "atlas", "atlas_cmd", "atlas_ui",
    # Delegating to a coding CLI from inside a delegated turn is a recursion
    # risk, so these never route anywhere but local, and in practice the tool
    # below refuses them outright.
    "code_claude", "code_codex", "code_deepseek", "code_nvidia", "code_ollama",
    "orchestrator",
})

#: Agents whose inputs are public by nature, where Gemini's large context and
#: low cost are a real win and nothing private leaves the machine.
_CLOUD_OK_AGENTS = frozenset({
    "research_info",
    "news",
    "worldmonitor",
    "globe",
    "rnd",
    "music",
})

LOCAL = "ollama"
CLOUD = "gemini"


def route_for(agent: str | None, *, allow_cloud: bool = True) -> str:
    """Which backend should handle work for ``agent``.

    Unknown agents default to LOCAL, deliberately -- see the module docstring.
    ``allow_cloud=False`` forces everything local, which is what a global
    privacy switch would flip.
    """
    if not allow_cloud or not gemini_available():
        return LOCAL
    name = (agent or "").strip().lower()
    if name in _LOCAL_ONLY_AGENTS:
        return LOCAL
    if name in _CLOUD_OK_AGENTS:
        return CLOUD
    return LOCAL


def gemini_available() -> bool:
    """True only if the Gemini backend is present AND configured.

    Imported lazily and defensively: this module must keep working, routing
    everything local, on a machine where Gemini was never set up.
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
    #: Force a backend. None means: apply the policy above.
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

#: Concurrency ceiling. Local generation is memory-bound, not CPU-bound: each
#: concurrent Ollama turn holds its own KV cache, and with num_ctx now at
#: 32768 (see dispatch._OLLAMA_NUM_CTX) four of those is already a lot of
#: resident memory on a laptop. Cloud calls are cheap to parallelise but are
#: bounded by the same pool so one fan-out cannot starve the machine.
_DEFAULT_MAX_WORKERS = int(os.environ.get("DOURMOUSE_DELEGATE_WORKERS", "4"))

_pool_lock = threading.Lock()


def _run_local(task: DelegationTask, timeout: float) -> DelegationResult:
    """One turn on the local model, through the real dispatch loop.

    Goes through run_dispatch_messages rather than a bare completion call so
    the delegated turn gets the SAME real tools the user would get -- a
    delegated "check my inbox" must actually be able to check the inbox.
    """
    import time

    from dourmouse.dispatch import run_dispatch_messages
    from dourmouse.general_roster import build_general_registry

    started = time.monotonic()
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
        # client is left None on purpose: run_dispatch_messages builds the
        # right one itself, honouring the per-agent backend split. Handing it
        # a pre-built client here would bypass that routing.
        report = run_dispatch_messages(
            messages,
            registry,
            forced_agent=task.agent or None,
        )
        return DelegationResult(
            task=task,
            ok=True,
            text=str(report.get("final_text") or ""),
            model_used=LOCAL,
            seconds=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 - one failed branch must not kill the fan-out
        return DelegationResult(
            task=task,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            model_used=LOCAL,
            seconds=time.monotonic() - started,
        )


def _run_cloud(task: DelegationTask, timeout: float) -> DelegationResult:
    """One turn on Gemini.

    No tools: this path is deliberately reserved for reasoning over text the
    caller already supplies. Giving the cloud model the private-data tools is
    exactly what the routing policy above exists to prevent.
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
            model_used=CLOUD,
        )
    try:
        usage: dict[str, Any] = {}

        def _on_usage(u: dict[str, Any]) -> None:
            usage.update(u or {})

        text = gemini_backend.call_gemini(task.prompt, timeout=timeout, on_usage=_on_usage)
        return DelegationResult(
            task=task,
            ok=True,
            text=str(text or ""),
            model_used=CLOUD,
            seconds=time.monotonic() - started,
            usage=usage,
        )
    except TypeError:
        # The backend may not accept on_usage yet; a missing metric must not
        # fail a real answer.
        try:
            text = gemini_backend.call_gemini(task.prompt, timeout=timeout)
            return DelegationResult(
                task=task, ok=True, text=str(text or ""), model_used=CLOUD,
                seconds=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001
            return DelegationResult(
                task=task, ok=False, error=f"{type(exc).__name__}: {exc}",
                model_used=CLOUD, seconds=time.monotonic() - started,
            )
    except Exception as exc:  # noqa: BLE001
        return DelegationResult(
            task=task,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            model_used=CLOUD,
            seconds=time.monotonic() - started,
        )


def delegate(
    tasks: list[DelegationTask],
    *,
    max_workers: int | None = None,
    timeout: float = 300.0,
    allow_cloud: bool = True,
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
            backend = (task.model or "").strip().lower() or route_for(
                task.agent, allow_cloud=allow_cloud
            )
            runner = _run_cloud if backend == CLOUD else _run_local
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, i, t) for i, t in enumerate(tasks)]
        concurrent.futures.wait(futures, timeout=timeout + 30)

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
