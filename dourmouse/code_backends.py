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
  honest subprocess pattern as the dev_coding ``claude_code`` tool. Real
  conversation continuity across repeated calls: the first call for a given
  ``cwd`` mints a session id (``--session-id``) and every later call for
  that same ``cwd`` resumes it (``--resume``) — see the "CODE-screen Claude
  CLI session continuity" block below for what was actually broken before
  this and how the fix was verified live.
- ``codex`` — the user's real Codex CLI (``codex exec``, their ChatGPT
  login), falling back to the OpenAI-compatible API when only a
  ``CODEX_API_KEY`` / ``OPENAI_API_KEY`` is available. CLI-first because
  that is exactly what the CODEX connection status probe measures.
- ``qwen`` / ``glm`` / ``kimi`` — cheap/free-tier Chinese-lab
  OpenAI-compatible backends (Alibaba Qwen, Zhipu GLM, Moonshot Kimi),
  resolved by ``dourmouse.cn_backends.load_backend`` — see that module
  for per-provider env vars and free-tier caveats. First step toward a
  "team of subagents" spread across many providers.

Every path returns REAL output or an honest error (Rules 2.1 / 2.2): a
missing key/CLI is NOT CONFIGURED, an API/CLI failure surfaces the real
error, and no result is ever fabricated. Secrets come only from env vars
(Rule 2.6). The data path is deterministic code — the model generates, the
agent/orchestrator decides how to apply.
"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from typing import Any

from dourmouse.config import (
    NVIDIA_DEFAULT_BASE_URL,
    load_nvidia_config,
    load_ollama_config,
    user_config_dir,
)

_DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
# v5.1: the NVIDIA NIM DeepSeek model used when the user has an NVIDIA key
# but no DeepSeek key. Overridable via DEEPSEEK_NVIDIA_MODEL. Verified live
# on the user's key (integrate.api.nvidia.com/v1/models).
_DEEPSEEK_NVIDIA_MODEL = "deepseek-ai/deepseek-v4-flash-0731"
_OUTPUT_CAP = 6_000

# -- CODE-screen Claude CLI session continuity ------------------------------ #
# Every ``code_claude`` call used to shell out to a brand-new `claude -p`
# process with zero memory of any previous call — verified live: two
# back-to-back run_code_task("claude", ...) calls, the second asking "what
# variable name did I just ask you to remember?", got "No variable given.
# This first message." The installed CLI (2.1.250) genuinely supports
# resuming a specific conversation across separate invocations though —
# `claude --help` documents `-r/--resume <session_id>` and
# `--session-id <uuid>` (assign an id up front), and a live round-trip
# (`claude -p ... --session-id <uuid>` then `claude -p ... --resume <uuid>`
# in a fresh process) genuinely recalled a fact from the first call. This
# threads that real session id through repeated calls so a CODE-screen
# conversation with Claude Code feels like ONE conversation, the same way
# talking to the CLI directly is one conversation.
#
# Keyed by cwd, not globally: the CODE screen's tool defaults every call to
# the same _PROJECT_ROOT cwd (see _make_code_tool), so in practice this is
# one running conversation per project directory — which also matches how
# Claude Code itself organizes sessions per project. A lock guards the dict
# because webui.py's ThreadingHTTPServer and all_hands.py can both reach
# run_code_task("claude", ...) concurrently.
_CLAUDE_SESSIONS: dict[str, str] = {}
_CLAUDE_SESSIONS_LOCK = threading.Lock()
# Exact substring of the CLI's real stderr (verified live above) when a
# tracked session id no longer resolves to a real conversation — e.g. the
# user pruned their local Claude Code session history out from under us.
_CLAUDE_NO_SESSION_ERR = "No conversation found with session ID"

# -- MCP bridge wiring (v13) ------------------------------------------------ #
# Gives every `claude` invocation from this module (code_claude, and any
# other run_code_task("claude", ...) caller) REAL, live access to Dourmouse's
# own tool registry via --mcp-config, not just its own generic bash/file
# tools. See mcp_bridge.py's own module docstring for the full rationale and
# the excluded-tool-name list (delegate_*/code_*/claude_code/codex_code —
# recursion risk, never exposed). Verified live on this machine: a manual
# `claude -p ... --mcp-config <path> --allowedTools "mcp__dourmouse__*"`
# call genuinely invoked the real list_tasks/world_pulse tools and returned
# real data, and the wildcard allowedTools pattern (rather than one flag per
# tool name) was confirmed live to actually match.
_MCP_ALLOWED_TOOLS = "mcp__dourmouse__*"
_mcp_config_path_cache: str | None = None
_mcp_config_lock = threading.Lock()


def _ensure_mcp_config_path() -> str:
    """Return the path to a real, up-to-date --mcp-config JSON, writing it
    once per process (cached) rather than once per call — the file's
    content only depends on sys.executable, which never changes mid-run.
    Lives in user_config_dir(), the same per-user, survives-updates
    location every other piece of Dourmouse config uses (config.py's own
    user_env_path) — never beside the package, which is read-only in a
    frozen build."""
    global _mcp_config_path_cache
    with _mcp_config_lock:
        if _mcp_config_path_cache is not None:
            return _mcp_config_path_cache
        from dourmouse.mcp_bridge import build_mcp_config_file

        path = user_config_dir() / "mcp-config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        build_mcp_config_file(path)
        _mcp_config_path_cache = str(path)
        return _mcp_config_path_cache


def _claude_session_key(cwd: str | None) -> str:
    return cwd or "."


def _claude_session_args(key: str, *, fresh: bool = False) -> list[str]:
    """Return the CLI args that continue (or, on ``fresh``, restart) the
    Claude conversation tracked for ``key``. Never guesses a session id that
    wasn't actually handed back by a real ``claude`` invocation — the first
    call for a key mints one via ``--session-id`` and every later call
    resumes that exact id via ``--resume``."""
    with _CLAUDE_SESSIONS_LOCK:
        session_id = None if fresh else _CLAUDE_SESSIONS.get(key)
        if session_id is None:
            session_id = str(uuid.uuid4())
            _CLAUDE_SESSIONS[key] = session_id
            return ["--session-id", session_id]
        return ["--resume", session_id]


def _forget_claude_session(key: str) -> None:
    with _CLAUDE_SESSIONS_LOCK:
        _CLAUDE_SESSIONS.pop(key, None)


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
        # v8.7: this is the API path only. ``run_code_task`` prefers the
        # user's real Codex CLI (their ChatGPT login) and only lands here as
        # a fallback, so the error names BOTH routes — the connections probe
        # reports on the CLI, and an error naming only the key read as a
        # contradiction of a green status light.
        key = os.environ.get("CODEX_API_KEY", "").strip()
        if not key:
            key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                # ASCII only: these surface on a Windows console, where a
                # non-ASCII dash renders as a replacement char.
                "NOT CONFIGURED: Codex needs EITHER the Codex CLI signed in "
                "(npm i -g @openai/codex, then 'codex login' - this is what "
                "the CODEX connection status checks) OR CODEX_API_KEY / "
                "OPENAI_API_KEY in .env. Nothing was run."
            )
        base = os.environ.get(
            "CODEX_BASE_URL", "https://api.openai.com/v1"
        ).strip()
        model = os.environ.get("CODEX_MODEL", "gpt-5-codex").strip()
        return base, key, model
    if name in ("qwen", "dashscope", "glm", "zhipu", "z.ai", "zai", "kimi", "moonshot"):
        # Chinese-lab OpenAI-compatible backends live in their own module
        # (kept out of this file to avoid a China-specific block growing
        # here) but return the same (base_url, api_key, model) contract, so
        # _run_openai_compat below runs them exactly like nvidia/deepseek.
        from dourmouse.cn_backends import load_backend as _load_cn_backend

        return _load_cn_backend(name)
    raise RuntimeError(
        f"ERROR: unknown code backend {backend!r} — use 'ollama', 'nvidia', "
        "'deepseek', 'codex', 'claude', 'qwen', 'glm' or 'kimi'."
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


def _inject_shared_context(task: str) -> str:
    """v8.31: the CLI-shelled-out backends (Claude Code CLI, Codex CLI)
    don't speak the ToolSpec protocol — unlike every other subagent, whose
    LLM call gets ``query_shared_memory`` as a real callable tool
    (general_roster.py's ``_query_shared_memory_tool``), a shelled-out CLI
    process can only ever see what is in its ``task`` argv string. This
    injects real retrieved shared-memory context into that string BEFORE
    the subprocess runs, mirroring ``dispatch._append_memory_context``'s
    own prepend-onto-the-last-user-turn pattern (front of the text, not
    buried mid-prompt — the same reason that function gives for why a
    short instruction on the user turn is followed more reliably than one
    in a system prompt).

    Swallows any lookup failure and returns ``task`` UNCHANGED — an
    observer/retrieval helper must never break the task it is enriching,
    same rule ``dispatch._maybe_ingest_memory`` and
    ``shared_rag.merged_search`` itself already follow. When shared memory
    is NOT_CONFIGURED on this machine (neither ``DOURMOUSE_GLOBAL_MEMORY``
    nor ``DOURMOUSE_SPATIAL_VAULT_PATH`` set — the common case today),
    ``merged_search`` honestly finds no source to consult and this
    injects nothing: no placeholder, no fabricated context.
    """
    try:
        from dourmouse.shared_rag import format_merged_result, merged_search

        result = merged_search(task, top_k=5)
    except Exception:  # noqa: BLE001 - injection must never break the task
        return task
    if not result.hits:
        return task
    context_block = format_merged_result(task, result)
    return f"{context_block}\n\n{task}"


def _run_claude_once(
    cli: str, task: str, session_args: list[str], *, cwd: str | None, timeout: int
) -> subprocess.CompletedProcess[str]:
    mcp_args: list[str] = []
    try:
        mcp_args = ["--mcp-config", _ensure_mcp_config_path(), "--allowedTools", _MCP_ALLOWED_TOOLS]
    except Exception:  # noqa: BLE001 - best-effort: a broken MCP config must
        # never stop coding from working at all; Claude just runs without
        # Dourmouse tool access for this one call (its own bash/file tools
        # are untouched either way).
        mcp_args = []
    try:
        return subprocess.run(
            [cli, "-p", *session_args, task, *mcp_args],
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
    session_key = _claude_session_key(cwd)
    session_args = _claude_session_args(session_key)
    proc = _run_claude_once(cli, task, session_args, cwd=cwd, timeout=timeout)
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 and "--resume" in session_args and _CLAUDE_NO_SESSION_ERR in err:
        # Our tracked session id no longer resolves to a real conversation
        # (e.g. the user pruned Claude Code's local session history out from
        # under us) — never keep retrying a dead id forever. Forget it and
        # start one honest fresh conversation instead of hard-failing the
        # whole task over bookkeeping the caller can't see or fix.
        _forget_claude_session(session_key)
        session_args = _claude_session_args(session_key)
        proc = _run_claude_once(cli, task, session_args, cwd=cwd, timeout=timeout)
        err = (proc.stderr or "").strip()
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        # v8.7: `claude -p` exits 1 with an EMPTY stderr when the CLI is
        # installed but not signed in — the single most likely failure here,
        # and "exited 1: (no stderr)" tells the user nothing actionable.
        # Name the probable cause without asserting it: on Windows the login
        # lives in the Credential Manager, so we cannot confirm it from here.
        if not err:
            raise RuntimeError(
                "claude exited 1 with no error output. The most likely cause "
                "is that the CLI is installed but NOT SIGNED IN - run "
                "'claude' on the host machine and complete /login, then "
                "retry. (Sign-in cannot be verified from here: Windows keeps "
                "it in the Credential Manager.)"
            )
        raise RuntimeError(f"claude exited {proc.returncode}: {err[-2000:]}")
    if not out:
        raise RuntimeError("claude returned no output (honest).")
    return out[-_OUTPUT_CAP:]


def _run_codex(task: str, *, cwd: str | None, timeout: int) -> str:
    """Run a coding task through the user's real Codex CLI (headless).

    v8.7: ``code_codex`` previously went straight to the OpenAI API, while
    the CODEX connection probe reports on the CLI + ~/.codex/auth.json. The
    two disagreed, so a user who ran ``codex login`` saw a green light and
    still got "needs CODEX_API_KEY". The CLI is now the primary path — the
    same thing the status light actually measures — and the API key remains
    a fallback for users who have one. Mirrors ``_run_claude``: real output
    or an honest error, never a fabricated result.
    """
    from dourmouse.general_roster import _find_codex_cli

    cli = _find_codex_cli()
    if cli is None:
        # No CLI: fall back to the API path, which raises its own honest
        # NOT CONFIGURED naming both routes when no key is set either.
        base_url, api_key, model = load_backend("codex")
        return _run_openai_compat(base_url, api_key, model, task, timeout)
    try:
        from dourmouse.mcp_bridge import ensure_codex_mcp_registered

        ensure_codex_mcp_registered(cli)
    except Exception:  # noqa: BLE001 - best-effort, same reasoning as the
        # Claude MCP wiring above: a registration failure must never break
        # a coding task that doesn't even need Dourmouse's own tools.
        pass
    timeout = max(1, min(int(timeout), 600))
    try:
        proc = subprocess.run(
            [cli, "exec", task, "--skip-git-repo-check"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"codex timed out after {timeout}s (task still running).") from None
    except OSError as exc:
        raise RuntimeError(f"could not run the codex CLI: {exc}") from exc
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = err[-2000:] or "(no stderr)"
        lowered = (err + out).lower()
        if "login" in lowered or "auth" in lowered or not err:
            raise RuntimeError(
                f"codex exited {proc.returncode}: {detail} — if this is an "
                "auth failure, run 'codex login' on the host machine and "
                "retry."
            )
        raise RuntimeError(f"codex exited {proc.returncode}: {detail}")
    if not out:
        raise RuntimeError("codex returned no output (honest).")
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
        # v8.31: inject real shared-memory context before the CLI ever
        # sees the task — see _inject_shared_context's own docstring for
        # why this is scoped to the CLI-shelled-out backends only.
        return _run_claude(_inject_shared_context(task), cwd=cwd, timeout=timeout)
    if name in ("codex", "openai_codex"):
        # v8.7: CLI first (what the CODEX status light measures), API key
        # only as a fallback — see _run_codex. v8.31: same shared-memory
        # injection as the claude branch above, before either the CLI or
        # its API fallback runs.
        return _run_codex(_inject_shared_context(task), cwd=cwd, timeout=timeout)
    base_url, api_key, model = load_backend(name)
    return _run_openai_compat(base_url, api_key, model, task, timeout)
