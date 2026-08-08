"""Multi-backend coding dispatch (v2.4).

Three preloaded coding subagents — ``code_nvidia``, ``code_deepseek``,
``code_claude`` — each route a REAL coding task through a specific LLM
backend:

- ``nvidia`` — NVIDIA NIM (OpenAI-compatible), using the same
  ``NVIDIA_API_KEY`` / ``NVIDIA_BASE_URL`` / ``NVIDIA_MODEL`` the
  orchestrator itself runs on.
- ``deepseek`` — DeepSeek (OpenAI-compatible). Prefers the Freebuff free
  tier env (``FREEBUFF_DEEPSEEK_API_KEY`` / ``_BASE_URL`` / ``_MODEL``) and
  falls back to plain ``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE_URL`` /
  ``DEEPSEEK_MODEL``. With neither set, falls back to the user's
  ``NVIDIA_API_KEY`` (NVIDIA NIM hosts DeepSeek models — the user runs
  solo with only an NVIDIA key). Honestly NOT CONFIGURED only when no
  key of any kind is set.
- ``claude`` — the user's real Claude Code CLI (``claude -p``), the same
  honest subprocess pattern as the dev_coding ``claude_code`` tool.

Every path returns REAL output or an honest error (Rules 2.1 / 2.2): a
missing key/CLI is NOT CONFIGURED, an API/CLI failure surfaces the real
error, and no result is ever fabricated. Secrets come only from env vars
(Rule 2.6). The data path is deterministic code — the model generates, the
agent/orchestrator decides how to apply.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from dourmouse.config import (
    NVIDIA_DEFAULT_BASE_URL,
    load_nvidia_config,
    load_ollama_config,
)

_DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
# v5.1: the NVIDIA NIM DeepSeek model used when the user has an NVIDIA key
# but no DeepSeek key. Overridable via DEEPSEEK_NVIDIA_MODEL. Verified live
# on the user's key (integrate.api.nvidia.com/v1/models).
_DEEPSEEK_NVIDIA_MODEL = "deepseek-ai/deepseek-v4-flash-0731"
_OUTPUT_CAP = 6_000

_CODING_SYSTEM = (
    "You are a coding agent inside the Dourmouse dispatch system. "
    "Write correct, tested code for the task. Return only the code and a "
    "brief explanation. Never claim work was done that wasn't."
)


def load_backend(backend: str) -> tuple[str, str, str]:
    """Resolve (base_url, api_key, model) for an OpenAI-compatible backend.

    Raises RuntimeError('NOT CONFIGURED: ...') when the backend cannot be
    used — never falls back to a fabricated result.
    """
    name = (backend or "").strip().lower()
    if name == "nvidia":
        try:
            cfg = load_nvidia_config()
        except ValueError as exc:
            raise RuntimeError(f"NOT CONFIGURED: {exc}") from exc
        return cfg.base_url, cfg.api_key, cfg.model
    if name == "ollama":
        # v4.0 local backend: keyless, OpenAI-compatible, zero API spend.
        cfg = load_ollama_config()
        return cfg.base_url, cfg.api_key, cfg.model
    if name in ("deepseek", "freebuff", "free_deepseek"):
        key = os.environ.get("FREEBUFF_DEEPSEEK_API_KEY", "").strip()
        base = os.environ.get("FREEBUFF_DEEPSEEK_BASE_URL", "").strip()
        model = os.environ.get("FREEBUFF_DEEPSEEK_MODEL", "").strip()
        if not key:
            key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
            base = base or os.environ.get("DEEPSEEK_BASE_URL", "").strip()
            model = model or os.environ.get("DEEPSEEK_MODEL", "").strip()
        if not key:
            # v5.1: no DeepSeek-specific key, but the user DOES have an
            # NVIDIA key (NVIDIA NIM hosts DeepSeek models). Route the
            # deepseek backend through NVIDIA with a DeepSeek model id —
            # DEEPSEEK_NVIDIA_MODEL overrides the default.
            nvidia_key = os.environ.get("NVIDIA_API_KEY", "").strip()
            if nvidia_key:
                return (
                    os.environ.get("NVIDIA_BASE_URL", "").strip()
                    or NVIDIA_DEFAULT_BASE_URL,
                    nvidia_key,
                    os.environ.get("DEEPSEEK_NVIDIA_MODEL", "").strip()
                    or _DEEPSEEK_NVIDIA_MODEL,
                )
            raise RuntimeError(
                "NOT CONFIGURED: the DeepSeek coding backend needs "
                "FREEBUFF_DEEPSEEK_API_KEY (Freebuff free tier), "
                "DEEPSEEK_API_KEY, or NVIDIA_API_KEY (NVIDIA NIM hosts "
                "DeepSeek models) in .env. Nothing was run."
            )
        return base or _DEEPSEEK_DEFAULT_BASE_URL, key, model or _DEEPSEEK_DEFAULT_MODEL
    if name in ("codex", "openai_codex"):
        # v5.0: OpenAI Codex — OpenAI-compatible endpoint. Key from
        # CODEX_API_KEY (preferred) or OPENAI_API_KEY; model env-overridable.
        key = os.environ.get("CODEX_API_KEY", "").strip()
        if not key:
            key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "NOT CONFIGURED: the Codex coding backend needs CODEX_API_KEY "
                "(or OPENAI_API_KEY) in .env. Nothing was run."
            )
        base = os.environ.get(
            "CODEX_BASE_URL", "https://api.openai.com/v1"
        ).strip()
        model = os.environ.get("CODEX_MODEL", "gpt-5-codex").strip()
        return base, key, model
    raise RuntimeError(
        f"ERROR: unknown code backend {backend!r} — use 'ollama', 'nvidia', "
        "'deepseek', 'codex' or 'claude'."
    )


# -- injectable for tests (no network in the suite) ------------------------ #

def _openai_client_factory(base_url: str, api_key: str) -> Any:
    from openai import OpenAI

    # Keyless local backends (Ollama) carry an empty key by design; the SDK
    # rejects empty strings but Ollama ignores the value (reviewer-caught).
    return OpenAI(api_key=api_key or "local-keyless", base_url=base_url)


def _run_openai_compat(
    base_url: str,
    api_key: str,
    model: str,
    task: str,
    timeout: int,
) -> str:
    # Reference the module global at CALL time (never bind it as a default
    # parameter): tests monkeypatch the module attribute, and a def-time
    # default would capture the original and ignore the patch.
    client = _openai_client_factory(base_url, api_key)
    # Keyless => Ollama: thinking models consume the budget on reasoning before
    # content; disable for a direct answer. NVIDIA ignores the option.
    extra_body = {"enable_thinking": False} if not api_key else None
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _CODING_SYSTEM},
                {"role": "user", "content": task},
            ],
            timeout=timeout,
            max_tokens=4000,
            extra_body=extra_body,
        )
    except Exception as exc:  # openai raises many exception types
        raise RuntimeError(f"{model} API call failed: {exc}") from exc
    text = ""
    if resp and getattr(resp, "choices", None) and resp.choices:
        text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError(f"{model} returned an empty response (honest).")
    return text


def _run_claude(task: str, *, cwd: str | None, timeout: int) -> str:
    # Lazy import: code_backends must not import general_roster at module
    # load time (general_roster imports code_backends for the new tools).
    from dourmouse.general_roster import _find_claude_cli

    cli = _find_claude_cli()
    if cli is None:
        raise RuntimeError(
            "NOT CONFIGURED: the Claude Code CLI ('claude') was not found on "
            "PATH. Install it (npm i -g @anthropic-ai/claude-code) or set "
            "CLAUDE_CODE_CLI=/absolute/path/to/claude in .env. Nothing was run."
        )
    timeout = max(1, min(int(timeout), 600))
    try:
        proc = subprocess.run(
            [cli, "-p", task],
            cwd=cwd,
            stdin=subprocess.DEVNULL,  # claude -p waits ~3s on stdin otherwise
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude timed out after {timeout}s (task still running).") from None
    except OSError as exc:
        raise RuntimeError(f"could not run the claude CLI: {exc}") from exc
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {err[-2000:] or '(no stderr)'}")
    if not out:
        raise RuntimeError("claude returned no output (honest).")
    return out[-_OUTPUT_CAP:]


def run_code_task(
    backend: str,
    task: str,
    *,
    cwd: str | None = None,
    timeout: int = 120,
) -> str:
    """Run a coding task through the chosen backend; returns REAL output.

    Raises RuntimeError on any configuration or execution failure — the
    caller surfaces it honestly, never as fabricated code.
    """
    task = (task or "").strip()
    if not task:
        raise RuntimeError("run_code_task requires a non-empty 'task'.")
    name = (backend or "").strip().lower()
    if name == "claude":
        return _run_claude(task, cwd=cwd, timeout=timeout)
    base_url, api_key, model = load_backend(name)
    return _run_openai_compat(base_url, api_key, model, task, timeout)
