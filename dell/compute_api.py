"""DOURMOUSE COMPUTE API — /v1 compute endpoints for the EXISTING Dell FastAPI app.

This module is NOT a server. It is not a second DOURMOUSE installation, it
does not create a venv, and it does not touch the existing /status endpoint.

It is a small FastAPI router that the EXISTING DOURMOUSE FastAPI app already
running on the Dell (C:\\DOURMOUSE\\run.py / C:\\DOURMOUSE\\backend\\main.py)
includes — turning that existing installation into the DOURMOUSE COMPUTE
SERVER for the main computer:

  GET  /v1/status   -> honest probe of the EXISTING Ollama installation
  POST /v1/generate -> {prompt, system?, temperature?, max_tokens?}
                       -> EXISTING Ollama / qwen3:1.7b, real latency
  POST /v1/chat     -> {messages: [...], temperature?}
                       -> EXISTING Ollama / qwen3:1.7b, real latency

Every inference call goes to the Ollama that already runs on the Dell
(http://127.0.0.1:11434) with the model that is already installed
(qwen3:1.7b). Responses are never invented; latency is measured.

Wire it into the existing app with ONE line (exact import depends on the
existing layout — this module sits next to the existing backend code):

    from compute_api import router      # if flat next to run.py
    app.include_router(router)

The existing /status endpoint stays exactly as it is (backwards compatible).

Optional auth: set DOURMOUSE_SERVER_API_KEY in the Dell environment, then
requests must send  X-API-Key: <key>  or  Authorization: Bearer <key>.

Env (all optional):
  DOURMOUSE_SERVER_MODEL   model used for inference (default qwen3:1.7b)
  OLLAMA_URL               Ollama root (default http://127.0.0.1:11434)
  DOURMOUSE_SERVER_API_KEY optional bearer key for the /v1 endpoints

Request bodies, API keys and credentials are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

VERSION = "1.0.0"
NODE = "Node-01"
SERVER_NAME = "DOURMOUSE-COMPUTE"
DEFAULT_MODEL = "qwen3:1.7b"
DEFAULT_OLLAMA_ROOT = "http://127.0.0.1:11434"

router = APIRouter(tags=["compute"])

_LOG = logging.getLogger("dourmouse-compute")
_LOG.setLevel(logging.INFO)


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


#: Injectable seam for hermetic tests (a fake replaces the network).
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - LAN Ollama
        body = resp.read()
    try:
        return resp.status, json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Ollama returned non-JSON: {exc}") from exc


def _get(url: str, timeout: float) -> tuple[int, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - LAN Ollama
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


def _ollama_chat(
    messages: list[dict[str, Any]],
    temperature: float | None,
    num_predict: int | None = None,
    timeout: float = 120.0,
) -> str:
    """One Ollama /api/chat call; returns the assistant text."""
    payload: dict[str, Any] = {
        "model": model_name(),
        "messages": messages,
        "stream": False,
        "options": {},
    }
    if temperature is not None:
        payload["options"]["temperature"] = max(0.0, min(2.0, float(temperature)))
    if num_predict is not None:
        payload["options"]["num_predict"] = int(num_predict)
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


@router.get("/v1/status")
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
        "server": SERVER_NAME,
        "version": VERSION,
        "latency_ms": latency_ms,
    }


@router.post("/v1/generate")
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
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            return JSONResponse(_result(False, error="'max_tokens' must be an integer"), status_code=400)
        max_tokens = max(1, min(max_tokens, 8192))
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system[:32_000]})
    messages.append({"role": "user", "content": prompt[:64_000]})
    started = time.perf_counter()
    try:
        text = _ollama_chat(messages, temperature, num_predict=max_tokens)
    except Exception as exc:  # noqa: BLE001 - surfaced honestly in the JSON
        _LOG.error("generate failed: %s", _safe(str(exc)))
        return JSONResponse(
            _result(False, error="generate failed", detail=_safe(str(exc))),
            status_code=502,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    _LOG.info("generate ok model=%s chars=%d latency_ms=%d", model_name(), len(text), latency_ms)
    return JSONResponse(_result(True, response=text, latency_ms=latency_ms))


@router.post("/v1/chat")
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
