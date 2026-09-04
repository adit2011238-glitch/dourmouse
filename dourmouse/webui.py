"""Local web UI server for the General Dispatch Agent (RUN:GENERAL).

A stdlib-only ThreadingHTTPServer (no Flask/Django) that serves the DOURMOUSE
HUD front end (ui/index.html) and exposes:

- GET  /                  -> ui/index.html
- GET  /assets/<file>     -> static files under ui/assets/
- GET  /api/roster        -> JSON description of subagents + tools + tiers
- GET  /api/repo          -> Project Memory: repo-fact count, last-scan meta,
                             newest facts; with ?q= an FTS5 search scoped to
                             source='repo' (v4.1 P6+)
- POST /api/repo/scan     -> idempotent re-index of the ATLAS repo (v4.1 P6+)
- GET  /api/atlas         -> ATLAS quant-engine panel: real repo status, FX
                             bootstrap progress, newest report, last run (v5.4)
- POST /api/atlas/run     -> start one managed ATLAS command (fx-daily, ...)
                             single-flight (v5.4)
- GET  /api/sessions      -> list session audit files under workspace/sessions
- GET  /api/deeplink      -> allow-listed workspace navigation from a
                             dourmouse:// deep link (v5.19, token-gated
                             off-loopback; 302 to the validated SPA route)
- GET  /api/version       -> secure self-update surface: current version +
                             latest from the signed latest.json feed, hash-
                             verified artifact (v5.19; honest configured:false)
- POST /api/chat          -> SSE stream: runs ChatSession.ask() and streams
                             transcript events live (tool_use, tool_result,
                             assistant_text, confirmation_requested, done)
- POST /api/confirm       -> resolves a pending confirmation by id

The confirmation gate is the interesting part: when a tool requires human
confirmation, the loop blocks on a threading.Event keyed by a generated id,
emits a confirmation_requested SSE event, and the browser POSTs /api/confirm
with {id, approved} to resume. This keeps Rule 2.8 (deterministic gating) and
Section 2.9 (requires-confirmation -> human approves) intact over HTTP.

Binds to 127.0.0.1 only. Secrets stay in .env; nothing is logged in full.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


def _log_traceback(tag: str) -> None:
    """Write the active exception's traceback to stderr (captured by the
    launchd runner into /tmp/dourmouse-ui.log) so an unexpected request
    failure is diagnosable instead of a bare 500."""
    print(f"[webui] {tag}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)

from dourmouse.chat import ChatSession
from dourmouse.config import (
    NvidiaConfig,
    OllamaConfig,
    OmniRouteConfig,
    load_llm_config,
)
from dourmouse.backend_fallback import load_llm_config_with_fallback
from dourmouse.dispatch import DispatchRegistry, JobTracker
from dourmouse.governance import RbacPolicy
from dourmouse.learn import learn_enabled, open_default_store, record_feedback
from dourmouse.live_runtime import LiveRuntime, live_enabled
from dourmouse.memory_store import MemoryStore, RemoteMemoryStoreUnavailable
from dourmouse.message_bus import MessageBus, get_message_bus
from dourmouse.planner import find_agents_for_query  # re-exported for callers


_DEFAULT_ROLE = "operator"


def _load_role() -> RbacPolicy:
    """Institutional RBAC role from env (DOURMOUSE_ROLE): operator / readonly.
    Any unknown value raises loudly at startup rather than silently running
    with a weaker policy than the operator intended."""
    role = __import__("os").environ.get("DOURMOUSE_ROLE", _DEFAULT_ROLE).strip()
    try:
        return RbacPolicy(role)
    except ValueError as exc:
        raise ValueError(f"DOURMOUSE_ROLE invalid: {exc}") from exc


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_UI_DIR = _PROJECT_ROOT / "ui"
_DEFAULT_PORT = int(__import__("os").environ.get("DOURMOUSE_UI_PORT", "8765"))

_MAX_AUDIO_BYTES = 50_000_000  # POST /api/speech body cap (50 MB, P7)

# v5.0 file upload: raw-body POST /api/upload?name=<file>, capped size,
# sandboxed under <workspace>/uploads/. Served back at /uploads/<name>.
_MAX_UPLOAD_BYTES = 50_000_000
_UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _uploads_root() -> Path:
    """<workspace>/uploads, created on demand (deterministic, Rule 2.8)."""
    import os

    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else _PROJECT_ROOT / "workspace"
    up = root / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    return up


def _sandboxed_upload_path(rel: str) -> Path | None:
    """The SAME whitelist+resolve+relative_to sandbox check the
    /uploads/<name> handler already does, factored out so every new
    reader (v13.5: /api/pdf/*) reuses one real, already-proven-safe
    implementation instead of re-deriving it. Returns None (never
    raises) for anything outside the uploads root or failing the name
    whitelist — the caller decides how to report that honestly."""
    if not _UPLOAD_NAME_RE.match(rel):
        return None
    root = _uploads_root()
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None

# Time a human has to approve/decline a gated action before it auto-declines.
_CONFIRM_TIMEOUT_SECONDS = 300.0


class _PendingConfirmation:
    __slots__ = ("confirm_id", "prompt_text", "_event", "_approved")

    def __init__(self, confirm_id: str, prompt_text: str) -> None:
        self.confirm_id = confirm_id
        self.prompt_text = prompt_text
        self._event = threading.Event()
        self._approved = False

    def resolve(self, approved: bool) -> None:
        self._approved = approved
        self._event.set()

    def wait(self) -> bool:
        self._event.wait(_CONFIRM_TIMEOUT_SECONDS)
        return self._approved


class WebConfirmationGate:
    """Human-in-the-loop confirmation gate backed by the web UI.

    ``__call__(prompt_text)`` blocks until the browser resolves the pending
    confirmation via ``resolve(id, approved)``. Used as the
    ``confirmation_gate`` for ChatSession.

    ONE shared gate instance lives on the server; ``set_emit`` swaps which
    SSE stream its events go to. Chat requests are serialized by
    ``session_lock`` so at most one confirmation can be pending at a time,
    which also makes the single ``confirm_resolver`` unambiguous.
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.set_emit(emit)
        self._pending: dict[str, _PendingConfirmation] = {}
        self._lock = threading.Lock()
        self._next_id = 0

    def set_emit(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit

    def __call__(self, prompt_text: str) -> bool:
        with self._lock:
            self._next_id += 1
            confirm_id = f"confirm-{self._next_id}"
            pending = _PendingConfirmation(confirm_id, prompt_text)
            self._pending[confirm_id] = pending
        self._emit(
            {
                "type": "confirmation_requested",
                "id": confirm_id,
                "prompt": prompt_text,
            }
        )
        approved = pending.wait()
        with self._lock:
            self._pending.pop(confirm_id, None)
        return approved

    def resolve(self, confirm_id: str, approved: bool) -> bool:
        with self._lock:
            pending = self._pending.get(confirm_id)
        if pending is None:
            return False
        pending.resolve(approved)
        return True

    def pending_items(self) -> list[tuple[str, str]]:
        """[(confirm_id, prompt_text), ...] for every confirmation still
        awaiting a response — used by the "just say send" chat intercept to
        decide whether a short affirm phrase is unambiguous."""
        with self._lock:
            return [(p.confirm_id, p.prompt_text) for p in self._pending.values()]


# "Just say send": a short, exact imperative affirm phrase resolves a single
# pending confirmation instead of starting a normal chat turn. Matched only
# as a WHOLE (trimmed, case-insensitive) message — never as a substring of a
# longer sentence — so ordinary chat like "let's go ahead and refactor this"
# is never mistaken for an approval.
_IMPERATIVE_AFFIRM_PHRASES = frozenset(
    {
        "yes",
        "y",
        "yeah",
        "yep",
        "confirm",
        "confirmed",
        "approve",
        "approved",
        "send",
        "send it",
        "go",
        "go ahead",
        "do it",
        "do it now",
        "send now",
        "yes send",
        "yes send it",
        "yes go ahead",
        "yes do it",
        "ok send it",
        "ok go ahead",
        "okay send it",
        "confirm it",
    }
)

_TRAILING_PUNCT_RE = re.compile(r"[!.\s]+$")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_affirm_text(text: str) -> str:
    normalized = (text or "").strip().lower()
    normalized = _TRAILING_PUNCT_RE.sub("", normalized).strip()
    return _WHITESPACE_RE.sub(" ", normalized)


def _is_imperative_affirm(text: str) -> bool:
    """True only for an exact (whole-message) match against the curated
    affirm-phrase set — deliberately not a substring/regex-search test, so a
    phrase embedded in a longer sentence never false-triggers."""
    return _normalize_affirm_text(text) in _IMPERATIVE_AFFIRM_PHRASES


class ActivityTracker:
    """Per-subagent live activity tracker for the Agent Map window.

    A pure observer: fed from the same event_sink the chat SSE uses, and
    NEVER affects dispatch (a raising tracker is swallowed). Tracks each
    subagent's status (idle / computing / auth), its last tool activity,
    and a bounded recent feed so the map can show "what that agent is
    doing" right now.
    """

    _MAX_FEED = 30

    def __init__(self, registry: DispatchRegistry) -> None:
        self._lock = threading.Lock()
        self._tool_to_agent: dict[str, str] = {}
        for sub in registry.all_subagents():
            for tool in sub.tools:
                self._tool_to_agent[tool.name] = sub.name
        self._status: dict[str, str] = {
            sub.name: "idle" for sub in registry.all_subagents()
        }
        self._last: dict[str, dict[str, Any] | None] = {
            sub.name: None for sub in registry.all_subagents()
        }
        self._feed: dict[str, list[dict[str, Any]]] = {
            sub.name: [] for sub in registry.all_subagents()
        }
        self._broadcast: Any = None

    def set_broadcast(self, fn: Any) -> None:
        """Wire a real push channel (v13.6, item 7's own flagged SSE gap:
        "the current implementation polls a snapshot every 2s, not a
        genuine SSE event stream"). ``fn`` is ``server.events_broadcast
        .broadcast`` — the SAME real fan-out hub `/api/events` already
        uses for Freebuff/all_hands/state_change events (nothing new
        server-side, just a new real event TYPE on the existing bus).
        Optional: with no broadcaster wired, ActivityTracker behaves
        exactly as before (poll-only via /api/activity), never breaks."""
        self._broadcast = fn

    def on_event(self, entry: dict[str, Any]) -> None:
        """Observer hook — swallow everything so dispatch never breaks."""
        try:
            changed = self._record(entry)
            if changed and self._broadcast is not None:
                self._broadcast_changed(changed)
        except Exception:
            pass

    def _broadcast_changed(self, changed: set[str]) -> None:
        """Push a real, compact delta (only the agents that actually
        changed, not a full snapshot) over the existing SSE hub. Wrapped
        so a broken/disconnected broadcaster can never take dispatch
        down — same discipline as on_event's own outer try/except."""
        with self._lock:
            payload = {
                "type": "agent_activity",
                "agents": {
                    name: {"status": self._status[name], "last": self._last[name]}
                    for name in changed
                },
            }
        try:
            self._broadcast(payload)
        except Exception:
            pass

    def _agent_for(self, entry: dict[str, Any]) -> str | None:
        return self._tool_to_agent.get(entry.get("name", ""))

    def _record(self, entry: dict[str, Any]) -> set[str]:
        """Applies one event to internal state; returns the set of agent
        names whose status/last actually changed (v13.6, used to drive a
        real SSE push — see set_broadcast). Empty set means nothing
        broadcast-worthy happened (e.g. an event for an unmapped tool)."""
        etype = entry.get("type")
        changed: set[str] = set()
        with self._lock:
            if etype == "tool_use":
                agent = self._agent_for(entry)
                if agent is None:
                    return changed
                changed.add(agent)
                self._status[agent] = "computing"
                self._last[agent] = {
                    "tool": entry.get("name"),
                    "args": (entry.get("raw_arguments") or "")[:400],
                    "result": "",
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                self._feed[agent].append(
                    {
                        "type": "tool_use",
                        "tool": entry.get("name"),
                        "args": (entry.get("raw_arguments") or "")[:400],
                        "at": datetime.now().isoformat(timespec="seconds"),
                    }
                )
                self._trim(agent)
            elif etype == "tool_result":
                agent = self._agent_for(entry)
                if agent is None:
                    return changed
                changed.add(agent)
                if self._last[agent] is not None:
                    self._last[agent]["result"] = (entry.get("text") or "")[:400]
                self._feed[agent].append(
                    {
                        "type": "tool_result",
                        "tool": entry.get("name"),
                        "text": (entry.get("text") or "")[:400],
                        "at": datetime.now().isoformat(timespec="seconds"),
                    }
                )
                self._trim(agent)
            elif etype == "confirmation_requested":
                # Flag the currently-computing agent as awaiting auth.
                for agent, status in self._status.items():
                    if status == "computing":
                        self._status[agent] = "auth"
                        changed.add(agent)
                        self._feed[agent].append(
                            {
                                "type": "auth",
                                "prompt": (entry.get("prompt") or "")[:400],
                                "at": datetime.now().isoformat(timespec="seconds"),
                            }
                        )
                        break
            elif etype == "live":
                # v2.8: always-on poll loop activity. Maps the feed tool to
                # its agent and records a LIVE line — the text is the REAL
                # handler output (or an honest poll-failure error), so the
                # window shows genuine current activity, never a stub.
                agent = self._agent_for(entry)
                if agent is None:
                    return changed
                # A poll must never clobber a mid-chat computing/auth state:
                # only idle/live agents return to their always-on LIVE status.
                if self._status[agent] not in ("idle", "live"):
                    return changed
                changed.add(agent)
                self._status[agent] = "live"
                self._last[agent] = {
                    "tool": entry.get("name"),
                    "args": (entry.get("raw_arguments") or "")[:400],
                    "result": "",
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                self._feed[agent].append(
                    {
                        "type": "live",
                        "tool": entry.get("name"),
                        "text": (entry.get("text") or "")[:400],
                        "at": datetime.now().isoformat(timespec="seconds"),
                    }
                )
                self._trim(agent)
            elif etype in ("done", "error"):
                # A run terminal event returns COMPUTING/AUTH agents to idle.
                # LIVE agents keep their always-on status — their poll loop
                # is independent of chat runs (v2.8).
                for agent in self._status:
                    if self._status[agent] in ("computing", "auth"):
                        self._status[agent] = "idle"
                        changed.add(agent)
        return changed

    def _trim(self, agent: str) -> None:
        feed = self._feed[agent]
        if len(feed) > self._MAX_FEED:
            del feed[: len(feed) - self._MAX_FEED]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "agents": {
                    name: {
                        "status": self._status[name],
                        "last": self._last[name],
                        "feed": list(self._feed[name]),
                    }
                    for name in self._status
                }
            }


class AttentionQueue:
    """v13: cross-screen "needs attention" feed.

    Real gap this closes, found live: a fabricated no-tool RESEARCH answer,
    a NOT CONFIGURED tool, or a timed-out CLI call each land quietly in one
    turn's own reply text — nothing surfaces them anywhere else, so a caveat
    on turn 4 of a scrolled-past CODE conversation is invisible unless the
    user happens to scroll back and read that exact reply carefully. This is
    a pure observer, fed from the same event_sink every chat turn already
    emits (see webui.py's ``sink()`` closure) — it NEVER affects dispatch,
    matching ActivityTracker's own established convention just above.
    In-memory only, bounded, process-lifetime — restarting the server
    clears it, same tradeoff ActivityTracker already accepts.
    """

    _MAX_ITEMS = 50

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        self._next_id = 1

    def _add(self, kind: str, summary: str, screen: str, detail: str = "") -> None:
        with self._lock:
            item = {
                "id": self._next_id,
                "at": datetime.now().isoformat(timespec="seconds"),
                "kind": kind,
                "summary": summary[:400],
                "screen": screen or "HOME",
                "detail": detail[:2000],
                "dismissed": False,
            }
            self._next_id += 1
            self._items.append(item)
            if len(self._items) > self._MAX_ITEMS:
                del self._items[: len(self._items) - self._MAX_ITEMS]

    def on_event(self, entry: dict[str, Any], screen: str = "HOME") -> None:
        """Observer hook — swallow everything so a bug here can never break
        a real chat turn (Rule: an observer must never affect execution)."""
        try:
            self._record(entry, screen)
        except Exception:
            pass

    def _record(self, entry: dict[str, Any], screen: str) -> None:
        etype = entry.get("type")
        if etype == "tool_result":
            text = entry.get("text") or ""
            name = entry.get("name") or "tool"
            if text.startswith("NOT CONFIGURED:"):
                self._add("not_configured", f"{name}: {text[len('NOT CONFIGURED:'):].strip()}", screen, text)
            elif text.startswith("ERROR:"):
                self._add("tool_error", f"{name}: {text[len('ERROR:'):].strip()}", screen, text)
            elif "timed out" in text.lower():
                self._add("timeout", f"{name} timed out", screen, text)
        elif etype == "error":
            self._add("error", str(entry.get("message") or "an error occurred"), screen)
        elif etype == "budget_exhausted":
            self._add("budget_exhausted", str(entry.get("reason") or "tool budget exhausted"), screen)
        elif etype == "done":
            final_text = entry.get("final_text") or ""
            if "[DOURMOUSE: Grounded Mode was on" in final_text:
                self._add(
                    "ungrounded_answer",
                    "Answered with zero tool calls despite Grounded Mode being on",
                    screen,
                    final_text,
                )
            elif "[DOURMOUSE: plan step(s) not executed via tools" in final_text:
                self._add(
                    "incomplete_plan",
                    "A plan step was claimed done without ever calling its tool",
                    screen,
                    final_text,
                )

    def snapshot(self, include_dismissed: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items)
        if not include_dismissed:
            items = [i for i in items if not i["dismissed"]]
        return list(reversed(items))  # newest first

    def dismiss(self, item_id: int) -> bool:
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    item["dismissed"] = True
                    return True
        return False

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class _SSEStream:
    """Wraps a response for Server-Sent-Events writes (thread-safe)."""

    def __init__(self, wfile) -> None:
        self._wfile = wfile
        self._lock = threading.Lock()
        # v13.5 "stop/directive bug" fix: this used to be a bare `pass` —
        # a real fact (the client's socket is gone, which is exactly what
        # STOP's ctrl.abort() produces) was detected and then silently
        # thrown away, so the dispatch loop upstream had no way to learn
        # the user had cancelled and kept running to completion regardless
        # (see dispatch.run_dispatch_messages' should_stop docstring for
        # the full diagnosis). Now recorded so should_stop() below can
        # answer honestly.
        self.client_gone = False

    def emit(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, default=str)
        with self._lock:
            try:
                self._wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self._wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.client_gone = True  # client went away; loop continues harmlessly,
                # but should_stop() can now see it and end the dispatch run early

    def should_stop(self) -> bool:
        """The real cancellation predicate threaded into
        dispatch.run_dispatch_messages — see this class's own client_gone
        comment and that function's should_stop docstring paragraph."""
        return self.client_gone


class _SSEBroadcast:
    """Fan-out hub for server-push events (v5.9 Freebuff live activity).

    The HUD keeps a long-lived GET /api/events connection; anything the
    watcher (or other background sources) emit is written to EVERY
    connected stream. Register/unregister are cheap and thread-safe, and a
    dead client is dropped on its next failed write.
    """

    def __init__(self) -> None:
        self._clients: list[_SSEStream] = []
        self._lock = threading.Lock()

    def register(self, stream: _SSEStream) -> None:
        with self._lock:
            self._clients.append(stream)

    def unregister(self, stream: _SSEStream) -> None:
        with self._lock:
            try:
                self._clients.remove(stream)
            except ValueError:
                pass

    def broadcast(self, payload: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients)
        # emit() swallows write errors internally; clients that went away
        # are dropped when their handler's finally unregisters them.
        for c in clients:
            c.emit(payload)


def _resolve_server_config(config: Any | None) -> Any | None:
    """v3.1: the real serving paths (serve_forever, desktop.launch) call
    run_server with config=None, which would leave server.config None and
    silently disable per-agent models (DOURMOUSE_MODEL_<AGENT>) for focus_agent
    routes and the roster UI. Resolve the config here so the feature works
    in production. v4.0: uses the unified ``load_llm_config()`` resolver, so
    the default deployment is Ollama (local) whenever it answers, with an
    honest NVIDIA fallback. A missing key is honestly left as None (chat
    still fails loudly per-call) — never a silent stub."""
    if config is not None:
        return config
    try:
        return load_llm_config_with_fallback()
    except ValueError:
        return None


def _backend_label(config: Any | None) -> str:
    """The active backend name for the UI (v4.0): 'ollama' | 'omniroute' |
    'nvidia'.

    Deterministic (Rule 2.8): Ollama/OmniRoute config objects carry keyless
    markers (empty api_key + localhost base) — resolved by type, never a
    guess. Returns 'default' honestly when no config is attached (tests).
    """
    if config is None:
        return "default"
    if isinstance(config, OllamaConfig):
        return "ollama"
    if isinstance(config, OmniRouteConfig):
        return "omniroute"
    return "nvidia"


def _system_telemetry() -> dict:
    """Real host telemetry for the HUD header + metrics micro-bars.

    Rule 2.2: real numbers or an honest ``unavailable`` — never simulated.
    ``psutil`` is a project dependency (report.py uses it); if it is missing
    for any reason the HUD shows "—" instead of fabricated values.
    """
    try:
        import os
        import platform

        import psutil  # type: ignore[import-not-found]
        mem = psutil.virtual_memory()
        return {
            "host": platform.node(),
            "mem_used_gb": round(mem.used / (1024 ** 3), 2),
            "mem_total_gb": round(mem.total / (1024 ** 3), 2),
            "mem_pct": round(mem.percent, 1),
            "cpu_pct": round(psutil.cpu_percent(interval=0.1), 1),
            # os.getloadavg() is Unix-only; on Windows it raises AttributeError
            # and took the WHOLE telemetry payload down with it — host, memory
            # and CPU were all discarded over one optional field. Load average
            # is simply absent there rather than fatal.
            **(
                {"load": [round(x, 2) for x in os.getloadavg()]}
                if hasattr(os, "getloadavg")
                else {}
            ),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as exc:  # honest degradation (Rule 2.2)
        return {"unavailable": True, "detail": f"{type(exc).__name__}: {exc}"}


def _effective_model(config: Any | None, agent: str) -> str:
    """v3.1: the NVIDIA model a subagent runs on, for the roster/agent UI.

    Deterministic (Rule 2.8): resolved from the loaded config's per-agent
    map, else the default model, else an honest 'default' label when no
    config is attached (tests). Never fabricated.
    """
    if config is not None and hasattr(config, "model_for_agent"):
        return config.model_for_agent(agent)
    return "default"


# --------------------------------------------------------------------------- #
# world-monitor-expansion (UX pass item 5) — server-side warm cache for
# COMMS (Gmail inbox listing) and WORLD (world_pulse), refreshed on a
# background interval so the UI reads a warm cache instantly instead of
# paying a live IMAP/feed round trip on every screen open. Mirrors
# remote_server.py's start_health_warmer() EXACTLY: module-level
# lock + Event + Thread, an idempotent start_*(), a stop_*() that joins,
# a loop that swallows every exception (a warmer crash must never take
# the app down) and refreshes at TTL/2 (a full window of margin before
# the cache would otherwise go stale) via Event.wait() so a stop is
# honoured immediately instead of after a full interval.
# --------------------------------------------------------------------------- #

def _world_pulse_ttl() -> float:
    """Same env var + default as world_pulse.py's own (private) _ttl() —
    duplicated rather than imported so this stays a read of world_pulse's
    PUBLIC contract (its TTL is documented, stable env-var behavior) and
    never reaches into its internals."""
    try:
        return float(os.environ.get("DOURMOUSE_WORLD_PULSE_TTL", "120"))
    except ValueError:
        return 120.0


_world_pulse_warmer_thread: threading.Thread | None = None
_world_pulse_warmer_stop = threading.Event()
_world_pulse_warmer_lock = threading.Lock()


def start_world_pulse_warmer() -> bool:
    """Background thread that keeps world_pulse's own cache warm.

    world_pulse.world_pulse_snapshot() already caches (default 120s TTL,
    DOURMOUSE_WORLD_PULSE_TTL) — see its module docstring's own real
    rate-limit awareness (Yahoo 429s, ECB's keyless/unthrottled endpoint,
    etc.) for why that TTL exists — but until now that cache only ever
    refreshed lazily, on whichever request happened to arrive after it
    went stale, so THAT request paid the full multi-source fetch cost
    (world_pulse.py's own ThreadPoolExecutor across every configured
    source). This warmer proactively refreshes at TTL/2 = 60s by default
    so the WORLD screen and /api/worldmap always read an already-warm
    cache — 60s is a real, deliberate choice: it stays well inside
    world_pulse's own 120s TTL window (never lets the cache actually go
    stale between passes, the same margin start_health_warmer() uses),
    while not re-hitting every upstream feed source more often than the
    120s TTL was already judged safe for (Yahoo's documented rate limits
    among them) — the interval tracks whatever DOURMOUSE_WORLD_PULSE_TTL
    is set to, not a second, independently-drifting constant.

    Idempotent; returns True if a thread is running when it returns.
    Opt out with DOURMOUSE_WORLD_PULSE_WARMER=0.
    """
    if os.environ.get("DOURMOUSE_WORLD_PULSE_WARMER", "1").strip().lower() in (
        "0", "false", "no", "off",
    ):
        return False
    global _world_pulse_warmer_thread
    with _world_pulse_warmer_lock:
        if _world_pulse_warmer_thread is not None and _world_pulse_warmer_thread.is_alive():
            return True
        _world_pulse_warmer_stop.clear()

        def _loop() -> None:
            from dourmouse.world_pulse import world_pulse_snapshot

            while not _world_pulse_warmer_stop.is_set():
                try:
                    world_pulse_snapshot(force=True)
                except Exception:  # noqa: BLE001,S110 - a warmer must never crash the app
                    pass
                _world_pulse_warmer_stop.wait(max(_world_pulse_ttl() / 2.0, 1.0))

        _world_pulse_warmer_thread = threading.Thread(
            target=_loop, name="dourmouse-world-pulse-warmer", daemon=True
        )
        _world_pulse_warmer_thread.start()
        return True


def stop_world_pulse_warmer(timeout: float = 2.0) -> None:
    global _world_pulse_warmer_thread
    _world_pulse_warmer_stop.set()
    with _world_pulse_warmer_lock:
        thread = _world_pulse_warmer_thread
        _world_pulse_warmer_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def _gmail_inbox_ttl() -> float:
    try:
        return float(os.environ.get("DOURMOUSE_GMAIL_INBOX_TTL", "180"))
    except ValueError:
        return 180.0


def _gmail_inbox_max_results() -> int:
    try:
        return int(os.environ.get("DOURMOUSE_GMAIL_INBOX_MAX", "25"))
    except ValueError:
        return 25


#: The warm cache itself — one entry, the "" query "recent inbox" view
#: /api/gmail/search's own handler already special-cases (see google_
#: services.gmail_search's own "empty query browses the most recent
#: messages" docstring). A REAL user search (any non-empty q) always
#: bypasses this cache entirely and hits Gmail live — caching an
#: arbitrary search query would be a much bigger, riskier feature
#: (unbounded cache growth, staleness on a query someone expects fresh
#: results from) that item 5 never asked for; only the listing COMMS
#: shows on open needs to feel instant.
_gmail_inbox_cache_lock = threading.Lock()
_gmail_inbox_cache: dict[str, Any] = {"at": 0.0, "max_results": None, "payload": None}


def _fetch_gmail_inbox_payload(max_results: int) -> dict[str, Any]:
    """The real (uncached) "" query listing, shaped exactly like
    /api/gmail/search's own JSON contract below. Never raises — every
    failure (NOT CONFIGURED, IMAP down, ...) is gmail_search's own honest
    text, reported through the same {"ok": False, "error": ...} shape the
    live endpoint already uses."""
    from dourmouse.google_services import gmail_search

    try:
        raw = gmail_search("", max_results)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    rows = []
    for line in raw.splitlines():
        m = re.match(r"^- \[(.*?)\] from (.*?) \| (.*?) \(uid (.+)\)$", line)
        if m:
            rows.append({
                "date": m.group(1), "from": m.group(2),
                "subject": m.group(3), "id": m.group(4),
            })
    return {"ok": True, "rows": rows, "raw": raw if not rows else None}


def _gmail_inbox_cache_get(max_results: int) -> dict[str, Any] | None:
    """The warm cache's payload if present, fresh, and for the SAME
    max_results the caller actually wants — else None. Never fetches: a
    None here means the caller is on a genuine cold start (nothing has
    warmed this yet) and must do its own one-off live fetch."""
    now = time.monotonic()
    with _gmail_inbox_cache_lock:
        c = _gmail_inbox_cache
        if (
            c["payload"] is not None
            and c["max_results"] == max_results
            and (now - c["at"]) < _gmail_inbox_ttl()
        ):
            return c["payload"]
    return None


def _gmail_inbox_cache_refresh(max_results: int) -> dict[str, Any]:
    """Always does the real (uncached) fetch and stores the result as the
    new warm-cache entry. Called by the warmer loop on its interval, and
    also by a request that hit a genuine cold start (see the endpoint
    below) so that ONE unlucky first request still gets a real answer
    instead of an empty cache forever."""
    payload = _fetch_gmail_inbox_payload(max_results)
    with _gmail_inbox_cache_lock:
        _gmail_inbox_cache["at"] = time.monotonic()
        _gmail_inbox_cache["max_results"] = max_results
        _gmail_inbox_cache["payload"] = payload
    return payload


_gmail_warmer_thread: threading.Thread | None = None
_gmail_warmer_stop = threading.Event()
_gmail_warmer_lock = threading.Lock()


def start_gmail_inbox_warmer() -> bool:
    """Background thread that keeps the COMMS "recent inbox" listing warm.

    gmail_search() has no cache of its own — every call opens a fresh
    IMAP connection and logs in (see google_services._imap()), which is
    real per-call cost AND the kind of frequent-login pattern mail
    providers rate-limit/flag if hammered. 180s (DOURMOUSE_GMAIL_INBOX_TTL)
    is the deliberate choice here: an inbox listing is far less time-
    critical than world_pulse's hazard data (a new email landing 1-2
    minutes before the cache catches up is a non-event, unlike a storm
    / conflict update), so the interval leans toward fewer IMAP logins
    rather than toward freshness — refreshed at half that (90s), the
    same TTL/2 margin convention as every other warmer here, so a request
    is never the one that hits an expired entry and pays the live cost.

    A REAL search (any non-empty query) is never affected by this warmer
    or its cache — see _gmail_inbox_cache's own docstring.

    Idempotent; returns True if a thread is running when it returns.
    Opt out with DOURMOUSE_GMAIL_WARMER=0 — also silently a no-op when
    Gmail itself isn't configured (gmail_configured() checks the address
    + app password are set), so a fresh install with no mail account
    connected never opens a socket or logs a spurious failure.
    """
    if os.environ.get("DOURMOUSE_GMAIL_WARMER", "1").strip().lower() in (
        "0", "false", "no", "off",
    ):
        return False
    from dourmouse.google_services import gmail_configured

    if not gmail_configured():
        return False
    global _gmail_warmer_thread
    with _gmail_warmer_lock:
        if _gmail_warmer_thread is not None and _gmail_warmer_thread.is_alive():
            return True
        _gmail_warmer_stop.clear()

        def _loop() -> None:
            while not _gmail_warmer_stop.is_set():
                try:
                    _gmail_inbox_cache_refresh(_gmail_inbox_max_results())
                except Exception:  # noqa: BLE001,S110 - a warmer must never crash the app
                    pass
                _gmail_warmer_stop.wait(max(_gmail_inbox_ttl() / 2.0, 1.0))

        _gmail_warmer_thread = threading.Thread(
            target=_loop, name="dourmouse-gmail-inbox-warmer", daemon=True
        )
        _gmail_warmer_thread.start()
        return True


def stop_gmail_inbox_warmer(timeout: float = 2.0) -> None:
    global _gmail_warmer_thread
    _gmail_warmer_stop.set()
    with _gmail_warmer_lock:
        thread = _gmail_warmer_thread
        _gmail_warmer_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def build_roster_payload(
    registry: DispatchRegistry, config: Any | None = None
) -> dict[str, Any]:
    subagents = []
    for subagent in sorted(registry.all_subagents(), key=lambda s: s.name):
        tools = []
        for tool in subagent.tools:
            tools.append(
                {
                    "name": tool.name,
                    "permission": tool.permission.value,
                    "description": tool.description.split(".")[0][:120],
                }
            )
        subagents.append(
            {
                "name": subagent.name,
                "domain": subagent.domain,
                "description": subagent.description,
                "model": _effective_model(config, subagent.name),
                "tools": tools,
            }
        )
    return {"subagents": subagents}


def build_link_topology(registry: DispatchRegistry) -> dict[str, Any]:
    """Neural-link topology for the Agent Map (v2.3).

    Nodes are every subagent; edges are the REAL relationships the engine
    supports:

    - ``delegate``: the orchestrator can spawn a nested run on every agent
      (self-dispatch, depth/budget bounded) — the dispatch paths.
    - ``memory``: the memory subagent is the shared-truth hub every agent can
      read/write (Both domain) — the neural "links".
    - ``peer``: agents in the same domain cluster (General / Live) form a
      working group.

    Deterministic (Rule 2.8) — pure registry structure, no LLM judgment.
    """
    agents = sorted(registry.all_subagents(), key=lambda s: s.name)
    names = [a.name for a in agents]
    nodes = [
        {
            "name": a.name,
            "domain": a.domain,
            "description": a.description,
            "tool_count": len(a.tools),
        }
        for a in agents
    ]
    edges: list[dict[str, str]] = []
    for name in names:
        if name == "orchestrator":
            continue
        edges.append({"source": "orchestrator", "target": name, "kind": "delegate"})
    for name in names:
        if name in ("memory", "orchestrator"):
            continue
        edges.append({"source": "memory", "target": name, "kind": "memory"})
    # Peer edges within the same domain cluster (undirected: one row each).
    by_domain: dict[str, list[str]] = {}
    for a in agents:
        by_domain.setdefault(a.domain, []).append(a.name)
    for _domain, group in by_domain.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if group[j] != "orchestrator":
                    edges.append({"source": group[i], "target": group[j], "kind": "peer"})
    return {"nodes": nodes, "edges": edges}


_SETUP_STATUS_CACHE_ENV = "DOURMOUSE_SETUP_CACHE_TTL"
_SETUP_STATUS_DEFAULT_TTL = 20.0  # seconds
_setup_status_cache: dict[str, Any] = {"server_id": None, "at": 0.0, "result": None}
_setup_status_cache_lock = threading.Lock()


def _setup_status_cache_ttl() -> float:
    try:
        return float(os.environ.get(_SETUP_STATUS_CACHE_ENV, str(_SETUP_STATUS_DEFAULT_TTL)))
    except ValueError:
        return _SETUP_STATUS_DEFAULT_TTL


def build_setup_status(server) -> dict[str, Any]:
    """v13.5 (live-diagnosed, real bug — "it didn't load properly in
    preview"): a real GET /api/setup on this exact machine measured
    15.4s. build_setup_status() (the uncached implementation below) makes
    several real, synchronous network/subprocess probes with no caching
    of its own — the compute-node health probe (dourmouse.remote_server.
    server_status(), called TWICE per request: once directly here and
    again inside connections.check_connections()), `claude --version`
    (subprocess, up to a real 10s timeout), and a macOS Keychain lookup
    (subprocess, up to 5s). Each individual probe already has its own
    documented, deliberately-short timeout (world_pulse_status's own
    docstring already fixed the identical class of bug for ITS probe,
    calling this exact endpoint "a panel whose entire purpose is a quick
    capability checklist") — but nothing wrapped the WHOLE function, so
    every poll paid the full cold cost again regardless.

    Cached here by (server identity, time) — NOT a bare time-only cache:
    keying on id(server) too means two DIFFERENT server objects (as every
    test in this suite constructs, e.g. test_atlas_cli.py's own direct
    call) never share a stale result, while the ONE real long-lived
    server this endpoint actually serves in production gets real caching
    across repeated polls. Default TTL 20s (DOURMOUSE_SETUP_CACHE_TTL to
    override) — the checklist doesn't change meaningfully faster than
    that in practice.
    """
    ttl = _setup_status_cache_ttl()
    now = time.monotonic()
    with _setup_status_cache_lock:
        if (
            _setup_status_cache["server_id"] == id(server)
            and (now - _setup_status_cache["at"]) < ttl
        ):
            return _setup_status_cache["result"]
    result = _build_setup_status_uncached(server)
    with _setup_status_cache_lock:
        _setup_status_cache.update(server_id=id(server), at=now, result=result)
    return result


def _build_setup_status_uncached(server) -> dict[str, Any]:
    """The real, honest capability checklist for the SETUP panel (Rule
    2.2) — every entry reports configured True/False + a one-line fix,
    never fabricates a capability. See build_setup_status() above for the
    caching wrapper (and the real 15.4s-load bug it fixes) around this.
    """
    import os

    items: dict[str, dict[str, Any]] = {}
    cfg = getattr(server, "config", None)
    items["llm_backend"] = {
        "configured": cfg is not None,
        "detail": (
            f"{_backend_label(cfg)} · {cfg.model}"
            if cfg is not None
            else "no backend config"
        ),
        "hint": "DOURMOUSE_LLM_BACKEND=ollama|omniroute|nvidia in .env",
    }
    # v5.26: the DOURMOUSE compute node (Dell) — compute infrastructure,
    # never a second DOURMOUSE. Honest online/offline from /v1/status.
    try:
        from dourmouse.remote_server import server_status

        srv = server_status()
        items["server"] = {
            "configured": True,  # env default exists; the node is optional
            "detail": (
                f"{(srv.get('node') or 'node')} · {(srv.get('model') or '?')} · "
                f"{srv.get('latency_ms')}ms ONLINE"
                if srv.get("online")
                else "compute node OFFLINE — local AI stays in charge"
            ),
            "hint": (
                "DOURMOUSE_SERVER_URL (default http://192.168.1.108:8000) — "
                "failover to local AI is automatic"
            ),
        }
    except Exception:  # noqa: BLE001 -- a broken import never blocks setup
        items["server"] = {
            "configured": False,
            "detail": "server module unavailable",
            "hint": "set DOURMOUSE_SERVER_URL",
        }
    # v5.27: the SELF-HOSTED world monitor (World Pulse).
    try:
        from dourmouse.world_pulse import world_pulse_status

        wp = world_pulse_status()
        items["world_pulse"] = {
            "configured": wp.get("configured", False),
            "detail": (
                f"{wp.get('sources_up', 0)}/{wp.get('sources_total', 0)} sources · "
                f"pulse {wp.get('pulse_score')} {wp.get('pulse_label')}"
                if wp.get("online")
                else "all sources offline"
            ),
            "hint": "self-hosted keyless monitor — no key needed",
        }
    except Exception:  # noqa: BLE001 -- a broken import never blocks setup
        items["world_pulse"] = {
            "configured": False,
            "detail": "world_pulse module unavailable",
            "hint": "",
        }
    try:
        from dourmouse.voice import voice_status

        vs = voice_status()
        items["voice"] = {
            "configured": bool(vs.get("enabled")),
            "detail": vs.get("stt", "") + " / " + vs.get("tts", ""),
            "hint": "DOURMOUSE_VOICE=1 (+ whisper/piper models)",
        }
    except Exception:  # noqa: BLE001 -- a broken voice import never blocks setup
        items["voice"] = {"configured": False, "detail": "voice module error", "hint": ""}
    items["codex"] = {
        "configured": bool(os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY")),
        "detail": "key " + ("present" if os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY") else "MISSING"),
        "hint": "CODEX_API_KEY in .env",
    }
    items["deepseek"] = {
        "configured": bool(
            os.environ.get("FREEBUFF_DEEPSEEK_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            # v5.1: no DeepSeek key needed — NVIDIA NIM hosts DeepSeek
            # models, so the user's NVIDIA_API_KEY powers this backend too.
            or os.environ.get("NVIDIA_API_KEY")
        ),
        "detail": "key " + (
            "present"
            if os.environ.get("FREEBUFF_DEEPSEEK_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("NVIDIA_API_KEY")
            else "MISSING"
        ),
        "hint": "DEEPSEEK_API_KEY / FREEBUFF_DEEPSEEK_API_KEY, or reuse NVIDIA_API_KEY",
    }
    try:
        from dourmouse.general_roster import _find_claude_cli

        cli = _find_claude_cli()
        items["claude"] = {
            "configured": cli is not None,
            "detail": f"CLI: {cli}" if cli else "claude CLI not on PATH",
            "hint": "npm i -g @anthropic-ai/claude-code",
        }
    except Exception:  # noqa: BLE001
        items["claude"] = {"configured": False, "detail": "check error", "hint": ""}
    try:
        # v5.3: the Codex CLI (ChatGPT login) and Freebuff Desktop status.
        from dourmouse.connections import check_connections

        conn = check_connections()
        items["codex_cli"] = {
            "configured": conn["codex"]["ok"],
            "detail": conn["codex"]["detail"],
            "hint": conn["codex"]["hint"],
        }
        items["freebuff"] = {
            "configured": conn["freebuff"]["ok"],
            "detail": conn["freebuff"]["detail"],
            "hint": conn["freebuff"]["hint"],
        }
        # v5.4: the ATLAS bridge row (repo + venv present and usable).
        items["atlas"] = {
            "configured": conn["atlas"]["ok"],
            "detail": conn["atlas"]["detail"],
            "hint": conn["atlas"]["hint"],
        }
    except Exception:  # noqa: BLE001 -- a broken probe never kills setup
        items["codex_cli"] = {"configured": False, "detail": "check error", "hint": ""}
        items["freebuff"] = {"configured": False, "detail": "check error", "hint": ""}
        items["atlas"] = {"configured": False, "detail": "check error", "hint": ""}
    try:
        from dourmouse.google_services import gmail_configured
        from dourmouse.google_services import status as gmail_status

        items["gmail"] = {
            "configured": gmail_configured(),
            "detail": gmail_status()["detail"],
            "hint": gmail_status()["hint"],
        }
    except Exception:  # noqa: BLE001
        items["gmail"] = {"configured": False, "detail": "google module error", "hint": ""}
    try:
        from dourmouse.spotify_services import status as spotify_status

        sst = spotify_status()
        items["spotify"] = {
            "configured": bool(sst.get("linked")),
            "detail": sst.get("detail", "not linked"),
            "hint": sst.get("hint", "developer.spotify.com -> Client ID + spotify_login"),
        }
    except Exception:  # noqa: BLE001
        items["spotify"] = {"configured": False, "detail": "spotify module error", "hint": ""}
    items["upload"] = {
        "configured": True,
        "detail": str(_uploads_root()),
        "hint": "drop files in the HUD",
    }
    mem = getattr(server, "memory", None)
    mem_ok = mem is not None and learn_enabled()
    mem_count = 0
    mem_slow = False
    if mem_ok:
        # v13.5 (live-diagnosed, real bug): mem.count() is fast for a local
        # MemoryStore but a REAL, un-cached network call for a
        # RemoteMemoryStore (a real remote RAG host, up to its own 15s
        # timeout — memory_store.py's own default) — measured live at
        # 15.6s on the very first /api/setup poll after a fresh start,
        # exactly the "SETUP panel whose entire purpose is a quick
        # checklist" problem world_pulse_status's own docstring already
        # names for a different probe. build_setup_status's own cache
        # above fixes every poll AFTER the first; this bounds the first
        # one too — a real background thread with a short real wait
        # (2s), falling back to an honest "checking…" instead of blocking
        # the whole request on the store's full internal timeout. The
        # count still gets the real remote answer eventually (the thread
        # keeps running even after this function gives up waiting on it);
        # this just stops one slow remote host from making the FIRST
        # setup-panel view of a session look hung.
        result: dict[str, Any] = {}

        def _count_in_background() -> None:
            try:
                result["count"] = mem.count()
            except Exception as exc:  # noqa: BLE001 -- a broken store must not kill setup
                result["error"] = exc

        t = threading.Thread(target=_count_in_background, daemon=True)
        t.start()
        t.join(timeout=2.0)
        if "count" in result:
            mem_count = result["count"]
        else:
            mem_slow = True  # either still running, or it raised -- either way, no real number yet
    items["memory"] = {
        "configured": mem_ok,
        "detail": ("checking… (remote store slow to answer)" if mem_slow else f"{mem_count} facts") if mem_ok else "off",
        "hint": "DOURMOUSE_LEARN=1 + FTS5 store",
    }
    if cfg is not None and hasattr(cfg, "model_for_agent"):
        items["orchestrator_model"] = {
            "configured": True,
            "detail": cfg.model_for_agent("orchestrator"),
            "hint": "ollama pull <that model> on a fresh device",
        }
    items["live"] = {
        "configured": getattr(server, "live_runtime", None) is not None,
        "detail": "poll loops running" if getattr(server, "live_runtime", None) else "off",
        "hint": "DOURMOUSE_LIVE=1",
    }
    return {"items": items}


def _bootstrap_neuro() -> None:
    """v5.6: background bootstrap of the neural orchestrator.

    Replays workspace/sessions/*.jsonl into experiences and auto-retrains
    when enough exist. Runs in a daemon thread at serve time; a raising
    store must never affect serving.
    """
    try:
        from dourmouse.orch_net import bootstrap_from_sessions

        bootstrap_from_sessions()
    except Exception:  # noqa: BLE001
        pass


def _safe_asset_path(rel: str) -> Path | None:
    target = (_UI_DIR / rel).resolve()
    try:
        target.relative_to(_UI_DIR.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})
# Host-header validation before any value is rendered into the pairing page:
# hostname / IPv4 / bracketed IPv6 only — rejects header-injection outright.
_SAFE_HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\])$")

#: Abandoned Google OAuth flows (user started login, never completed consent)
#: are pruned after this long — one dict entry per attempt, never a leak.
_OAUTH_PENDING_TTL_SECONDS = 600.0


def _pending_created_ts(pending: dict[str, Any] | None) -> float | None:
    """Unix timestamp of an OAuth pending entry's creation, or None when
    absent/unparseable (treated as stale by the pruner)."""
    raw = (pending or {}).get("created")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _phone_url_host(hostname: str) -> str:
    """A safe non-loopback host from the Host header, or '' to skip it."""
    host = (hostname or "").strip()
    if not host or host in _LOOPBACK_HOSTS:
        return ""
    if not _SAFE_HOST_RE.match(host):
        return ""
    return host


def _qr_svg(url: str) -> str:
    """A real scannable QR for ``url`` as an inline SVG, or '' honestly.

    Uses segno (pure-Python, zero native deps) when installed; without it
    returns an empty string and the pairing page shows the URL text only —
    never a fake/unscannable QR (Rule 2.2)."""
    try:
        import segno
    except ImportError:
        return ""
    try:
        qr = segno.make(url, error="m")
        return qr.svg_data_uri(scale=4, dark="#4FC3F7", light="rgba(0,0,0,0)")
    except Exception:  # noqa: BLE001 -- a broken QR must never break the page
        return ""


#: Cache + in-flight registry for GET /api/vision/status dependency probes
#: (overlay's pywebview, tray's pystray+Pillow, wakeword's openwakeword +
#: sounddevice). These are real import probes, not fabricated — but real,
#: once per PROCESS is enough: whether a package is installed cannot change
#: while this server is running, and importing ``openwakeword`` (its ONNX
#: runtime) genuinely took several seconds to over ten in this sandbox on
#: first import — probing it fresh, and synchronously, on every poll would
#: turn an honest status endpoint into a multi-second (or worse) stall.
#: Each probe runs on its own daemon thread the first time it's needed;
#: ``_vision_dependency_status`` waits only up to a shared time budget and
#: reports None (never a fabricated True/False) if the probe is still
#: genuinely unresolved when the budget runs out — the thread keeps running
#: in the background and every request after it gets the real cached answer.
_VISION_DEPENDENCY_CACHE: dict[str, bool] = {}
_VISION_PROBE_THREADS: dict[str, threading.Thread] = {}
_VISION_PROBE_LOCK = threading.Lock()


def _vision_dependency_start(key: str, probe: Callable[[], Any]) -> None:
    """Kick off (or no-op if already resolved/running) a background probe."""
    if key in _VISION_DEPENDENCY_CACHE:
        return
    with _VISION_PROBE_LOCK:
        if key in _VISION_DEPENDENCY_CACHE or key in _VISION_PROBE_THREADS:
            return

        def _run() -> None:
            try:
                probe()
                _VISION_DEPENDENCY_CACHE[key] = True
            except Exception:  # noqa: BLE001 -- any probe failure reads as "not available"
                _VISION_DEPENDENCY_CACHE[key] = False

        thread = threading.Thread(
            target=_run, daemon=True, name=f"dourmouse-vision-probe-{key}"
        )
        _VISION_PROBE_THREADS[key] = thread
        thread.start()


def _vision_dependency_status(key: str, deadline: float) -> bool | None:
    """The real answer if it's known; otherwise waits until ``deadline``
    (a ``time.monotonic()`` value shared across this request's several
    probes, so N probes cost at most one time budget, not N of them) and
    returns None — an honest "still checking", not a guess — if the probe
    hasn't resolved by then."""
    if key in _VISION_DEPENDENCY_CACHE:
        return _VISION_DEPENDENCY_CACHE[key]
    thread = _VISION_PROBE_THREADS.get(key)
    if thread is not None:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return _VISION_DEPENDENCY_CACHE.get(key)


class _Handler(BaseHTTPRequestHandler):
    server_version = "AtlasDourmouseWebUI/0.1"

    def log_message(self, fmt, *args):  # quieter logs
        pass

    # -- v4.0 auth gate (multi-device, spec Phase 9) ---------------------- #

    def _authorized(self) -> bool:
        """True when this request may proceed.

        - No DOURMOUSE_ACCESS_TOKEN configured → everything allowed (loopback
          posture, unchanged).
        - Loopback client (127.0.0.1 / ::1 / localhost) → allowed (the
          desktop app and local chat stay token-free — zero config change).
        - Otherwise: Bearer header OR dourmouse_session cookie must match the
          configured token. Constant-time comparison (no timing side
          channel), Rule 2.6 (token from env, never logged).
        - v5.15: a valid Google user session (dourmouse_user_session cookie)
          also authorizes — anyone who signed in with their own Google
          account gets this server's access on the same terms.
        """
        import hmac

        token = getattr(self.server, "access_token", "") or ""
        if not token:
            return True
        host = (self.client_address[0] if self.client_address else "") or ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "dourmouse_session" and hmac.compare_digest(value, token):
                return True
            if name == "dourmouse_user_session" and self._session_user() is not None:
                return True
        return False

    def _session_user(self) -> str | None:
        """The logged-in Google user (email) for this request, or None."""
        store = getattr(self.server, "auth", None)
        if store is None:
            return None
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "dourmouse_user_session":
                return store.session_email(value.strip())
        return None

    def _send_unauthorized(self) -> None:
        """401 for APIs, a redirect to /login for page navigations."""
        if self.path.startswith("/api/"):
            self._send_json({"error": "unauthorized"}, status=401)
            return
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- plumbing --------------------------------------------------------- #

    def _send_json(self, payload: dict[str, Any], status: int = 200,
                   headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes ----------------------------------------------------------- #

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/login":
            # v4.0: the token-login page is reachable WITHOUT auth.
            self._serve_static("login.html")
            return
        if path == "/mobile":
            # v5.13: the phone-pairing page is reachable WITHOUT auth (a
            # fresh phone must be able to land on it before it has the
            # token). Renders the real connection status + scannable QR
            # codes server-side.
            self._handle_mobile_page()
            return
        if path == "/voice":
            # v5.x: minimal voice-command interface (ui/voice.html) — like
            # /mobile, served pre-auth so a fresh device can land on it;
            # its API calls (status/STT/TTS/chat) still respect the token
            # gate and it points the operator to /login when needed.
            self._serve_static("voice.html")
            return
        if path == "/hud":
            # v5.x: Stark MK. Zero HUD (ui/hud.html) — the tactical command
            # surface. Served pre-auth like /voice; its API calls respect
            # the token gate and it points the operator to /login when
            # needed.
            self._serve_static("hud.html")
            return
        if path == "/api/auth/status":
            # v5.15: Google login status — pre-auth so the login page can
            # decide whether to show the sign-in button (honest, Rule 2.2).
            self._handle_auth_status()
            return
        if path == "/api/auth/google/start":
            # v5.15: kick off the Google OAuth dance (redirect to Google).
            self._handle_google_start()
            return
        if path == "/api/auth/google/callback":
            # v5.15: Google redirects here with ?code&state after consent.
            self._handle_google_callback()
            return
        if path == "/api/auth/me":
            # v5.15: who am I (null when not signed in with Google). Placed
            # PRE-gate (reviewer-caught): it only reveals the request's own
            # identity — a signed-out client must get {"me": null}, not 401.
            self._handle_auth_me()
            return
        if path == "/api/auth/claim":
            # v5.22.11: system-browser sign-in bridge — adopt the session a
            # real-browser consent parked under our claim code. PRE-gate like
            # status: it only unlocks a session the caller's own code owns.
            self._handle_auth_claim()
            return
        if not self._authorized():
            self._send_unauthorized()
            return
        if path in ("/setup", "/setup.html"):
            # v8.9: first-run setup. Served without a session for the same
            # reason the setup POSTs are — a fresh install has no config to
            # authenticate against yet.
            self._serve_static("setup.html")
            return
        if path in ("/", "/console", "/console.html"):
            # v8.9: an install with no working backend goes to setup instead
            # of a console that looks alive and answers nothing. That silent
            # dead-end is exactly what a packaged build hits, because the
            # bundle deliberately ships no .env.
            try:
                from dourmouse.config import is_configured

                if not is_configured():
                    self.send_response(302)
                    self.send_header("Location", "/setup")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            except Exception:  # noqa: BLE001 - never block the UI on this check
                pass
            # v8.7 — the console is now the default surface: nine screens
            # over the existing endpoints, with a code-toolchain picker and
            # a voice mode that surfaces live tool activity. The HUD is
            # unchanged and still served at /hud (and /index.html), so
            # nothing was removed — only the default landing changed.
            self._serve_static("console.html")
        elif path in ("/dispatch", "/index.html"):
            # v8.7: the HUD, unchanged. It keeps /index.html because the
            # deeplink redirect targets that path — the hash router that
            # serves #/atlas, #/world, #/portfolio etc lives ONLY here
            # (console.html has no hash routing), so deeplinks must not be
            # pointed at "/" now that "/" is the console.
            self._serve_static("index.html")
        elif path in ("/os", "/os.html"):
            # v8.5 — the OS interface: conversation-first with real system
            # panels (compute, tools, memory, connections) bound to the
            # existing endpoints. Additive: "/" and "/app" are untouched.
            self._serve_static("os.html")
        elif path in ("/app", "/app.html"):
            # v8.3 — the consumer interface: chat-first, three modes
            # (chat/research/code) mapped to real focus_agents. Served
            # alongside the HUD rather than replacing it, so the operator
            # surface stays available at "/".
            self._serve_static("app.html")
        elif path in ("/map", "/map.html"):
            self._serve_static("map.html")
        elif path in ("/workspace", "/workspace.html"):
            # world-monitor-expansion: the Vision floating multi-window
            # workspace (item 1 of the task) — real draggable/resizable
            # panels (Gmail/companion chat/research/world map), hand-
            # gesture window control, and voice commands. A new file
            # (ui/workspace.html) rather than folding into console.html's
            # already-370KB single script, matching how ui/index.html (the
            # HUD) already sits alongside it as its own page.
            self._serve_static("workspace.html")
        elif path in ("/atlas-lab", "/atlas-lab.html"):
            # v5.22.6: the dedicated ATLAS window — a second DOURMOUSE that
            # is ONLY the strategy lab (live GitHub-synced leaderboard).
            self._serve_static("atlas_lab.html")
        elif path in ("/all-hands", "/all-hands.html"):
            # v5.22.9: the dedicated ALL HANDS window — one goal, every
            # resource (Claude/Codex/ChatGPT/DeepSeek/web) in parallel.
            self._serve_static("all_hands.html")
        elif path in ("/design-system", "/design-system.html"):
            # The live reference for ui/assets/dourmouse-ui.css: every
            # component rendered at once, so a change to the shared design
            # system can be eyeballed in one place instead of hunting for a
            # screen that happens to use the control being changed.
            self._serve_static("design-system.html")
        elif path.startswith("/agent/"):
            # v2.7: each agent gets its own LIVE window (/agent/<name>).
            agent_name = urllib.parse.unquote(path[len("/agent/"):]).strip("/")
            if agent_name not in self.server.registry.subagent_names:
                self.send_error(404, "no such agent")
            else:
                self._serve_static("agent.html")
        elif path.startswith("/api/agent/"):
            agent_name = urllib.parse.unquote(path[len("/api/agent/"):]).strip("/")
            self._handle_agent_api(agent_name)
        elif path.startswith("/assets/"):
            # v5.22.3 fix: the URL prefix is /assets/<file> but the files
            # live at ui/assets/<file> — re-add the directory before serving
            # (a latent bug: every /assets/* request used to 404).
            self._serve_static("assets/" + path[len("/assets/"):])
        elif path == "/sw.js":
            # v5.20: the offline-shell service worker (desktop portfolio
            # Phase 5). Real JS content type; no-store keeps the SW script
            # re-validating per the browser's own update rules.
            self._serve_static("sw.js")
        elif path == "/manifest.json":
            # v5.22.3: the PWA web manifest — lets a phone install
            # DourMouse as a standalone app (own icon, full screen).
            self._serve_static("manifest.json")
        elif path == "/api/roster":
            self._send_json(
                build_roster_payload(self.server.registry, self.server.config)
            )
        elif path == "/api/links":
            self._send_json(build_link_topology(self.server.registry))
        elif path == "/api/activity":
            self._send_json(self.server.tracker.snapshot())
        elif path == "/api/tasks":
            # v5.x: read-only task list for the HUD — same store the tasks
            # agent uses (workspace/tasks.json), so the dashboard and the
            # agent never diverge.
            from dourmouse.live_feeds import list_tasks

            tasks = list_tasks(include_done=True)
            self._send_json(
                {
                    "tasks": tasks,
                    "active": sum(1 for t in tasks if not t.get("done")),
                    "total": len(tasks),
                }
            )
        elif path == "/api/events":
            # v5.9: server-push fan-out — the HUD's long-lived SSE connection
            # for live Freebuff thread activity. Anything broadcast (watcher
            # events, etc.) is written to every connected client; the stream
            # stays open until the client disconnects.
            self._handle_events()
        elif path == "/api/selfimprove":
            # v4.0 Phase 13: honest self-review digest over real bus traffic.
            from dourmouse.self_improve import build_daily_digest

            self._send_json(build_daily_digest(self.server.registry))
        elif path == "/api/find_agent":
            qs = urllib.parse.parse_qs(parsed.query)
            query = (qs.get("q") or [""])[0].strip()
            limit_raw = (qs.get("limit") or ["3"])[0]
            try:
                limit = int(limit_raw)
            except ValueError:
                limit = 3
            self._send_json(
                {"query": query, "matches": find_agents_for_query(self.server.registry, query, limit)}
            )
        elif path == "/api/sessions":
            self._send_json(self.server.list_sessions())
        elif path == "/api/sessions/recent":
            self._send_json(self.server.list_recent_sessions())
        elif path == "/api/session/current":
            # v8.31: reload-survival groundwork. The live ChatSession
            # already persists every turn to workspace/sessions/<id>.jsonl
            # (chat.py's _persist, hash-chained); nothing ever read it back
            # for the UI, so a browser reload always rebuilt a blank thread
            # even though the SAME session id kept showing in the footer.
            # This is that missing read path — the CURRENT session's full
            # turn-by-turn transcript, straight off the ledger this server
            # is already writing.
            result = self.server.get_session_transcript(None)
            self._send_json(result, status=200 if result.get("ok") else 404)
        elif path.startswith("/api/session/"):
            # Same shape, for any past session by id (the "name" field
            # /api/sessions already lists, minus the .jsonl suffix) — lets a
            # future "reopen this old thread" UI reuse the identical path.
            session_id = urllib.parse.unquote(path[len("/api/session/"):])
            result = self.server.get_session_transcript(session_id)
            self._send_json(result, status=200 if result.get("ok") else 404)
        elif path == "/api/jobs":
            self._send_json(
                {"jobs": self.server.jobs.snapshot(), "count": self.server.jobs.count()}
            )
        elif path == "/api/attention":
            # v13: the cross-screen "needs attention" feed — see
            # AttentionQueue's own docstring for the gap this closes.
            items = self.server.attention.snapshot()
            self._send_json({"items": items, "count": len(items)})
        elif path == "/api/news":
            # v13: the NEWS screen's catch-up read on page load — live
            # updates after that arrive over GET /api/events (the same
            # broadcast hub the HUD already uses), which is what
            # dourmouse.news_stream.NewsStreamWatcher pushes onto when
            # news_stream=True. Honest empty state when the watcher never
            # started (news_stream=False — every test server, by design).
            watcher = getattr(self.server, "news_watcher", None)
            if watcher is None:
                self._send_json({"items": [], "status": None, "running": False})
            else:
                qs = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int(qs.get("limit", ["50"])[0])
                except ValueError:
                    limit = 50
                important_only = qs.get("important", ["0"])[0] in ("1", "true")
                items = watcher.recent(limit=limit, important_only=important_only)
                self._send_json({"items": items, "status": watcher.status(), "running": True})
        elif path == "/api/budget":
            self._send_json(
                {
                    "budget": self.server.session.cost_budget.snapshot(),
                    "rbac": self.server.session.rbac.snapshot(),
                }
            )
        elif path == "/api/backend":
            # v4.0: which LLM backend is live + the effective default model.
            cfg = self.server.config
            self._send_json(
                {
                    "backend": _backend_label(cfg),
                    "model": cfg.model if cfg is not None else None,
                    "base_url": cfg.base_url if cfg is not None else None,
                }
            )
        elif path == "/api/telemetry":
            # v5.22.14: real host telemetry (mem/cpu/load) — replaces the
            # previously SIMULATED MEM_LOAD + metrics micro-bars (audit fix).
            self._send_json(_system_telemetry())
        elif path == "/api/settings/orchestrator-model":
            # world-monitor-expansion: backend half of the orchestrator
            # model picker — a Settings UI agent calls this to build the
            # picker. See config.py's persisted-setting section for the
            # storage design and _orchestrator_backend_catalog for how
            # "configured" is determined (real probes/env checks only).
            try:
                self._handle_orchestrator_model_get()
            except Exception as exc:  # noqa: BLE001 - a settings read must never 500
                self._send_json({"current": None, "backends": [], "error": str(exc)[:200]})
        elif path == "/api/settings/grounded-mode":
            # v13: backend half of the Grounded Mode toggle — off by
            # default, see config.grounded_mode_enabled's own docstring for
            # the live bug this exists to catch.
            try:
                from dourmouse.config import grounded_mode_enabled

                self._send_json({"enabled": grounded_mode_enabled()})
            except Exception as exc:  # noqa: BLE001 - a settings read must never 500
                self._send_json({"enabled": False, "error": str(exc)[:200]})
        elif path == "/api/setup/status":
            # v8.9 first-run setup. Every field is a REAL probe (is Ollama
            # actually answering, is a key actually present) — setup must
            # never report a backend as ready because it is merely named.
            try:
                from dourmouse.firstrun import setup_status

                self._send_json(setup_status())
            except Exception as exc:  # noqa: BLE001 - setup must never 500
                self._send_json({"configured": False, "error": str(exc)[:200]})
        elif path == "/api/worldmap":
            # v8.9: locatable world-monitor items for the map screen. Reads
            # the cached world_pulse snapshot, so hitting this does not add
            # load on the upstream feeds. Never raises: a total failure is
            # reported as empty layers with the real reason attached.
            try:
                from dourmouse.world_pulse import world_pulse_geo

                self._send_json(world_pulse_geo())
            except Exception as exc:  # noqa: BLE001 - honest, never a 500
                self._send_json({
                    "layers": {}, "counts": {}, "unmappable": {},
                    "error": str(exc)[:200],
                })
        elif path == "/api/world/history/range":
            # v8.20 time scrubber: the timestamps of real recorded snapshots
            # within the last N hours (default 24), for a UI slider to
            # populate its stops from. Never a 500 — an empty/missing
            # history store just means the slider has nothing yet.
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                hours = float((qs.get("hours") or ["24"])[0])
            except ValueError:
                hours = 24.0
            try:
                from dourmouse.world_pulse_history import history_range

                self._send_json({"range": history_range(hours)})
            except Exception as exc:  # noqa: BLE001 - honest, never a 500
                self._send_json({"range": [], "error": str(exc)[:200]})
        elif path == "/api/world/history":
            # v8.20 time scrubber: the real recorded snapshot nearest
            # `minutes_ago`. `found: false` (not a fabricated snapshot) when
            # nothing has been recorded that far back yet.
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                minutes_ago = float((qs.get("minutes_ago") or ["0"])[0])
            except ValueError:
                minutes_ago = 0.0
            try:
                from dourmouse.world_pulse_history import history_at

                snap = history_at(minutes_ago)
                self._send_json({"found": snap is not None, "snapshot": snap})
            except Exception as exc:  # noqa: BLE001 - honest, never a 500
                self._send_json({"found": False, "snapshot": None, "error": str(exc)[:200]})
        elif path == "/api/world/regions":
            # v8.20 watch regions: list persisted regions (CRUD create is
            # POST /api/world/regions, delete is POST
            # /api/world/regions/delete — this server has no do_DELETE).
            try:
                from dourmouse.world_watch_regions import list_regions

                self._send_json({"regions": list_regions()})
            except Exception as exc:  # noqa: BLE001 - honest, never a 500
                self._send_json({"regions": [], "error": str(exc)[:200]})
        elif path == "/api/world/regions/hits":
            # v8.20 watch regions: which real, currently-located items fall
            # inside each persisted region right now.
            try:
                from dourmouse.world_pulse import world_pulse_geo
                from dourmouse.world_watch_regions import check_region_hits

                self._send_json({"hits": check_region_hits(world_pulse_geo())})
            except Exception as exc:  # noqa: BLE001 - honest, never a 500
                self._send_json({"hits": {}, "error": str(exc)[:200]})
        elif path == "/api/world/correlations":
            # v8.20: cross-layer proximity pairs from the current snapshot.
            qs = urllib.parse.parse_qs(parsed.query)
            threshold_km = None
            if qs.get("threshold_km"):
                try:
                    threshold_km = float(qs["threshold_km"][0])
                except ValueError:
                    threshold_km = None
            try:
                from dourmouse.world_correlation import find_correlations
                from dourmouse.world_pulse import world_pulse_geo

                self._send_json({"correlations": find_correlations(world_pulse_geo(), threshold_km)})
            except Exception as exc:  # noqa: BLE001 - honest, never a 500
                self._send_json({"correlations": [], "error": str(exc)[:200]})
        elif path == "/api/world/brief":
            # v8.20: the deterministic "what happened" overnight brief.
            try:
                from dourmouse.world_brief import generate_brief
                from dourmouse.world_pulse import world_pulse_snapshot

                self._send_json(generate_brief(world_pulse_snapshot()))
            except Exception as exc:  # noqa: BLE001 - honest, never a 500
                self._send_json({
                    "text": "Brief unavailable right now.", "mode": "template",
                    "error": str(exc)[:200],
                })
        elif path == "/api/memory":
            self._handle_memory_api()
        elif path == "/api/hands_free/status":
            # v13.4: real, LIVE status of the hands-free loop this server
            # actually started (or honestly didn't) at boot — see
            # run_server()'s own hands-free wiring. Not the same thing as
            # wakeword_status()'s dependency-capability check above (that
            # reports what COULD run; this reports what actually IS
            # running right now), a real "listening" indicator the UI can
            # poll.
            status = dict(getattr(self.server, "hands_free_status", {"enabled": False, "reason": "not started"}))
            controller = getattr(self.server, "hands_free", None)
            status["running"] = bool(controller and controller.running)
            self._send_json(status)
        elif path == "/api/memory/search":
            # v13.4: real remote read for the shared RAG database, explicit
            # user request ("move the actual rag to [the desktop], that
            # machine has more storage") -- the desktop keeps owning the
            # real SQLite file locally on its own disk (zero network-
            # SQLite risk, a real, documented corruption hazard over SMB/
            # network shares); the Mac's dourmouse.memory_store.
            # RemoteMemoryStore calls this over the network instead of
            # opening the file directly. Same real FTS5 search this
            # server already runs locally -- not a second implementation.
            self._handle_memory_remote_search()
        elif path == "/api/profile":
            # v8.14: the one-time working-style profile — status for SETTINGS.
            self._handle_profile_status()
        elif path == "/api/repo":
            # v4.1 (P6+): Project Memory — repo index status, last scan,
            # recent facts, and ?q= search (all scoped to source='repo').
            self._handle_repo_api()
        elif path == "/api/projects/imported":
            # v8.30: real projects discovered from Claude Code's and Codex
            # CLI's own on-disk session history — a source for the PROJECTS
            # bookshelf alongside its existing manual (localStorage) shelf.
            # Read-only against both tools' data; never raises (see
            # dourmouse/project_import.py for the on-disk formats).
            from dourmouse.project_import import get_imported_projects

            self._send_json(get_imported_projects())
        elif path == "/api/projects/bookkeeper":
            # world-monitor-expansion: the persisted project bookkeeper —
            # real per-project name/context/last-activity, layered on top
            # of /api/projects/imported above and incrementally refreshed
            # (see dourmouse/project_bookkeeper.py). A plain GET here is a
            # cheap file read, not a rescan; POST
            # /api/projects/bookkeeper/refresh forces one.
            from dourmouse.project_bookkeeper import get_bookkeeper

            try:
                self._send_json(get_bookkeeper())
            except Exception as exc:  # noqa: BLE001 - honest, never a 500
                self._send_json({
                    "last_refreshed": None, "projects": [], "error": str(exc)[:200],
                })
        elif path == "/api/messages":
            self._handle_messages_api()
        elif path == "/api/voice":
            # v4.1 (P7): honest voice capability report for the HUD.
            from dourmouse.voice import voice_status

            self._send_json(voice_status())
        elif path == "/api/setup":
            # v5.0: capability checklist for the SETUP panel (honest, Rule 2.2).
            self._send_json(build_setup_status(self.server))
        elif path == "/api/connections":
            # v5.3: deterministic per-account connection status (honest).
            from dourmouse.connections import check_connections

            self._send_json(check_connections())
        elif path == "/api/browser/status":
            # v5.25: browser-agent engine/state (never launches Chrome here).
            from dourmouse.browser_agent import browser_status

            self._send_json(browser_status())
        elif path == "/api/vision/status":
            # world-monitor-expansion: honest status roll-up for the Vision
            # family (overlay/tray/wakeword/vision_bridge/proactive) — see
            # _handle_vision_status for what's real vs honestly unknown.
            self._handle_vision_status()
        elif path == "/api/browser/activity":
            # v5.25: the browser agent's activity ring buffer.
            from dourmouse.browser_agent import browser_activity

            qs = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            self._send_json({"activity": browser_activity(limit)})
        elif path == "/api/browser/screenshot":
            # v5.25: serve the latest browser-agent screenshot as PNG.
            from dourmouse.browser_agent import latest_screenshot

            qs = urllib.parse.parse_qs(parsed.query)
            name = (qs.get("name") or ["latest"])[0]
            shot = latest_screenshot(name)
            if shot is None:
                self.send_error(404, "no browser screenshot yet — ask the agent to take one")
                return
            try:
                body = shot.read_bytes()
            except OSError:
                self.send_error(404, "screenshot unreadable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/email/identity":
            # v5.25: Dourmouse's own mail identity (name, own address, SMTP).
            from dourmouse.email_identity import identity_status

            self._send_json(identity_status())
        elif path == "/api/gmail/search":
            # v8.29: COMMS is a real Gmail inbox now, not a general chat
            # dump — this calls the EXACT SAME gmail_search() the mail
            # agent's tool uses, completely unmodified, and reshapes its
            # already-real "- [date] from X | subject (uid N)" text rows
            # into JSON so the UI can render an actual message list. No LLM
            # anywhere in this path: a read-only inbox listing doesn't need
            # one, so there is no inference latency and nothing to
            # hallucinate. Deliberately read-only and un-gated, matching
            # gmail_search's own Permission.REGULAR tier — it was never
            # behind a confirmation gate as a tool either. Sending stays
            # off this endpoint entirely: gmail_send is
            # REQUIRES_CONFIRMATION, so compose+send goes through the
            # normal /api/chat + confirmation-gate flow, never a raw
            # bypass here.
            import re

            from dourmouse.google_services import gmail_search

            qs = urllib.parse.parse_qs(parsed.query)
            query = (qs.get("q") or [""])[0]
            try:
                max_results = int((qs.get("max_results") or ["20"])[0])
            except ValueError:
                max_results = 20
            # world-monitor-expansion (UX pass item 5): the "" query — the
            # "recent inbox" view COMMS opens with — reads the warm cache
            # start_gmail_inbox_warmer() keeps refreshed in the background
            # instead of paying a live IMAP round trip on every screen
            # open. A REAL search (non-empty q) always goes straight to
            # gmail_search live, below, exactly as before — caching an
            # arbitrary query was never what this item asked for. A cache
            # miss here means a genuine cold start (server just booted,
            # the warmer hasn't completed its first pass, or the warmer
            # is disabled/unconfigured) — _gmail_inbox_cache_refresh does
            # the SAME real fetch inline, once, and warms the cache for
            # every request after it.
            if not query.strip():
                cached = _gmail_inbox_cache_get(max_results)
                self._send_json(cached if cached is not None else _gmail_inbox_cache_refresh(max_results))
                return
            try:
                raw = gmail_search(query, max_results)
            except Exception as exc:  # noqa: BLE001 - IMAP/OAuth failures, reported honestly
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            else:
                rows = []
                for line in raw.splitlines():
                    m = re.match(r"^- \[(.*?)\] from (.*?) \| (.*?) \(uid (.+)\)$", line)
                    if m:
                        rows.append({
                            "date": m.group(1), "from": m.group(2),
                            "subject": m.group(3), "id": m.group(4),
                        })
                # Not every real response is a row list — "inbox is empty",
                # a NOT CONFIGURED message, or a re-auth prompt are all
                # legitimate non-row outcomes reported honestly to the UI,
                # not treated as errors.
                self._send_json({"ok": True, "rows": rows, "raw": raw if not rows else None})
        elif path == "/api/gmail/read":
            # v8.29: same discipline as /api/gmail/search — calls the real,
            # unmodified gmail_read(), read-only, no LLM, no gate (matches
            # the tool's own Permission.REGULAR tier).
            from dourmouse.google_services import gmail_read

            qs = urllib.parse.parse_qs(parsed.query)
            message_id = (qs.get("id") or [""])[0]
            if not message_id:
                self._send_json({"ok": False, "error": "id is required"})
            else:
                try:
                    text = gmail_read(message_id)
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                else:
                    self._send_json({"ok": True, "text": text})
        elif path == "/api/server":
            # v5.26: the DOURMOUSE compute node (Dell) health/latency report.
            from dourmouse.remote_server import server_status

            self._send_json(server_status())
        elif path == "/api/world/pulse":
            # v5.27: the SELF-HOSTED world monitor (World Pulse) snapshot.
            from dourmouse.world_pulse import world_pulse_snapshot

            self._send_json(world_pulse_snapshot())
        elif path == "/api/world/sources":
            # v5.27: per-source health of the self-hosted monitor.
            from dourmouse.world_pulse import world_pulse_status

            self._send_json(world_pulse_status())
        elif path == "/api/atlas":
            # v5.4: ATLAS quant-engine panel — real telemetry + last run.
            from dourmouse.atlas_cli import atlas_panel_snapshot

            self._send_json(atlas_panel_snapshot())
        elif path == "/api/atlas-lab":
            # v5.22.1: ATLAS LAB — LLM backtesting + strategy catalog.
            from dourmouse.atlas_lab import get_state

            state = get_state()
            self._send_json({
                "ok": True,
                "strategy_count": len(state.strategies),
                "last_sync": state.last_sync,
                "sync_error": state.sync_error,
                "version": state.version,
                "backtest_queue": len(state.backtest_requests),
            })
        elif path == "/api/atlas-lab/strategies":
            from dourmouse.atlas_lab import list_strategies

            self._send_json({"ok": True, "strategies": list_strategies()})
        elif path == "/api/atlas-lab/leaderboard":
            # v5.22.6: best→worst ranked strategies for the Atlas window.
            from dourmouse.atlas_lab import leaderboard

            self._send_json({"ok": True, "leaderboard": leaderboard()})
        elif path == "/api/allhands":
            # v5.22.9: all All-Hands runs (newest first) for the window.
            from dourmouse.all_hands import default_runner

            self._send_json({"ok": True, "runs": default_runner().all_runs()})
        elif path.startswith("/api/allhands/"):
            # v5.22.9: ONE All-Hands run's live snapshot.
            from dourmouse.all_hands import default_runner

            run_id = urllib.parse.unquote(path[len("/api/allhands/"):])
            snap = default_runner().snapshot(run_id)
            if snap is None:
                self._send_json({"ok": False, "error": "run not found"}, status=404)
            else:
                self._send_json({"ok": True, "run": snap})
        elif path == "/api/atlas-lab/reports":
            from dourmouse.atlas_lab import get_reports

            self._send_json({"ok": True, "reports": get_reports()})
        elif path == "/api/atlas-lab/backtest":
            from dourmouse.atlas_lab import list_backtests

            self._send_json({"ok": True, "backtests": list_backtests()})
        elif path.startswith("/api/atlas-lab/backtest/"):
            from dourmouse.atlas_lab import get_backtest_status

            req_id = path[len("/api/atlas-lab/backtest/"):]
            result = get_backtest_status(req_id)
            if result is None:
                self._send_json({"ok": False, "error": "backtest not found"}, status=404)
            else:
                self._send_json({"ok": True, "backtest": result})
        elif path.startswith("/api/atlas-lab/strategies/"):
            from dourmouse.atlas_lab import get_strategy_detail

            strategy_id = path[len("/api/atlas-lab/strategies/"):]
            detail = get_strategy_detail(strategy_id)
            if detail is None:
                self._send_json({"ok": False, "error": "strategy not found"}, status=404)
            else:
                self._send_json({"ok": True, "strategy": detail})
        elif path == "/api/atlas-lab/proposals":
            # v8.16: strategy-proposal review queue (LLM-authored code,
            # human-gated — see atlas_proposals.py module docstring).
            from dourmouse.atlas_proposals import list_proposals

            qs = urllib.parse.parse_qs(parsed.query)
            status = (qs.get("status") or [None])[0]
            self._send_json({"ok": True, "proposals": list_proposals(status=status)})
        elif path.startswith("/api/atlas-lab/proposals/"):
            from dourmouse.atlas_proposals import get_proposal

            proposal_id = path[len("/api/atlas-lab/proposals/"):]
            proposal = get_proposal(proposal_id)
            if proposal is None:
                self._send_json({"ok": False, "error": "proposal not found"}, status=404)
            else:
                self._send_json({"ok": True, "proposal": proposal})
        elif path == "/api/atlas-lab/runs":
            from dourmouse.atlas_proposals import list_runs

            qs = urllib.parse.parse_qs(parsed.query)
            proposal_id = (qs.get("proposal_id") or [None])[0]
            self._send_json({"ok": True, "runs": list_runs(proposal_id=proposal_id)})
        elif path.startswith("/api/atlas-lab/runs/"):
            from dourmouse.atlas_proposals import get_run

            run_id = path[len("/api/atlas-lab/runs/"):]
            run = get_run(run_id)
            if run is None:
                self._send_json({"ok": False, "error": "run not found"}, status=404)
            else:
                self._send_json({"ok": True, "run": run})
        elif path == "/api/atlas-lab/generator/status":
            from dourmouse import atlas_generator as gen

            self._send_json({
                "ok": True,
                "interval_seconds": gen._GENERATOR_INTERVAL_SECONDS,
                "max_pending": gen._MAX_PENDING_GENERATED,
                "pending_generated_count": gen._pending_generated_count(),
            })
        elif path == "/api/freebuff":
            # v5.5: Freebuff Desktop panel — account, projects, threads.
            from dourmouse.freebuff_bridge import freebuff_panel_snapshot

            self._send_json(freebuff_panel_snapshot())
        elif path == "/api/neuro":
            # v5.6: Neural Orchestrator panel — honest learning state.
            from dourmouse.orch_net import status as neuro_status

            self._send_json(neuro_status())
        elif path == "/api/spotify":
            # v5.7: Spotify panel — config/linked state + now playing.
            from dourmouse.spotify_services import status as ss_status

            payload = ss_status()
            if payload.get("linked"):
                try:
                    from dourmouse.spotify_services import now_playing

                    payload["now_playing"] = now_playing()
                except Exception:  # noqa: BLE001 - honest panel, never crash
                    payload["now_playing"] = "SPOTIFY: playback read failed."
            self._send_json(payload)
        elif path == "/api/artifacts":
            # v5.8: published artifacts (markdown / table / series) for the
            # renderer panel. Optional ?id= returns one record; /clear (POST)
            # wipes the session store. Real store data, never fabricated.
            qs = urllib.parse.parse_qs(parsed.query)
            aid = (qs.get("id") or [""])[0].strip()
            store = getattr(self.server, "artifacts", None)
            if store is None:
                from dourmouse.artifacts import default_store

                store = default_store()
            if aid:
                record = store.get(aid)
                if record is None:
                    self._send_json({"error": f"no such artifact: {aid}"}, status=404)
                else:
                    self._send_json({"artifact": record})
                return
            self._send_json({"artifacts": store.list()})
        elif path == "/api/state":
            # v5.14 Phase R0: the cross-device state snapshot — watchlist,
            # alerts inbox, prefs, recent activity, per-device workspaces.
            # One server, one source of truth, every device reads it.
            # v5.17: scoped to THIS request's owner — a signed-in Google user
            # sees only their own data (+ shared/system alerts); signed-out
            # clients see the shared bucket. The browser's session cookie
            # rides along on the same-origin fetch automatically.
            store = getattr(self.server, "state", None)
            if store is None:
                from dourmouse.state_store import StateStore

                store = StateStore()
            from dourmouse.state_store import SHARED_OWNER

            me = self._session_user()
            # v5.20: the offline service worker caches ONLY shared-scope
            # snapshots (X-Dourmouse-Scope: shared) — a signed-in user's
            # personal data must never be persisted in the SW cache, where
            # every client of this origin could replay it offline.
            self._send_json(
                {**store.snapshot(me or SHARED_OWNER), "me": me},
                headers={"X-Dourmouse-Scope": "shared" if me is None else "personal"},
            )
        elif path == "/api/palette":
            # v5.14 Phase R0: the command-centre index — destinations,
            # agents, and commands. Same data powers the desktop ⌘K overlay
            # and the mobile ⚡ sheet; natural-language queries can later
            # search the same index.
            self._send_json(self._build_palette())
        elif path == "/api/deeplink":
            # v5.19: deep-link navigation (dourmouse://atlas/research/...).
            # The strict allow-list parser lives SERVER-side so every
            # platform shares one gate; the response is a 302 to the
            # validated SPA hash route (or JSON with ?format=json for
            # programmatic clients). Sits AFTER the auth gate above, so it
            # is token-gated off-loopback exactly like every other API.
            self._handle_deeplink(parsed)
        elif path == "/api/version":
            # v5.19: secure self-update surface — the current version plus
            # the latest release from the signed feed (hash-verified
            # artifact). Cached server-side (6h TTL), so the HUD never
            # blocks on the network repeatedly; unset feed honestly reports
            # configured:false and never fabricates a version.
            from dourmouse.updates import check_for_updates

            self._send_json(check_for_updates().as_dict())
        elif path == "/api/mt5":
            # v8.3: MT5 paper-broker panel — subprocess-bounded snapshot so
            # the HUD poll can never hang on the terminal.
            from dourmouse.mt5_ops import mt5_panel_snapshot

            self._send_json(mt5_panel_snapshot())
        elif path == "/api/tv":
            # v8.4: TradingView bridge panel — legs, webhook URL, recent
            # signals (honest: reports configured=False without a secret).
            from dourmouse.tradingview_ops import tv_panel_snapshot

            self._send_json(tv_panel_snapshot())
        elif path == "/api/files":
            # v5.0: list uploaded files (name, size, age) newest first.
            try:
                files = []
                for f in sorted(_uploads_root().glob("*")):
                    # v13.5 (live-caught, real bug): this project (and the
                    # workspace/uploads dir it lives under) sits on an
                    # ExFAT-formatted external volume — macOS synthesizes a
                    # real "._<name>" AppleDouble sidecar file the moment
                    # ANY file is written there (no native xattr support on
                    # ExFAT). Confirmed live: uploading edge_report.pdf via
                    # a real curl POST produced BOTH edge_report.pdf AND a
                    # real "._edge_report.pdf" sitting right next to it,
                    # and this listing showed both as if they were two real
                    # uploads — the sidecar is unopenable junk, not a
                    # second file the user ever chose. Skip dotfiles.
                    if f.name.startswith("."):
                        continue
                    if f.is_file():
                        files.append(
                            {
                                "name": f.name,
                                "size": f.stat().st_size,
                                "modified": datetime.fromtimestamp(
                                    f.stat().st_mtime
                                ).isoformat(timespec="seconds"),
                            }
                        )
                self._send_json({"files": files})
            except OSError as exc:
                self._send_json({"files": [], "error": str(exc)}, status=500)
        elif path.startswith("/uploads/"):
            # v5.0: serve an uploaded file back (sandboxed to the uploads root).
            rel = urllib.parse.unquote(path[len("/uploads/"):])
            if not _UPLOAD_NAME_RE.match(rel):
                self.send_error(400, "bad upload name")
                return
            target = (_uploads_root() / rel).resolve()
            try:
                target.relative_to(_uploads_root().resolve())
            except ValueError:
                self.send_error(400, "bad upload name")
                return
            if not target.is_file():
                self.send_error(404, "no such upload")
                return
            body = target.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/pdf/info":
            # v13.5, Vision OS "GPU-accelerated technical document reader"
            # (dourmouse/pdf_reader.py — real PDFium, see that module's own
            # docstring for what's real vs explicitly not built/Marker).
            # Sandboxed to the uploads root, SAME whitelist+resolve+
            # relative_to pattern as the /uploads/ handler right above.
            from dourmouse.pdf_reader import pdf_info

            qs = urllib.parse.parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0].strip()
            target = _sandboxed_upload_path(name)
            if target is None:
                self._send_json({"ok": False, "error": "bad file name"}, status=400)
                return
            self._send_json(pdf_info(target))
        elif path == "/api/pdf/text":
            from dourmouse.pdf_reader import page_text

            qs = urllib.parse.parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0].strip()
            target = _sandboxed_upload_path(name)
            if target is None:
                self._send_json({"text": "", "error": "bad file name"}, status=400)
                return
            try:
                page = int((qs.get("page") or ["0"])[0])
            except ValueError:
                self._send_json({"text": "", "error": "bad page number"}, status=400)
                return
            self._send_json({"text": page_text(target, page)})
        elif path == "/api/pdf/page.png":
            from dourmouse.pdf_reader import render_page_png

            qs = urllib.parse.parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0].strip()
            target = _sandboxed_upload_path(name)
            if target is None:
                self.send_error(400, "bad file name")
                return
            try:
                page = int((qs.get("page") or ["0"])[0])
            except ValueError:
                self.send_error(400, "bad page number")
                return
            try:
                png_bytes = render_page_png(target, page)
            except RuntimeError as exc:
                self.send_error(404, str(exc))
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png_bytes)
        elif path == "/api/gdelt/graph":
            # v13.6, Vision OS "real-time global event ingestion + kinetic
            # knowledge graph" (dourmouse/gdelt_graph.py — real GDELT GKG
            # 2.1, see that module's own docstring for scope). Read-only
            # snapshot of the shared, continuously-updated graph — never
            # blocks on a live fetch, always returns whatever the
            # background poller has already ingested (honestly empty
            # right after boot until its first cycle completes).
            from dourmouse.gdelt_graph import get_graph

            qs = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int((qs.get("limit") or ["150"])[0])
            except ValueError:
                limit = 150
            self._send_json(get_graph().snapshot(limit_nodes=limit))
        elif path == "/api/gdelt/status":
            from dourmouse.gdelt_graph import graph_status

            self._send_json(graph_status())
        elif path == "/api/git/log":
            # v13.6, Vision OS item 9's safe subset — real, read-only
            # history of DOURMOUSE'S OWN repo (never arbitrary user
            # files, never a mutating git call — see git_timetravel.py's
            # own docstring for the deliberate scope boundary).
            from dourmouse.git_timetravel import log as git_log

            qs = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            file_filter = (qs.get("path") or [""])[0].strip() or None
            self._send_json(git_log(_PROJECT_ROOT, limit=limit, path=file_filter))
        elif path == "/api/git/diff":
            from dourmouse.git_timetravel import diff as git_diff

            qs = urllib.parse.parse_qs(parsed.query)
            commit_hash = (qs.get("hash") or [""])[0].strip()
            self._send_json(git_diff(_PROJECT_ROOT, commit_hash))
        elif path == "/api/git/changed_files":
            from dourmouse.git_timetravel import changed_files as git_changed_files

            qs = urllib.parse.parse_qs(parsed.query)
            commit_hash = (qs.get("hash") or [""])[0].strip()
            self._send_json(git_changed_files(_PROJECT_ROOT, commit_hash))
        elif path == "/api/git/file_at":
            from dourmouse.git_timetravel import file_at as git_file_at

            qs = urllib.parse.parse_qs(parsed.query)
            commit_hash = (qs.get("hash") or [""])[0].strip()
            file_path = (qs.get("path") or [""])[0].strip()
            self._send_json(git_file_at(_PROJECT_ROOT, commit_hash, file_path))
        elif path == "/api/semantic/graph":
            # v13.6, Vision OS item 2 ("Qdrant + Ollama embeddings,
            # semantic-proximity gravity clustering physics") —
            # dourmouse/semantic_graph.py, real local Qdrant + the
            # existing Ollama embedding cache, applied to the real
            # memory store. server.memory can be a RemoteMemoryStore
            # (no local fact_embeddings cache to build against) —
            # reported honestly rather than attempted.
            from dourmouse.semantic_graph import build_semantic_graph

            if not isinstance(self.server.memory, MemoryStore):
                self._send_json({
                    "ok": False, "nodes": [], "edges": [],
                    "error": "NOT CONFIGURED: semantic clustering needs a local memory store (this server is using a remote one)",
                })
                return
            self._send_json(build_semantic_graph(self.server.memory, _uploads_root().parent))
        elif path == "/api/semantic/status":
            from dourmouse.semantic_graph import semantic_graph_available

            from dourmouse.memory_embed import embed_enabled

            self._send_json({
                "qdrant_available": semantic_graph_available(),
                "embed_enabled": embed_enabled(),
                "local_memory_store": isinstance(self.server.memory, MemoryStore),
            })
        elif path == "/api/usage":
            # v13.6: real, persisted Claude + Ollama usage totals — see
            # dourmouse/usage_tracker.py's own docstring for exactly
            # what's real (Claude: real cost+tokens from the CLI's own
            # result event) vs. honestly not attempted (a fabricated
            # Ollama dollar figure).
            from dourmouse.usage_tracker import get_totals

            self._send_json(get_totals())
        elif path == "/api/speech":
            # v4.1 (P7): GET = local TTS, returns audio/wav bytes.
            qs = urllib.parse.parse_qs(parsed.query)
            text = (qs.get("text") or [""])[0].strip()
            if not text:
                self._send_json(
                    {"configured": True, "error": "missing ?text= parameter"},
                    status=400,
                )
                return
            from dourmouse.voice import VoiceNotConfiguredError, text_to_speech

            try:
                wav = text_to_speech(text)
            except VoiceNotConfiguredError as exc:
                self._send_json({"configured": False, "error": f"NOT CONFIGURED: {exc}"})
                return
            except ValueError as exc:
                self._send_json({"configured": True, "error": str(exc)}, status=400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(wav)
        else:
            self.send_error(404, "not found")

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/login":
            # v4.0: token exchange — sets the dourmouse_session cookie.
            self._handle_login()
            return
        if parsed.path == "/api/auth/logout":
            # v5.15: end the Google user session (pre-auth so a signed-out
            # client can always clear its cookie).
            self._handle_auth_logout()
            return
        if parsed.path.startswith("/api/setup/"):
            # v8.9 first-run setup. PRE-auth by necessity: on a fresh install
            # there is no configuration yet, so requiring a session here would
            # lock the user out of the screen that exists to configure them.
            # Bounded by design — the handler writes only an ALLOWLISTED set
            # of config keys, and the server binds loopback only.
            self._handle_setup(parsed.path)
            return
        if not self._authorized():
            self._send_unauthorized()
            return
        if parsed.path == "/api/deeplink":
            # v5.20: programmatic deep-link navigation (the desktop shell's
            # already-running path): allow-list parsed server-side, then a
            # validated `navigate` SSE broadcast so the open window routes
            # without a browser. Never executes anything.
            self._handle_deeplink_post()
        elif parsed.path == "/api/chat":
            self._handle_chat()
        elif parsed.path == "/api/confirm":
            self._handle_confirm()
        elif parsed.path == "/api/attention/dismiss":
            body = self._read_json_body()
            try:
                item_id = int(body.get("id"))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "detail": "id must be an integer"})
            else:
                ok = self.server.attention.dismiss(item_id)
                self._send_json({"ok": ok})
        elif parsed.path == "/api/role":
            self._handle_role()
        elif parsed.path == "/api/settings/orchestrator-model":
            # world-monitor-expansion: persists the orchestrator's chosen
            # model (see config.save_orchestrator_model_setting). Behind
            # the normal auth gate above, unlike /api/setup/* — this is a
            # post-first-run settings change, not the bootstrap flow.
            self._handle_orchestrator_model_post()
        elif parsed.path == "/api/settings/grounded-mode":
            # v13: persists the Grounded Mode toggle (see
            # config.save_grounded_mode_setting). Same post-first-run
            # settings-change auth posture as the orchestrator-model POST.
            self._handle_grounded_mode_post()
        elif parsed.path == "/api/vision/kill-switch":
            # world-monitor-expansion: a REAL toggle for dourmouse/tray.py's
            # privacy kill switch, reachable from the browser console even
            # when the native tray icon process isn't running — writes the
            # exact same shared state file every module in the Vision family
            # reads (dourmouse.tray.load_state/save_state), so a flip here
            # is honored everywhere, not a second competing notion of state.
            self._handle_vision_kill_switch_post()
        elif parsed.path == "/api/voice/command":
            # world-monitor-expansion: parses one utterance against the
            # Vision workspace's bounded voice-command grammar (see
            # dourmouse/voice_commands.py). Read-only/stateless — never
            # performs the action itself.
            self._handle_voice_command_post()
        elif parsed.path == "/api/feedback":
            self._handle_feedback()
        elif parsed.path == "/api/speech":
            # v4.1 (P7): POST = local STT; raw audio bytes in the body.
            self._handle_speech_stt()
        elif parsed.path == "/api/repo/scan":
            # v4.1 (P6+): re-index the ATLAS repo (idempotent) + persist meta.
            self._handle_repo_scan()
        elif parsed.path == "/api/upload":
            # v5.0: raw-body file upload into the sandboxed uploads root.
            self._handle_upload()
        elif parsed.path == "/api/rag/upload":
            # v13.4: real user request — "a page where files can be
            # uploaded to the shared rag database". Same raw-body upload
            # contract as /api/upload (name query param, size cap, sandbox
            # write), then ADDITIONALLY extracts real text and remembers
            # it into the shared MemoryStore — reusing bulk_ingest.py's
            # own extraction (one extractor, not a second one for the
            # manual-upload path vs the bulk laptop/Drive scan).
            self._handle_rag_upload()
        elif parsed.path == "/api/tasks":
            # v5.x: HUD task intake — deterministic CRUD into the same
            # workspace/tasks.json the tasks agent owns.
            body = self._read_json_body()
            title = (body.get("title") or "").strip()
            if not title:
                self._send_json({"ok": False, "error": "title is required"}, status=400)
                return
            from dourmouse.live_feeds import add_task

            try:
                task = add_task(title)
            except RuntimeError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json({"ok": True, "task": task})
        elif parsed.path == "/api/projects/bookkeeper/refresh":
            # world-monitor-expansion: force the incremental refresh
            # described in dourmouse/project_bookkeeper.py — real work
            # only for projects whose last_active moved since the last
            # persisted checkpoint; everything else is served from the
            # persisted record as-is.
            from dourmouse.project_bookkeeper import refresh as refresh_bookkeeper

            try:
                self._send_json({"ok": True, **refresh_bookkeeper()})
            except Exception as exc:  # noqa: BLE001 - honest, never a 500
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
        elif parsed.path == "/api/projects/create":
            # v13.4: real create on the PROJECTS bookshelf, explicit user
            # request. Same deterministic-CRUD shape as /api/world/regions
            # above — no LLM in this path, real validation errors come
            # back as 400 with the honest reason.
            body = self._read_json_body()
            from dourmouse.project_bookkeeper import create_project

            try:
                record = create_project(
                    name=body.get("name") or "",
                    path=body.get("path") or "",
                    description=body.get("description") or "",
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json({"ok": True, "project": record})
        elif parsed.path == "/api/projects/delete":
            # POST, not DELETE — this server implements do_GET/do_POST
            # only, matching every other mutating action in this file
            # (see /api/world/regions/delete's own comment).  "Delete"
            # here means stop tracking on the bookshelf — see
            # project_bookkeeper.delete_project's own docstring for why
            # this never touches the real directory or its session files.
            body = self._read_json_body()
            path = (body.get("path") or "").strip()
            if not path:
                self._send_json({"ok": False, "error": "path is required"}, status=400)
                return
            from dourmouse.project_bookkeeper import delete_project

            removed = delete_project(path)
            self._send_json({"ok": True, "removed": removed})
        elif parsed.path == "/api/world/regions":
            # v8.20 watch regions: create. Same deterministic-CRUD shape as
            # /api/tasks above — no LLM in this path, real validation errors
            # come back as 400 with the honest reason, never silently
            # clamped or guessed.
            body = self._read_json_body()
            from dourmouse.world_watch_regions import add_region

            try:
                region = add_region(
                    name=(body.get("name") or "").strip(),
                    min_lat=body.get("min_lat"), max_lat=body.get("max_lat"),
                    min_lon=body.get("min_lon"), max_lon=body.get("max_lon"),
                )
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json({"ok": True, "region": region})
        elif parsed.path == "/api/world/regions/delete":
            # v8.20 watch regions: delete. POST, not DELETE — this server
            # implements do_GET/do_POST only, matching every other mutating
            # action in this file.
            body = self._read_json_body()
            region_id = (body.get("id") or "").strip()
            if not region_id:
                self._send_json({"ok": False, "error": "id is required"}, status=400)
                return
            from dourmouse.world_watch_regions import delete_region

            self._send_json({"ok": True, "deleted": delete_region(region_id)})
        elif parsed.path == "/api/push-notify":
            # v8.2: external watchers (tools/watch_dourmouse.py) surface a
            # real event into the AGENT COMMS bus — e.g. an upstream push
            # to the dourmouse repo. Loopback-facing; the watcher runs on
            # 127.0.0.1. Real event, never fabricated (Rule 2.1).
            self._handle_push_notify()
        elif parsed.path == "/api/tv-webhook":
            # v8.4: TradingView alert webhook — TradingView POSTs alert
            # messages here (form-encoded payload= OR raw JSON). Parses,
            # validates the TV_WEBHOOK_SECRET when configured, persists to
            # workspace/tv_signals.jsonl and broadcasts to the COMMS bus.
            self._handle_tv_webhook()
        elif parsed.path == "/api/atlas/run":
            # v5.4: start one managed ATLAS command (single-flight).
            self._handle_atlas_run()
        elif parsed.path == "/api/atlas-lab/sync":
            # v5.22.1: force a GitHub sync of the strategy lab.
            from dourmouse.atlas_lab import sync

            result = sync()
            self._send_json(result)
        elif parsed.path == "/api/allhands":
            # v5.22.9: start an All-Hands run from the window / HUD button.
            from dourmouse.all_hands import start_all_hands

            body = self._read_json_body()
            goal = (body.get("goal") or "").strip()
            if not goal:
                self._send_json({"ok": False, "error": "goal is required"}, status=400)
                return
            run_id = start_all_hands(goal, owner=self._session_user())
            self._send_json({"ok": True, "run_id": run_id})
        elif parsed.path == "/api/atlas-lab/backtest":
            # v5.22.1: submit a prompt-driven backtest.
            from dourmouse.atlas_lab import submit_backtest

            body = self._read_json_body()
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                self._send_json({"ok": False, "error": "prompt is required"}, status=400)
                return
            pair = (body.get("pair") or "EURUSD").strip()
            try:
                result = submit_backtest(prompt, pair=pair)
                self._send_json({"ok": True, **result})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
        elif parsed.path == "/api/atlas-lab/proposals":
            # v8.16: idea -> LLM-authored strategy code, queued for review.
            # Synchronous (the LLM call is the only latency, ~5-30s — same
            # order as any chat response, no background thread needed).
            from dourmouse.atlas_proposals import propose_from_idea

            body = self._read_json_body()
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                self._send_json({"ok": False, "error": "prompt is required"}, status=400)
                return
            source = (body.get("source") or "chat").strip()
            try:
                proposal = propose_from_idea(prompt, source=source)
                self._send_json({"ok": True, "proposal": proposal})
            except (RuntimeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
        elif parsed.path.startswith("/api/atlas-lab/proposals/") and parsed.path.endswith("/approve"):
            # Execution can take up to 90s (sandboxed subprocess) — this
            # returns a "running" placeholder immediately; poll
            # /api/atlas-lab/runs/<id> for the real result.
            from dourmouse.atlas_proposals import approve_and_run_async

            proposal_id = parsed.path[len("/api/atlas-lab/proposals/"):-len("/approve")]
            body = self._read_json_body()
            target = (body.get("target") or "local").strip()
            try:
                run = approve_and_run_async(proposal_id, target=target)
                self._send_json({"ok": True, "run": run})
            except KeyError:
                self._send_json({"ok": False, "error": "proposal not found"}, status=404)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
        elif parsed.path.startswith("/api/atlas-lab/proposals/") and parsed.path.endswith("/reject"):
            from dourmouse.atlas_proposals import reject_proposal

            proposal_id = parsed.path[len("/api/atlas-lab/proposals/"):-len("/reject")]
            body = self._read_json_body()
            reason = (body.get("reason") or "").strip()
            result = reject_proposal(proposal_id, reason=reason)
            if result is None:
                self._send_json({"ok": False, "error": "proposal not found"}, status=404)
            else:
                self._send_json({"ok": True, "proposal": result})
        elif parsed.path == "/api/atlas-lab/generator/run-now":
            # v8.16: manual trigger — same 2-LLM-call latency as any chat
            # idea, so synchronous is fine (matches propose_from_idea's own
            # HTTP handler above).
            from dourmouse import atlas_generator as gen

            try:
                proposal = gen.generate_and_propose()
                if proposal is None:
                    self._send_json({
                        "ok": True, "skipped": True,
                        "reason": f"already {gen._MAX_PENDING_GENERATED} generator "
                                  "proposals pending review — review some first",
                    })
                else:
                    self._send_json({"ok": True, "skipped": False, "proposal": proposal})
            except (RuntimeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
        elif parsed.path == "/api/neuro/train":
            # v5.6: force a background retrain of the neural orchestrator.
            self._handle_neuro_train()
        elif parsed.path == "/api/history/import":
            # v8.13: pull Claude Code + Codex CLI session history into memory.
            self._handle_history_import()
        elif parsed.path == "/api/memory/remember":
            # v13.4: the write half of the remote RAG contract — see
            # GET /api/memory/search's route comment above.
            self._handle_memory_remote_remember()
        elif parsed.path == "/api/profile/generate":
            # v8.14: the one-time working-style profile.
            self._handle_profile_generate()
        elif parsed.path == "/api/spotify/login":
            # v5.7: start the one-time Spotify account linking (background).
            from dourmouse.spotify_services import spotify_login

            message = spotify_login(background=True)
            self._send_json({"ok": True, "message": message})
        elif parsed.path == "/api/spotify/search":
            # v5.21 HUD music section: structured track search (the user
            # clicks a row to play). Real API rows, never fabricated.
            from dourmouse.spotify_services import search_tracks_data

            payload = self._read_json_body()
            if not isinstance(payload, dict):
                payload = {}
            query = str(payload.get("query") or "")
            limit = payload.get("limit") or 8
            try:
                results = search_tracks_data(query, limit)
            except (RuntimeError, ValueError) as error:
                self._send_json({"ok": False, "error": str(error)})
                return
            self._send_json({"ok": True, "results": results})
        elif parsed.path == "/api/spotify/play":
            # v5.21 HUD music section: play a track/playlist URI. The panel
            # button click IS the human approval (Rule 2.9) — the roster's
            # confirmation gate guards the agent path, not the user's own
            # click.
            from dourmouse.spotify_services import play_uri

            payload = self._read_json_body()
            if not isinstance(payload, dict):
                payload = {}
            uri = str(payload.get("uri") or "")
            try:
                message = play_uri(uri)
            except (RuntimeError, ValueError) as error:
                self._send_json({"ok": False, "message": str(error)})
                return
            self._send_json({"ok": not message.startswith("ERROR"), "message": message})
        elif parsed.path == "/api/spotify/playlists":
            # v5.21 HUD music section: the user's playlists, each playable.
            from dourmouse.spotify_services import playlists_data

            try:
                playlists = playlists_data()
            except (RuntimeError, ValueError) as error:
                self._send_json({"ok": False, "error": str(error)})
                return
            self._send_json({"ok": True, "playlists": playlists})
        elif parsed.path == "/api/spotify/recent":
            # v5.21 HUD music section: recently played, each row playable.
            from dourmouse.spotify_services import recently_played_data

            try:
                recent = recently_played_data()
            except (RuntimeError, ValueError) as error:
                self._send_json({"ok": False, "error": str(error)})
                return
            self._send_json({"ok": True, "recent": recent})
        elif parsed.path == "/api/spotify/top":
            # v5.21 HUD music section: most-played tracks, each playable.
            from dourmouse.spotify_services import top_tracks_data

            payload = self._read_json_body()
            if not isinstance(payload, dict):
                payload = {}
            time_range = str(payload.get("time_range") or "medium_term")
            try:
                top = top_tracks_data(time_range=time_range)
            except (RuntimeError, ValueError) as error:
                self._send_json({"ok": False, "error": str(error)})
                return
            self._send_json({"ok": True, "top": top})
        elif parsed.path == "/api/spotify/control":
            # v5.21 HUD music section: next/previous/pause/resume. Same
            # human-click contract as /api/spotify/play.
            from dourmouse.spotify_services import playback_control

            payload = self._read_json_body()
            if not isinstance(payload, dict):
                payload = {}
            action = str(payload.get("action") or "")
            try:
                message = playback_control(action)
            except (RuntimeError, ValueError) as error:
                self._send_json({"ok": False, "message": str(error)})
                return
            self._send_json({"ok": not message.startswith("ERROR"), "message": message})
        elif parsed.path == "/api/artifacts/clear":
            # v5.8: wipe the session artifact store (a fresh renderer slate).
            store = getattr(self.server, "artifacts", None)
            if store is None:
                from dourmouse.artifacts import default_store

                store = default_store()
            cleared = store.clear()
            self._send_json({"ok": True, "cleared": cleared})
        elif parsed.path == "/api/state/watchlist":
            # v5.14 Phase R0: star/unstar a symbol. Writes the ONE store and
            # broadcasts a state_change over SSE so every connected device
            # (desktop, phone, tablet) updates live.
            self._handle_state_watchlist()
        elif parsed.path == "/api/state/alerts":
            # v5.14 Phase R0: dismiss / mute / unmute / prioritize alerts.
            self._handle_state_alerts()
        elif parsed.path == "/api/state/prefs":
            # v5.14 Phase R0: set one preference (last-write-wins).
            self._handle_state_prefs()
        elif parsed.path == "/api/state/workspace":
            # v5.14 Phase R0: record where THIS device left off (resume).
            self._handle_state_workspace()
        else:
            self.send_error(404, "not found")

    # -- implementations -------------------------------------------------- #

    def _handle_speech_stt(self) -> None:
        """v4.1 (P7): POST /api/speech — local STT of raw audio bytes.

        Returns {"configured": true, "text": ...} on success, or an honest
        {"configured": false, "error": "NOT CONFIGURED: ..."} when the gate
        is off or the engine/model is unavailable (Rule 2.2). Body size is
        capped and a malformed Content-Length never crashes the handler."""
        raw_len = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            self._send_json(
                {"configured": True, "error": "invalid Content-Length"},
                status=400,
            )
            return
        if length < 0 or length > _MAX_AUDIO_BYTES:
            self._send_json(
                {"configured": True, "error": f"audio body too large (max {_MAX_AUDIO_BYTES} bytes)"},
                status=400,
            )
            return
        audio = self.rfile.read(length) if length else b""
        if not audio:
            self._send_json(
                {"configured": True, "error": "no audio data in request body"},
                status=400,
            )
            return
        from dourmouse.voice import VoiceNotConfiguredError, speech_to_text

        try:
            text = speech_to_text(audio)
        except VoiceNotConfiguredError as exc:
            self._send_json({"configured": False, "error": f"NOT CONFIGURED: {exc}"})
            return
        except ValueError as exc:
            self._send_json({"configured": True, "error": str(exc)}, status=400)
            return
        if not text:
            # honest: whisper ran but heard nothing (VAD silence)
            self._send_json(
                {"configured": True, "text": "", "error": "no speech detected in the audio"}
            )
            return
        self._send_json({"configured": True, "text": text})

    def _handle_upload(self) -> None:
        """v5.0: POST /api/upload?name=<file> — raw file bytes into uploads.

        Sandboxed to <workspace>/uploads/ with a strict name whitelist and a
        hard size cap; reports REAL errors (Rule 2.2). Returns the file name
        and its absolute path so the model can read it via ``read_upload``.
        """
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        name = (qs.get("name") or [""])[0].strip()
        raw_len = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "invalid Content-Length"}, status=400)
            return
        if length < 0 or length > _MAX_UPLOAD_BYTES:
            self._send_json(
                {"ok": False, "error": f"upload too large (max {_MAX_UPLOAD_BYTES} bytes)"},
                status=400,
            )
            return
        if not _UPLOAD_NAME_RE.match(name):
            # Drain the request body before responding: Windows closes a
            # socket holding unread received data via RST, so the client
            # would see a connection reset instead of this 400. Bounded by
            # the size cap checked above.
            try:
                self.rfile.read(length)
            except OSError:
                pass
            self._send_json(
                {
                    "ok": False,
                    "error": (
                        "filename must be 1-120 chars of letters/digits/"
                        "dot/underscore/dash (no paths)"
                    ),
                },
                status=400,
            )
            return
        data = self.rfile.read(length) if length else b""
        if not data:
            self._send_json({"ok": False, "error": "empty upload body"}, status=400)
            return
        try:
            root = _uploads_root()
            target = (root / name).resolve()
            target.relative_to(root.resolve())  # sandbox re-check
            with open(target, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            self._send_json({"ok": False, "error": f"upload failed: {exc}"}, status=500)
            return
        self._send_json(
            {"ok": True, "name": name, "size": len(data), "path": str(target)}
        )

    def _handle_rag_upload(self) -> None:
        """v13.4: POST /api/rag/upload?name=<file> — raw file bytes,
        written into the sandboxed uploads root (same contract as
        /api/upload above: name whitelist, size cap, Windows-safe drain-
        before-400), then indexed for real into the shared RAG database
        (dourmouse/memory_store.py) via bulk_ingest.py's own text
        extraction — the same extractor the bulk laptop/Drive scans use,
        not a second, competing implementation. Honest when a file has no
        extractable text (an image, a binary) — the file is still saved,
        but the response says plainly that nothing was indexed, never a
        fabricated success.
        """
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        name = (qs.get("name") or [""])[0].strip()
        raw_len = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "invalid Content-Length"}, status=400)
            return
        if length < 0 or length > _MAX_UPLOAD_BYTES:
            self._send_json(
                {"ok": False, "error": f"upload too large (max {_MAX_UPLOAD_BYTES} bytes)"},
                status=400,
            )
            return
        if not _UPLOAD_NAME_RE.match(name):
            try:
                self.rfile.read(length)
            except OSError:
                pass
            self._send_json(
                {
                    "ok": False,
                    "error": (
                        "filename must be 1-120 chars of letters/digits/"
                        "dot/underscore/dash (no paths)"
                    ),
                },
                status=400,
            )
            return
        data = self.rfile.read(length) if length else b""
        if not data:
            self._send_json({"ok": False, "error": "empty upload body"}, status=400)
            return
        try:
            root = _uploads_root()
            target = (root / name).resolve()
            target.relative_to(root.resolve())  # sandbox re-check
            with open(target, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            self._send_json({"ok": False, "error": f"upload failed: {exc}"}, status=500)
            return

        from dourmouse.bulk_ingest import _extract_local_text
        from dourmouse.general_roster import _open_memory_store

        try:
            text = _extract_local_text(target)
        except Exception as exc:  # noqa: BLE001 — a bad file must still report the real save
            self._send_json(
                {"ok": True, "name": name, "size": len(data), "path": str(target),
                 "indexed": False, "index_error": f"{type(exc).__name__}: {exc}"}
            )
            return
        if text is None or not text.strip():
            self._send_json(
                {"ok": True, "name": name, "size": len(data), "path": str(target),
                 "indexed": False, "reason": "no extractable text (image/binary/empty file)"}
            )
            return
        store = _open_memory_store()
        if isinstance(store, Exception):
            self._send_json(
                {"ok": True, "name": name, "size": len(data), "path": str(target),
                 "indexed": False, "index_error": str(store)}
            )
            return
        store.remember("manual_upload", str(target), text[:200_000])
        self._send_json(
            {"ok": True, "name": name, "size": len(data), "path": str(target),
             "indexed": True, "indexed_chars": min(len(text), 200_000)}
        )

    def _handle_push_notify(self) -> None:
        """v8.2: POST /api/push-notify — external event into the COMMS bus.

        Body: {"from", "subject", "body"}. Posts a REAL broadcast message
        to the inter-agent bus so the HUD's AGENT COMMS panel shows it
        immediately. Used by tools/watch_dourmouse.py to surface upstream
        pushes. Sender name is sanitized; body is capped by the bus itself.
        Never fabricated — the watcher only sends events it actually saw.
        """
        try:
            body = self._read_json_body()
        except Exception as exc:  # noqa: BLE001 -- honest malformed-body failure
            self._send_json({"ok": False, "error": f"bad body: {exc}"}, status=400)
            return
        sender = str(body.get("from") or "watchdog").strip()[:40] or "watchdog"
        subject = str(body.get("subject") or "EXTERNAL EVENT").strip()[:200]
        text = str(body.get("body") or "").strip()
        if not text:
            self._send_json({"ok": False, "error": "body is required"}, status=400)
            return
        bus = getattr(self.server, "bus", None) or get_message_bus()
        bus.post(sender, "BROADCAST", subject, text)
        self._send_json({"ok": True, "from": sender, "subject": subject})

    def _handle_tv_webhook(self) -> None:
        """v8.4: POST /api/tv-webhook — TradingView alert webhook.

        Reads the RAW body (TradingView sends form-encoded ``payload=`` by
        default, or raw JSON), hands it to tradingview_ops.handle_tv_webhook
        for parse/validate/persist/broadcast, and answers 200 to stop
        TradingView retrying. Caps the body at 256 KB."""
        from dourmouse.tradingview_ops import handle_tv_webhook

        raw_len = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "invalid Content-Length"},
                            status=400)
            return
        if length < 0 or length > 256 * 1024:
            self._send_json({"ok": False, "error": "webhook body too large"},
                            status=400)
            return
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "") or ""
        bus = getattr(self.server, "bus", None) or get_message_bus()
        result = handle_tv_webhook(body, ctype, bus=bus)
        self._send_json(result, status=200 if result.get("ok") else 400)

    def _handle_agent_api(self, name: str) -> None:
        """v2.7: focused live snapshot for ONE agent window.

        Returns the agent's identity + toolkit plus its live status/last
        activity/feed (from the ActivityTracker). 404 for unknown agents.
        """
        sub = None
        for s in self.server.registry.all_subagents():
            if s.name == name:
                sub = s
                break
        if sub is None:
            self._send_json({"error": f"no such agent: {name}"}, status=404)
            return
        snap = self.server.tracker.snapshot().get("agents", {}).get(name, {})
        # v3.0: this agent's inter-agent inbox + unread count (real bus data).
        bus = getattr(self.server, "bus", None) or get_message_bus()
        inbox: list[dict] = []
        unread = 0
        try:
            inbox = bus.inbox(name, limit=20)
            # v3.0: opening an agent's window / selecting it on the map READS
            # its inbox — those messages are marked read FOR THAT AGENT (a
            # broadcast stays unread for everyone else until it reads it), so
            # badges clear for the reader without clearing other agents'.
            for m in inbox:
                bus.mark_read(m["id"], name)
                m["read"] = True
            unread = bus.unread_count(name)
        except Exception:
            pass
        self._send_json(
            {
                "agent": {
                    "name": sub.name,
                    "domain": sub.domain,
                    "description": sub.description,
                    "model": _effective_model(
                        getattr(self.server, "config", None), sub.name
                    ),
                    "tools": [
                        {
                            "name": t.name,
                            "permission": t.permission.value,
                            "description": t.description.split(".")[0][:120],
                        }
                        for t in sub.tools
                    ],
                },
                "status": snap.get("status", "idle"),
                "last": snap.get("last"),
                "feed": snap.get("feed", []),
                "inbox": inbox,
                "unread": unread,
            }
        )

    def _serve_static(self, rel: str) -> None:
        target = _safe_asset_path(rel)
        if target is None:
            self.send_error(404, "not found")
            return
        # v5.13: web-app manifests and app icons need REAL content types or
        # install-to-home-screen silently degrades (manifest ignored, icon
        # unused) even though the bytes are served.
        ctype = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".mjs": "application/javascript",  # v5.23: MediaPipe vision bundle (self-hosted)
            ".wasm": "application/wasm",       # v5.23: MediaPipe vision WASM
            ".webmanifest": "application/manifest+json",
            ".json": "application/json",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".wav": "audio/wav",
        }.get(Path(rel).suffix, "application/octet-stream")
        body = target.read_bytes()
        # v8.2 — the [ATLAS TERMINAL] button opens the streamlit terminal;
        # its port is configurable via DOURMOUSE_ATLAS_TERMINAL_PORT (default
        # 8511, the start_atlas_ui.sh default). Injected at serve time so the
        # button never hardcodes a stale port.
        if rel == "index.html":
            port = os.environ.get("DOURMOUSE_ATLAS_TERMINAL_PORT", "8511").strip() or "8511"
            marker = b"<script>"
            inject = (
                b"<script>window.__ATLAS_TERMINAL_PORT__='"
                + port.encode()
                + b"';</script>\n<script>"
            )
            if marker in body:
                body = body.replace(marker, inject, 1)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_chat(self) -> None:
        # v5.15: bind the logged-in Google user to THIS request thread so the
        # agent tools (gmail_search/read/send, calendar) act on the signed-in
        # user's account. ThreadingHTTPServer is one thread per request, so the
        # thread-local dies with the request — no cross-user leakage.
        from dourmouse import google_auth

        google_auth.set_current_user(self._session_user())
        # Reviewer-caught: the thread-local MUST be cleared even on the early
        # returns below — one future refactor to a shared-thread server and
        # a leaked user would route user A's chat into user B's account.
        try:
            return self._handle_chat_authed()
        finally:
            google_auth.set_current_user(None)

    def _handle_chat_authed(self) -> None:
        """The authorized half of /api/chat (user bound; thread-local cleared
        by the caller's finally)."""
        body = self._read_json_body()
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            self._send_json({"error": "prompt is required"}, status=400)
            return
        # v8.18: voice/text response split. The speak-and-listen UI
        # (ui/voice.html) marks its /api/chat calls with voice: true because
        # it transcribes the request and speaks the reply back with zero
        # human review in between; the typed UI (ui/index.html) never sends
        # this, including its own mic button, which only fills the text box
        # for the user to read/edit/send like any typed message. Missing or
        # falsy defaults to the existing text-channel behavior.
        voice_channel = bool(body.get("voice"))
        # Captured before any focus_agent routing-directive wrapping below,
        # so the "just say send" intercept sees exactly what the user typed.
        raw_prompt = prompt
        # v13: which console screen this directive was sent from (HOME,
        # CODE, RESEARCH, ...) — the frontend now sends this so a reload's
        # session-restore can put the turn back on its own thread instead of
        # flattening every screen's conversation onto HOME (see chat.py's
        # ask()/_persist() docstrings for the other half of this fix).
        # Missing/blank from an older client build degrades to HOME, which
        # is that client's only real thread anyway.
        screen = (body.get("screen") or "HOME").strip() or "HOME"
        focus_agent = (body.get("focus_agent") or "").strip()
        if focus_agent and focus_agent not in self.server.registry.subagent_names:
            self._send_json(
                {"error": f"unknown subagent: {focus_agent!r}"}, status=400
            )
            return
        # v3.1 per-agent models: a focus_agent route runs on THAT agent's
        # configured NVIDIA model (DOURMOUSE_MODEL_<AGENT>), so e.g. a coding
        # agent can use a coding-tuned model while research uses another.
        model_override = None
        # v13.2: CLAUDE CODE toolchain talks to the real CLI DIRECTLY —
        # explicit user request ("I only want to be talking to claude
        # directly when doing so"). The ROUTING DIRECTIVE wrapper below and
        # Dourmouse's whole orchestrator/roster prompt are for the OTHER
        # subagents, whose "tools" are Dourmouse ToolSpecs the orchestrator
        # LLM calls; code_claude's only "tool" is a real, separate program
        # with its own real reasoning — wrapping its prompt in routing
        # prose a different model would have needed, and then running it
        # through THAT model's tool loop, is not talking to Claude at all.
        # See _handle_code_claude_passthrough for the real streamed path.
        if focus_agent and focus_agent != "code_claude":
            # v13.1 (live-reproduced): the old phrasing ("using ONLY the
            # ... subagent and its tools") read as an instruction to
            # actually CALL tools, not just a scope restriction — a plain
            # factual question ("what is retrograde motion") looped
            # web_search/fetch_url 8 times and burned the whole tool
            # budget instead of just answering from what the model
            # already knows. The scope restriction stays; added the
            # answer-directly-when-you-can escape hatch.
            prompt = (
                f"[ROUTING DIRECTIVE] You may ONLY use the '{focus_agent}' "
                f"subagent's tools for this — never another subagent's "
                f"tools. If you already know the answer, answer directly; "
                f"only call a tool for live/current data, something you're "
                f"unsure of, or when the user explicitly asks you to look "
                f"something up or take an action. TASK: {prompt}"
            )
            if self.server.config is not None:
                model_override = self.server.config.model_for_agent(focus_agent)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        stream = _SSEStream(self.wfile)

        def sink(entry: dict[str, Any]) -> None:
            stream.emit(entry)
            self.server.tracker.on_event(entry)
            self.server.attention.on_event(entry, screen=screen)

        # ONE shared gate per server. The wiring (emit swap + resolver) and
        # the run must be atomic under session_lock so a concurrent request
        # can never steal the shared gate's emit or resolver mid-flight.
        gate = self.server.gate
        session = self.server.session
        previous_gate = session.confirmation_gate
        report: dict[str, Any] | None = None
        error_msg: str | None = None
        # v5.8: during THIS request the artifact store streams live
        # "artifact" SSE events into the same stream as everything else, so
        # publish_artifact calls render in the HUD the instant they happen.
        artifacts_store = getattr(self.server, "artifacts", None)
        if artifacts_store is not None:
            artifacts_store.set_sink(stream.emit)
        # v13.2: CLAUDE CODE toolchain — direct, live-streamed passthrough
        # to the real CLI. Runs without session_lock/gate, same rationale
        # as the slash-command path right below: this is a real SEPARATE
        # program with its own tool permissions (--permission-mode
        # bypassPermissions in stream_claude, v13.5 — full terminal parity,
        # see code_backends.py's own comment), never Dourmouse's own
        # confirmation_gate/orchestrator loop.
        if focus_agent == "code_claude":
            try:
                self._handle_code_claude_passthrough(raw_prompt, screen, sink)
            finally:
                if artifacts_store is not None:
                    artifacts_store.set_sink(None)
            self.close_connection = True
            return
        # v5.22.9: slash commands (/all /claude /codex /chatgpt /freebuff) and
        # the "use all resources" All-Hands goal route through the SAME SSE
        # stream as normal chat — assistant_text chunks + a terminal done — so
        # every client renders them identically, and the ALL HANDS window
        # opens when a run starts. Runs without the session lock (no gate).
        from dourmouse import all_hands

        slash = all_hands.parse_slash(prompt)
        if slash is not None or all_hands.detect_all_hands(prompt):
            try:
                self._handle_slash_chat(prompt, slash, stream, sink)
            finally:
                if artifacts_store is not None:
                    artifacts_store.set_sink(None)
            # Prompt SSE termination exactly like the normal chat path
            # (reviewer-caught: the early return must close the stream).
            self.close_connection = True
            return

        # "Just say send": a short imperative affirm phrase (e.g. "send it",
        # "go ahead") resolves a pending confirmation instead of starting a
        # normal chat turn. Runs OUTSIDE session_lock and BEFORE acquiring
        # it deliberately — the thread that's actually waiting on the
        # confirmation is blocked holding session_lock for the duration of
        # its session.ask() call, so taking the lock here would deadlock
        # against the very confirmation we're trying to resolve.
        if _is_imperative_affirm(raw_prompt):
            pending = self.server.gate.pending_items()
            if len(pending) == 1:
                confirm_id, prompt_text = pending[0]
                ok = self._resolve_confirmation(confirm_id, True)
                final_text = (
                    f"Approved: {prompt_text}"
                    if ok
                    else "That confirmation is no longer pending."
                )
                sink(
                    {
                        "type": "confirmation_resolved",
                        "id": confirm_id,
                        "approved": True,
                        "ok": ok,
                    }
                )
                sink({"type": "done", "final_text": final_text})
                self.close_connection = True
                return
            if len(pending) > 1:
                # More than one pending confirmation — never guess which one
                # "send it" means. List them briefly and let the user pick.
                listing = "; ".join(f"{cid} — {txt}" for cid, txt in pending)
                final_text = (
                    "Multiple confirmations are pending, so I won't guess "
                    f"which one you mean: {listing}"
                )
                sink({"type": "assistant_text", "text": final_text})
                sink({"type": "done", "final_text": final_text})
                self.close_connection = True
                return
            # Zero pending confirmations — this is ordinary chat content
            # (e.g. the user really did just type "yes" or "go ahead" as a
            # conversational reply), so fall through to the normal turn.

        with self.server.session_lock:
            gate.set_emit(stream.emit)
            session.confirmation_gate = gate
            self.server.confirm_resolver = gate.resolve
            try:
                report = session.ask(
                    prompt,
                    max_turns=8,
                    event_sink=sink,
                    model=model_override,
                    voice=voice_channel,
                    display_text=raw_prompt,
                    screen=screen,
                    # v13: a real bug fixed here, live-caught through an
                    # actual directive against the CODE screen's "docs"
                    # toolchain — this used to rely PURELY on the wrapped
                    # "[ROUTING DIRECTIVE]..." sentence above being read
                    # correctly by the model, never on the real forced_agent
                    # mechanism dispatch.py already built to bypass
                    # build_plan()'s comma-splitting fallback. Live-
                    # reproduced: a slideshow request with real commas in
                    # it got split into 3 fragments routed to the wrong
                    # agents. `focus_agent` is already validated above
                    # against the real subagent_names, so passing it
                    # straight through is safe.
                    forced_agent=focus_agent or None,
                    should_stop=stream.should_stop,
                )
            except Exception as exc:  # surface real failures to the UI
                error_msg = str(exc)
            finally:
                session.confirmation_gate = previous_gate
                self.server.confirm_resolver = None
                gate.set_emit(lambda _e: None)
                if artifacts_store is not None:
                    artifacts_store.set_sink(None)

        # Emit the terminal event AFTER the lock is released so a queued
        # second request can immediately start its own run. It rides the
        # sink (stream.emit happens inside sink) so the ActivityTracker also
        # resets computing/auth agents to idle promptly — a live agent used
        # in a chat returns to its always-on [LIVE] state on the next poll
        # (v2.8), instead of showing "computing" indefinitely on the map.
        if error_msg is not None:
            sink({"type": "error", "message": error_msg})
        elif report is not None:
            sink(
                {
                    "type": "done",
                    "final_text": report.get("final_text", ""),
                    "transcript": report.get("transcript", []),
                }
            )
        # Terminate the SSE response after done/error so the client gets EOF
        # instead of hanging on keep-alive.
        self.close_connection = True

    def _handle_slash_chat(self, prompt: str, slash, stream, sink) -> None:
        """v5.22.9: route a slash command / All-Hands goal through the same
        SSE stream as normal chat (assistant_text chunks + done).

        ``slash`` is ``(cmd, text)`` from all_hands.parse_slash, or None
        when the prompt matched the "use all resources" natural-language
        detector (then it becomes an /all run). The ALL HANDS window opens
        on the ``allhands_started`` event the HUD listens for. Runs without
        the session lock — no confirmation gate involved.
        """
        from dourmouse import all_hands

        if slash is not None:
            cmd, text = slash
        else:
            cmd, text = "all", prompt
        sink({"type": "brain", "model": f"slash:{cmd}", "escalated": False})
        try:
            result = all_hands.run_slash(cmd, text, owner=self._session_user())
        except Exception as exc:  # noqa: BLE001 -- surface the real failure
            result = {"ok": False, "text": f"ERROR: {exc}"}
        final = str(result.get("text") or "")
        if result.get("run_id"):
            # The dedicated ALL HANDS window opens on this event.
            sink({"type": "allhands_started", "run_id": result["run_id"]})
        sink({"type": "assistant_text", "text": final})
        sink({"type": "done", "final_text": final})
        # v5.22.9 (sweep-found): slash runs bypass ask() so they were never
        # audited — record them in the same hash-chained session ledger.
        try:
            session = getattr(self.server, "session", None)
            if session is not None and hasattr(session, "record_slash"):
                tools = [f"slash:{cmd}"]
                if result.get("run_id"):
                    tools.append("allhands:" + str(result["run_id"]))
                session.record_slash(prompt, final, tools=tools)
        except Exception:  # noqa: BLE001 -- an audit failure never breaks chat
            pass

    def _handle_code_claude_passthrough(self, prompt: str, screen: str, sink) -> None:
        """v13.2: the CODE screen's CLAUDE CODE toolchain, talking to the
        real Claude Code CLI directly and LIVE — explicit user request
        ("I only want to be talking to claude directly when doing so",
        "same thought tokens", "exact same experience as ... the
        terminal"). code_backends.stream_claude does the real work (see
        its own module docstring for the event-translation detail); this
        just wires its callbacks onto the SAME sink/SSE vocabulary every
        other screen already renders (assistant_delta/thinking_delta/
        tool_use/tool_result/done) — a real, no-adapter drop-in, not a new
        client-side rendering path.

        No session_lock/confirmation_gate here (same as _handle_slash_chat
        right above): this is a real separate program with its OWN tool
        permissions, never Dourmouse's orchestrator loop or roster prompt.
        """
        import time

        from dourmouse import code_backends
        from dourmouse.general_roster import _PROJECT_ROOT

        start = time.perf_counter()
        sink({"type": "brain", "model": "claude-code-cli", "local": False})
        final_text = ""

        def _on_claude_usage(usage: dict[str, Any]) -> None:
            # v13.6: real usage bar -- persist AND emit live so a
            # connected client can show this turn's real cost the
            # instant it's known, not just via a separately-polled
            # /api/usage total.
            from dourmouse.usage_tracker import record_claude_usage

            record_claude_usage(usage)
            sink({"type": "usage", "backend": "claude", **usage})

        try:
            final_text = code_backends.stream_claude(
                prompt,
                cwd=str(_PROJECT_ROOT),
                timeout=300,
                on_delta=lambda text: sink({"type": "assistant_delta", "text": text}),
                on_thinking=lambda text: sink({"type": "thinking_delta", "text": text}),
                on_tool_use=lambda name, args: sink(
                    {"type": "tool_use", "name": name, "raw_arguments": args}
                ),
                on_tool_result=lambda text: sink(
                    {"type": "tool_result", "name": "claude", "text": text}
                ),
                on_usage=_on_claude_usage,
            )
        except Exception as exc:  # noqa: BLE001 -- surface the real failure, never fabricate
            sink({"type": "error", "message": str(exc)})
            final_text = f"ERROR: {exc}"
        else:
            sink({"type": "assistant_text", "text": final_text})
            sink({"type": "done", "final_text": final_text})
        # Bypasses session.ask() same as the slash-command path above —
        # same audit gap, same fix.
        try:
            session = getattr(self.server, "session", None)
            if session is not None and hasattr(session, "record_slash"):
                session.record_slash(
                    prompt, final_text, tools=["code_claude"], screen=screen,
                    elapsed_ms=(time.perf_counter() - start) * 1000.0,
                )
        except Exception:  # noqa: BLE001 -- an audit failure never breaks chat
            pass

    def _handle_events(self) -> None:
        """v5.9: long-lived server-push SSE for the HUD feed.

        Registers this response's stream on the server's broadcast hub, then
        blocks reading the request body until the client disconnects (the
        loop ends on EOF/ConnectionReset; ``finally`` unregisters the
        stream). The broadcast hub pushes watcher events from its own
        thread while this handler simply holds the connection open.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        stream = _SSEStream(self.wfile)
        hub = getattr(self.server, "events_broadcast", None)
        if hub is None:
            # No hub (tests / unusual server) — emit nothing, close cleanly.
            self.close_connection = True
            return
        hub.register(stream)
        # Late subscriber honesty: the watcher emitted its "online/offline"
        # event at CONNECT time, before this browser attached — replay the
        # current status so the HUD always knows the watch state.
        watcher = getattr(self.server, "freebuff_watcher", None)
        if watcher is not None:
            state = "online" if watcher.online else "offline"
            stream.emit(
                {
                    "type": "freebuff_watch",
                    "state": state,
                    "detail": "" if watcher.online else "app unreachable",
                }
            )
        try:
            while True:
                try:
                    chunk = self.rfile.read(1024)
                except (ConnectionResetError, OSError):
                    break
                if not chunk:
                    break
        finally:
            hub.unregister(stream)
            self.close_connection = True

    def _resolve_confirmation(self, confirm_id: str, approved: bool) -> bool:
        """Resolve a pending confirmation via the shared gate resolver.

        The gate lives on the active chat request thread; ``confirm_resolver``
        is the shared handle to reach it. This is the ONE path that resolves
        a confirmation — both the UI-click POST /api/confirm handler and the
        "just say send" chat intercept call through here so approval logic
        never forks.
        """
        resolver = getattr(self.server, "confirm_resolver", None)
        if resolver is None:
            return False
        return resolver(confirm_id, approved)

    def _handle_confirm(self) -> None:
        body = self._read_json_body()
        confirm_id = body.get("id") or ""
        approved = bool(body.get("approved"))
        if getattr(self.server, "confirm_resolver", None) is None:
            self._send_json({"ok": False, "error": "no active chat"}, status=409)
            return
        ok = self._resolve_confirmation(confirm_id, approved)
        self._send_json({"ok": ok, "id": confirm_id, "approved": approved})

    def _handle_login(self) -> None:
        """POST /api/login — exchange the token for a session cookie.

        Body: {"token": "..."}. On success sets HttpOnly dourmouse_session
        cookie (SameSite=Strict, Path=/) and returns ok. On failure 401.
        When no token is configured (loopback posture) returns ok:False with
        enabled:False so a stray login page can't confuse anyone.
        """
        import hmac

        token = getattr(self.server, "access_token", "") or ""
        body = self._read_json_body()
        provided = (body.get("token") or "").strip()
        if not token:
            self._send_json({"ok": False, "enabled": False})
            return
        if not provided or not hmac.compare_digest(provided, token):
            self._send_json({"ok": False, "error": "invalid token"}, status=401)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header(
            "Set-Cookie",
            f"dourmouse_session={token}; Path=/; HttpOnly; SameSite=Strict",
        )
        self.send_header("Content-Length", str(len(b'{"ok": true}')))
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    # -- v5.15: Google OAuth login (per-user identity) -------------------- #

    def _google_redirect_uri(self) -> str:
        """The callback URL for THIS request's Host header.

        Loopback (http://127.0.0.1:<port>/api/auth/google/callback) works as-is
        with Google OAuth. Non-loopback deployments must either serve https or
        register the exact http redirect in Google Cloud Console — the Host
        header is sanitized (hostname / IPv4 / bracketed IPv6 only) so it can
        never inject a path or header (same guard as the pairing page).

        The Host header's PORT is honored when present and sane (reviewer-
        caught: behind a proxy the external port differs from server_port, and
        sending the wrong port makes Google reject with redirect_uri_mismatch).

        DOURMOUSE_OAUTH_REDIRECT_BASE overrides all of it. This matters
        because Google only accepts plain-http redirects on loopback: reach
        this server over a VPN address (e.g. a Tailscale 100.x IP) and the
        derived URI is non-loopback http, which Google rejects outright with
        "Error 400: invalid_request" — before the user ever sees a consent
        screen. Pinning the registered loopback URI here lets sign-in work
        from any host, provided the browser can still reach that loopback
        address (an SSH tunnel, or a browser on the server itself).
        """
        pinned = os.environ.get("DOURMOUSE_OAUTH_REDIRECT_BASE", "").strip()
        if pinned:
            return pinned.rstrip("/") + "/api/auth/google/callback"

        host_header = self.headers.get("Host", "") or ""
        host, port = host_header, None
        # "host:port" splits on the LAST colon; bracketed IPv6 "[::1]:8765"
        # is safe because the brackets group the colons.
        if ":" in host_header and not host_header.startswith("["):
            host, _, port = host_header.rpartition(":")
        elif host_header.startswith("[") and "]:" in host_header:
            host, _, port = host_header.rpartition(":")
        if not _SAFE_HOST_RE.match(host or "") or "/" in host:
            host, port = "127.0.0.1", None
        if port is None or not port.isdigit() or not (1 <= int(port) <= 65535):
            port = str(self.server.server_port)
        return f"http://{host}:{port}/api/auth/google/callback"

    def _handle_auth_status(self) -> None:
        """GET /api/auth/status — honest Google-login readiness + who I am."""
        from dourmouse.google_auth import status as google_status

        payload = google_status()
        payload["me"] = self._session_user()
        payload["token_gate"] = bool(getattr(self.server, "access_token", ""))
        self._send_json(payload)

    def _prune_oauth_pending(self) -> None:
        """Drop abandoned OAuth flows older than the TTL (reviewer-caught:
        a user who never completes consent would otherwise leak one dict
        entry per attempt forever). Entries without a parseable ``created``
        (legacy/planted) are treated as stale too. Also prunes the
        system-browser claim slots (v5.22.11) — same TTL, same discipline.
        Caller holds oauth_lock (and claim_lock when pruned)."""
        cutoff = datetime.now().timestamp() - _OAUTH_PENDING_TTL_SECONDS
        stale = [
            state
            for state, pending in self.server.oauth_pending.items()
            if _pending_created_ts(pending) is None
            or _pending_created_ts(pending) < cutoff
        ]
        for state in stale:
            self.server.oauth_pending.pop(state, None)
        claims = getattr(self.server, "claim_pending", {})
        stale_claims = [
            code
            for code, claim in claims.items()
            if _pending_created_ts(claim) is None
            or _pending_created_ts(claim) < cutoff
        ]
        for code in stale_claims:
            claims.pop(code, None)

    def _handle_google_start(self) -> None:
        """GET /api/auth/google/start[?claim=CODE] — 302 to Google consent (PKCE).

        v5.22.11: ``?claim=CODE`` is the system-browser bridge. Google refuses
        sign-in inside the desktop app's embedded WebKit webview, so the
        login page passes a single-use claim code: the consent page opens in
        the user's REAL browser, the callback parks the completed session
        under that code, and the webview adopts it via GET /api/auth/claim.
        Without ``claim`` the flow is unchanged (plain in-app redirect)."""
        import secrets

        from dourmouse import google_auth

        if not google_auth.google_configured():
            self._send_json(
                {"ok": False, "error": "Google OAuth NOT CONFIGURED — see /api/auth/status"},
                status=400,
            )
            return
        # Refuse early on a redirect URI Google is guaranteed to reject, and
        # say why. Sending the user to a consent screen that dead-ends on
        # "Error 400: invalid_request" is the worst version of this failure:
        # the cause (reached the server over a non-loopback address) is
        # nowhere in the message Google shows.
        redirect_uri = self._google_redirect_uri()
        host = urllib.parse.urlparse(redirect_uri).hostname or ""
        if not (host in ("127.0.0.1", "::1", "localhost") or redirect_uri.startswith("https://")):
            self._send_json(
                {
                    "ok": False,
                    "error": (
                        f"Google will reject this sign-in: the redirect URI is "
                        f"{redirect_uri!r}, and Google only accepts plain http "
                        "redirects on loopback (127.0.0.1 / localhost)."
                    ),
                    "fix": (
                        "Reach Dourmouse on http://127.0.0.1:<port> to sign in "
                        "(an SSH tunnel works: "
                        "ssh -N -L 8765:127.0.0.1:8765 <user>@<host>), or set "
                        "DOURMOUSE_OAUTH_REDIRECT_BASE=http://127.0.0.1:8765 "
                        "to always use the URI registered in Google Cloud Console."
                    ),
                    "redirect_uri": redirect_uri,
                },
                status=400,
            )
            return

        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        claim = (qs.get("claim") or [""])[0].strip() or None
        state = secrets.token_urlsafe(24)
        verifier, challenge = google_auth.new_pkce()
        redirect_uri = self._google_redirect_uri()
        with self.server.oauth_lock:
            self._prune_oauth_pending()
            self.server.oauth_pending[state] = {
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "redirect_to": "/",
                "claim": claim,
                "created": datetime.now().isoformat(),
            }
        url = google_auth.authorization_url(redirect_uri, state, challenge)
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_google_callback(self) -> None:
        """GET /api/auth/google/callback?code=..&state=.. — exchange, verify,
        create a user session, redirect to /."""
        from dourmouse import google_auth

        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        code = (qs.get("code") or [""])[0].strip()
        state = (qs.get("state") or [""])[0].strip()
        with self.server.oauth_lock:
            self._prune_oauth_pending()
            pending = self.server.oauth_pending.pop(state, None)
        if pending is None:
            self._send_json(
                {"ok": False, "error": "OAuth state missing/expired — start the login again"},
                status=400,
            )
            return
        error = (qs.get("error") or [""])[0].strip()
        if error:
            # Google refused consent (access_denied / server error) — the
            # state is consumed (single-use); send the user home with a
            # friendly reason instead of a raw Google 502.
            self.send_response(302)
            self.send_header("Location", "/login?reason=denied")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            tokens = google_auth.exchange_code(
                code, pending["redirect_uri"], pending["verifier"]
            )
            identity = google_auth.verify_id_token(str(tokens.get("id_token") or ""))
        except RuntimeError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=502)
            return
        # v5.22.13: the post-exchange half must NEVER surface as a bare 500.
        # Any unexpected failure here is logged to stderr (captured by the
        # launchd runner) and returned as a readable 502 JSON — Rule 2.2
        # (honest error, not a raw traceback page).
        try:
            email = identity["email"]
            store = self.server.auth
            store.upsert_user(
                email,
                tokens,
                name=identity.get("name", ""),
                picture=identity.get("picture", ""),
                sub=identity.get("sub", ""),
            )
            sid = store.create_session(email, identity.get("name", ""), identity.get("picture", ""))
            claim = pending.get("claim")
            if claim:
                # v5.22.11: system-browser bridge — the consent page ran in the
                # user's REAL browser (Google blocks WebKit webviews). Park the
                # session under the single-use claim code; the app's webview
                # adopts it via /api/auth/claim and lands on /. The browser is
                # sent to a plain "you can close this tab" page.
                with self.server.claim_lock:
                    self.server.claim_pending[claim] = {
                        "sid": sid,
                        "email": email,
                        "name": identity.get("name", ""),
                        "picture": identity.get("picture", ""),
                        "created": datetime.now().isoformat(),
                    }
                self.send_response(302)
                self.send_header("Location", "/login?claimed=1")
            else:
                self.send_response(302)
                self.send_header("Location", pending.get("redirect_to") or "/")
                # Secure is intentionally omitted: the server is plain HTTP on
                # loopback/LAN (browsers ignore Secure from http:// anyway). If
                # DourMouse is ever served over HTTPS, add Secure here AND on
                # the logout cookie below — HttpOnly + SameSite=Strict already
                # apply.
                self.send_header(
                    "Set-Cookie",
                    f"dourmouse_user_session={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000",
                )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception as exc:  # noqa: BLE001 - log + honest 502, never a bare 500
            _log_traceback(f"google callback post-exchange failed: {exc}")
            self._send_json(
                {"ok": False, "error": f"GOOGLE AUTH: session creation failed: {exc}"},
                status=502,
            )

    def _handle_auth_me(self) -> None:
        """GET /api/auth/me — the signed-in identity, or null."""
        email = self._session_user()
        if email is None:
            self._send_json({"me": None})
            return
        profile = self.server.auth.user_profile(email)
        self._send_json({"me": profile})

    def _handle_auth_claim(self) -> None:
        """GET /api/auth/claim?code=CODE — adopt a system-browser session
        (v5.22.11). The consent page runs in the user's REAL browser (Google
        blocks embedded WebKit webviews); when the callback parks the session
        under the claim code, the app's webview polls here and lands signed
        in. Single-use + TTL-pruned — a code can be redeemed once, ever.
        Pre-auth (like /api/auth/status): it only reveals a session the
        caller's own claim code unlocked."""
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        code = (qs.get("code") or [""])[0].strip()
        if not code:
            self._send_json({"ok": False, "error": "claim code required"}, status=400)
            return
        with self.server.oauth_lock:
            self._prune_oauth_pending()
        with self.server.claim_lock:
            claim = self.server.claim_pending.pop(code, None)
        if claim is None:
            self._send_json(
                {"ok": False, "error": "claim code unknown/expired — start the login again"},
                status=404,
            )
            return
        sid = claim["sid"]
        self.send_response(200)
        # Same cookie contract as the in-app callback: HttpOnly, SameSite,
        # 30-day lifetime. The webview gets this response and is signed in.
        self.send_header(
            "Set-Cookie",
            f"dourmouse_user_session={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000",
        )
        self._send_json({"ok": True, "me": {
            "email": claim["email"],
            "name": claim.get("name", ""),
            "picture": claim.get("picture", ""),
        }})

    def _handle_auth_logout(self) -> None:
        """POST /api/auth/logout — end the user session + clear the cookie.

        Also best-effort REVOKES the user's Google refresh token so a stolen
        token cannot outlive the logout (reviewer-caught dead code: the
        revoke helper now has a caller). Failures never block logout.
        """
        from dourmouse import google_auth

        store = getattr(self.server, "auth", None)
        email = self._session_user()
        if store is not None and email is not None:
            try:
                refresh = store.user_tokens(email).get("refresh_token")
                if refresh:
                    google_auth.revoke_token(str(refresh))
            except Exception:  # noqa: BLE001,S110 - best-effort, logout must not fail
                pass
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "dourmouse_user_session" and store is not None:
                store.delete_session(value.strip())
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # Same caveat as the login cookie: Secure belongs here too once the
        # server can be served over HTTPS (plain-HTTP loopback today).
        self.send_header(
            "Set-Cookie",
            "dourmouse_user_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
        )
        self.send_header("Content-Length", str(len(b'{"ok": true}')))
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def _handle_mobile_page(self) -> None:
        """v5.13: GET /mobile — phone-pairing page with real QR codes.

        Server-rendered from ui/mobile.html: fills the connection status
        (access token configured + non-loopback binding?), the phone-
        reachable URLs (LAN + Tailscale via mobile_link.detect_addresses)
        each with a REAL scannable QR (segno svg_data_uri — honest: if
        segno is missing the QR block is omitted, never faked), and the
        steps. Reached without auth so a fresh phone can land on it.
        """
        from dourmouse import mobile_link

        token = getattr(self.server, "access_token", "") or ""
        addrs = mobile_link.detect_addresses()
        # The port the phone is actually hitting comes from the Host header
        # (or the default). QR encodes the LOGIN page — the token entry the
        # phone needs first.
        host_header = (self.headers.get("Host") or "").strip()
        port = _DEFAULT_PORT
        hostname = host_header
        if ":" in host_header:
            maybe_host, maybe_port = host_header.rsplit(":", 1)
            if maybe_port.isdigit():
                port = int(maybe_port)
                hostname = maybe_host
        # The URL the phone actually used (Host header), validated, so the QR
        # always points back at itself — works for Tailscale DNS names, LAN
        # IPs, and router hostnames alike, not just the detected addresses.
        self_host = _phone_url_host(hostname)

        if token:
            status = (
                "<span class='ok'>REMOTE ACCESS: CONFIGURED</span><br>"
                "Token gate active — non-loopback clients must present the "
                "token (constant-time check, cookie session)."
            )
        else:
            status = (
                "<span class='warn'>REMOTE ACCESS: NOT CONFIGURED</span><br>"
                "No DOURMOUSE_ACCESS_TOKEN on the server — the phone cannot "
                "authenticate. On your Mac run:<br>"
                "<div class='cmd'>python -m dourmouse.mobile_link</div>"
            )

        qr_urls: list[str] = []
        for ip in addrs["lan"]:
            qr_urls.append(
                f"<div class='urlrow'>"
                f"<div class='qr'>{_qr_svg(mobile_link.pairing_url(ip, port))}</div>"
                f"<div class='meta'><div class='label'>LAN // SAME WI-FI</div>"
                f"<div class='url'>{mobile_link.pairing_url(ip, port)}</div>"
                f"<div class='note'>open in Safari — or scan with the Camera app</div>"
                f"</div></div>"
            )
        for ip in addrs["tailscale"]:
            qr_urls.append(
                f"<div class='urlrow'>"
                f"<div class='qr'>{_qr_svg(mobile_link.pairing_url(ip, port))}</div>"
                f"<div class='meta'><div class='label'>TAILSCALE // ANYWHERE</div>"
                f"<div class='url'>{mobile_link.pairing_url(ip, port)}</div>"
                f"<div class='note'>phone needs the Tailscale app on the same tailnet</div>"
                f"</div></div>"
            )
        primary: list[str] = []
        if self_host and self_host not in addrs["lan"] and self_host not in addrs["tailscale"]:
            # A phone that reached /mobile via a host the machine's own
            # detection did not list (Tailscale DNS name, router hostname):
            # lead with the URL it is ALREADY on, so the QR is self-consistent.
            self_url = f"http://{self_host}:{port}/login"
            primary.append(
                f"<div class='urlrow'>"
                f"<div class='qr'>{_qr_svg(self_url)}</div>"
                f"<div class='meta'><div class='label'>THIS DEVICE // the URL you opened</div>"
                f"<div class='url'>{self_url}</div>"
                f"<div class='note'>scan to go straight to the access gate</div>"
                f"</div></div>"
            )
        urls_html = "".join(primary + qr_urls) if (primary or qr_urls) else (
            "<span class='bad'>NO PHONE-REACHABLE ADDRESS FOUND</span><br>"
            "This Mac has no private IPv4 (Wi-Fi/Ethernet) and no Tailscale "
            "interface. On the Mac: join Wi-Fi or run 'tailscale up'."
        )

        template = (_UI_DIR / "mobile.html").read_text(encoding="utf-8")
        body = (
            template.replace("{{STATUS}}", status)
            .replace("{{URLS}}", urls_html)
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _handle_setup(self, path: str) -> None:
        """v8.9 first-run setup endpoints.

        Three actions, all bounded: validate a key against the real API,
        probe a node, and save an ALLOWLISTED set of config values. Nothing
        here can execute a tool or read arbitrary files, which is why it is
        safe to expose before a session exists.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, OSError):
            self._send_json({"ok": False, "detail": "malformed request"}, status=400)
            return
        try:
            from dourmouse import firstrun

            if path == "/api/setup/validate-key":
                self._send_json(firstrun.validate_nvidia_key(body.get("api_key", "")))
            elif path == "/api/setup/probe-node":
                self._send_json(firstrun.probe_node(body.get("url", "")))
            elif path == "/api/setup/save":
                self._send_json(firstrun.save_config(body.get("values") or {}))
            elif path == "/api/setup/restart":
                self._send_json(firstrun.restart_app())
            else:
                self._send_json({"ok": False, "detail": "unknown setup action"}, status=404)
        except Exception as exc:  # noqa: BLE001 - setup must never 500
            self._send_json({"ok": False, "detail": str(exc)[:200]}, status=200)

    # -- world-monitor-expansion: orchestrator model setting -------------- #
    #
    # Backend half of a Settings UI picker being built separately (another
    # agent), for "which model does the top-level orchestrator run on".
    # GET returns the current effective value plus a real availability
    # catalog; POST persists a choice via config.save_orchestrator_model_
    # setting, which config.model_for_agent("orchestrator") then reads back
    # fresh from disk on the orchestrator's next turn (see config.py's own
    # docstring for the full precedence: env override > persisted setting >
    # built-in default > plain default model).

    def _orchestrator_backend_catalog(self) -> list[dict[str, Any]]:
        """Real availability of every backend the orchestrator-model
        setting can point at: the 3 core system backends load_llm_config()
        already chooses among (nvidia/ollama/omniroute), plus the wider
        coding-family menu code_backends.py / cn_backends.py already wire
        up for the code_* agents (deepseek/qwen/glm/kimi/codex/claude) —
        surfaced here too since a user may reasonably want the orchestrator
        itself pointed at one of them.

        Every entry is a REAL probe or a REAL env-var check (Rule 2.2) — a
        backend with no key/CLI/reachable server reports configured=False
        WITH the real reason, never omitted and never guessed.
        """
        from dourmouse import code_backends
        from dourmouse import config as cfg_mod

        entries: list[dict[str, Any]] = []

        nvidia_key = bool(os.environ.get("NVIDIA_API_KEY", "").strip())
        entries.append({
            "name": "nvidia",
            "configured": nvidia_key,
            "model": os.environ.get("NVIDIA_MODEL", "").strip() or cfg_mod.NVIDIA_DEFAULT_MODEL,
            "detail": "NVIDIA_API_KEY set" if nvidia_key else "NVIDIA_API_KEY not set",
        })
        try:
            ollama_ok = cfg_mod.ollama_available(timeout=0.75)
        except Exception:  # noqa: BLE001 - a probe must never break the endpoint
            ollama_ok = False
        entries.append({
            "name": "ollama",
            "configured": ollama_ok,
            "model": os.environ.get("OLLAMA_MODEL", "").strip() or cfg_mod.OLLAMA_DEFAULT_MODEL,
            "detail": "local server answered" if ollama_ok
                      else "local Ollama server not reachable at 127.0.0.1:11434",
        })
        try:
            omni_ok = cfg_mod.omniroute_available(timeout=0.75)
        except Exception:  # noqa: BLE001 - a probe must never break the endpoint
            omni_ok = False
        entries.append({
            "name": "omniroute",
            "configured": omni_ok,
            "model": os.environ.get("OMNIROUTE_MODEL", "").strip() or cfg_mod.OMNIROUTE_DEFAULT_MODEL,
            "detail": "gateway answered" if omni_ok else "OmniRoute gateway not reachable",
        })

        for name in ("deepseek", "qwen", "glm", "kimi", "codex"):
            try:
                _base, _key, model = code_backends.load_backend(name)
                entries.append({"name": name, "configured": True, "model": model, "detail": "configured"})
            except RuntimeError as exc:
                entries.append({"name": name, "configured": False, "model": "", "detail": str(exc)[:200]})

        # claude: routed via the CLI, not load_backend() — code_backends.py
        # has no "claude" branch (_run_claude shells out directly). Probe
        # the CLI the same way run_code_task's claude path does.
        try:
            from dourmouse.general_roster import _find_claude_cli

            cli = _find_claude_cli()
            entries.append({
                "name": "claude",
                "configured": cli is not None,
                "model": "",
                "detail": ("Claude Code CLI found on PATH" if cli is not None
                           else "Claude Code CLI ('claude') not found on PATH"),
            })
        except Exception as exc:  # noqa: BLE001 - a probe must never break the endpoint
            entries.append({"name": "claude", "configured": False, "model": "", "detail": str(exc)[:200]})

        return entries

    def _resolve_orchestrator_backend_model(self, backend: str) -> tuple[bool, str, str]:
        """(configured, model, detail) for one backend name from the SAME
        catalog _orchestrator_backend_catalog builds — used by the POST
        handler to resolve a {"backend": "<name>"} choice server-side."""
        name = (backend or "").strip().lower()
        for entry in self._orchestrator_backend_catalog():
            if entry["name"] == name:
                return bool(entry["configured"]), str(entry["model"]), str(entry["detail"])
        return False, "", f"unknown backend {backend!r}"

    def _active_llm_backend_name(self) -> str:
        """The REAL, currently-active backend identity ('nvidia'/'ollama'/
        'omniroute'/''), read from the actual resolved config object on
        self.server.config rather than re-deriving DOURMOUSE_LLM_BACKEND's
        'auto' resolution logic a second time (that logic already ran once,
        in config.load_llm_config(), to build self.server.config — asking
        again here could disagree with it, e.g. if Ollama's reachability
        changed between the two checks). Empty string when the server has
        no config object (a state config.py's own callers already handle
        by degrading, never guessing) — deliberately not a default guess.
        """
        from dourmouse import config as cfg_mod

        cfg = getattr(self.server, "config", None)
        if isinstance(cfg, cfg_mod.NvidiaConfig):
            return "nvidia"
        if isinstance(cfg, cfg_mod.OllamaConfig):
            return "ollama"
        if isinstance(cfg, cfg_mod.OmniRouteConfig):
            return "omniroute"
        return ""

    def _handle_orchestrator_model_get(self) -> None:
        """GET /api/settings/orchestrator-model."""
        from dourmouse import config as cfg_mod

        persisted = cfg_mod.orchestrator_model_setting()
        persisted_backend = cfg_mod.orchestrator_backend_setting()
        env_override = os.environ.get("DOURMOUSE_MODEL_ORCHESTRATOR", "").strip()
        # Real bug fixed here: a persisted model with no matching backend
        # tag (or a backend tag that isn't the CURRENTLY ACTIVE one) is
        # never actually applied by model_for_agent — reporting it as
        # "persisted"/active would be a lie the UI can't tell apart from a
        # setting that genuinely took effect. Report it honestly instead.
        active_backend = self._active_llm_backend_name()
        persisted_is_live = bool(persisted) and persisted_backend == active_backend
        if env_override:
            current, source = env_override, "env_override"
        elif persisted_is_live:
            current, source = persisted, "persisted"
        elif os.environ.get("NVIDIA_API_KEY", "").strip():
            # The stated default: nothing chosen yet AND an NVIDIA key is
            # configured -> default to that backend's model.
            current = os.environ.get("NVIDIA_MODEL", "").strip() or cfg_mod.NVIDIA_DEFAULT_MODEL
            source = "default_nvidia"
        else:
            cfg = self.server.config
            current = (
                cfg.model_for_agent("orchestrator")
                if cfg is not None and hasattr(cfg, "model_for_agent")
                else None
            )
            source = "default_active_backend"
        self._send_json({
            "current": current,
            "source": source,
            "persisted": persisted or None,
            "persisted_backend": persisted_backend or None,
            "active_backend": active_backend or None,
            "persisted_is_live": persisted_is_live,
            "backends": self._orchestrator_backend_catalog(),
        })

    def _handle_orchestrator_model_post(self) -> None:
        """POST /api/settings/orchestrator-model.

        Body is either ``{"backend": "<name>"}`` (resolved server-side to
        that backend's real current model, via the same catalog GET
        returns) or ``{"model": "<raw model id>"}`` (a manual override for
        a specific model id the catalog doesn't enumerate, e.g. a different
        NVIDIA NIM model). Saving does NOT require the backend to already
        be configured — the setting may point at something the user hasn't
        finished setting up yet; the response says so honestly via
        ``configured`` instead of blocking the save (degrade honestly,
        never crash — same rule every backend loader in this file follows).
        """
        from dourmouse import config as cfg_mod

        body = self._read_json_body()
        model = str(body.get("model") or "").strip()
        backend = str(body.get("backend") or "").strip().lower()
        configured: bool | None = None
        # Real bug fixed here: the catalog GET also lists deepseek/qwen/
        # glm/kimi/codex/claude — real backends, but reached through
        # code_backends.py/cn_backends.py for the CODE screen's OWN
        # toolchain picker, a totally separate dispatch path from the
        # orchestrator's config.model_for_agent(). Picking one of them
        # HERE used to silently persist a value that model_for_agent could
        # never apply to any of NvidiaConfig/OllamaConfig/OmniRouteConfig —
        # a save that reported success but changed nothing. Refuse it
        # honestly instead of accepting an impossible choice.
        _CORE_ORCHESTRATOR_BACKENDS = {"nvidia", "ollama", "omniroute"}
        if backend and backend not in _CORE_ORCHESTRATOR_BACKENDS:
            catalog_names = {e["name"] for e in self._orchestrator_backend_catalog()}
            if backend in catalog_names:
                self._send_json({
                    "ok": False,
                    "detail": (
                        f"{backend!r} can't be the orchestrator's model — it's reached "
                        "through the CODE screen's own toolchain picker, not this "
                        "setting. The orchestrator (the routing brain behind every "
                        "screen) only runs on nvidia, ollama, or omniroute."
                    ),
                })
            else:
                self._send_json({"ok": False, "detail": f"unknown backend {backend!r}"})
            return
        if not model and backend:
            configured, model, detail = self._resolve_orchestrator_backend_model(backend)
            if not model:
                self._send_json({"ok": False, "detail": detail})
                return
        if not model:
            self._send_json({"ok": False, "detail": "provide either 'backend' or 'model'"})
            return
        # A raw model-id override (no backend) can't be safety-checked
        # against any backend, so it's persisted un-tagged — see
        # save_orchestrator_model_setting's own docstring for why an
        # untagged value is never auto-applied by model_for_agent.
        result = cfg_mod.save_orchestrator_model_setting(model, backend=backend)
        if configured is not None:
            result["configured"] = configured
        self._send_json(result)

    def _handle_grounded_mode_post(self) -> None:
        """POST /api/settings/grounded-mode. Body: {"enabled": bool}."""
        from dourmouse import config as cfg_mod

        body = self._read_json_body()
        enabled = bool(body.get("enabled"))
        result = cfg_mod.save_grounded_mode_setting(enabled)
        self._send_json(result)

    def _handle_memory_api(self) -> None:
        """v2.9: honest Store & Learn stats for the dashboard.

        ``active`` means the learning loop is on AND a store is attached
        (DOURMOUSE_LEARN=0 or a missing FTS5 build -> inactive). ``count`` is
        the REAL number of stored facts — evidence, not a stub.
        """
        store = self.server.memory
        active = store is not None and learn_enabled()
        count = 0
        if active:
            try:
                count = store.count()
            except Exception:
                count = 0
        self._send_json(
            {
                "active": active,
                "count": count,
                "gate": (
                    "DOURMOUSE_LEARN=0 disables the learning loop"
                    if not active
                    else "learning loop on"
                ),
            }
        )

    def _handle_memory_remote_search(self) -> None:
        """GET /api/memory/search?q=<query>&limit=<n>&source=<optional> —
        the read half of the remote RAG contract (see the route comment
        above). Runs the exact same MemoryStore.search() this server
        already uses locally; a remote Mac's RemoteMemoryStore just calls
        this instead of opening the SQLite file itself."""
        if self.server.memory is None:
            self._send_json(
                {"ok": False, "error": "long-term memory is not configured on this machine"},
                status=409,
            )
            return
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        query = (qs.get("q") or [""])[0]
        source = (qs.get("source") or [None])[0]
        try:
            limit = int((qs.get("limit") or ["10"])[0])
        except ValueError:
            limit = 10
        try:
            hits = self.server.memory.search(query, limit=limit, source=source)
        except RemoteMemoryStoreUnavailable as exc:
            # A machine configured with DOURMOUSE_MEMORY_REMOTE_URL asks
            # ANOTHER machine for its memory. When that machine is off or
            # off-network, this is a dependency being down, not this
            # server erroring -- 503 says so, and says it in a way a
            # human reading the UI can act on. Previously this fell into
            # the generic handler below and surfaced as a bare 500 with a
            # urllib traceback string, which told the user nothing about
            # what to actually do.
            self._send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": (
                        "This machine is configured to keep its memory on another "
                        "machine (DOURMOUSE_MEMORY_REMOTE_URL in .env). Start "
                        "Dourmouse there, or unset that variable to use this "
                        "machine's own local memory store instead."
                    ),
                },
                status=503,
            )
            return
        except Exception as exc:  # noqa: BLE001 - honest, never a 500 the caller can't parse
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self._send_json({"ok": True, "hits": hits})

    def _handle_memory_remote_remember(self) -> None:
        """POST /api/memory/remember — the write half of the remote RAG
        contract. Body: {"source", "title", "body"}. Same real
        MemoryStore.remember() this server already uses for its own
        remember/recall tools -- one real store, local calls and remote
        calls both land in it the same way."""
        if self.server.memory is None:
            self._send_json(
                {"ok": False, "error": "long-term memory is not configured on this machine"},
                status=409,
            )
            return
        body = self._read_json_body()
        try:
            result = self.server.memory.remember(
                source=str(body.get("source") or ""),
                title=str(body.get("title") or ""),
                body=str(body.get("body") or ""),
            )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self._send_json({"ok": True, "result": result})

    def _handle_history_import(self) -> None:
        """v8.13: POST /api/history/import — pull Claude Code + Codex CLI
        session history into long-term memory (roadmap item 4).

        Synchronous, not backgrounded like /api/neuro/train: verified live
        against the user's real 81-session/116MB history, the whole import
        (both sources) completes in ~3s — well inside one HTTP request, and
        a synchronous result is simpler to show honestly than a poll loop
        for something this fast. Runs against ``self.server.memory`` (the
        SAME store the rest of Store & Learn uses), so an import shows up
        in /api/memory's count and in recall immediately, no restart.
        """
        if self.server.memory is None:
            self._send_json(
                {"ok": False, "error": "long-term memory is not configured "
                 "(DOURMOUSE_LEARN=0, or SQLite FTS5 unavailable on this build)"},
                status=409,
            )
            return
        from dourmouse.history_import import import_all_history

        try:
            result = import_all_history(self.server.memory)
        except Exception as exc:  # honest failure surface (Rule 2.2)
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self._send_json({"ok": True, **result})

    def _handle_profile_status(self) -> None:
        """v8.14: GET /api/profile — honest status for the SETTINGS panel.

        ``exists`` gates the UI: the button only offers to generate once,
        matching the explicit "once, at setup" spec on
        dourmouse.personality_profile (never on a schedule, never
        silently regenerated).
        """
        from dourmouse.personality_profile import PROFILE_SOURCE, PROFILE_TITLE, has_profile

        if self.server.memory is None:
            self._send_json({"exists": False, "profile": None})
            return
        # Live bug this guard exists for: against a RemoteMemoryStore this
        # raised AttributeError ('object has no attribute get') and the
        # connection was dropped outright -- curl reported HTTP 000, not a
        # 500, so the failure did not even look like a failure. The store
        # interface itself is fixed in memory_store.py; this reports the
        # remaining honest cases (remote unreachable, or an operation the
        # remote API genuinely cannot serve) as real JSON instead of
        # taking the endpoint down.
        try:
            exists = has_profile(self.server.memory)
            profile = None
            if exists:
                fact = self.server.memory.get(PROFILE_SOURCE, PROFILE_TITLE)
                profile = fact["body"] if fact else None
        except Exception as exc:
            self._send_json({
                "exists": False,
                "profile": None,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return
        self._send_json({"exists": exists, "profile": profile})

    def _handle_profile_generate(self) -> None:
        """v8.14: POST /api/profile/generate — the one-time working-style
        profile (roadmap: "should only be done once at the beginning of
        setup"). Synchronous: one LLM call, same real-time budget as any
        other chat turn this server already makes.
        """
        if self.server.memory is None:
            self._send_json(
                {"ok": False, "error": "long-term memory is not configured "
                 "(DOURMOUSE_LEARN=0, or SQLite FTS5 unavailable on this build)"},
                status=409,
            )
            return
        from dourmouse.personality_profile import generate_profile

        try:
            result = generate_profile(
                self.server.memory, client=self.server.client, config=self.server.config
            )
        except Exception as exc:  # honest failure surface (Rule 2.2)
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self._send_json({"ok": True, **result})

    def _handle_neuro_train(self) -> None:
        """v5.6: POST /api/neuro/train — force a background retrain.

        Single-flight: if a trainer is already running it honestly reports
        that instead of queueing. Returns ok even when the retrain is
        async — the panel polls GET /api/neuro for the result.
        """
        from dourmouse.orch_net import retrain_now

        names = [s.name for s in self.server.registry.all_subagents()]
        if retrain_now(names):
            self._send_json({"ok": True, "training": True})
        else:
            self._send_json(
                {
                    "ok": False,
                    "error": "a retrain is already running, or DOURMOUSE_NET is off",
                },
                status=409,
            )

    def _handle_feedback(self) -> None:
        """v2.9: operator 👍/👎 rating of the last completed turn.

        Stored as a 'feedback' fact in the long-term store so recall surfaces
        it and the model learns what the operator liked. v5.6: the SAME
        rating is the neural orchestrator's reward signal (sample-weight
        reweight + background retrain). Honest errors: no store -> 409, bad
        rating -> 400, nothing fabricated.
        """
        body = self._read_json_body()
        rating = (body.get("rating") or "").strip()
        # v5.6: apply the rating to the neural store FIRST (independent of
        # the memory store — the NN learns even when recall is off). A
        # raising neural store must never break feedback.
        if rating in ("good", "bad"):
            try:
                from dourmouse.orch_net import apply_feedback as _neuro_feedback

                stem = Path(self.server.session.session_file).stem
                _neuro_feedback(stem, rating)
            except Exception:
                pass
        store = self.server.memory
        if store is None or not learn_enabled():
            self._send_json(
                {
                    "ok": False,
                    "error": (
                        "memory disabled (DOURMOUSE_LEARN=0 or no store) — "
                        "feedback not stored"
                    ),
                },
                status=409,
            )
            return
        try:
            msg = record_feedback(
                store, self.server.session.session_file, rating
            )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if msg.startswith("ERROR"):
            # No completed turn to rate — honest failure, not ok=True noise.
            self._send_json({"ok": False, "error": msg}, status=404)
            return
        self._send_json({"ok": True, "message": msg})

    def _handle_repo_api(self) -> None:
        """v4.1 (P6+): GET /api/repo — Project Memory panel data.

        With ``?q=`` runs an FTS5 search scoped to repo facts; without it
        returns the honest status: fact count, last-scan meta (sidecar JSON),
        and the newest indexed facts. Memory disabled -> NOT CONFIGURED with
        the exact reason, never a fabricated zero.
        """
        from dourmouse.learn import learn_enabled
        from dourmouse.repo_index import (
            load_scan_meta,
            repo_facts,
            repo_search,
            repo_status,
        )

        store = self.server.memory
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        query = (qs.get("q") or [""])[0].strip()
        if store is None or not learn_enabled():
            self._send_json(
                {
                    "configured": False,
                    "error": (
                        "memory disabled (DOURMOUSE_LEARN=0 or no store) — "
                        "the repo index is NOT CONFIGURED"
                    ),
                }
            )
            return
        try:
            if query:
                self._send_json(
                    {
                        "configured": True,
                        "query": query,
                        "hits": repo_search(store, query, limit=10),
                    }
                )
                return
            status = repo_status(store)
            self._send_json(
                {
                    "configured": True,
                    "facts": status["facts"],
                    "last_scan": load_scan_meta(store),
                    "recent": repo_facts(store, limit=12),
                }
            )
        except Exception as exc:  # noqa: BLE001 -- honest 500, never crash the connection
            self._send_json({"configured": True, "error": str(exc)}, status=500)

    def _handle_vision_status(self) -> None:
        """world-monitor-expansion: GET /api/vision/status — an honest
        status roll-up for the Vision family: dourmouse/overlay.py,
        dourmouse/tray.py, dourmouse/wakeword.py, dourmouse/vision_bridge.py,
        dourmouse/proactive.py.

        The load-bearing honesty fact this endpoint must not paper over:
        THIS process (webui.py, what a browser talks to) does not itself
        start overlay.py, tray.py, or wakeword.py — they are standalone
        ``python -m dourmouse.<module>`` processes (or, for proactive.py
        only, wired into dourmouse.desktop's NATIVE launcher, not this one).
        So "is it actually running right now" is genuinely unobservable from
        a stateless HTTP request for three of the five, and each field below
        says exactly that instead of guessing. Two fields ARE real live
        reads: the kill-switch state (a shared on-disk file every module in
        this family reads) and the vision-bridge reachability (a real
        loopback HTTP probe, same one ui/index.html's browser poller makes).
        """
        from dourmouse.desktop import _import_webview
        from dourmouse.tray import _import_pystray
        from dourmouse.tray import load_state as _load_kill_switch_state
        from dourmouse.wakeword import (
            _capture_available,
            _inference_available,
            wakeword_enabled,
            wakeword_model,
            wakeword_threshold,
        )

        kill_switch = _load_kill_switch_state()

        def _require_inference() -> None:
            if not _inference_available():
                raise RuntimeError("openwakeword not installed")

        def _require_capture() -> None:
            if not _capture_available():
                raise RuntimeError("sounddevice not installed")

        # Kick off every dependency probe concurrently (each a no-op after
        # its first call, ever, in this process — see
        # _vision_dependency_start) and wait for at most ONE shared time
        # budget total, not one per probe — openwakeword's first import
        # alone can take several seconds in this sandbox, and four
        # sequential waits would make that four times worse.
        _vision_dependency_start("overlay_webview", _import_webview)
        _vision_dependency_start("tray_pystray", _import_pystray)
        _vision_dependency_start("wakeword_inference", _require_inference)
        _vision_dependency_start("wakeword_capture", _require_capture)
        _deadline = time.monotonic() + 1.2
        overlay_dependency_ok = _vision_dependency_status("overlay_webview", _deadline)
        tray_dependency_ok = _vision_dependency_status("tray_pystray", _deadline)
        inference_ok = _vision_dependency_status("wakeword_inference", _deadline)
        capture_ok = _vision_dependency_status("wakeword_capture", _deadline)

        def _tri(value: bool | None) -> Any:
            return value if value is not None else "checking — retry shortly"

        # overlay.py: no started-here wiring and no process registry
        # anywhere records whether `python -m dourmouse.overlay` is live —
        # honest "unknown", not a guess. The one thing we CAN check is
        # whether its one real dependency is installed.
        overlay = {
            "dependency_installed": _tri(overlay_dependency_ok),
            "running": "unknown",
            "note": (
                "always-on-top status window; not started by this web "
                "server or by the native desktop launcher — only runs when "
                "someone launches `.venv/bin/python -m dourmouse.overlay` "
                "in a live desktop session. Whether it's on screen right "
                "now cannot be determined from an HTTP request."
            ),
        }

        # tray.py: same shape as overlay — the kill-switch STATE it owns is
        # real (above); whether the tray icon PROCESS is alive is not
        # observable here.
        tray = {
            "dependency_installed": _tri(tray_dependency_ok),
            "running": "unknown",
            "kill_switch": {
                "mic_enabled": kill_switch.mic_enabled,
                "camera_enabled": kill_switch.camera_enabled,
                "updated_at": kill_switch.updated_at or None,
            },
            "note": (
                "the kill-switch state above is real — it's the same "
                "on-disk file every reader in this family shares. Whether "
                "the tray icon process itself is running (`.venv/bin/"
                "python -m dourmouse.tray`) is not observable from here."
            ),
        }

        # wakeword.py: same honest shape as wakeword.wakeword_status().
        # Nothing in this repo auto-starts a WakeWordListener, so
        # "listening" is unknown by the same logic as overlay/tray above.
        wakeword = {
            "enabled": wakeword_enabled(),
            "inference_engine": (
                "openwakeword" if inference_ok else "checking" if inference_ok is None
                else "not-configured"
            ),
            "capture_engine": (
                "sounddevice" if capture_ok else "checking" if capture_ok is None
                else "not-configured"
            ),
            "model": wakeword_model(),
            "threshold": wakeword_threshold(),
        }
        wakeword["listening"] = "unknown"
        wakeword["note"] = (
            "wake-word detection itself (openWakeWord ONNX inference on "
            "synthetic frames) is genuinely verified in this sandbox; the "
            "CONTINUOUS MICROPHONE CAPTURE loop is not — every sample "
            "sounddevice returned here was silence, the signature of a "
            "process never granted real macOS microphone (TCC) permission. "
            "Confirming a real spoken wake word fires needs a live desktop "
            "session with mic permission actually granted."
        )

        # vision_bridge.py: a REAL loopback probe, not a guess — the exact
        # request ui/index.html's browser-side poller makes.
        from dourmouse.vision_bridge import bridge_port

        port = bridge_port()
        vb_reachable = False
        vb_state: dict[str, Any] | None = None
        vb_error = None
        try:
            import urllib.error
            import urllib.request

            req_url = f"http://127.0.0.1:{port}/api/vision-state"
            with urllib.request.urlopen(req_url, timeout=0.5) as resp:  # noqa: S310 -- loopback only
                vb_state = json.loads(resp.read().decode("utf-8"))
                vb_reachable = True
        except (OSError, ValueError, urllib.error.URLError) as exc:
            vb_error = str(exc)
        vision_bridge = {
            "configured_port": port,
            "reachable": vb_reachable,
            "state": vb_state,
            "error": None if vb_reachable else (
                vb_error or "no vision bridge reachable on this port"
            ),
            "note": (
                "started by dourmouse/tray.py alongside its tray icon "
                "(TrayApp.run -> _start_vision_bridge), not by this web "
                "server. reachable=false most often just means tray.py "
                "isn't running right now, matching this bridge's own "
                "documented fail-open default."
            ),
        }

        # proactive.py: the allowlist itself is real and static; whether it
        # is actually WIRED to interrupt depends on running inside
        # dourmouse.desktop's native launcher (not this browser-facing
        # server) with its own SSE hub and env gate, both checkable here.
        from dourmouse.proactive import ALLOWED_ALERT_KINDS

        proactive_env_enabled = os.environ.get("DOURMOUSE_PROACTIVE_SURFACE", "1") != "0"
        events_hub = getattr(self.server, "events_broadcast", None)
        proactive = {
            "allowed_alert_kinds": list(ALLOWED_ALERT_KINDS),
            "env_enabled": proactive_env_enabled,
            "events_hub_present": events_hub is not None,
            "wired": "unknown — only true inside a live native desktop "
                     "session (dourmouse.desktop), never in this browser-"
                     "facing web server",
            "note": (
                "of the three allowed kinds, only \"system\" has a real "
                "add_alert(...) call site in this codebase today (the "
                "ATLAS-run-started handler); \"world\" and \"atlas\" are "
                "wired and ready but nothing currently produces them."
            ),
        }

        self._send_json(
            {
                "kill_switch": {
                    "mic_enabled": kill_switch.mic_enabled,
                    "camera_enabled": kill_switch.camera_enabled,
                    "updated_at": kill_switch.updated_at or None,
                },
                "overlay": overlay,
                "tray": tray,
                "wakeword": wakeword,
                "vision_bridge": vision_bridge,
                "proactive": proactive,
            }
        )

    def _handle_vision_kill_switch_post(self) -> None:
        """world-monitor-expansion: POST /api/vision/kill-switch.

        Body: ``{"action": "kill_all"}`` (both off, no confirmation — the
        exact same one-call semantics as tray.KillSwitch.kill_all(), which
        is what TrayApp's own "Kill camera + mic NOW" menu item calls) or
        ``{"action": "set_mic", "enabled": bool}`` / ``{"action":
        "set_camera", "enabled": bool}``.

        This calls dourmouse.tray.KillSwitch directly — the SAME class the
        native tray icon uses — so a flip from the browser writes the exact
        shared state file every module in this family reads, and is honored
        everywhere, whether or not the tray icon process happens to be
        running. Unknown/malformed actions are rejected 400, never silently
        ignored.
        """
        from dourmouse.tray import KillSwitch

        body = self._read_json_body()
        action = str(body.get("action") or "").strip()
        ks = KillSwitch()
        if action == "kill_all":
            state = ks.kill_all()
        elif action == "set_mic":
            state = ks.set_mic(bool(body.get("enabled")))
        elif action == "set_camera":
            state = ks.set_camera(bool(body.get("enabled")))
        else:
            self._send_json(
                {"ok": False, "error": "action must be one of: kill_all, set_mic, set_camera"},
                status=400,
            )
            return
        self._send_json(
            {
                "ok": True,
                "kill_switch": {
                    "mic_enabled": state.mic_enabled,
                    "camera_enabled": state.camera_enabled,
                    "updated_at": state.updated_at or None,
                },
            }
        )

    def _handle_voice_command_post(self) -> None:
        """world-monitor-expansion: POST /api/voice/command — the ONE real
        place dourmouse.voice_commands.parse_voice_command runs, so
        ui/workspace.html's browser JS never re-implements the grammar
        client-side. Body: {"text": "<utterance>"}. Deterministic parse,
        never an LLM call (Rule 2.8) — this endpoint only recognizes and
        structures the fixed 4-command grammar; it does not itself perform
        any action. {"ok": true, "recognized": false} (not a 400) when the
        text is real but doesn't match the grammar — the caller's own
        honest fallback (route it to the companion agent as ordinary chat)
        is a normal outcome, not an error."""
        from dourmouse.voice_commands import parse_voice_command

        body = self._read_json_body()
        text = str(body.get("text") or "")
        cmd = parse_voice_command(text)
        if cmd is None:
            self._send_json({"ok": True, "recognized": False})
            return
        self._send_json({"ok": True, "recognized": True, "command": cmd.to_dict()})

    def _handle_atlas_run(self) -> None:
        """v5.4: POST /api/atlas/run — start one managed ATLAS command.

        Body: {"command": "fx-daily" | "fx-refresh" | "fx-verify" |
        "fx-universe" | "health" | "version" | "fx-daily-no-refresh"}.
        Single-flight: a run already in progress is honestly refused (409),
        never queued. The command's real progress/result is polled via
        GET /api/atlas (last_run). Unknown commands are rejected 400.
        """
        from dourmouse.atlas_cli import atlas_run_manager

        body = self._read_json_body()
        command = (body.get("command") or "").strip()
        try:
            started = atlas_run_manager.launch(command)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if not started:
            self._send_json(
                {
                    "ok": False,
                    "error": "an ATLAS command is already running (single-flight) — wait or poll /api/atlas",
                },
                status=409,
            )
            return
        # v5.14 Phase R0: a started ATLAS run is a SYSTEM alert in the
        # DOURMOUSE ALERTS inbox, fanned out live over the SSE hub.
        store = getattr(self.server, "state", None)
        if store is not None:
            try:
                store.add_alert(
                    kind="system",
                    title=f"ATLAS run started: {command}",
                    detail="managed run · progress via #/atlas",
                    link="#/atlas",
                )
                # owner '*' — a system alert, visible to everyone; the
                # broadcast marks it shared so every client refreshes.
                from dourmouse.state_store import SHARED_OWNER as _SHARED

                self._state_changed("alerts", owner=_SHARED)
            except Exception:  # noqa: BLE001 - an alert must never fail the run launch
                pass
        self._send_json({"ok": True, "command": command, "running": True})

    # -- v5.14 Phase R0: cross-device state (one store, SSE fan-out) ------ #

    def _state(self) -> Any:
        store = getattr(self.server, "state", None)
        if store is None:
            from dourmouse.state_store import StateStore

            store = StateStore()
        return store

    def _state_owner(self) -> str:
        """The data scope for THIS request (v5.17): the signed-in Google
        user's email, or the shared bucket when nobody is signed in. Every
        state read/write passes this so two signed-in people on one server
        never see each other's watchlist / alerts / prefs / workspace."""
        from dourmouse.state_store import SHARED_OWNER

        return self._session_user() or SHARED_OWNER

    def _state_changed(self, section: str, owner: str, **payload: Any) -> None:
        """Broadcast a state_change over the SSE hub so every connected
        device (desktop / phone / tablet) refreshes the affected section
        live — the cheapest reliable realtime there is (spec §9).

        v5.17: the broadcast carries the ACTING OWNER so clients can ignore
        other users' events (the metadata must not cross the wire to another
        signed-in person — reviewer-caught). The refetch itself is always
        scoped by each client's own session cookie."""
        hub = getattr(self.server, "events_broadcast", None)
        if hub is not None:
            hub.broadcast(
                {"type": "state_change", "section": section,
                 "owner": owner, **payload}
            )

    def _handle_state_watchlist(self) -> None:
        """POST /api/state/watchlist — body: {action: add|remove, symbol, name?}."""
        body = self._read_json_body()
        action = (body.get("action") or "").strip().lower()
        symbol = (body.get("symbol") or "").strip()
        owner = self._state_owner()
        store = self._state()
        removed = False
        try:
            if action == "add":
                store.add_watch(symbol, name=body.get("name") or "",
                                source=body.get("source") or "desktop",
                                owner=owner)
            elif action == "remove":
                removed = store.remove_watch(symbol, owner=owner)
            else:
                self._send_json(
                    {"ok": False, "error": f"unknown action {action!r} (add|remove)"},
                    status=400,
                )
                return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self._state_changed("watchlist", owner=owner, symbol=symbol, action=action)
        self._send_json({"ok": True, "removed": removed,
                         "watchlist": store.watchlist(owner)})

    def _handle_state_alerts(self) -> None:
        """POST /api/state/alerts — actions: dismiss {id}, mute {kind},
        unmute {kind}, priority {id, priority}."""
        body = self._read_json_body()
        action = (body.get("action") or "").strip().lower()
        owner = self._state_owner()
        store = self._state()
        # Honest result (Rule 2.2): a dismiss/priority of an unknown id is
        # NOT silently reported as success — the caller sees the bool.
        # v5.17: dismissing/reprioritizing ANOTHER user's alert is refused
        # (ownership-guarded UPDATE in the store).
        changed = True
        try:
            if action == "dismiss":
                changed = store.dismiss_alert(int(body.get("id") or 0), owner=owner)
            elif action == "mute":
                store.mute(body.get("kind") or "", owner=owner)
            elif action == "unmute":
                store.unmute(body.get("kind") or "", owner=owner)
            elif action == "priority":
                changed = store.set_priority(int(body.get("id") or 0),
                                             int(body.get("priority") or 0),
                                             owner=owner)
            else:
                self._send_json(
                    {"ok": False,
                     "error": f"unknown action {action!r} (dismiss|mute|unmute|priority)"},
                    status=400,
                )
                return
        except (ValueError, TypeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self._state_changed("alerts", owner=owner, action=action)
        self._send_json({"ok": changed, "alerts": store.alerts(owner),
                         "muted": store.muted_sources(owner)})

    def _handle_state_prefs(self) -> None:
        """POST /api/state/prefs — body: {key, value} (last-write-wins),
        scoped to the request's owner (v5.17)."""
        body = self._read_json_body()
        owner = self._state_owner()
        store = self._state()
        try:
            store.set_pref(body.get("key") or "", body.get("value"), owner=owner)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self._state_changed("prefs", owner=owner, key=body.get("key") or "")
        self._send_json({"ok": True, "prefs": store.prefs(owner)})

    def _handle_state_workspace(self) -> None:
        """POST /api/state/workspace — body: {device, workspace}: where THIS
        device left off, for the optional resume banner (spec §8), scoped to
        the request's owner (v5.17 — resume is personal, not cross-user)."""
        body = self._read_json_body()
        owner = self._state_owner()
        store = self._state()
        try:
            record = store.set_workspace(body.get("device") or "",
                                         body.get("workspace") or "",
                                         owner=owner)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self._state_changed("workspace", owner=owner, device=record["device"])
        self._send_json({"ok": True, "workspaces": store.workspaces(owner)})

    def _build_palette(self) -> dict[str, Any]:
        """The command-centre index (spec §16): every destination, every
        agent, and the common commands — one source for ⌘K and the mobile ⚡
        sheet. Deterministic; never fabricated (Rule 2.8)."""
        destinations = [
            {"id": "home", "label": "Home", "href": "#/"},
            {"id": "atlas", "label": "Open ATLAS", "href": "#/atlas"},
            {"id": "world", "label": "Open World Monitor", "href": "#/world"},
            {"id": "portfolio", "label": "Show portfolio", "href": "#/portfolio"},
            {"id": "markets", "label": "Open Markets", "href": "#/markets"},
            {"id": "intelligence", "label": "Open Intelligence", "href": "#/intelligence"},
            {"id": "alerts", "label": "Show my alerts", "href": "#/alerts"},
            {"id": "settings", "label": "Open Settings", "href": "#/settings"},
        ]
        agents = []
        try:
            for name in sorted(self.server.registry.subagent_names):
                agents.append({"name": name, "label": f"Find agent: {name}",
                               "href": f"/agent/{name}"})
        except Exception:  # noqa: BLE001 - a broken roster never kills the palette
            agents = []
        commands = [
            {"id": "fx-daily", "label": "Run fx-daily", "action": "run-fx-daily"},
            {"id": "fx-refresh", "label": "Run fx-refresh", "action": "run-fx-refresh"},
        ]
        return {"destinations": destinations, "agents": agents,
                "commands": commands}

    def _handle_deeplink_post(self) -> None:
        """v5.20: POST /api/deeplink — body {to: <target>}. Same allow-list
        gate as the GET route, but instead of a 302 it broadcasts a
        validated ``navigate`` SSE event over the hub so EVERY open window
        (the desktop shell's webview, a phone on /mobile, another browser
        tab) routes to the workspace live — the already-running-app path for
        ``dourmouse://`` deep links. Only allow-list hrefs are ever
        broadcast; a hostile ``to`` is a plain 400, never executed.
        """
        from dourmouse.deeplink import parse_deeplink

        body = self._read_json_body()
        parsed_target = parse_deeplink((body.get("to") or "").strip())
        if not parsed_target["ok"]:
            self._send_json({"ok": False, "error": parsed_target["reason"]},
                            status=400)
            return
        self._state_changed_navigate(parsed_target["href"])
        self._send_json({"ok": True, "href": parsed_target["href"]})

    def _state_changed_navigate(self, href: str) -> None:
        """Broadcast a validated navigation to every connected window. The
        href comes from the allow-list parser, so only [A-Za-z0-9_/-#] ever
        reaches the clients' location.hash (never executed)."""
        hub = getattr(self.server, "events_broadcast", None)
        if hub is not None:
            hub.broadcast({"type": "navigate", "href": href})

    def _handle_deeplink(self, parsed) -> None:
        """v5.19: GET /api/deeplink?to=<target>[&format=json] — allow-listed
        navigation. ``to`` accepts ``dourmouse://atlas/research/example``,
        ``atlas``, or ``atlas/research``. The parser drops anything off the
        allow-list with an honest reason (never executed — no shell, no
        paths, no arbitrary URLs). Returns a 302 to the validated SPA hash
        route so a browser or webview click lands in the workspace;
        ``format=json`` returns the parsed target for programmatic clients
        (the desktop shell uses it to drive its own router).
        """
        from dourmouse.deeplink import parse_deeplink

        qs = urllib.parse.parse_qs(parsed.query)
        target = (qs.get("to") or [""])[0].strip()
        parsed_target = parse_deeplink(target)
        if not parsed_target["ok"]:
            self._send_json(
                {"ok": False, "error": parsed_target["reason"]}, status=400
            )
            return
        if (qs.get("format") or [""])[0].strip() == "json":
            self._send_json({"ok": True, **parsed_target})
            return
        # Location MUST resolve to the SPA root, not back onto /api/deeplink
        # (a fragment-only Location resolves against the REQUEST uri and would
        # loop forever). v8.7: this targets "/index.html" EXPLICITLY rather
        # than "/" — the hash router that handles #/atlas, #/world, etc lives
        # only in the HUD, and "/" now serves the console (no hash routing),
        # so a bare "/" would silently drop every deeplink on the home screen.
        self.send_response(302)
        self.send_header("Location", "/index.html" + parsed_target["href"])
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_repo_scan(self) -> None:
        """v4.1 (P6+): POST /api/repo/scan — idempotent re-index of ATLAS.

        Runs the same deterministic scan_repo as the atlas_repo_scan tool
        against ATLAS_REPO_PATH, persists the summary to the sidecar meta,
        and returns it. Honest errors: memory off -> 409, unset/invalid repo
        path -> NOT CONFIGURED, scan failure -> 500 with the real reason.
        """
        from dourmouse.atlas_ops import AtlasNotConfiguredError, get_atlas_repo_path
        from dourmouse.learn import learn_enabled
        from dourmouse.repo_index import save_scan_meta, scan_repo

        store = self.server.memory
        if store is None or not learn_enabled():
            self._send_json(
                {
                    "ok": False,
                    "error": (
                        "memory disabled (DOURMOUSE_LEARN=0 or no store) — "
                        "the repo index is NOT CONFIGURED"
                    ),
                },
                status=409,
            )
            return
        try:
            root = get_atlas_repo_path()
        except AtlasNotConfiguredError as exc:
            self._send_json(
                {"ok": False, "error": f"NOT CONFIGURED — {exc}"}, status=409
            )
            return
        try:
            stats = scan_repo(store, root)
            save_scan_meta(store, stats, root)
        except Exception as exc:  # noqa: BLE001 -- honest 500, never crash the connection
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self._send_json({"ok": True, "root": str(root), "stats": stats})

    def _handle_messages_api(self) -> None:
        """v3.0/3.2: inter-agent bus traffic for the COMMS panels.

        Real messages from the bus (Rule 2.1 — the bodies are whatever the
        agents actually posted), newest first, plus per-agent unread counts
        so the UI can badge inboxes.

        v3.2 optional ``?since=msg-<N>``: return ONLY messages newer than
        that id so the map can poll cheaply and animate each new message as
        it arrives (a pulse on the neural graph). Unread counts are ALWAYS
        absolute — computed from the whole bus, never the filtered window.
        """
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        limit_raw = (qs.get("limit") or ["30"])[0]
        try:
            limit = max(1, min(int(limit_raw), 200))
        except ValueError:
            limit = 30
        since_num = 0
        since_raw = (qs.get("since") or [""])[0].strip()
        m = re.match(r"^(?:msg-)?(\d+)$", since_raw)
        if m:
            since_num = int(m.group(1))
        bus = getattr(self.server, "bus", None) or get_message_bus()
        messages = []
        for msg in bus.snapshot(limit):
            mid = re.match(r"^msg-(\d+)$", str(msg.get("id", "")))
            if mid and int(mid.group(1)) > since_num:
                messages.append(msg)
        unread = {}
        for sub in self.server.registry.all_subagents():
            try:
                unread[sub.name] = bus.unread_count(sub.name)
            except Exception:
                unread[sub.name] = 0
        self._send_json(
            {
                "messages": messages,
                "unread": unread,
                "count": bus.count(),
            }
        )

    def _handle_role(self) -> None:
        """Phase A3: switch THIS conversation's RBAC role (audited).

        Elevation gate: a conversation may never switch to a role MORE
        permissive than the app-level DOURMOUSE_ROLE. A readonly deployment
        cannot self-elevate to operator through the UI — the app role is the
        ceiling (defense in depth on top of RbacPolicy enforcement)."""
        body = self._read_json_body()
        role = (body.get("role") or "").strip()
        if not role:
            self._send_json({"error": "role is required"}, status=400)
            return
        # Validate the name first so a garbage role gets a 400, not a 403.
        try:
            RbacPolicy(role)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        app_role = getattr(self.server, "app_role", "operator")
        if app_role != "operator" and role != "readonly":
            self._send_json(
                {
                    "error": (
                        f"REFUSED: role {role!r} would exceed the app-level "
                        f"role {app_role!r}. Nothing was changed."
                    )
                },
                status=403,
            )
            return
        # Serialize with chat so a role never changes mid-turn.
        with self.server.session_lock:
            snapshot = self.server.session.set_role(role)
        self._send_json({"ok": True, "rbac": snapshot})


def run_server(
    registry: DispatchRegistry,
    *,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    client: Any | None = None,
    config: NvidiaConfig | None = None,
    live_polling: bool = False,
    memory: MemoryStore | None = None,
    bus: MessageBus | None = None,
    reporting: bool = False,
    neuro: bool = False,
    artifacts=None,
    freebuff_events: bool = False,
    news_stream: bool = False,
    news_stream_poll_interval: float | None = None,
    state=None,
    auth=None,
    session_file: Path | str | None = None,
) -> ThreadingHTTPServer:
    """Start the UI server. Returns the running ThreadingHTTPServer.

    ``session_file`` (v8.31): which on-disk session ledger the server's
    live ``ChatSession`` writes to and resumes from. None (default,
    unchanged) mints a fresh ``session_<timestamp>.jsonl`` every call, same
    as before this parameter existed — every existing caller (desktop.py,
    the test suite) is unaffected. A caller that wants a conversation to
    survive a process RESTART, not just a browser reload, passes the same
    path back in (e.g. the most recent ``workspace/sessions/*.jsonl``);
    ChatSession.__init__ already knows how to resume one (``_load_state``)
    — this just gives run_server a way to hand it one.

    ``live_polling`` (v2.8): when True (and DOURMOUSE_LIVE is not disabled),
    an always-on LiveRuntime is started against the server's own tracker,
    so the preloaded news/markets/mail/tasks agents poll their real feeds
    continuously and their windows show live activity without any prompt.
    Tests keep it False (default) so the suite never touches the network.

    ``memory`` (v2.9): the long-term store for the Store & Learn loop. None
    (default) means NO learning — hermetic tests never touch a real store.
    The real serving paths (serve_forever, desktop.launch) explicitly open
    the default store and pass it in.

    ``bus`` (v3.0): the inter-agent message bus. None uses the process
    singleton so tools/live-runtime/UI share one channel; tests pass a fresh
    bus. When ``memory`` is set, bus posts are mirrored to the store.

    ``reporting`` (v4.0): when True (and DOURMOUSE_REPORT is not disabled), a
    DailyReporter thread posts the morning briefing to the bus + tracker at
    DOURMOUSE_REPORT_TIME (default 08:30). Tests keep it False (default).

    ``freebuff_events`` (v5.9): when True, a background watcher consumes the
    Freebuff app's /api/events SSE stream and broadcasts live thread
    activity (turn started/finished, created, status change) to every HUD
    connected via GET /api/events. Tests keep it False (default) so the
    suite never touches the Freebuff app.

    ``news_stream`` (v13): when True, a background NewsStreamWatcher polls
    real disaster/conflict/news feeds (dourmouse.news_stream, reusing
    world_pulse.py's own already-proven fetchers) on a fixed interval and
    broadcasts every new item to the same GET /api/events hub — the
    backend half of "our own customizable news mile app within Dourmouse
    that updates without us having to prompt" (verbatim from the task that
    motivated this). Tests keep it False (default) so the suite never
    touches the network.
    """

    def _list_sessions() -> dict[str, Any]:
        import os

        raw = os.environ.get("DOURMOUSE_WORKSPACE")
        root = Path(raw).expanduser() if raw else _PROJECT_ROOT / "workspace"
        sessions_dir = root / "sessions"
        if not sessions_dir.is_dir():
            return {"sessions": []}
        files = sorted(sessions_dir.glob("*.jsonl"), reverse=True)[:20]
        return {
            "sessions": [
                {
                    "name": f.name,
                    "path": str(f),
                    "size": f.stat().st_size,
                }
                for f in files
            ]
        }

    def _list_recent_sessions() -> dict[str, Any]:
        """Recent sessions with one-line summaries (v2.0 Phase 2.3).

        Surfaces REAL data already on disk (workspace/sessions/*.jsonl): the
        first user message + last assistant final_text per session, so the
        dashboard shows "what was I doing" instead of a blank console. Each
        record in the JSONL is one turn; the first record's ``user`` and the
        last record's ``final_text`` give the one-line summary.
        """
        import os

        raw = os.environ.get("DOURMOUSE_WORKSPACE")
        root = Path(raw).expanduser() if raw else _PROJECT_ROOT / "workspace"
        sessions_dir = root / "sessions"
        if not sessions_dir.is_dir():
            return {"sessions": []}
        files = sorted(sessions_dir.glob("*.jsonl"), reverse=True)[:10]
        out = []
        for f in files:
            first_user = ""
            last_answer = ""
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    if not first_user:
                        first_user = (rec.get("user") or "").strip()
                    if rec.get("final_text"):
                        last_answer = (rec.get("final_text") or "").strip()
            except (json.JSONDecodeError, OSError):
                continue
            out.append(
                {
                    "name": f.name,
                    "first_user": first_user[:140],
                    "last_answer": last_answer[:140],
                    "turns": f.stat().st_size and sum(
                        1 for ln in f.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()
                    ),
                }
            )
        return {"sessions": out}

    _SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    def _get_session_transcript(session_id: str | None) -> dict[str, Any]:
        """Full turn-by-turn transcript for one session (reload survival).

        ``session_id`` None means "whichever session this server process is
        currently running" — ``server.session`` is the live ``ChatSession``,
        already durably appending one hash-chained record per turn to
        ``session_file`` (chat.py's ``_persist``, unchanged). A page reload
        keeps the same server process (same footer SESSION id, confirmed
        live), so the live object's own ``session_file`` IS the reload
        target — no directory scan, no guessing which file is "current".

        A concrete ``session_id`` is any past session's file stem (exactly
        what /api/sessions already lists as ``name`` minus ``.jsonl``),
        read the same way, for a future "reopen an old thread" UI.

        Reuses the existing ledger format wholesale (no new storage, no
        second persistence path) — each returned "turn" is one JSONL record
        as chat.py already writes it: ``user`` text, ``final_text`` reply,
        and the raw ``transcript`` of tool_use/tool_result/assistant_text
        events from that turn, which is exactly what the console's own
        addYou()/addReply()/act() already know how to render live.
        """
        import os

        if session_id is None:
            target = Path(server.session.session_file)
        else:
            if not _SESSION_ID_RE.match(session_id):
                return {"ok": False, "error": "invalid session id"}
            raw = os.environ.get("DOURMOUSE_WORKSPACE")
            root = Path(raw).expanduser() if raw else _PROJECT_ROOT / "workspace"
            sessions_dir = (root / "sessions").resolve()
            candidate = (sessions_dir / f"{session_id}.jsonl").resolve()
            # Defense in depth beyond the regex above: the resolved path
            # must stay inside sessions_dir (session_id is untrusted
            # request input reaching straight into a filesystem path).
            if sessions_dir != candidate.parent:
                return {"ok": False, "error": "invalid session id"}
            target = candidate
        if not target.is_file():
            return {"ok": False, "error": "session not found", "id": target.stem}
        turns: list[dict[str, Any]] = []
        try:
            for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                turns.append(
                    {
                        "turn": rec.get("turn"),
                        "timestamp": rec.get("timestamp"),
                        "elapsed_ms": rec.get("elapsed_ms"),
                        "user": rec.get("user", ""),
                        # v13: the raw, unwrapped text the user actually typed
                        # (rec.get("user") is what the MODEL saw — for a
                        # focus_agent turn that's the internal "[ROUTING
                        # DIRECTIVE] ..." wrapper, which used to leak straight
                        # into the restored transcript). Older records predate
                        # this field entirely; fall back to "user" so they
                        # still restore exactly as before.
                        "display_text": rec.get("display_text") or rec.get("user", ""),
                        # v13: which console screen this turn belongs to, so
                        # restore can put it back on its own thread instead of
                        # flattening every screen onto HOME. Older records
                        # predate this too; default HOME matches their only
                        # actual behavior before per-screen restore existed.
                        "screen": rec.get("screen") or "HOME",
                        "final_text": rec.get("final_text", ""),
                        "transcript": rec.get("transcript", []),
                        "interventions": rec.get("interventions", []),
                    }
                )
        except (json.JSONDecodeError, OSError) as exc:
            return {"ok": False, "error": f"could not read session: {exc}", "id": target.stem}
        return {"ok": True, "id": target.stem, "turns": turns}

    # Validate configuration BEFORE any side effect: an invalid DOURMOUSE_ROLE
    # must fail loudly before a socket is ever bound (institutional: no
    # partially-initialized service).
    rbac = _load_role()

    # v4.0: resolve the access token ONCE at serve time (never per request —
    # deterministic, Rule 2.8). The desktop app on loopback stays exempt.
    from dourmouse.config import access_token

    access = access_token()
    if access and host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"DOURMOUSE: binding to {host} with DOURMOUSE_ACCESS_TOKEN set — "
            "remote clients must present the token (Bearer or cookie)."
        )
    elif not access and host not in ("127.0.0.1", "localhost", "::1"):
        # v5.32: REFUSE to bind instead of warning. An empty token makes
        # _authorized() return True for every request, so a network-bound
        # server with no token hands the full agent surface — Gmail, the
        # filesystem, run_command — to anyone who can reach the port. The old
        # behaviour printed this warning and started anyway, which means the
        # one deployment that most needs the token (a LAN server) was the one
        # where a scrolled-past log line was the only defence.
        #
        # The escape hatch is deliberately explicit and ugly: no operator sets
        # it by accident, and it appears in `ps`/task manager for anyone
        # auditing the box.
        if os.environ.get(
            "DOURMOUSE_ALLOW_INSECURE_BIND", ""
        ).strip().lower() not in ("1", "true", "yes", "on"):
            raise RuntimeError(
                f"refusing to bind to {host} without DOURMOUSE_ACCESS_TOKEN: "
                "every route would be open to anyone who can reach this port "
                "(mail, files, shell). Set DOURMOUSE_ACCESS_TOKEN to a long "
                "random value, or bind to 127.0.0.1 for local-only use. "
                "To override anyway (NOT recommended, e.g. an isolated test "
                "network), set DOURMOUSE_ALLOW_INSECURE_BIND=1."
            )
        print(
            f"WARNING: binding to {host} with NO DOURMOUSE_ACCESS_TOKEN and "
            "DOURMOUSE_ALLOW_INSECURE_BIND=1 — every route on this port is "
            "unauthenticated. Anyone who can reach it can drive mail, files "
            "and shell."
        )

    server = ThreadingHTTPServer((host, port), _Handler)
    # Fixed app-level role: the ceiling every conversation-level switch is
    # measured against (see _handle_role elevation gate).
    server.app_role = rbac.role
    server.access_token = access  # v4.0: auth gate (empty = loopback-only)
    server.registry = registry
    server.client = client
    server.config = config
    # v4.0: expose the resolved backend label so the UI can show which brain
    # is live (ollama/nvidia/default) without re-resolving per request.
    server.backend_label = _backend_label(config)
    server.tracker = ActivityTracker(registry)
    server.attention = AttentionQueue()
    # v13.5 (Vision OS checklist item 6, contextual chimes): wired here
    # rather than at JobTracker's own default so it stays testable/
    # injectable (test_dispatch.py's JobTracker tests construct their own
    # instances with no chime_fn at all) and so a broken chimes.py import
    # can never take down server startup (try/except, same discipline as
    # every other optional-feature wiring in this function — see the
    # hands_free block above).
    try:
        from dourmouse.chimes import announce_job_result

        server.jobs = JobTracker(chime_fn=announce_job_result)
    except Exception:
        server.jobs = JobTracker()
    server.memory = memory  # v2.9: long-term store for the learning loop
    # v3.0: the inter-agent message bus. Defaults to the process singleton so
    # the messenger tools, the live runtime, and the UI share ONE channel;
    # tests pass an explicit fresh bus for isolation. When a memory store is
    # attached, posted messages are mirrored into long-term memory (source
    # "bus") so the system LEARNS from inter-agent traffic too.
    server.bus = bus if bus is not None else get_message_bus()
    if memory is not None:
        def _mirror_to_memory(msg: dict) -> None:
            try:
                memory.remember(
                    source="bus",
                    title=f"{msg.get('from', '?')}->{msg.get('to', '*')} {msg.get('subject', '')[:120]}",
                    body=msg.get("body", "")[:1200],
                )
            except Exception:
                pass  # a broken memory mirror never breaks the bus

        server.bus.on_post(_mirror_to_memory)
    server.list_sessions = _list_sessions
    server.list_recent_sessions = _list_recent_sessions
    server.get_session_transcript = _get_session_transcript
    # v5.8: the artifact renderer store. Defaults to the process singleton
    # (tools publish into the same store the HUD reads); tests pass a fresh
    # one for isolation, exactly like bus/memory.
    if artifacts is None:
        from dourmouse.artifacts import default_store

        artifacts = default_store()
    server.artifacts = artifacts
    # v5.14 Phase R0: the cross-device state store (watchlists, alerts,
    # prefs, recent activity, per-device last workspace). The server is the
    # single source of truth — every device reads and writes this one store.
    # Tests pass a fresh store (in-memory) for isolation, like bus/artifacts.
    if state is None:
        from dourmouse.state_store import default_store as default_state_store

        state = default_state_store()
    server.state = state
    # v5.15: Google OAuth login — per-user identity + sessions. Tests pass a
    # fresh in-memory AuthStore; the serving path persists under workspace/auth.
    if auth is None:
        from dourmouse.google_auth import default_auth_store

        auth = default_auth_store()
    server.auth = auth
    # v5.15: bind the store so the agent tools resolve per-user tokens from
    # the REAL mounted store (not a throwaway).
    from dourmouse.google_auth import bind_auth_store

    bind_auth_store(auth)
    server.oauth_pending: dict[str, dict[str, Any]] = {}
    server.oauth_lock = threading.Lock()
    # v5.22.11: system-browser sign-in bridge. Google refuses sign-in inside
    # embedded WebKit webviews ("this browser or app may not be secure"), so
    # the desktop app opens the consent page in the user's REAL browser; the
    # completed session is parked here under a single-use claim code and the
    # app adopts it via GET /api/auth/claim. Same TTL discipline as
    # oauth_pending (pruned together).
    server.claim_pending: dict[str, dict[str, Any]] = {}
    server.claim_lock = threading.Lock()
    server.confirm_resolver = None  # set per-chat via gate resolver closure
    server.session = ChatSession(
        registry,
        session_file=Path(session_file) if session_file is not None else None,
        client=client,
        config=config,
        job_tracker=server.jobs,
        rbac=rbac,
        memory=memory,
    )
    server.session_lock = threading.Lock()
    server.gate = WebConfirmationGate(lambda _e: None)  # shared; emit swapped per request
    server.daemon_threads = True
    # v2.8: always-on live agent loops (news/markets/mail/tasks/rnd). The
    # runtime emits into the SAME tracker the agent windows poll, so live
    # activity is visible immediately. Env-gated: DOURMOUSE_LIVE=0 disables.
    server.live_runtime: LiveRuntime | None = None
    if live_polling and live_enabled():
        server.live_runtime = LiveRuntime(registry, server.tracker, bus=server.bus)
        server.live_runtime.start()
    # v5.x: user-defined recurring workflows ("do this every Monday"). The
    # runner executes workspace/schedules.jsonl entries when they come due.
    from dourmouse.schedules import SchedulerRunner

    server.scheduler_runner: SchedulerRunner | None = None
    if live_polling and live_enabled():
        server.scheduler_runner = SchedulerRunner(registry, server.tracker, bus=server.bus)
        server.scheduler_runner.start()
    # world-monitor-expansion (UX pass item 5): warm server-side caches for
    # COMMS (Gmail inbox listing) and WORLD (world_pulse), started at BOOT
    # rather than lazily on first screen visit, so both feel instant by the
    # time a user opens the console. Gated on BOTH ``reporting`` (the flag
    # start_health_warmer() and the other real-serving-path-only threads
    # use, just below) AND live_polling/live_enabled() (the same gate
    # LiveRuntime/SchedulerRunner use just above) — neither alone is
    # narrow enough: test_live_runtime.py calls run_server(live_polling=
    # True) with reporting at its False default (live_polling alone would
    # have opened a REAL IMAP connection / hit live world_pulse sources in
    # that test — caught live: it left a real warmer thread running that
    # then starved a later test of its OWN, monkeypatched thread because
    # start_*_warmer() is idempotent), and test_reporter_wired_via_
    # run_server calls run_server(reporting=True) with live_polling at its
    # False default (reporting alone would have opened them there). Only
    # the real serving path (serve_forever) sets both True by default.
    if reporting and live_polling and live_enabled():
        start_world_pulse_warmer()
        start_gmail_inbox_warmer()
        # v13.6: same real-network-in-a-background-thread gate as the two
        # warmers above, for the same reason — GDELT ingestion opens real
        # sockets to data.gdeltproject.org and must never do so from a
        # test's default run_server() call.
        from dourmouse.gdelt_graph import start_gdelt_graph_poller

        start_gdelt_graph_poller()
    # v4.0: proactive daily briefing (automation engine). Env-gated by
    # DOURMOUSE_REPORT (default on); tests opt out via reporting=False.
    from dourmouse.report import DailyReporter

    # v5.6: neural orchestrator — the real serving path bootstraps the net
    # from session history in the background (never blocks startup). Tests
    # keep neuro=False so the suite stays hermetic.
    server.neuro = neuro
    # v5.9: Freebuff live-activity fan-out. The hub is ALWAYS present (so
    # GET /api/events works everywhere); the watcher that feeds it only runs
    # when freebuff_events=True — tests keep it off so nothing touches the
    # Freebuff app. The watcher emits into the hub, the hub pushes to every
    # connected HUD stream.
    server.events_broadcast = _SSEBroadcast()
    # v13.6: real push for the agent-swarm graph (Vision OS item 7's own
    # flagged gap — "the current implementation polls a snapshot every
    # 2s, not a genuine SSE event stream"). ActivityTracker now emits a
    # real "agent_activity" event on this SAME existing hub every time an
    # agent's status actually changes — no new endpoint, no new
    # infrastructure, just a new event type on the fan-out that already
    # serves /api/events. A poll-based consumer (the workspace.html Agent
    # Map, or a native shell that hasn't wired an SSE client yet) is
    # completely unaffected; this is additive.
    server.tracker.set_broadcast(server.events_broadcast.broadcast)
    # v5.22.9: All-Hands runs broadcast their progress on the SAME hub the
    # HUD and the dedicated window listen to (live per-brain cards).
    from dourmouse import all_hands

    all_hands.bind_events_hub(server.events_broadcast)
    # v5.22.14: the ATLAS strategy lab also broadcasts sync events on the same
    # hub — the HUD shows the leaderboard updating live without any refresh.
    from dourmouse import atlas_lab

    atlas_lab.bind_events_hub(server.events_broadcast)
    # Start the auto-sync loop at boot so strategies from the GitHub repo
    # (valerygordon200-byte/atlas-strategy-lab) flow in with zero user steps
    # even if nobody opens the ATLAS window. Gated behind ``reporting`` so
    # tests (which default to reporting=False) never touch the real git repo
    # or filesystem.
    if reporting:
        atlas_lab.start_auto_sync()
        # v8.16: the autonomous idea generator — same reporting gate as
        # auto-sync above, for the same reason (tests never want a
        # background LLM loop touching the real proposal store).
        from dourmouse import atlas_generator

        atlas_generator.start_idea_generator()
        # v5.32: keep the compute-node health probe warm. The fast lane reads
        # ONLY a cached probe (it never probes itself, by design), and the only
        # thing populating that cache was the /api/connections call inside the
        # World/Settings view renderers — which run once at page load, not on a
        # timer. With a 30s TTL the Dell therefore served just the chats sent in
        # the first 30 seconds after a page load and was silently unused after
        # that, despite answering ~3x faster than the local fast model. Gated
        # behind ``reporting`` like the other background threads so the test
        # suite never opens a socket.
        from dourmouse.remote_server import start_health_warmer

        start_health_warmer()
    server.freebuff_watcher = None
    if freebuff_events:
        from dourmouse.freebuff_events import FreebuffEventWatcher

        server.freebuff_watcher = FreebuffEventWatcher(
            server.events_broadcast.broadcast
        )
        server.freebuff_watcher.start()
    server.news_watcher = None
    if news_stream:
        from dourmouse.news_stream import NewsStreamWatcher

        watcher_kwargs: dict[str, Any] = {}
        if news_stream_poll_interval is not None:
            # Test seam only — the real serving path never sets this, so
            # production always gets NewsStreamWatcher's own real default.
            watcher_kwargs["poll_interval"] = news_stream_poll_interval
        server.news_watcher = NewsStreamWatcher(
            server.events_broadcast.broadcast, **watcher_kwargs
        )
        server.news_watcher.start()
    if neuro:
        try:
            from dourmouse.orch_net import orch_enabled

            if orch_enabled():
                threading.Thread(
                    target=_bootstrap_neuro, daemon=True, name="neuro-bootstrap"
                ).start()
        except Exception:  # noqa: BLE001, S110 - bootstrap must never block serving
            pass
    server.daily_reporter: DailyReporter | None = None
    if reporting:
        from dourmouse.report import schedule_brief_on_open

        server.daily_reporter = DailyReporter(registry, server.tracker, server.bus)
        server.daily_reporter.start()
        # v5.22.13: the daily briefing fires ~15s after the app opens (not
        # just at the scheduled time) — headlines, unread mail, market
        # movers and the ATLAS strategy report land on the feed immediately.
        # Env-gated by DOURMOUSE_BRIEF_ON_OPEN (default on).
        server.brief_on_open_thread = schedule_brief_on_open(server.daily_reporter)
    # v13.4: hands-free conversational loop — real user request ("a
    # conversational llm you can talk to without pressing buttons").
    # Self-gated by DOURMOUSE_HANDS_FREE (HandsFreeController.start()
    # itself checks it and refuses honestly, same pattern wakeword.py's
    # own env gate already uses) rather than a run_server() parameter, so
    # a plain env-var flip is enough to turn this on/off — no code
    # change, matching every other opt-in capability flag in this file.
    # Attempted unconditionally; a real failure (missing dependency, no
    # mic permission, DOURMOUSE_HANDS_FREE off) is reported honestly on
    # server.hands_free_status and never crashes the server itself.
    server.hands_free: Any = None
    server.hands_free_status = {"enabled": False, "reason": "not attempted"}
    try:
        from dourmouse.hands_free import HandsFreeController, hands_free_enabled

        if hands_free_enabled():
            def _hands_free_dispatch(heard_text: str) -> str:
                # The SAME real dispatch path a typed/voice-button message
                # already goes through (_handle_chat_authed's own
                # session.ask() call, same session_lock) — never a second,
                # competing implementation. voice=True shapes the reply
                # for being SPOKEN (dispatch.py's own voice/text response
                # split — short, no markdown structure), screen="HANDS_FREE"
                # so a session-restore can tell this thread apart from the
                # typed HOME conversation.
                with server.session_lock:
                    server.gate.set_emit(lambda _e: None)
                    server.session.confirmation_gate = server.gate
                    server.confirm_resolver = server.gate.resolve
                    try:
                        report = server.session.ask(
                            heard_text, max_turns=8, voice=True, screen="HANDS_FREE",
                        )
                    finally:
                        server.confirm_resolver = None
                return report.get("final_text") or "I didn't get a reply for that."

            server.hands_free = HandsFreeController(dispatch_fn=_hands_free_dispatch)
            ok, reason = server.hands_free.start()
            server.hands_free_status = {"enabled": ok, "reason": reason}
        else:
            server.hands_free_status = {
                "enabled": False,
                "reason": "DOURMOUSE_HANDS_FREE is off — set DOURMOUSE_HANDS_FREE=1 to enable.",
            }
    except Exception as exc:  # noqa: BLE001 - a hands-free startup failure must never take down the server
        server.hands_free_status = {"enabled": False, "reason": f"startup failed: {exc}"}
    return server


def serve_forever(
    registry: DispatchRegistry,
    *,
    host: str | None = None,
    port: int = _DEFAULT_PORT,
    client: Any | None = None,
    config: NvidiaConfig | None = None,
    live_polling: bool = True,
    memory: MemoryStore | None = None,
    reporting: bool = True,
) -> None:
    """Blocking entry point: run the UI server until Ctrl+C.

    ``live_polling`` defaults ON here (the real serving path) so the always-
    on agent loops run in production; run_server keeps it off by default for
    hermetic tests. DOURMOUSE_LIVE=0 still disables it (see live_runtime).

    ``memory`` (v2.9): when None, the default long-term store is opened so
    the Store & Learn loop runs in the real serving path (DOURMOUSE_LEARN=0 or
    a missing FTS5 build honestly disables it — see learn.open_default_store).

    ``host`` (v4.0): None resolves DOURMOUSE_HOST env (default 127.0.0.1) so
    the app binds where the operator says, with the auth warning above.
    """
    from dourmouse.config import bind_host

    if host is None:
        host = bind_host()
    if memory is None:
        memory = open_default_store()
    # v3.1: resolve the config so server.config is set and per-agent models
    # (DOURMOUSE_MODEL_<AGENT>) actually apply in the real app (reviewer-caught:
    # passing None left the focus_agent override and roster labels inert).
    config = _resolve_server_config(config)
    server = run_server(
        registry,
        host=host,
        port=port,
        client=client,
        config=config,
        live_polling=live_polling,
        memory=memory,
        reporting=reporting,
        # v5.6: the real serving path learns — bootstrap from session history.
        neuro=True,
        # v5.9: the real serving path surfaces live Freebuff thread activity.
        freebuff_events=True,
        # v13: the real serving path runs the forever-refreshing news feed.
        news_stream=True,
    )
    print(f"Dourmouse UI running at http://{host}:{port}")
    print(f"Registry: {', '.join(sorted(registry.subagent_names))}")
    # A configured model that was never pulled fails as a bare "404 page not
    # found" on every request that routes to it, which reads as a network
    # fault rather than a missing model. Say it once, at startup, where it is
    # actionable — a silent mismatch cost a working fast lane for weeks.
    try:
        from dourmouse import config as _cfg
        from dourmouse.model_check import check_configured_models

        _report = check_configured_models(
            {
                "fast lane": _cfg.fast_lane_model(),
                "local": os.environ.get("OLLAMA_MODEL", "").strip(),
            }
        )
        if _report["missing"]:
            print(f"MODELS: {_report['detail']}")
            for _label, _name in _report["missing"].items():
                print(f"  fix: ollama pull {_name}   ({_label})")
    except Exception:  # noqa: BLE001 - a diagnostic must never block startup
        pass
    if server.live_runtime is not None:
        print(f"Live polling: {server.live_runtime.poll_count} scheduled poll loop(s) running")
    if server.daily_reporter is not None and server.daily_reporter.running:
        print("Daily report: scheduled (DOURMOUSE_REPORT_TIME)")
    if server.memory is not None and learn_enabled():
        # v13.5 (live-reproduced, real crash — "it didn't load properly"):
        # server.memory can be a RemoteMemoryStore (DOURMOUSE_MEMORY_REMOTE_URL
        # set, see general_roster._open_memory_store's own remote-first
        # check) whose .count() genuinely RAISES RemoteMemoryStoreUnavailable
        # when the remote machine is unreachable — unlike the local
        # MemoryStore.count(), which never raises. This one line had no
        # try/except while the diagnostic block right above it does (same
        # "a diagnostic must never block startup" rule) — a single dead
        # remote RAG host at boot time took down the ENTIRE server before
        # it ever started listening. Confirmed live: a fresh process died
        # here with RemoteMemoryStoreUnavailable the moment the configured
        # remote host (this session's own desktop RAG migration) was
        # slow/unreachable at startup.
        #
        # v13.5: also bounded to a real short wait (2s), same reasoning as
        # build_setup_status's own memory-count fix right above this
        # function — a slow (not just dead) remote host still has a real
        # 15s timeout to pay before this try/except catches it, and this
        # banner print runs BEFORE the server's accept loop starts, so a
        # slow remote host was making every real client connection attempt
        # during that window fail outright, not just "load slowly".
        _mem_result: dict[str, Any] = {}

        def _startup_mem_count() -> None:
            try:
                _mem_result["count"] = server.memory.count()
            except Exception as exc:  # noqa: BLE001 - a diagnostic must never block startup
                _mem_result["error"] = exc

        _mem_thread = threading.Thread(target=_startup_mem_count, daemon=True)
        _mem_thread.start()
        _mem_thread.join(timeout=2.0)
        if "count" in _mem_result:
            print(f"Store & Learn: {_mem_result['count']} fact(s) in long-term memory")
        elif "error" in _mem_result:
            print(f"Store & Learn: fact count unavailable ({_mem_result['error']})")
        else:
            print("Store & Learn: fact count still loading (remote store slow to answer) — continuing startup")
    try:
        from dourmouse.orch_net import status as _neuro_status

        ns = _neuro_status()
        print(
            f"Neural orchestrator: "
            f"{'ACTIVE (blending)' if ns['active'] else 'learning' if ns['enabled'] else 'off'} — "
            f"{ns['experience_count']} experience(s)"
        )
    except Exception:  # noqa: BLE001, S110 - a broken neuro import never blocks serving
        print("Neural orchestrator: off")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server.live_runtime is not None:
            server.live_runtime.stop()
        if server.daily_reporter is not None:
            server.daily_reporter.stop()
        if server.freebuff_watcher is not None:
            server.freebuff_watcher.stop()
        if server.news_watcher is not None:
            server.news_watcher.stop()
        if server.memory is not None:
            server.memory.close()
        server.server_close()


if __name__ == "__main__":
    from dourmouse.general_roster import build_general_registry

    serve_forever(build_general_registry())
