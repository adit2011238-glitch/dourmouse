"""DourmouseServerClient — the Dell compute-node client (v5.26).

The Dell is compute infrastructure, not DOURMOUSE. This module is the ONLY
thing on the main computer that talks to it:

    MAIN DOURMOUSE ── DourmouseServerClient ──> http://<Dell>/v1/* ──> Ollama

Guarantees (Rule 2.1 / 2.2 — a dead node must never break the main app):

- ``status()`` / ``generate()`` / ``chat()`` NEVER raise on a dead node:
  they return an honest ``{"success": False, "error": ...}`` result. The
  only exceptions are programmer errors (bad argument types).
- ``generate_with_fallback()`` is the failover seam: tries the Dell, and on
  ANY failure transparently calls the local fallback callable. It never
  raises and never reports a fabricated success.
- The health probe is cheap (1.5 s timeout), cached for 30 s, and isolated
  — a slow/offline Dell costs one short TCP timeout per TTL window.
- All HTTP is stdlib ``urllib`` — the client adds zero new dependencies.

Config (env, all optional — never hard-coded per site):
  DOURMOUSE_SERVER_URL      base URL, default http://192.168.1.108:8000
  DOURMOUSE_SERVER_API_KEY  optional bearer key sent as X-API-Key
  DOURMOUSE_SERVER_MODEL    model name (default qwen3:1.7b — informational,
                            the Dell decides what it serves)
  DOURMOUSE_SERVER_TIMEOUT  per-request read timeout seconds (default 90)
  DOURMOUSE_SERVER_HEALTH_TTL  health cache seconds (default 30)
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DEFAULT_SERVER_URL = "http://192.168.1.108:8000"
DEFAULT_SERVER_MODEL = "qwen3:1.7b"
DEFAULT_TIMEOUT = 90.0
DEFAULT_HEALTH_TTL = 30.0
# Status probe budget: generous enough for a real LAN node on WiFi, short
# enough that an absent node costs almost nothing per TTL window.
_CONNECT_TIMEOUT = 2.0

_health_cache: dict[str, Any] = {
    "at": 0.0,
    "online": False,
    "payload": None,
}
_health_lock = threading.Lock()


def server_url() -> str:
    raw = os.environ.get("DOURMOUSE_SERVER_URL", "").strip()
    return raw.rstrip("/") or DEFAULT_SERVER_URL


def server_url_configured() -> bool:
    """True only when the operator EXPLICITLY set DOURMOUSE_SERVER_URL.

    The fast lane must never probe the DEFAULT address on a machine that
    never opted into the Dell: a silent 2s connect-timeout on every reply
    would be a speed regression, not an improvement. Engaged routing is
    opt-in via the env var (v5.30).
    """
    return bool(os.environ.get("DOURMOUSE_SERVER_URL", "").strip())


def server_online_cached() -> bool:
    """Online check that NEVER probes — reads only a FRESH cached probe.

    Used by the fast lane so a pure-chat reply pays zero latency when the
    node is down: if the last probe is stale or the node was offline, this
    returns False instantly and the local fast model answers. The UI's
    /api/connections poll keeps the cache warm; a genuinely dead Dell just
    means the lane stays local.
    """
    now = time.monotonic()
    with _health_lock:
        if (now - _health_cache["at"]) < server_health_ttl():
            return bool(_health_cache["online"])
    return False


def server_api_key() -> str:
    return os.environ.get("DOURMOUSE_SERVER_API_KEY", "").strip()


def server_model() -> str:
    return os.environ.get("DOURMOUSE_SERVER_MODEL", "").strip() or DEFAULT_SERVER_MODEL


def server_timeout() -> float:
    try:
        return float(os.environ.get("DOURMOUSE_SERVER_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


def server_health_ttl() -> float:
    try:
        return float(os.environ.get("DOURMOUSE_SERVER_HEALTH_TTL", str(DEFAULT_HEALTH_TTL)))
    except ValueError:
        return DEFAULT_HEALTH_TTL


def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = _CONNECT_TIMEOUT,
) -> tuple[int, dict[str, Any]]:
    """One HTTP call to the Dell. Returns (status, json). Raises on failure."""
    url = server_url() + path
    headers = {"Accept": "application/json"}
    key = server_api_key()
    if key:
        headers["X-API-Key"] = key
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - LAN compute node
            raw = resp.read(1_000_000)
    except urllib.error.HTTPError as exc:
        # A reachable server that refused us (401/4xx/5xx) is NOT "unreachable"
        # — surface the real status so callers give an honest reason.
        try:
            parsed = json.loads(exc.read(1_000_000))
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        return exc.code, parsed
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"non-JSON response from {url}: {exc}") from exc
    return resp.status, parsed


def server_available(force: bool = False) -> bool:
    """Cached probe: is the Dell answering /v1/status (or legacy /status)?

    Never raises, never takes longer than ~1.5 s per TTL window. ``force``
    bypasses the cache (used right before an actual offload).
    """
    now = time.monotonic()
    with _health_lock:
        if not force and (now - _health_cache["at"]) < server_health_ttl():
            return bool(_health_cache["online"])
    online = False
    payload: dict[str, Any] | None = None
    try:
        status, payload = _request("GET", "/v1/status", timeout=_CONNECT_TIMEOUT)
        online = status == 200 and isinstance(payload, dict) and payload.get("status") == "online"
    except Exception:  # noqa: BLE001 - probe must never raise
        online = False
    with _health_lock:
        _health_cache["at"] = now
        _health_cache["online"] = online
        _health_cache["payload"] = payload if online else None
    return online


def server_status() -> dict[str, Any]:
    """Full, honest status dict for the UI / connections panel. Never raises.

    Fast-offline path: when the node is KNOWN offline and the probe cache is
    still fresh, the result returns instantly without re-probing — so a dead
    Dell costs one short probe per TTL window, never per UI poll.
    """
    base = {
        "configured": bool(os.environ.get("DOURMOUSE_SERVER_URL", "").strip()),
        "url": server_url(),
        "online": False,
        "node": None,
        "model": server_model(),
        "version": None,
        "ollama": None,
        "latency_ms": None,
        "error": None,
    }
    now = time.monotonic()
    with _health_lock:
        fresh_offline = (
            (now - _health_cache["at"]) < server_health_ttl()
            and not _health_cache["online"]
        )
        if fresh_offline:
            base["error"] = "compute node offline (last probe)"
            return base
    started = time.perf_counter()
    try:
        status, payload = _request("GET", "/v1/status", timeout=_CONNECT_TIMEOUT)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if status == 200 and isinstance(payload, dict) and payload.get("status") == "online":
            base.update(
                online=True,
                node=payload.get("node"),
                model=payload.get("model") or base["model"],
                version=payload.get("version"),
                ollama=bool(payload.get("ollama")),
                latency_ms=latency_ms,
            )
            with _health_lock:
                _health_cache.update(at=now, online=True, payload=payload)
        else:
            base["error"] = f"server returned HTTP {status}"
            with _health_lock:
                _health_cache.update(at=now, online=False, payload=None)
    except Exception as exc:  # noqa: BLE001 - status must never raise
        base["error"] = f"{type(exc).__name__}: {exc}"
        with _health_lock:
            _health_cache.update(at=now, online=False, payload=None)
    return base


class ServerUnavailable(RuntimeError):
    """Raised ONLY by the OpenAI-shaped surface (callers opt in)."""


class DourmouseServerClient:
    """The Dell client. ``generate`` / ``chat`` never raise on a dead node.

    ``status()`` -> dict. ``generate()`` / ``chat()`` -> dict with
    ``success`` (True/False) — check it. ``chat.completions.create(...)``
    exists for OpenAI-shaped call sites and RAISES ``ServerUnavailable`` on
    failure (that is the opt-in contract).
    """

    def __init__(self, timeout: float | None = None) -> None:
        self._timeout = timeout if timeout is not None else server_timeout()

    # ---- plain dict surfaces (never raise) ------------------------------- #
    def status(self) -> dict[str, Any]:
        return server_status()

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        if not prompt or not str(prompt).strip():
            return {"success": False, "error": "generate requires a prompt."}
        payload: dict[str, Any] = {"prompt": str(prompt)}
        if system:
            payload["system"] = str(system)
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            status, body = _request("POST", "/v1/generate", payload, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - honest failure, never raise
            return {
                "success": False,
                "error": f"server unreachable: {type(exc).__name__}",
                "detail": str(exc)[:300],
            }
        return _bundle(status, body, self._timeout)

    def chat(self, messages: list[dict[str, Any]], temperature: float | None = None) -> dict[str, Any]:
        if not isinstance(messages, list) or not messages:
            return {"success": False, "error": "chat requires a non-empty messages list."}
        payload: dict[str, Any] = {"messages": messages[:64]}
        if temperature is not None:
            payload["temperature"] = temperature
        try:
            status, body = _request("POST", "/v1/chat", payload, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - honest failure, never raise
            return {
                "success": False,
                "error": f"server unreachable: {type(exc).__name__}",
                "detail": str(exc)[:300],
            }
        return _bundle(status, body, self._timeout)

    # ---- OpenAI-shaped surface (raises ServerUnavailable — opt-in) ------- #
    def chat_completions_create(
        self,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        **_: Any,
    ) -> Any:
        result = self.chat(messages or [], temperature=temperature)
        if not result.get("success"):
            raise ServerUnavailable(result.get("error") or "server unavailable")
        return _OpenAIResponse(result["response"])


def _bundle(status: int, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Shape a Dell response into the client contract, validating it."""
    if status == 401:
        return {
            "success": False,
            "error": "server refused the request (401) — check DOURMOUSE_SERVER_API_KEY.",
        }
    if status != 200:
        detail = (body or {}).get("detail") or (body or {}).get("error") or f"HTTP {status}"
        return {"success": False, "error": str(detail)[:300]}
    if not isinstance(body, dict):
        return {"success": False, "error": "server returned malformed JSON."}
    response = body.get("response")
    if not isinstance(response, str) or not response.strip():
        return {"success": False, "error": "server returned an empty response."}
    return {
        "success": True,
        "response": response,
        "model": body.get("model"),
        "node": body.get("node"),
        "latency_ms": body.get("latency_ms"),
    }


class _OpenAIResponse:
    """Minimal OpenAI-shaped completion for slotting into existing call sites."""

    def __init__(self, text: str) -> None:
        self.choices = [_OpenAIChoice(text)]


class _OpenAIChoice:
    def __init__(self, text: str) -> None:
        self.message = _OpenAIMessage(text)


class _OpenAIMessage:
    def __init__(self, text: str) -> None:
        self.content = text
        self.tool_calls = None


def generate_with_fallback(
    prompt: str,
    local_fallback: Callable[[str], str],
    system: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """The failover seam: Dell first, local on ANY failure. Never raises.

    Returns ``{"success": True, "response": ..., "via": "server"|"local",
    "latency_ms": ...}`` or ``{"success": False, "error": ...}`` only when
    BOTH paths fail (a truly broken local backend — the orchestrator must
    never fabricate a result).
    """
    client = DourmouseServerClient()
    started = time.perf_counter()
    try:
        result = client.generate(prompt, system=system, temperature=temperature)
    except Exception:  # noqa: BLE001 - any client bug falls back too
        result = {"success": False, "error": "client error"}
    if result.get("success"):
        return {
            "success": True,
            "response": result["response"],
            "via": "server",
            "node": result.get("node"),
            "model": result.get("model"),
            "latency_ms": result.get("latency_ms"),
        }
    try:
        local_text = local_fallback(prompt)
    except Exception as exc:  # noqa: BLE001 - honest double failure
        return {
            "success": False,
            "error": f"server unavailable AND local fallback failed: {exc}",
            "server_error": result.get("error"),
        }
    return {
        "success": True,
        "response": local_text,
        "via": "local",
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def local_ollama_fallback(prompt: str, system: str | None = None) -> str:
    """The default local fallback: one native Ollama call on this machine.

    Uses the SAME native client the dispatch loop uses (OllamaNativeClient),
    so the fallback is the real local AI provider — never a stub.
    """
    from dourmouse.config import load_ollama_config
    from dourmouse.dispatch import OllamaNativeClient

    cfg = load_ollama_config()
    client = OllamaNativeClient(cfg)
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = client.chat.completions.create(model=cfg.model_for_agent("ORCHESTRATOR"), messages=messages, stream=False)
    text = getattr(resp.choices[0].message, "content", "") or ""
    if not text:
        raise RuntimeError("local Ollama returned an empty response.")
    return text
