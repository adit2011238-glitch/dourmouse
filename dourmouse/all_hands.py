"""All-Hands mode + slash commands (v5.22.9) — one goal, every resource.

The user hands DourMouse a goal and says "use all resources" (or types
``/all <goal>``); the All-Hands runner fans that goal out to every real
brain this machine can reach — in parallel — and a synthesizer (the FAST
hosted brain, never the slow local one) merges the results into one answer:

- ``claude``   — the user's real Claude Code CLI (``claude -p``)
- ``nvidia``   — NVIDIA NIM (the hosted fast brain; also the synthesizer)
- ``deepseek`` — DeepSeek via the Freebuff free-tier env or NVIDIA NIM
- ``codex``    — the OpenAI Codex/ChatGPT backend (honest NOT CONFIGURED
                 without a key — a red card, never a fake result)
- ``web``      — live DuckDuckGo + Wikipedia search

Every brain reports REAL output or an honest error (Rules 2.1/2.2): a
missing key/CLI is NOT CONFIGURED, an API/CLI failure surfaces the real
error, nothing is ever fabricated. Progress streams over the same SSE
broadcast hub the HUD uses, so the dedicated ALL HANDS window updates
live while the run works.

Slash commands (parsed server-side, work from ANY client):
- ``/all <goal>``      — start an All-Hands run (same as "use all resources")
- ``/claude <task>``   — one task on the Claude Code CLI
- ``/codex <task>``    — one task on the OpenAI Codex/ChatGPT backend
- ``/chatgpt <task>``  — alias of /codex (the OpenAI backend)
- ``/freebuff <task>`` — dispatch a task into a REAL Freebuff thread
- ``/nvidia <task>``   — one task on NVIDIA NIM (fast, hosted)

Secrets come only from env vars (Rule 2.6); nothing is logged in full.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any, Callable

from dourmouse import code_backends

#: How long a single brain may work before its card goes red.
_BRAIN_TIMEOUT = 300.0
#: Per-brain result length fed to the synthesizer (bounded context).
_RESULT_CAP = 1_600
#: The synthesizer's own output cap.
_SYNTH_CAP = 6_000

#: Recognised slash commands -> (label, backend key).
SLASH_COMMANDS: dict[str, tuple[str, str]] = {
    "all": ("ALL HANDS", "all"),
    "claude": ("CLAUDE", "claude"),
    "codex": ("CODEX", "codex"),
    "chatgpt": ("CHATGPT (OpenAI)", "codex"),
    "freebuff": ("FREEBUFF", "freebuff"),
    "nvidia": ("NVIDIA", "nvidia"),
    "deepseek": ("DEEPSEEK", "deepseek"),
}

_ALL_HANDS_RE = re.compile(
    r"\b(use all (?:of )?(?:your |the )?resources?|all hands|all-resources|"
    r"use every (?:resource|brain|model))\b",
    re.IGNORECASE,
)

#: Default All-Hands brain roster — every real resource this machine can
#: reach. Order is display order. Each entry: label + backend key.
DEFAULT_BRAINS: list[tuple[str, str]] = [
    ("CLAUDE (Code CLI)", "claude"),
    ("NVIDIA (hosted)", "nvidia"),
    ("DEEPSEEK (Freebuff tier)", "deepseek"),
    ("CHATGPT/CODEX (OpenAI)", "codex"),
    ("WEB SEARCH", "web"),
]

_BRAIN_SYSTEM = (
    "You are one independent worker inside DourMouse's ALL-HANDS run. "
    "You receive a goal the user wants achieved. Work on it thoroughly and "
    "independently with the tools/backend you have. Return your best "
    "contribution: concrete findings, code, steps, or a decisive answer. "
    "Never claim you used a tool you did not use; if you cannot do "
    "something, say so plainly. Keep it focused (under ~1200 words)."
)

_SYNTH_SYSTEM = (
    "You are the synthesizer for an ALL-HANDS run. Several independent "
    "workers each returned their contribution to the user's goal. Merge "
    "them into ONE clear, decisive final answer for the user: what was "
    "found, what is the recommended action, and any caveats. Attribute "
    "each point to its worker (CLAUDE/NVIDIA/DEEPSEEK/CHATGPT/WEB) when "
    "useful. If a worker reported NOT CONFIGURED or failed, say so once, "
    "briefly. Be direct and concrete; no filler."
)


# --------------------------------------------------------------------------- #
# Slash parsing
# --------------------------------------------------------------------------- #

def parse_slash(prompt: str) -> tuple[str, str] | None:
    """Split a leading slash command off a prompt.

    Returns ``(command, text)`` for a known command, e.g.
    ``("/claude", "refactor this")``, or None when the prompt is not a
    slash command (unknown commands return None — the normal chat handles
    them; the UI surfaces the unknown-command error honestly).
    """
    p = (prompt or "").strip()
    if not p.startswith("/"):
        return None
    first, _, rest = p.partition(" ")
    cmd = first[1:].strip().lower()  # drop the leading "/"
    if cmd not in SLASH_COMMANDS:
        return None
    return cmd, rest.strip()


def detect_all_hands(prompt: str) -> bool:
    """True when the goal explicitly asks to use every resource."""
    return bool(_ALL_HANDS_RE.search(prompt or ""))


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #

class AllHandsRunner:
    """Runs one goal across every configured brain, in parallel.

    Thread-safe: each run mutates only its own slot under the lock. A run
    starts N worker daemon threads (one per brain) plus the synthesizer
    thread that waits for all of them. Every status change is broadcast on
    the optional SSE hub as ``{"type": "allhands", ...}`` so the dedicated
    window updates live.
    """

    def __init__(self, hub: Any | None = None,
                 brain_runners: dict[str, Callable[[str], str]] | None = None) -> None:
        self._hub = hub
        self._runs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        #: Injectable brain runners (tests substitute fakes; the default
        #: implementation is _default_brain). Keyed by backend name.
        self._brains: dict[str, Callable[[str], str]] = brain_runners or {}

    # -- public API ------------------------------------------------------ #

    def bind_hub(self, hub: Any | None) -> None:
        """Attach the SSE broadcast hub (called by run_server at mount)."""
        self._hub = hub

    def start(self, goal: str, *, owner: str | None = None,
              brains: list[tuple[str, str]] | None = None) -> str:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("all-hands goal is required")
        run_id = uuid.uuid4().hex[:12]
        roster = list(brains or DEFAULT_BRAINS)
        with self._lock:
            self._runs[run_id] = {
                "id": run_id,
                "goal": goal[:2000],
                "owner": owner or "",
                "started": _now(),
                "status": "running",
                #: The roster THIS run actually used — the synthesizer must
                #: merge exactly these brains (reviewer-caught: hardcoding
                #: DEFAULT_BRAINS would silently drop custom-roster results).
                "roster": roster,
                "brains": {
                    key: {
                        "label": label,
                        "backend": key,
                        "status": "pending",
                        "result": None,
                        "error": None,
                        "elapsed": None,
                    }
                    for label, key in roster
                },
                "synthesis": None,
                "synth_backend": "nvidia",
                "error": None,
                "finished": None,
            }
            self._order.append(run_id)
        self._emit(run_id, {"type": "allhands", "status": "started"})
        # Fan out: one daemon thread per brain.
        for label, key in roster:
            threading.Thread(
                target=self._run_brain, args=(run_id, key, label, goal),
                daemon=True, name=f"allhands-{key}",
            ).start()
        threading.Thread(
            target=self._synthesize, args=(run_id, goal),
            daemon=True, name=f"allhands-synth-{run_id}",
        ).start()
        return run_id

    def snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return _copy(run) if run is not None else None

    def all_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_copy(self._runs[r]) for r in self._order]

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._runs.values() if r["status"] == "running")

    # -- workers --------------------------------------------------------- #

    def _run_brain(self, run_id: str, key: str, label: str, goal: str) -> None:
        started = time.monotonic()
        self._set_brain(run_id, key, {"status": "running"})
        self._emit(run_id, {"type": "allhands", "brain": key, "status": "running"})
        try:
            runner = self._brains.get(key)
            if runner is not None:
                result = runner(goal)
            else:
                # Pin the REAL backend to THIS brain. Without force_backend
                # every worker silently falls back to the default (nvidia)
                # brain — five identical "different" answers. Live-caught
                # in the 33-directive sweep.
                result = _default_brain(goal, force_backend=key)
            self._set_brain(run_id, key, {
                "status": "done",
                "result": result[:_RESULT_CAP * 4],
                "elapsed": round(time.monotonic() - started, 1),
            })
            self._emit(run_id, {
                "type": "allhands", "brain": key, "status": "done",
                "summary": (result or "")[:240],
            })
        except Exception as exc:  # noqa: BLE001 -- an honest red card
            # v13: real bug fixed here, live-caught through an actual /all
            # directive against codex — a genuine, honest error ("You've
            # hit your usage limit... try again at Sep 17th, 2026") sat at
            # the END of the real exception text (a CLI error message is a
            # boilerplate banner + the full echoed prompt FIRST, then the
            # actual failure reason — exactly why _run_codex/_run_claude's
            # own `err[-2000:]` already keeps the TAIL, not the head, of
            # real CLI stderr). Truncating with `[:600]`/`[:240]` here
            # threw that away again one layer up, showing the boilerplate
            # banner and cutting off before the one line that actually
            # explained what went wrong. `[-600:]`/`[-240:]` keeps the
            # part that matters.
            self._set_brain(run_id, key, {
                "status": "error",
                "error": str(exc)[-600:],
                "elapsed": round(time.monotonic() - started, 1),
            })
            self._emit(run_id, {
                "type": "allhands", "brain": key, "status": "error",
                "error": str(exc)[-240:],
            })

    def _synthesize(self, run_id: str, goal: str) -> None:
        # Wait for every brain to settle (done or error) with a ceiling.
        deadline = time.monotonic() + _BRAIN_TIMEOUT + 60
        while time.monotonic() < deadline:
            with self._lock:
                run = self._runs[run_id]
                if all(b["status"] in ("done", "error") for b in run["brains"].values()):
                    break
            time.sleep(0.5)
        with self._lock:
            run = self._runs[run_id]
            roster = run.get("roster") or DEFAULT_BRAINS
            parts = []
            for label, key in roster:
                b = run["brains"].get(key)
                if b is None:
                    continue
                if b["status"] == "done" and b.get("result"):
                    parts.append(f"[{label}]\n{b['result'][:_RESULT_CAP]}")
                elif b["status"] == "error":
                    parts.append(f"[{label}] REPORTED HONESTLY: {b.get('error', 'failed')}")
                else:
                    parts.append(f"[{label}] (no result)")
            synth_task = (
                f"USER GOAL: {goal}\n\n"
                f"WORKER CONTRIBUTIONS:\n" + "\n\n".join(parts)
            )
        # v13: real bug fixed here, live-caught through an actual /all
        # directive — the synthesizer was hardcoded to "nvidia" with no
        # fallback. NVIDIA is currently 403'ing on every real inference
        # call (external, account-level, documented elsewhere in this
        # codebase) — live result before this fix: CLAUDE answered
        # correctly ("Paris.") in 7.6s, every other brain reported an
        # honest error, and the synthesis came back completely EMPTY,
        # because the one hardcoded synth backend was the one that's
        # currently dead. The entire point of All-Hands is the merged
        # final answer; a run where a real brain succeeded but the user
        # sees nothing at all is the worst possible failure mode for this
        # feature specifically. Fixed with an honest try-nvidia-then-
        # ollama fallback — nvidia stays first choice (fast, hosted,
        # matches this module's own stated design intent in its docstring)
        # but a failure no longer means silence.
        synth_backend_used = "nvidia"
        try:
            synthesis = _default_brain(synth_task, system=_SYNTH_SYSTEM,
                                       force_backend="nvidia")
        except Exception as nvidia_exc:
            try:
                synth_backend_used = "ollama"
                synthesis = _default_brain(synth_task, system=_SYNTH_SYSTEM,
                                           force_backend="ollama")
            except Exception as exc:  # noqa: BLE001 -- the run still reports honestly
                with self._lock:
                    run = self._runs[run_id]
                    run["status"] = "done"
                    run["error"] = (
                        f"synthesis failed on both nvidia ({nvidia_exc}) "
                        f"and its ollama fallback ({exc})"
                    )
                    run["finished"] = _now()
                self._emit(run_id, {"type": "allhands", "status": "done",
                                    "error": run["error"]})
                return
        with self._lock:
            run = self._runs[run_id]
            run["synthesis"] = synthesis[:_SYNTH_CAP]
            run["synth_backend"] = synth_backend_used
            run["status"] = "done"
            run["finished"] = _now()
        self._emit(run_id, {"type": "allhands", "status": "done",
                            "synthesis": run["synthesis"]})

    # -- helpers --------------------------------------------------------- #

    def _set_brain(self, run_id: str, key: str, patch: dict[str, Any]) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or key not in run["brains"]:
                return
            run["brains"][key].update(patch)

    def _emit(self, run_id: str, payload: dict[str, Any]) -> None:
        if self._hub is None:
            return
        try:
            self._hub.broadcast({**payload, "run_id": run_id})
        except Exception:  # noqa: BLE001 -- a dead hub never breaks a run
            pass


def _copy(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    import copy
    return copy.deepcopy(run)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Default brain implementation (real backends)
# --------------------------------------------------------------------------- #

def _default_brain(task: str, *, system: str | None = None,
                   force_backend: str | None = None) -> str:
    """Run one task on the real backends (or an injectable runner in tests).

    ``force_backend`` pins the backend (used by the synthesizer to always
    use the fast hosted NVIDIA brain). The task string carries the system
    instructions for backends without a separate system slot (claude CLI).
    """
    from dourmouse.general_roster import _web_search_tool

    system = system or _BRAIN_SYSTEM
    backend = force_backend or "nvidia"
    if backend == "web":
        # The web brain must search the GOAL, never the system wrapper —
        # strip the "GOAL/TASK: " marker if a wrapped task ever lands here
        # (reviewer-hardened).
        #
        # v13: real bug fixed here, live-caught through an actual /all
        # directive — str.partition() returns exactly a 3-tuple
        # (before, sep, after), never 4 values. Every call to this branch
        # raised "ValueError: not enough values to unpack (expected 4, got
        # 3)" before this fix, meaning the web brain has been silently
        # crashing (reported honestly as an error, per the try/except in
        # _run_brain — but crashing all the same) on every single All-Hands
        # run since whenever this line was written.
        before, _, goal = task.partition("GOAL/TASK: ")
        query = (goal or before or task).strip()
        return _web_search_tool({"query": query})
    wrapped = f"{system}\n\nGOAL/TASK: {task}"
    if backend in ("claude", "codex", "openai_codex"):
        # v13: real bug fixed here — codex used to fall straight through to
        # the raw API-key path below (load_backend + _run_openai_compat),
        # reporting "NOT CONFIGURED: Codex needs EITHER the Codex CLI
        # signed in... OR CODEX_API_KEY/OPENAI_API_KEY" even when the real
        # Codex CLI IS signed in (confirmed live via /api/connections:
        # "Codex CLI codex-cli 0.144.6 - logged in (chatgpt)") — because it
        # never actually tried the CLI. run_code_task's own docstring says
        # exactly this: CLI first (what the CODEX connection status
        # measures), the API key only as a fallback. claude was already
        # special-cased to use run_code_task; codex belongs in the same
        # branch, not the raw API-only path meant for backends with no CLI
        # of their own (nvidia/deepseek/qwen/glm/kimi).
        return code_backends.run_code_task(backend, wrapped, timeout=min(int(_BRAIN_TIMEOUT), 600))
    base, api_key, model = code_backends.load_backend(backend)
    return code_backends._run_openai_compat(base, api_key, model, wrapped,
                                            timeout=min(int(_BRAIN_TIMEOUT), 600))


# --------------------------------------------------------------------------- #
# Module singleton (the running server's runner)
# --------------------------------------------------------------------------- #

_runner: AllHandsRunner | None = None
_runner_lock = threading.Lock()


def default_runner() -> AllHandsRunner:
    """The process-wide runner (lazy, thread-safe)."""
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = AllHandsRunner()
        return _runner


def bind_events_hub(hub: Any | None) -> None:
    """Attach the SSE hub to the default runner (called by run_server)."""
    default_runner().bind_hub(hub)


def start_all_hands(goal: str, *, owner: str | None = None) -> str:
    """Convenience: start a run on the default runner."""
    return default_runner().start(goal, owner=owner)


# --------------------------------------------------------------------------- #
# Slash command execution (server-side, SSE-shaped)
# --------------------------------------------------------------------------- #

def run_slash(cmd: str, text: str, *, owner: str | None = None) -> dict[str, Any]:
    """Execute one slash command synchronously; returns a result dict.

    Used by the webui slash flow. Returns ``{ok, text, run_id?}`` where
    ``text`` is the answer to render, or ``{ok: False, text}`` with the
    honest error (Rule 2.2). Long-running commands (``/all``) return
    immediately with the run_id — the progress streams separately.
    """
    text = (text or "").strip()
    if cmd == "all":
        if not text:
            return {"ok": False, "text": "/all requires a goal — e.g. /all research the EV charging market"}
        run_id = start_all_hands(text, owner=owner)
        return {
            "ok": True,
            "text": f"ALL HANDS STARTED (run {run_id}) — every resource is on the goal. "
                    f"Progress is streaming in the ALL HANDS window.",
            "run_id": run_id,
        }
    if cmd == "freebuff":
        return _run_freebuff_slash(text)
    if cmd == "claude":
        return _run_backend_slash("claude", text)
    if cmd in ("codex", "chatgpt"):
        return _run_backend_slash("codex", text)
    if cmd == "nvidia":
        return _run_backend_slash("nvidia", text)
    if cmd == "deepseek":
        return _run_backend_slash("deepseek", text)
    return {"ok": False, "text": f"unknown slash command /{cmd}"}


def _run_backend_slash(backend: str, text: str) -> dict[str, Any]:
    if not text:
        return {"ok": False, "text": f"/{backend} requires a task after the command"}
    try:
        out = _default_brain(text, force_backend=backend)
    except RuntimeError as exc:
        return {"ok": False, "text": str(exc)}
    return {"ok": True, "text": out}


def _run_freebuff_slash(text: str) -> dict[str, Any]:
    """Dispatch a task into a REAL Freebuff thread (the user's Freebuff app).

    Picks the first project the app reports (the user's most recent
    workspace) and creates a thread with the prompt as its first message —
    a real Freebuff agent runs it there. The answer lands in Freebuff;
    this reports the thread id honestly.
    """
    if not text:
        return {"ok": False, "text": "/freebuff requires a task — e.g. /freebuff draft a Q3 review"}
    from dourmouse.freebuff_bridge import FreebuffDispatchError, freebuff_dispatch, freebuff_projects
    try:
        projects = freebuff_projects()
    except Exception as exc:  # noqa: BLE001 -- surfaced honestly
        return {"ok": False, "text": f"FREEBUFF (reported honestly): NOT CONFIGURED — {exc}"}
    if not projects:
        return {"ok": False, "text": "FREEBUFF (reported honestly): the app reports no projects — open one in Freebuff first."}
    project = projects[0]["path"]
    try:
        out = freebuff_dispatch(text, project)
    except FreebuffDispatchError as exc:
        return {"ok": False, "text": f"FREEBUFF DISPATCH FAILED (honest): {exc}"}
    tid = str(out["thread"].get("id", ""))
    title = str(out["thread"].get("title", "")).replace("\n", " ")[:80]
    return {
        "ok": True,
        "text": f"FREEBUFF DISPATCHED: thread {tid} created in {project} ({title}) — "
                f"a real Freebuff agent is running the task there now. Ask "
                f"'read my Freebuff thread {tid}' to pull the answer.",
    }
