"""DOURMOUSE SERVER — the Dell compute node (v1.1.0).

The Dell is NOT DOURMOUSE. The Dell is compute infrastructure for DOURMOUSE:
a dedicated inference node that runs Qwen3 1.7B on the local Ollama and
serves it over a tiny, stable LAN API. All state, memory, integrations and
the orchestrator stay on the MAIN computer.

Endpoints (LAN-only — do NOT port-forward or expose publicly):
  GET  /status        -> legacy status (backwards compatible with v1.0.0)
  GET  /v1/status     -> {status, node, model, ollama, version, latency_ms}
  POST /v1/generate   -> {prompt, system?, temperature?, max_tokens?}
                         => {success, node, model, response, latency_ms}
  POST /v1/chat       -> {messages: [...], temperature?}
                         => {success, node, model, response, latency_ms}

Optional auth: set DOURMOUSE_SERVER_API_KEY, then send
  X-API-Key: <key>   (or  Authorization: Bearer <key>)

Run:
  uvicorn dell_server:app --host 0.0.0.0 --port 8000
  (or:  python dell_server.py   — same thing, no reload for stability)

Env (all optional):
  DOURMOUSE_SERVER_MODEL  model served to Ollama (default qwen3:1.7b)
  OLLAMA_URL              Ollama root (default http://127.0.0.1:11434)
  DOURMOUSE_SERVER_API_KEY  optional bearer key for the /v1 endpoints
  DOURMOUSE_SERVER_PORT    port (default 8000)

Logging: requests, model, latency and errors are logged; request BODIES,
API keys and credentials are NEVER logged. Low-RAM, low-background-CPU:
no reload, no workers, keep-alive off, no background tasks.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

try:
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover - startup guard, not a code path
    raise SystemExit(
        "FastAPI is required:  pip install fastapi uvicorn\n"
        "(already installed on this Dell per the setup)."
    )

VERSION = "1.1.0"
NODE = "Node-01"
DEFAULT_MODEL = "qwen3:1.7b"
DEFAULT_OLLAMA_ROOT = "http://127.0.0.1:11434"

_LOG = logging.getLogger("dourmouse-server")
_LOG.setLevel(logging.INFO)
_HANDLER = logging.StreamHandler()
_HANDLER.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
_LOG.addHandler(_HANDLER)

app = FastAPI(title="DOURMOUSE SERVER (Dell compute node)", version=VERSION)


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


def model_name() -> str:
    return _env("DOURMOUSE_SERVER_MODEL", "OLLAMA_MODEL", default=DEFAULT_MODEL)


def ollama_root() -> str:
    return _env("OLLAMA_URL", default=DEFAULT_OLLAMA_ROOT).rstrip("/")


#: Injectable for hermetic tests (a fake post fn replaces the network).
_ollama_post: Any = None


def _post(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
    """POST JSON to Ollama; returns (status, parsed_json). Raises on failure."""
    if _ollama_post is not None:
        return _ollama_post(url, payload, timeout)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (LAN Ollama)
        body = resp.read()
    try:
        return resp.status, json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Ollama returned non-JSON: {exc}") from exc


def _get(url: str, timeout: float) -> tuple[int, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (LAN Ollama)
        body = resp.read()
    try:
        return resp.status, json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Ollama returned non-JSON: {exc}") from exc


def ollama_available(timeout: float = 2.0) -> bool:
    """True when the local Ollama answers /api/tags. Never raises."""
    try:
        status, _ = _get(f"{ollama_root()}/api/tags", timeout=timeout)
        return status == 200
    except Exception:  # noqa: BLE001 - probe must never raise
        return False


def _ollama_chat(messages: list[dict[str, Any]], temperature: float | None, timeout: float = 120.0) -> str:
    """One Ollama /api/chat call, returns the assistant text."""
    payload: dict[str, Any] = {
        "model": model_name(),
        "messages": messages,
        "stream": False,
        "options": {},
    }
    if temperature is not None:
        payload["options"]["temperature"] = max(0.0, min(2.0, float(temperature)))
    try:
        status, data = _post(f"{ollama_root()}/api/chat", payload, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = _safe(exc.read(200).decode("utf-8", "replace"))
        raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001 - connection errors, readable
        raise RuntimeError(f"Ollama unreachable: {type(exc).__name__}: {exc}") from exc
    if status != 200:
        raise RuntimeError(f"Ollama returned HTTP {status}.")
    msg = data.get("message") or {}
    text = msg.get("content") or ""
    if not text:
        raise RuntimeError("Ollama returned an empty response.")
    return text


def _safe(text: str) -> str:
    """Bound a snippet for logging/errors (never full bodies)."""
    return text[:400]


def _check_auth(x_api_key: str | None, authorization: str | None) -> None:
    """Optional bearer auth. Key set => requests without it are 401."""
    key = _env("DOURMOUSE_SERVER_API_KEY")
    if not key:
        return
    supplied = (x_api_key or "").strip()
    if not supplied and authorization:
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
    if supplied != key:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def _result(success: bool, **extra: Any) -> dict[str, Any]:
    out = {"success": success, "node": NODE, "model": model_name()}
    out.update(extra)
    return out


@app.get("/status")
def legacy_status() -> dict[str, Any]:
    """Backwards-compatible status (v1.0.0 shape + a little more)."""
    return {
        "system": "DOURMOUSE",
        "status": "online",
        "version": VERSION,
        "node": NODE,
        "model": model_name(),
        "memory": True,
        "tools": True,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    }


@app.get("/v1/status")
def v1_status(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(x_api_key, authorization)
    started = time.perf_counter()
    ok = ollama_available()
    latency_ms = int((time.perf_counter() - started) * 1000)
    return {
        "status": "online",
        "node": NODE,
        "model": model_name(),
        "ollama": ok,
        "version": VERSION,
        "latency_ms": latency_ms,
    }


@app.post("/v1/generate")
async def v1_generate(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Generate one completion from a plain prompt (+ optional system)."""
    _check_auth(x_api_key, authorization)
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(_result(False, error="request body must be JSON"), status_code=400)
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse(_result(False, error="'prompt' is required"), status_code=400)
    system = str(body.get("system") or "").strip()
    temperature = body.get("temperature")
    max_tokens = body.get("max_tokens")
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system[:32_000]})
    messages.append({"role": "user", "content": prompt[:64_000]})
    started = time.perf_counter()
    try:
        text = _ollama_chat(messages, temperature)
    except Exception as exc:  # noqa: BLE001 - surfaced honestly in the JSON
        _LOG.error("generate failed: %s", _safe(str(exc)))
        return JSONResponse(
            _result(False, error="generate failed", detail=_safe(str(exc))),
            status_code=502,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    _LOG.info("generate ok model=%s chars=%d latency_ms=%d", model_name(), len(text), latency_ms)
    return JSONResponse(
        _result(True, response=text, latency_ms=latency_ms, max_tokens=max_tokens)
    )


@app.post("/v1/chat")
async def v1_chat(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Chat: standard OpenAI-format {messages:[{role,content}...]}."""
    _check_auth(x_api_key, authorization)
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(_result(False, error="request body must be JSON"), status_code=400)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse(_result(False, error="'messages' list is required"), status_code=400)
    cleaned: list[dict[str, Any]] = []
    for m in messages[:64]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        content = str(m.get("content") or "").strip()
        if role in ("system", "user", "assistant") and content:
            cleaned.append({"role": role, "content": content[:64_000]})
    if not cleaned:
        return JSONResponse(_result(False, error="no valid messages"), status_code=400)
    temperature = body.get("temperature")
    started = time.perf_counter()
    try:
        text = _ollama_chat(cleaned, temperature)
    except Exception as exc:  # noqa: BLE001 - surfaced honestly in the JSON
        _LOG.error("chat failed: %s", _safe(str(exc)))
        return JSONResponse(
            _result(False, error="chat failed", detail=_safe(str(exc))),
            status_code=502,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    _LOG.info("chat ok model=%s turns=%d latency_ms=%d", model_name(), len(cleaned), latency_ms)
    return JSONResponse(_result(True, response=text, latency_ms=latency_ms))


if __name__ == "__main__":
    import uvicorn

    port = int(_env("DOURMOUSE_SERVER_PORT", default="8000"))
    _LOG.info(
        "DOURMOUSE SERVER %s starting on 0.0.0.0:%d (model=%s, ollama=%s) — "
        "LAN ONLY, do not expose publicly.",
        VERSION,
        port,
        model_name(),
        ollama_root(),
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
