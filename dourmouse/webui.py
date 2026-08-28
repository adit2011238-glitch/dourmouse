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
from dourmouse.memory_store import MemoryStore
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

    def on_event(self, entry: dict[str, Any]) -> None:
        """Observer hook — swallow everything so dispatch never breaks."""
        try:
            self._record(entry)
        except Exception:
            pass

    def _agent_for(self, entry: dict[str, Any]) -> str | None:
        return self._tool_to_agent.get(entry.get("name", ""))

    def _record(self, entry: dict[str, Any]) -> None:
        etype = entry.get("type")
        with self._lock:
            if etype == "tool_use":
                agent = self._agent_for(entry)
                if agent is None:
                    return
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
                    return
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
                    return
                # A poll must never clobber a mid-chat computing/auth state:
                # only idle/live agents return to their always-on LIVE status.
                if self._status[agent] not in ("idle", "live"):
                    return
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


class _SSEStream:
    """Wraps a response for Server-Sent-Events writes (thread-safe)."""

    def __init__(self, wfile) -> None:
        self._wfile = wfile
        self._lock = threading.Lock()

    def emit(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, default=str)
        with self._lock:
            try:
                self._wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self._wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # client went away; loop continues harmlessly


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


def build_setup_status(server) -> dict[str, Any]:
    """v5.0: honest capability checklist for the SETUP panel (Rule 2.2).

    Every entry reports configured True/False + a one-line fix. Never
    fabricates a capability: a missing key/CLI/model is NOT CONFIGURED.
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
    if mem_ok:
        try:
            mem_count = mem.count()
        except Exception:  # noqa: BLE001 -- a broken store must not kill setup
            mem_count = 0
    items["memory"] = {
        "configured": mem_ok,
        "detail": f"{mem_count} facts" if mem_ok else "off",
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
        elif path in ("/atlas-lab", "/atlas-lab.html"):
            # v5.22.6: the dedicated ATLAS window — a second DOURMOUSE that
            # is ONLY the strategy lab (live GitHub-synced leaderboard).
            self._serve_static("atlas_lab.html")
        elif path in ("/all-hands", "/all-hands.html"):
            # v5.22.9: the dedicated ALL HANDS window — one goal, every
            # resource (Claude/Codex/ChatGPT/DeepSeek/web) in parallel.
            self._serve_static("all_hands.html")
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
        elif path == "/api/jobs":
            self._send_json(
                {"jobs": self.server.jobs.snapshot(), "count": self.server.jobs.count()}
            )
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
        elif parsed.path == "/api/role":
            self._handle_role()
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
        if focus_agent:
            prompt = (
                f"[ROUTING DIRECTIVE] Complete this task using ONLY the "
                f"'{focus_agent}' subagent and its tools; do not use any "
                f"other subagent's tools. TASK: {prompt}"
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
        exists = has_profile(self.server.memory)
        profile = None
        if exists:
            fact = self.server.memory.get(PROFILE_SOURCE, PROFILE_TITLE)
            profile = fact["body"] if fact else None
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
    state=None,
    auth=None,
) -> ThreadingHTTPServer:
    """Start the UI server. Returns the running ThreadingHTTPServer.

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
        print(f"Store & Learn: {server.memory.count()} fact(s) in long-term memory")
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
        if server.memory is not None:
            server.memory.close()
        server.server_close()


if __name__ == "__main__":
    from dourmouse.general_roster import build_general_registry

    serve_forever(build_general_registry())
