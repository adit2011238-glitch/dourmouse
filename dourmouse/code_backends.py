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

import json
import os
from pathlib import Path
import subprocess
import threading
import uuid
from typing import Any, Callable

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
# Chars of a CLI backend's output handed back to the caller (tail-kept —
# see _run_claude / _run_codex / stream_claude).
#
# v13.7 (2026-09-03, user directive "maximize context windows of
# everything"): was 6_000, which made this the outlier in its own repo —
# general_roster.py caps the SAME two CLIs at 20_000 (_CLAUDE_OUTPUT_CAP /
# _CODEX_OUTPUT_CAP), and sandbox.py and system_access.py both use 20_000
# for command output. So identical Claude output arrived truncated to a
# third of its length depending only on which module called it. Aligned on
# the house 20_000.
#
# This is affordable now specifically because of the window change made in
# the same pass: dispatch._MAX_LLM_TOKENS's arithmetic budgets 5,000 est
# tokens for one full-size in-flight tool result against the 32,768 window,
# and 20_000 chars is exactly that 5,000 by the repo's chars/4 convention.
# Older copies of the same result are cut again by
# dispatch._MAX_TOOL_RESULT_CHARS, so this does not compound across turns.
_OUTPUT_CAP = 20_000

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
# v5.21 (live-reproduced): --strict-mcp-config is REQUIRED here, not
# optional hardening. Without it the CLI also loads the user's own
# claude.ai connectors on top of ours, and Claude preferred those: asked
# for the latest email it called mcp__claude_ai_Gmail__search_threads and
# came back with "Gmail auth insufficient scope. Need reauthorize
# connector" -- which is exactly the "Dourmouse can't access my Google
# Workspace" symptom the user reported. Dourmouse's OWN gmail_search was
# working the entire time and returning real inbox rows; it was simply
# never reached. --allowedTools alone did not prevent this, because it
# gates which tools may run, not which MCP servers get loaded and offered.
#
# With the flag, exactly one Gmail path exists on this route: Dourmouse's
# own, which authenticates through the credentials in local_secrets.py
# rather than through a claude.ai connector the user would have to
# re-authorise separately.
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

# Response cap for the API (non-CLI) coding backends — see
# ``_run_openai_compat``, which serves ollama / nvidia / deepseek / codex /
# qwen / glm / kimi.
#
# v13.7 (2026-09-03, user directive "maximize context windows of
# everything"): was a bare 4000 inline. Two reasons that was too small:
#   * A real coding task ("write module X plus its tests") routinely
#     exceeds 4000 tokens of OUTPUT, and this function has no continuation
#     path — whatever the cap cuts is simply lost.
#   * Every model this routes to is reasoning-capable, and this repo's
#     recurring landmine is that such a model spends the cap on reasoning
#     inside ``content`` BEFORE the answer starts (see dispatch.py's
#     _DEFAULT_MAX_TOKENS comment for the three separate times that bug has
#     been fixed here). The ``enable_thinking: False`` extra_body below only
#     covers the keyless/Ollama case, and dispatch.py has measured that even
#     that flag is ignored on Ollama's OpenAI-compat endpoint.
#
# 8000 and not higher, deliberately: this one constant is sent to several
# providers, and a max_tokens ABOVE a provider's own per-response ceiling is
# a hard 400, not a soft clamp — so the constant has to sit at or under the
# lowest ceiling in the set, which is DeepSeek's documented 8192 for
# deepseek-chat. NOT VERIFIED LIVE in this change: no API call was made
# against any of these providers here; that 8192 figure is from DeepSeek's
# published limit, not a measurement taken on this machine. A deployment
# that only ever uses higher-ceiling backends (NVIDIA NIM, Codex) can raise
# it with DOURMOUSE_CODE_MAX_TOKENS rather than editing this.
_CODE_MAX_TOKENS = 8000
_CODE_MAX_TOKENS_ENV = "DOURMOUSE_CODE_MAX_TOKENS"


def _code_max_tokens() -> int:
    """Coding-backend response cap, honouring the env override.

    Floors at 512 — below that the cap cannot fit even a short function
    plus the reasoning preamble these models emit first, which is the exact
    failure the constant above exists to avoid.
    """
    raw = os.environ.get(_CODE_MAX_TOKENS_ENV, "").strip()
    if raw:
        try:
            return max(512, int(raw))
        except ValueError:
            pass
    return _CODE_MAX_TOKENS


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
            # Module global read at CALL time, same reason as the
            # _openai_client_factory note above.
            max_tokens=_code_max_tokens(),
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
        mcp_args = [
            "--mcp-config", _ensure_mcp_config_path(),
            "--strict-mcp-config",
            "--allowedTools", _MCP_ALLOWED_TOOLS,
        ]
    except Exception:  # noqa: BLE001 - best-effort: a broken MCP config must
        # never stop coding from working at all; Claude just runs without
        # Dourmouse tool access for this one call (its own bash/file tools
        # are untouched either way).
        mcp_args = []
    try:
        return subprocess.run(
            [
                cli, "-p", "--permission-mode", "bypassPermissions",
                *session_args, task, *mcp_args,
            ],
            cwd=cwd,
            env=_cli_env(cli),
            stdin=subprocess.DEVNULL,  # claude -p waits ~3s on stdin otherwise
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude timed out after {timeout}s (task still running).") from None
    except OSError as exc:
        raise RuntimeError(f"could not run the claude CLI: {exc}") from exc


def _cli_env(cli: str | None = None) -> dict[str, str]:
    """The environment to hand a coding CLI child process.

    Starts from a full copy of this process's own environment -- that
    inheritance is the whole point, and must not be narrowed. The Claude
    Code CLI resolves the user's subscription session itself, natively:
    from the macOS Keychain under the service name "Claude Code-credentials",
    or from ~/.claude/.credentials.json on Linux and Windows. Nothing here
    reads, injects, copies or logs a token; stripping the environment (or
    passing a hand-built one) is what would break that lookup, so we don't.

    What IS added is PATH. A macOS app launched from the Dock or via `open`
    does not inherit the user's shell PATH: the dourmouse2.app server
    process was measured running with exactly

        PATH=/usr/bin:/bin:/usr/sbin:/sbin

    which contains neither ~/.local/bin (where `claude` actually lives on
    this machine) nor any Node install. So the binary could not be found,
    and even once found by absolute path it would have failed to run,
    because `claude` is a Node program that needs `node` on PATH. The
    directory the CLI itself was resolved from goes first, since a CLI's
    siblings are the most likely place its own helpers live.
    """
    env = dict(os.environ)

    extra: list[str] = []
    if cli:
        extra.append(str(Path(cli).resolve().parent))
    for d in ("~/.local/bin", "~/.claude/local", "~/bin",
              "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        extra.append(str(Path(d).expanduser()))

    nvm = Path("~/.nvm/versions/node").expanduser()
    if nvm.is_dir():
        try:
            for version_dir in sorted(nvm.iterdir(), reverse=True):
                bin_dir = version_dir / "bin"
                if bin_dir.is_dir():
                    extra.append(str(bin_dir))
                    break  # the newest install is enough; don't stack them all
        except OSError:
            pass

    seen: set[str] = set()
    ordered: list[str] = []
    for part in extra + (env.get("PATH", "") or "").split(os.pathsep):
        if part and part not in seen:
            seen.add(part)
            ordered.append(part)
    env["PATH"] = os.pathsep.join(ordered)
    return env


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


# v13.2: real-time passthrough (explicit user request — "I only want to be
# talking to claude directly", "same thought tokens", "exact same experience
# as ... the terminal"). ``_run_claude`` above blocks on subprocess.run
# until the ENTIRE `claude -p` call finishes, then hands the whole answer
# to Dourmouse's OWN orchestrator LLM as one opaque tool_result string for
# it to read and re-narrate — that is a different model paraphrasing
# Claude's output after the fact, not "talking to Claude directly", and it
# throws away every live signal (text tokens as they're generated, the
# extended-thinking trace, each real tool call) until the whole run is
# already over.
#
# `claude -p --output-format stream-json --include-partial-messages` emits
# the CLI's real internal event stream as NDJSON while it runs — verified
# live on this machine: `stream_event` wraps the same Anthropic Messages
# API streaming shape (message_start/content_block_start/
# content_block_delta/content_block_stop/message_delta/message_stop) used
# everywhere else in this codebase for a live token feed, so
# content_block_delta.delta.text_delta.text IS the real per-token answer,
# content_block_delta.delta.thinking_delta.thinking IS the real reasoning
# trace, and a content_block_start with content_block.type=="tool_use" IS
# a real tool call Claude itself is making — not narrated, not summarized,
# the actual thing. webui.py wires this directly into the SAME SSE event
# vocabulary (assistant_delta/thinking_delta/tool_use/tool_result) the rest
# of the UI already renders, bypassing run_dispatch_messages and the
# Dourmouse system/roster prompt entirely for this one toolchain.
#
# --permission-mode bypassPermissions (v13.5, explicit user request: "give
# claude default approval for all mcps, i want the exact same way claude is
# used in terminal or on claude desktop within dourmouse, no skimping no
# difference at all"): this used to be acceptEdits, which only auto-approved
# file edits/writes and left Bash and every other tool category asking a
# permission question this headless subprocess has no TTY to answer, so
# those calls just silently failed or hung — the actual "skimping" the user
# was reporting. bypassPermissions is the real flag `claude --help` documents
# for "Bypass all permission checks" — the same trust level the user already
# operates at when running `claude` directly in their own terminal, so a
# CODE-screen task now gets the identical real tool access a terminal
# session would. This does NOT touch the separate, structural mcp_bridge.py
# exclusion list (delegate_task/code_*/claude_code/codex_code) — that guard
# exists to stop Claude recursively re-invoking itself/Dourmouse's own
# orchestration loop through the MCP bridge, a real infinite-loop/cost risk
# unrelated to file-edit approval, and stays in place.
def stream_claude(
    task: str,
    *,
    cwd: str | None,
    timeout: int,
    on_delta: Callable[[str], None],
    on_thinking: Callable[[str], None] | None = None,
    on_tool_use: Callable[[str, str], None] | None = None,
    on_tool_result: Callable[[str], None] | None = None,
    on_usage: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Stream one real ``claude -p`` run live; returns the final result text
    (the CLI's own ``type:"result"`` ``result`` field — the same string
    ``_run_claude`` returns, so a caller only wanting the final text can
    treat this as a drop-in). Same session continuity (``--session-id``/
    ``--resume``) and MCP tool wiring as ``_run_claude``; same honest
    NOT CONFIGURED / real-error contract (Rule 2.2) — nothing here ever
    fabricates a result.

    ``on_tool_use(name, raw_arguments_json)`` fires TWICE per tool call:
    once with empty arguments the instant Claude starts the call (so the
    UI can show "USING <tool>" immediately, matching how it feels watching
    the terminal), and once more with the complete arguments once Claude
    finishes emitting them. ``on_tool_result(text)`` fires when Claude's
    own tool execution reports back (a ``role: user`` message carrying a
    ``tool_result`` content block — Claude runs its OWN tools directly;
    this is Dourmouse OBSERVING that, never re-executing anything).
    """
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

    # Tell the model what it is and what it has, once per session.
    #
    # Reported by the user: "the models don't know what tools or agents they
    # can use". That was literally true. MCP tools are discoverable but not
    # announced, so every turn opened with a ToolSearch round trip before any
    # work could start -- and sometimes ended with the model concluding it had
    # no relevant tool when it plainly did. Google Drive was reachable for
    # weeks by an agent that never knew to look.
    #
    # Prepended to the FIRST prompt of a session only. The CLI is invoked with
    # --session-id on the first call for a working directory and --resume
    # after, so it holds the conversation itself; re-sending a ~2,300-token
    # briefing every turn would be paid for every turn and would tell the
    # model nothing it had not already been told.
    with _CLAUDE_SESSIONS_LOCK:
        _first_turn = session_key not in _CLAUDE_SESSIONS
    if _first_turn:
        try:
            from dourmouse.model_context import claude_orchestrator_preamble

            task = f"{claude_orchestrator_preamble()}\n\n---\n\n{task}"
        except Exception:  # noqa: BLE001 - a briefing must never break a turn
            pass

    mcp_args: list[str] = []
    try:
        mcp_args = [
            "--mcp-config", _ensure_mcp_config_path(),
            "--strict-mcp-config",
            "--allowedTools", _MCP_ALLOWED_TOOLS,
        ]
    except Exception:  # noqa: BLE001 - best-effort, see _run_claude_once's own comment
        mcp_args = []

    def _run_once(session_args: list[str]) -> tuple[int, str, str]:
        proc = subprocess.Popen(
            [
                cli, "-p", "--output-format", "stream-json",
                "--include-partial-messages", "--verbose",
                "--permission-mode", "bypassPermissions",
                *session_args, task, *mcp_args,
            ],
            cwd=cwd,
            env=_cli_env(cli),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stopped = threading.Event()

        def _watchdog() -> None:
            if not stopped.wait(timeout):
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001 - best-effort kill
                    pass

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()
        final_result = ""
        # Per content_block index: the tool's name (known at block start)
        # and its input_json_delta fragments, joined once the block closes.
        tool_name_by_index: dict[int, str] = {}
        tool_args_by_index: dict[int, list[str]] = {}
        try:
            for raw_line in proc.stdout or []:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "stream_event":
                    inner = ev.get("event") or {}
                    itype = inner.get("type")
                    if itype == "content_block_start":
                        block = inner.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            idx = inner.get("index")
                            name = block.get("name") or "tool"
                            tool_name_by_index[idx] = name
                            tool_args_by_index[idx] = []
                            if on_tool_use:
                                on_tool_use(name, "")
                    elif itype == "content_block_delta":
                        delta = inner.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta" and delta.get("text"):
                            on_delta(delta["text"])
                        elif dtype == "thinking_delta" and on_thinking and delta.get("thinking"):
                            on_thinking(delta["thinking"])
                        elif dtype == "input_json_delta":
                            idx = inner.get("index")
                            tool_args_by_index.setdefault(idx, []).append(
                                delta.get("partial_json", "")
                            )
                    elif itype == "content_block_stop":
                        idx = inner.get("index")
                        if idx in tool_name_by_index and on_tool_use:
                            on_tool_use(
                                tool_name_by_index[idx],
                                "".join(tool_args_by_index.get(idx, [])),
                            )
                elif etype == "user" and on_tool_result:
                    for block in (ev.get("message") or {}).get("content") or []:
                        if block.get("type") != "tool_result":
                            continue
                        content = block.get("content")
                        text = content if isinstance(content, str) else json.dumps(content)
                        on_tool_result((text or "")[:2000])
                elif etype == "result":
                    final_result = ev.get("result") or ""
                    if on_usage:
                        # v13.6: real usage bar ("how much usage you have
                        # used on claude") -- the CLI's own real result
                        # event, live-verified this session (a real `claude
                        # -p` call), carries genuine cost/token accounting
                        # (total_cost_usd, usage.input_tokens/output_tokens/
                        # cache_*) that used to be discarded here (only
                        # .result text was kept). Never fabricated: any
                        # field missing from a real response is simply
                        # absent from this dict, not zero-filled.
                        raw_usage = ev.get("usage") or {}
                        usage_out: dict[str, Any] = {}
                        if isinstance(ev.get("total_cost_usd"), (int, float)):
                            usage_out["cost_usd"] = float(ev["total_cost_usd"])
                        for field in (
                            "input_tokens", "output_tokens",
                            "cache_creation_input_tokens", "cache_read_input_tokens",
                        ):
                            value = raw_usage.get(field)
                            if isinstance(value, int):
                                usage_out[field] = value
                        if usage_out:
                            try:
                                on_usage(usage_out)
                            except Exception:  # noqa: BLE001 - a usage-tracking failure must never break the real reply
                                pass
        finally:
            stopped.set()
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        stderr_text = ""
        try:
            if proc.stderr:
                stderr_text = proc.stderr.read() or ""
        except Exception:  # noqa: BLE001
            pass
        return proc.returncode, final_result, stderr_text

    session_args = _claude_session_args(session_key)
    returncode, final_result, err = _run_once(session_args)
    if returncode != 0 and "--resume" in session_args and _CLAUDE_NO_SESSION_ERR in err:
        _forget_claude_session(session_key)
        session_args = _claude_session_args(session_key)
        returncode, final_result, err = _run_once(session_args)
    err = err.strip()
    if returncode != 0:
        if not err:
            raise RuntimeError(
                "claude exited 1 with no error output. The most likely cause "
                "is that the CLI is installed but NOT SIGNED IN - run "
                "'claude' on the host machine and complete /login, then "
                "retry."
            )
        raise RuntimeError(f"claude exited {returncode}: {err[-2000:]}")
    if not final_result:
        raise RuntimeError("claude returned no output (honest).")
    return final_result[-_OUTPUT_CAP:]


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
