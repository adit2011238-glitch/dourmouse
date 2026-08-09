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
import re
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from dourmouse.chat import ChatSession
from dourmouse.config import NvidiaConfig, OllamaConfig, load_llm_config
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


def _resolve_server_config(config: NvidiaConfig | None) -> NvidiaConfig | None:
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
        return load_llm_config()
    except ValueError:
        return None


def _backend_label(config: Any | None) -> str:
    """The active backend name for the UI (v4.0): 'ollama' | 'nvidia'.

    Deterministic (Rule 2.8): Ollama config objects carry a keyless marker
    (empty api_key + localhost base) — resolved by type, never a guess.
    Returns 'default' honestly when no config is attached (tests).
    """
    if config is None:
        return "default"
    if isinstance(config, OllamaConfig):
        return "ollama"
    return "nvidia"


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
        "hint": "DOURMOUSE_LLM_BACKEND=ollama|nvidia in .env",
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
        return False

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

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        if not self._authorized():
            self._send_unauthorized()
            return
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
        elif path in ("/map", "/map.html"):
            self._serve_static("map.html")
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
            self._serve_static(path[len("/assets/"):])
        elif path == "/api/roster":
            self._send_json(
                build_roster_payload(self.server.registry, self.server.config)
            )
        elif path == "/api/links":
            self._send_json(build_link_topology(self.server.registry))
        elif path == "/api/activity":
            self._send_json(self.server.tracker.snapshot())
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
        elif path == "/api/memory":
            self._handle_memory_api()
        elif path == "/api/repo":
            # v4.1 (P6+): Project Memory — repo index status, last scan,
            # recent facts, and ?q= search (all scoped to source='repo').
            self._handle_repo_api()
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
        elif path == "/api/atlas":
            # v5.4: ATLAS quant-engine panel — real telemetry + last run.
            from dourmouse.atlas_cli import atlas_panel_snapshot

            self._send_json(atlas_panel_snapshot())
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
        if not self._authorized():
            self._send_unauthorized()
            return
        if parsed.path == "/api/chat":
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
        elif parsed.path == "/api/atlas/run":
            # v5.4: start one managed ATLAS command (single-flight).
            self._handle_atlas_run()
        elif parsed.path == "/api/neuro/train":
            # v5.6: force a background retrain of the neural orchestrator.
            self._handle_neuro_train()
        elif parsed.path == "/api/spotify/login":
            # v5.7: start the one-time Spotify account linking (background).
            from dourmouse.spotify_services import spotify_login

            message = spotify_login(background=True)
            self._send_json({"ok": True, "message": message})
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
        if not _UPLOAD_NAME_RE.match(name):
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
        ctype = "text/html" if rel.endswith(".html") else (
            "text/css" if rel.endswith(".css") else (
                "application/javascript" if rel.endswith(".js") else "application/octet-stream"
            )
        )
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_chat(self) -> None:
        body = self._read_json_body()
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            self._send_json({"error": "prompt is required"}, status=400)
            return
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
        with self.server.session_lock:
            gate.set_emit(stream.emit)
            session.confirmation_gate = gate
            self.server.confirm_resolver = gate.resolve
            try:
                report = session.ask(
                    prompt, max_turns=8, event_sink=sink, model=model_override
                )
            except Exception as exc:  # surface real failures to the UI
                error_msg = str(exc)
            finally:
                session.confirmation_gate = previous_gate
                self.server.confirm_resolver = None
                gate.set_emit(lambda _e: None)

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

    def _handle_confirm(self) -> None:
        body = self._read_json_body()
        confirm_id = body.get("id") or ""
        approved = bool(body.get("approved"))
        # The gate lives on the active chat request thread; hand it a shared
        # resolver so confirms can reach it.
        resolver = getattr(self.server, "confirm_resolver", None)
        if resolver is None:
            self._send_json({"ok": False, "error": "no active chat"}, status=409)
            return
        ok = resolver(confirm_id, approved)
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
        self._send_json({"ok": True, "command": command, "running": True})

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
                for line in f.read_text(errors="replace").splitlines():
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
                        1 for ln in f.read_text(errors="replace").splitlines() if ln.strip()
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
        print(
            "WARNING: DOURMOUSE_HOST is set to a non-loopback address but "
            "DOURMOUSE_ACCESS_TOKEN is NOT set — anyone who can reach this "
            "port can drive the dashboard. Set a token first."
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
    # v4.0: proactive daily briefing (automation engine). Env-gated by
    # DOURMOUSE_REPORT (default on); tests opt out via reporting=False.
    from dourmouse.report import DailyReporter

    # v5.6: neural orchestrator — the real serving path bootstraps the net
    # from session history in the background (never blocks startup). Tests
    # keep neuro=False so the suite stays hermetic.
    server.neuro = neuro
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
        server.daily_reporter = DailyReporter(registry, server.tracker, server.bus)
        server.daily_reporter.start()
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
    )
    print(f"Dourmouse UI running at http://{host}:{port}")
    print(f"Registry: {', '.join(sorted(registry.subagent_names))}")
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
        if server.memory is not None:
            server.memory.close()
        server.server_close()


if __name__ == "__main__":
    from dourmouse.general_roster import build_general_registry

    serve_forever(build_general_registry())
