"""General-domain subagent roster (v2.0 Section 4, General rows).

Built from the ground up against the dispatch engine in dispatch.py. Every
tool is REAL where a backend exists, and honestly NOT CONFIGURED where one
doesn't (Rule 2.2 — no silent stubs, no fabricated output):

- Research/Info: web search via Wikipedia's keyless public API (stdlib only).
- Comms: drafting is real and REGULAR; sending is confirmation-gated and
  NOT CONFIGURED until a channel backend is wired.
- Scheduling: time-slot proposal is a real deterministic function; calendar
  reads are read-only and NOT CONFIGURED without a backend.
- Dev/Coding: run_python executes real code in a sandboxed workspace dir;
  file read/write are scoped to the workspace; deploy is confirmation-gated.
- Admin/Ops: listing is read-only; deletion is per-item confirmation-gated.
- Memory: reads/writes the Obsidian vault via OBSIDIAN_VAULT_PATH
  (filesystem-backed until the Phase 2 MCP fix lands — same env var).

Permission tiers (Section 2.9) are enforced by the ENGINE, not here: tools
only declare their tier via ToolSpec.permission.

DELIBERATE DECISION (recorded for review): run_python is REGULAR, not
confirmation-gated. The user asked for a Dev/Coding subagent whose job is to
write/test/debug code, and "local code changes" are Regular tier in Section
2.9. Caveat noted by review: `python -c` can read any file/env and reach the
network — the workspace cwd is a convention, not a sandbox. If this system
later operates where that power is unacceptable, flip run_python to
REQUIRES_CONFIRMATION (change its Permission and add a confirm_prompt) with
no other impact. Risk/Guardrail (Trading) remains fully deterministic and
untouched.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from dourmouse.dispatch import (
    DispatchRegistry,
    Permission,
    Subagent,
    ToolSpec,
    current_dispatch_context,
    run_dispatch_messages,
    system_message,
)
from dourmouse import net_errors
from dourmouse.message_bus import BROADCAST, get_message_bus
from dourmouse.system_access import build_system_subagent

_DELEGATE_RESULT_CAP = 6_000


_WORKSPACE_ENV = "DOURMOUSE_WORKSPACE"
_VAULT_ENV = "OBSIDIAN_VAULT_PATH"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _workspace_root() -> Path:
    """Workspace root: DOURMOUSE_WORKSPACE env or <project>/workspace. Created."""
    raw = os.environ.get(_WORKSPACE_ENV)
    root = Path(raw).expanduser() if raw else _PROJECT_ROOT / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_resolve(base: Path, rel: str) -> Path:
    """Resolve rel under base, refusing to escape it (path-traversal guard).

    The refusal names the ACTUAL allowed root so an agent that guesses an
    absolute path can recover: it needs a path RELATIVE to that root.
    """
    target = (base / rel).resolve()
    base_resolved = base.resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError:
        raise ValueError(
            f"path escapes the allowed root: {rel!r}. The allowed root is "
            f"{base_resolved} — pass 'path' RELATIVE to it (e.g. "
            f"'notes/x.txt'), never an absolute path."
        )
    return target


def _sandbox_path_note() -> str:
    """Guidance appended to workspace file-tool descriptions at registry
    build time: agents learn the EXACT sandbox root up front, so they pass
    relative paths instead of guessing absolute ones and getting REFUSED.
    """
    return (
        " 'path' is RELATIVE to the workspace root "
        f"{_workspace_root()} — never pass an absolute path."
    )


def _vault_root() -> Path:
    raw = os.environ.get(_VAULT_ENV)
    if not raw:
        raise RuntimeError(
            "OBSIDIAN_VAULT_PATH is not set — Memory agent has no vault to "
            "read/write. Set it in .env (see .env.example)."
        )
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise RuntimeError(f"OBSIDIAN_VAULT_PATH is not a directory: {root}")
    return root


# --------------------------------------------------------------------------- #
# Claude Code — delegate coding work to the user's real Claude Code CLI
# --------------------------------------------------------------------------- #

_CLAUDE_OUTPUT_CAP = 20_000


def _find_claude_cli() -> str | None:
    """Locate the Claude Code CLI: CLAUDE_CODE_CLI env override, else PATH.

    Returns an absolute path, or None if unavailable (the tool then reports
    NOT CONFIGURED honestly — no silent stub, Rule 2.2).
    """
    raw = os.environ.get("CLAUDE_CODE_CLI")
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        resolved = shutil.which(raw)
        if resolved:
            return resolved
        return None
    return shutil.which("claude")


def _run_cli_delegate(
    *,
    cli: str,
    argv: list[str],
    cli_name: str,
    tool_label: str,
    display_name: str,
    cwd: str,
    timeout: int,
    output_cap_attr: str,
) -> str:
    """Run a headless coding CLI and format its REAL output (v5.3 — the
    shared engine behind the claude_code / codex_code tools). Never
    fabricates a result: a non-zero exit, a timeout, or an exec failure is
    reported honestly. The output cap is read from the module global at CALL
    time so tests can shrink it deterministically."""
    output_cap = globals().get(output_cap_attr, 20_000)  # type: ignore[no-any-return]
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,  # both CLIs wait on stdin otherwise
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # non-zero exits are surfaced, never raised
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: {tool_label} timed out after {timeout}s (task still running)."
    except OSError as exc:
        return f"ERROR: could not run the {cli_name} CLI: {exc}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = [f"EXIT CODE: {proc.returncode}"]
    if out:
        truncated = out[-output_cap:]
        parts.append(
            "STDOUT:\n" + truncated
            + ("\n[output truncated]" if len(out) > output_cap else "")
        )
    if err:
        parts.append("STDERR:\n" + err[-output_cap:])
    if proc.returncode != 0 and not out:
        parts.append(
            f"({display_name} exited non-zero; see STDERR for the real error.)"
        )
    return "\n".join(parts)


def _claude_code_tool(arguments: dict[str, Any]) -> str:
    """Run a coding task through the user's real Claude Code CLI.

    Uses headless mode (`claude -p <task>`) so the task is executed and its
    REAL stdout/stderr are returned. Never fabricates a result: a missing
    CLI, a non-zero exit, or a timeout is reported honestly.
    """
    task = (arguments.get("task") or "").strip()
    if not task:
        return "ERROR: claude_code requires a non-empty 'task'."
    cli = _find_claude_cli()
    if cli is None:
        return (
            "NOT CONFIGURED: the Claude Code CLI ('claude') was not found on "
            "PATH. Install it (npm i -g @anthropic-ai/claude-code) or set "
            "CLAUDE_CODE_CLI=/absolute/path/to/claude in .env. Nothing was "
            "run and no result was fabricated."
        )
    try:
        timeout = max(1, min(int(arguments.get("timeout_seconds", 300)), 600))
    except (TypeError, ValueError):
        return "ERROR: timeout_seconds must be an integer."
    cwd = (arguments.get("cwd") or str(_PROJECT_ROOT)).strip()
    return _run_cli_delegate(
        cli=cli,
        argv=[cli, "-p", task],
        cli_name="claude",
        tool_label="claude_code",
        display_name="Claude Code",
        cwd=cwd,
        timeout=timeout,
        output_cap_attr="_CLAUDE_OUTPUT_CAP",
    )


# --------------------------------------------------------------------------- #
# Codex — delegate coding work to the user's real Codex CLI (v5.3)
# --------------------------------------------------------------------------- #

_CODEX_OUTPUT_CAP = 20_000


def _find_codex_cli() -> str | None:
    """Locate the Codex CLI: CODEX_CLI env override, else PATH.

    Returns an absolute path, or None if unavailable (the tool then reports
    NOT CONFIGURED honestly — no silent stub, Rule 2.2). The CLI uses the
    ChatGPT login already in ~/.codex/auth.json, so no API key is needed.
    """
    raw = os.environ.get("CODEX_CLI")
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        resolved = shutil.which(raw)
        if resolved:
            return resolved
        return None
    return shutil.which("codex")


def _codex_code_tool(arguments: dict[str, Any]) -> str:
    """Run a coding task through the user's real Codex CLI (headless).

    Uses `codex exec <task> --skip-git-repo-check` so the task runs in the
    chosen cwd and its REAL stdout/stderr are returned. Never fabricates a
    result: a missing CLI, a non-zero exit, a timeout, or a usage-limit
    error is reported honestly.
    """
    task = (arguments.get("task") or "").strip()
    if not task:
        return "ERROR: codex_code requires a non-empty 'task'."
    cli = _find_codex_cli()
    if cli is None:
        return (
            "NOT CONFIGURED: the Codex CLI ('codex') was not found on PATH. "
            "Install it (npm i -g @openai/codex) or set CODEX_CLI=/absolute/"
            "path/to/codex in .env. Nothing was run and no result was "
            "fabricated."
        )
    try:
        timeout = max(1, min(int(arguments.get("timeout_seconds", 300)), 600))
    except (TypeError, ValueError):
        return "ERROR: timeout_seconds must be an integer."
    cwd = (arguments.get("cwd") or str(_PROJECT_ROOT)).strip()
    return _run_cli_delegate(
        cli=cli,
        argv=[cli, "exec", task, "--skip-git-repo-check"],
        cli_name="codex",
        tool_label="codex_code",
        display_name="Codex",
        cwd=cwd,
        timeout=timeout,
        output_cap_attr="_CODEX_OUTPUT_CAP",
    )


# --------------------------------------------------------------------------- #
# Research/Info — web search (keyless, stdlib-only, REAL)
# --------------------------------------------------------------------------- #

def _wikipedia_search(query: str, max_results: int = 5) -> str:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max_results,
            "format": "json",
        }
    )
    url = f"https://en.wikipedia.org/w/api.php?{params}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "dourmouse/0.1"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    hits = payload.get("query", {}).get("search", [])
    if not hits:
        return "WEB SEARCH: no results found (honest — no fabricated facts)."
    lines = []
    for i, hit in enumerate(hits, 1):
        title = hit.get("title", "")
        snippet = (hit.get("snippet") or "").replace("<span class=\"searchmatch\">", "").replace("</span>", "")
        lines.append(f"{i}. {title} — {snippet}")
    return "WEB SEARCH RESULTS (Wikipedia, live):\n" + "\n".join(lines)


def _brave_search(query: str, max_results: int = 5) -> str | None:
    """Keyless general web search via Brave's HTML results page.

    2026-08: DuckDuckGo's HTML endpoints now serve bot challenges (HTTP 202
    with no parseable results) to keyless scrapers. Brave still returns a
    real, server-rendered results page without a key, so it becomes the
    primary keyless engine. Returns formatted results or None if nothing
    usable came back (caller falls through to DuckDuckGo then Wikipedia).
    """
    url = (
        "https://search.brave.com/search?q="
        + urllib.parse.quote(query)
        + "&source=web"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read(400_000).decode("utf-8", errors="replace")
    parts = html.split('<div class="snippet ')
    if len(parts) < 2:
        return None
    lines: list[str] = []
    n = 0
    for part in parts[1:]:
        if n >= max_results:
            break
        href = re.search(r'<a href="(https?://[^"]+)"', part)
        title = re.search(r'class="title[^"]*"[^>]*>([^<]+)<', part)
        if not (href and title):
            continue
        t = re.sub(r"<[^>]+>", "", title.group(1)).strip()
        text = re.sub(r"<script[\s\S]*?</script>", " ", part)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        idx = text.find(t)
        desc = text[idx + len(t): idx + len(t) + 240].strip() if idx != -1 else ""
        n += 1
        lines.append(f"{n}. {t} — {desc}\n   {href.group(1)}")
    if not lines:
        return None
    return "WEB SEARCH RESULTS (Brave, live):\n" + "\n".join(lines)


def _duckduckgo_search(query: str, max_results: int = 5) -> str | None:
    """Keyless general web search via DuckDuckGo's HTML endpoint.

    Returns formatted results, or None if nothing usable came back (caller
    falls back to Wikipedia). Raises on transport errors. DDG hrefs are
    protocol-relative (//duckduckgo.com/l/?uddg=...) — normalized to https:
    so fetch_url can actually follow them.
    """
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (dourmouse/0.1)"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read(200_000).decode("utf-8", errors="replace")
    # Attribute-order-independent: lookahead ensures the class, then grab
    # href wherever it appears in the same <a> tag, then the inner text.
    anchors = re.findall(
        r'<a(?=[^>]*class="result__a")[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        html,
    )
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html)
    if not anchors:
        return None
    lines = []
    for i in range(min(max_results, len(anchors))):
        href, title = anchors[i]
        title = re.sub(r"(?s)<[^>]+>", "", title).strip()
        snippet = re.sub(r"(?s)<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        if href.startswith("//"):
            href = "https:" + href
        lines.append(f"{i + 1}. {title} — {snippet}\n   {href}")
    return "WEB SEARCH RESULTS (DuckDuckGo, live):\n" + "\n".join(lines)


def _web_search_tool(arguments: dict[str, Any]) -> str:
    query = arguments.get("query", "").strip()
    if not query:
        return "ERROR: web_search requires a non-empty 'query'."
    try:
        max_results = int(arguments.get("max_results", 5))
    except (TypeError, ValueError):
        # The model sometimes passes "five"; that must not crash the tool.
        return "ERROR: max_results must be an integer."
    errors: list[str] = []
    # Falls through to this if every engine returns unparseable-but-not-raising.
    last_exc: BaseException | str = "all engines returned no parseable results"
    # Keyless engines in resilience order: Brave first (2026-08: DDG's HTML
    # endpoints serve bot challenges), DuckDuckGo second (may recover), then
    # Wikipedia as the always-reliable fallback.
    for name, fn in (("Brave", _brave_search), ("DuckDuckGo", _duckduckgo_search)):
        try:
            res = fn(query, max_results)
            if res is not None:
                return res
            errors.append(f"{name} returned no parseable results")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{name} failed: {exc}")
            last_exc = exc
    try:
        return _wikipedia_search(query, max_results)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        errors.append(f"Wikipedia failed: {exc}")
        last_exc: BaseException | str = exc
    # Every keyless engine is down or unparseable. Classify on the last real
    # failure so an offline machine reads as offline rather than "not found",
    # and keep the per-engine detail in the log rather than in chat.
    return net_errors.report(
        last_exc,
        what=f"results for {query!r}",
        source="web_search",
        extra={"query": query, "engine_errors": errors},
        prefix="WEB SEARCH FAILED (reported honestly):",
    )


def _strip_html(raw: str) -> str:
    """Crude HTML -> text for read-back purposes (stdlib only, no deps)."""
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = raw.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    return raw.strip()


def _fetch_url_tool(arguments: dict[str, Any]) -> str:
    url = (arguments.get("url") or "").strip()
    if not url:
        return "ERROR: fetch_url requires a 'url'."
    if not url.lower().startswith(("http://", "https://")):
        return "ERROR: fetch_url only accepts http(s) URLs (got a non-web scheme)."
    try:
        max_chars = int(arguments.get("max_chars", 8000))
    except (TypeError, ValueError):
        return "ERROR: max_chars must be an integer."
    req = urllib.request.Request(
        url, headers={"User-Agent": "dourmouse/0.1"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(max_chars * 2 + 4096).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return net_errors.report(
            exc,
            what=f"the page at {url}",
            source="fetch_url",
            extra={"url": url},
            prefix="FETCH FAILED (reported honestly):",
        )
    text = _strip_html(raw)[:max_chars]
    if not text:
        return "FETCH: page returned no readable text (honest)."
    return f"FETCHED {url} ({len(text)} chars):\n{text}"


def _open_url_tool(arguments: dict[str, Any]) -> str:
    url = (arguments.get("url") or "").strip()
    if not url:
        return "ERROR: open_url requires a 'url'."
    import webbrowser

    try:
        opened = webbrowser.open(url, new=2)
    except Exception as exc:
        return f"OPEN FAILED (reported honestly): {exc}"
    if not opened:
        return f"OPEN FAILED: browser returned False for {url!r} (honest)."
    return f"OPENED IN BROWSER: {url}"


# --------------------------------------------------------------------------- #
# Comms — draft is real; sending is confirmation-gated + NOT CONFIGURED
# --------------------------------------------------------------------------- #

def _draft_message_tool(arguments: dict[str, Any]) -> str:
    channel = arguments.get("channel", "email")
    recipient = arguments.get("recipient", "")
    subject = arguments.get("subject", "")
    body = arguments.get("body", "")
    if not body.strip():
        return "ERROR: draft_message requires a 'body'."
    draft = {
        "channel": channel,
        "to": recipient,
        "subject": subject,
        "body": body,
        "status": "draft — NOT SENT",
    }
    # Persist the draft in the workspace so it is a real deliverable.
    drafts_dir = _workspace_root() / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    fname = f"draft_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    (drafts_dir / fname).write_text(json.dumps(draft, indent=2))
    return (
        f"DRAFT CREATED (NOT SENT): {channel} to {recipient or '(unspecified)'} "
        f"subject={subject or '(none)'} — saved to workspace/drafts/{fname}. "
        "Sending requires confirmation and a configured channel."
    )


def _send_draft_tool(arguments: dict[str, Any]) -> str:
    # Confirmation-gated at the engine level. No channel backend exists yet,
    # so even a confirmed send is honestly NOT CONFIGURED.
    return (
        "NOT CONFIGURED: no messaging channel backend wired yet (SMTP/Slack "
        "arrive with the Phase 3 front end). The draft is a deliverable; "
        "nothing was sent."
    )


# --------------------------------------------------------------------------- #
# Scheduling — proposal is deterministic; calendar reads are read-only
# --------------------------------------------------------------------------- #

def _propose_time_slots_tool(arguments: dict[str, Any]) -> str:
    try:
        duration_min = int(arguments.get("duration_minutes", 30))
        days_ahead = max(1, int(arguments.get("days_ahead", 5)))
        start_hour = int(arguments.get("start_hour", 9))
        end_hour = int(arguments.get("end_hour", 17))
    except (TypeError, ValueError):
        return "ERROR: duration_minutes/days_ahead/start_hour/end_hour must be integers."
    if duration_min <= 0:
        return "ERROR: duration_minutes must be > 0."
    if not (0 <= start_hour < end_hour <= 24):
        return "ERROR: need 0 <= start_hour < end_hour <= 24."

    slots = []
    today = datetime.now().date()
    for offset in range(1, days_ahead + 1):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:  # skip weekends
            continue
        cursor = datetime(day.year, day.month, day.day, start_hour)
        end_of_day = datetime(day.year, day.month, day.day, end_hour)
        while cursor + timedelta(minutes=duration_min) <= end_of_day:
            slots.append(
                f"{cursor.strftime('%Y-%m-%d %H:%M')} "
                f"({duration_min} min)"
            )
            cursor += timedelta(minutes=30)
        if len(slots) >= 10:
            break
    if not slots:
        return "No slots could be proposed in the given window."
    return "PROPOSED TIME SLOTS (deterministic; none booked):\n" + "\n".join(slots)


def _list_calendar_events_tool(arguments: dict[str, Any]) -> str:
    """v5.15: real per-user calendar reads via Google sign-in; honest
    NOT CONFIGURED without an OAuth user (Rule 2.2)."""
    from dourmouse.google_services import calendar_events

    return calendar_events(arguments.get("max_results", 5))


# --------------------------------------------------------------------------- #
# Dev/Coding — real code execution in a sandboxed workspace
# --------------------------------------------------------------------------- #

def _run_python_tool(arguments: dict[str, Any]) -> str:
    code = arguments.get("code", "")
    if not code.strip():
        return "ERROR: run_python requires a non-empty 'code' string."
    try:
        timeout = int(arguments.get("timeout_seconds", 30))
    except (TypeError, ValueError):
        return "ERROR: timeout_seconds must be an integer."
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_workspace_root()),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return f"ERROR: code timed out after {timeout}s: {exc}"
    except OSError as exc:
        return f"ERROR: could not run python: {exc}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = [f"EXIT CODE: {proc.returncode}"]
    if out:
        parts.append(f"STDOUT:\n{out}")
    if err:
        parts.append(f"STDERR:\n{err}")
    return "\n".join(parts)


def _read_file_tool(arguments: dict[str, Any]) -> str:
    try:
        target = _safe_resolve(_workspace_root(), arguments.get("path", ""))
    except ValueError as exc:
        return f"REFUSED: {exc}"
    if not target.is_file():
        return f"ERROR: no such file in workspace: {arguments.get('path')!r}"
    return target.read_text(encoding="utf-8", errors="replace")


def _search_files_tool(arguments: dict[str, Any]) -> str:
    """grep-style content search across the workspace (v2.0 Phase 2.2).

    Uses `grep -rn` via subprocess with a pure-Python fallback when grep is
    unavailable. Returns file:line:match lines, workspace-scoped via
    _safe_resolve so nothing outside the sandbox is searched.
    """
    query = arguments.get("query", "").strip()
    if not query:
        return "ERROR: search_files requires a non-empty 'query'."
    try:
        target = _safe_resolve(_workspace_root(), arguments.get("path", "."))
    except ValueError as exc:
        return f"REFUSED: {exc}"
    if not target.is_dir():
        return f"ERROR: not a directory in workspace: {arguments.get('path')!r}"
    try:
        max_results = int(arguments.get("max_results", 50))
    except (TypeError, ValueError):
        return "ERROR: max_results must be an integer."

    grep = shutil.which("grep")
    if grep:
        try:
            proc = subprocess.run(
                [grep, "-rn", "--", query, str(target)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            raw = proc.stdout
        except (subprocess.TimeoutExpired, OSError):
            raw = ""
    else:
        raw = ""
        for p in sorted(target.rglob("*")):
            if p.is_dir() or p.is_symlink():
                continue
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if query in line:
                        raw += f"{p.relative_to(_workspace_root())}:{i}:{line}\n"
            except OSError:
                continue
            if raw.count("\n") >= max_results:
                break

    lines = [ln for ln in raw.splitlines() if ln.strip()][:max_results]
    if not lines:
        return f"SEARCH: no matches for {query!r} in workspace."
    return "SEARCH RESULTS (workspace):\n" + "\n".join(lines)


def _diff_preview_tool(arguments: dict[str, Any], *, for_write: bool = False) -> str:
    """Unified diff of a proposed write vs the current file, WITHOUT writing
    (v2.0 Phase 2.2). Shows exactly what would change. When ``for_write`` is
    True (write_file already committed), the header says so — a diff shown
    AFTER a write is a "what changed" confirmation, not a preview."""
    try:
        target = _safe_resolve(_workspace_root(), arguments.get("path", ""))
    except ValueError as exc:
        return f"REFUSED: {exc}"
    new_content = arguments.get("content", "")
    header = "DIFF (what changed in this write):" if for_write else "DIFF PREVIEW (not written):"
    if not target.exists():
        return f"DIFF (new file): {target.relative_to(_workspace_root())} would be created ({len(new_content)} chars)."
    if not target.is_file():
        return f"ERROR: not a file in workspace: {arguments.get('path')!r}"
    old = target.read_text(encoding="utf-8", errors="replace").splitlines()
    new = new_content.splitlines()
    diff = "\n".join(
        difflib.unified_diff(old, new, fromfile=str(target.relative_to(_workspace_root())), tofile=str(target.relative_to(_workspace_root())))
    )
    if not diff.strip():
        return f"DIFF: no changes ({arguments.get('path')!r} already matches the proposed content)."
    return header + "\n" + diff


def _write_file_tool(arguments: dict[str, Any]) -> str:
    try:
        target = _safe_resolve(_workspace_root(), arguments.get("path", ""))
    except ValueError as exc:
        return f"REFUSED: {exc}"
    existed = target.exists()
    # v2.0 Phase 2.2 default UX: when the target already exists, surface the
    # unified diff in the result so the model/transcript/human can see EXACTLY
    # what changed, not just "WROTE 400 chars". for_write=True so the header
    # reads as a what-changed confirmation (the file HAS been written).
    diff_note = ""
    if existed:
        diff_note = _diff_preview_tool(arguments, for_write=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(arguments.get("content", ""))
    verb = "UPDATED" if existed else "WROTE"
    msg = f"{verb} workspace file: {target.relative_to(_workspace_root())} ({len(arguments.get('content', ''))} chars)"
    if diff_note:
        msg += "\n\n" + diff_note
    return msg


def _edit_file_tool(arguments: dict[str, Any]) -> str:
    """str-replace targeted edit (v2.0 Phase 2.2).

    Mirrors the str_replace tool's uniqueness constraint: old_str must occur
    EXACTLY once, or the edit is refused — silent multi-match edits are a
    correctness hazard.
    """
    try:
        target = _safe_resolve(_workspace_root(), arguments.get("path", ""))
    except ValueError as exc:
        return f"REFUSED: {exc}"
    old_str = arguments.get("old_str", "")
    new_str = arguments.get("new_str", "")
    if not old_str:
        return "ERROR: edit_file requires a non-empty 'old_str'."
    if not target.is_file():
        return f"ERROR: no such file in workspace: {arguments.get('path')!r}"
    text = target.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_str)
    if count == 0:
        return f"ERROR: old_str not found in {arguments.get('path')!r} — nothing edited."
    if count > 1:
        return (
            f"ERROR: old_str found {count} times in {arguments.get('path')!r} — "
            "refusing ambiguous multi-match edit. Narrow old_str or use write_file."
        )
    new_text = text.replace(old_str, new_str, 1)
    target.write_text(new_text)
    diff = "\n".join(
        difflib.unified_diff(
            text.splitlines(), new_text.splitlines(),
            fromfile=str(target.relative_to(_workspace_root())), tofile=str(target.relative_to(_workspace_root())),
        )
    )
    return "EDITED workspace file " + str(target.relative_to(_workspace_root())) + " (1 occurrence):\n" + diff


def _deploy_tool(arguments: dict[str, Any]) -> str:
    return (
        "NOT CONFIGURED: deploy/publish path not wired (requires "
        "confirmation AND a configured target). Nothing was deployed."
    )


# --------------------------------------------------------------------------- #
# Admin/Ops — read-only listing; per-item confirmed deletion
# --------------------------------------------------------------------------- #

def _list_files_tool(arguments: dict[str, Any]) -> str:
    try:
        target = _safe_resolve(_workspace_root(), arguments.get("path", "."))
    except ValueError as exc:
        return f"REFUSED: {exc}"
    if not target.is_dir():
        return f"ERROR: not a directory in workspace: {arguments.get('path')!r}"
    entries = sorted(
        p.name + ("/" if p.is_dir() else "") for p in target.iterdir()
    )
    return "WORKSPACE LISTING:\n" + ("\n".join(entries) if entries else "(empty)")


def _delete_file_tool(arguments: dict[str, Any]) -> str:
    # Engine enforces the per-item confirmation tier BEFORE this handler
    # runs. This function is the confirmed action.
    try:
        target = _safe_resolve(_workspace_root(), arguments.get("path", ""))
    except ValueError as exc:
        return f"REFUSED: {exc}"
    if not target.is_file():
        return f"ERROR: no such file in workspace: {arguments.get('path')!r}"
    target.unlink()
    return f"DELETED workspace file: {arguments.get('path')!r}"


# --------------------------------------------------------------------------- #
# Memory — Obsidian vault reads/writes + SQLite FTS5 long-term store (A1)
# --------------------------------------------------------------------------- #

_MEMORY_DB_ENV = "DOURMOUSE_MEMORY_DB"


def _memory_db_path() -> Path:
    """Memory store DB: DOURMOUSE_MEMORY_DB env, else <workspace>/memory/<db>."""
    raw = os.environ.get(_MEMORY_DB_ENV)
    if raw:
        return Path(raw).expanduser()
    return _workspace_root() / "memory" / "atlas_memory.db"


def _open_memory_store():
    """Open the shared long-term store (Phase A1), honestly NOT CONFIGURED
    when SQLite FTS5 is unavailable (Rule 2.2 — never a silent grep fake)."""
    from dourmouse.memory_store import MemoryStore, MemoryStoreUnavailable

    try:
        return MemoryStore(_memory_db_path())
    except MemoryStoreUnavailable as exc:
        return exc


def _remember_tool(arguments: dict[str, Any]) -> str:
    """Persist a fact/note to the long-term store (source, title, body)."""
    store = _open_memory_store()
    if isinstance(store, Exception):
        return f"NOT CONFIGURED: {store}"
    try:
        return store.remember(
            source=arguments.get("source", "agent"),
            title=arguments.get("title", ""),
            body=arguments.get("body", ""),
        )
    except ValueError as exc:
        return f"ERROR: {exc}"
    finally:
        store.close()


def _recall_tool(arguments: dict[str, Any]) -> str:
    """FTS5-ranked full-text recall from the long-term store."""
    store = _open_memory_store()
    if isinstance(store, Exception):
        return f"NOT CONFIGURED: {store}"
    try:
        hits = store.search(
            query=arguments.get("query", ""),
            limit=arguments.get("max_results", 10),
        )
    finally:
        store.close()
    if not hits:
        return "MEMORY RECALL: no matches in the long-term store (honest)."
    lines = []
    for h in hits:
        lines.append(
            f"- [{h['source']}] {h['title']} (score {h['score']})\n    {h['snippet']}"
        )
    return "MEMORY RECALL RESULTS (SQLite FTS5, live):\n" + "\n".join(lines)

def _semantic_recall_tool(arguments: dict[str, Any]) -> str:
    """v4.1 (P6): vector recall with an honest FTS5 fallback."""
    store = _open_memory_store()
    if isinstance(store, Exception):
        return f"NOT CONFIGURED: {store}"
    try:
        from dourmouse.memory_embed import semantic_search

        result = semantic_search(
            store,
            query=str(arguments.get("query", "") or "").strip(),
            limit=int(arguments.get("max_results", 5)),
        )
    except (TypeError, ValueError):
        return "ERROR: max_results must be an integer."
    finally:
        store.close()
    hits = result["hits"]
    if not hits:
        return (
            f"MEMORY SEARCH ({result['method'].upper()}): "
            "no matches in the long-term store (honest)."
        )
    lines = [f"MEMORY SEARCH RESULTS ({result['method'].upper()}):"]
    for h in hits:
        lines.append(
            f"- [{h['source']}] {h['title']} (score {h['score']})\n    {h['snippet']}"
        )
    return "\n".join(lines)


def _query_shared_memory_tool(arguments: dict[str, Any]) -> str:
    """The 'shared database all LLMs can use' tool (shared_rag.py): a
    read-only search across whichever of {local Ollama-embedded
    GlobalMemory, desktop spatial vault} is actually configured on THIS
    machine, tagged by source. See shared_rag.py's own docstring for the
    full merge design and the defensive schema-probing behind the vault
    half. Honestly NOT CONFIGURED when neither source is enabled."""
    query = str(arguments.get("query", "") or "").strip()
    if not query:
        return "ERROR: query_shared_memory requires a non-empty 'query'."
    try:
        top_k = int(arguments.get("top_k", 5))
    except (TypeError, ValueError):
        return "ERROR: top_k must be an integer."
    from dourmouse.shared_rag import format_merged_result, merged_search

    result = merged_search(query, top_k=max(1, min(top_k, 20)))
    return format_merged_result(query, result)


def _search_vault_tool(arguments: dict[str, Any]) -> str:
    query = arguments.get("query", "").strip().lower()
    if not query:
        return "ERROR: search_vault requires a non-empty 'query'."
    try:
        root = _vault_root()
    except RuntimeError as exc:
        return f"NOT CONFIGURED: {exc}"
    matches = []
    for p in sorted(root.rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if query in text.lower():
            matches.append(str(p.relative_to(root)))
        if len(matches) >= int(arguments.get("max_results", 10)):
            break
    if not matches:
        return f"VAULT SEARCH: no notes containing {query!r}."
    return "VAULT SEARCH RESULTS:\n" + "\n".join(matches)


def _read_note_tool(arguments: dict[str, Any]) -> str:
    try:
        root = _vault_root()
        target = _safe_resolve(root, arguments.get("path", ""))
    except RuntimeError as exc:
        return f"NOT CONFIGURED: {exc}"
    except ValueError as exc:
        return f"REFUSED: {exc}"
    if not target.is_file():
        return f"ERROR: no such note in vault: {arguments.get('path')!r}"
    return target.read_text(encoding="utf-8", errors="replace")


def _write_note_tool(arguments: dict[str, Any]) -> str:
    try:
        root = _vault_root()
        target = _safe_resolve(root, arguments.get("path", ""))
    except RuntimeError as exc:
        return f"NOT CONFIGURED: {exc}"
    except ValueError as exc:
        return f"REFUSED: {exc}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(arguments.get("content", ""))
    return f"WROTE vault note: {arguments.get('path')!r}"


# --------------------------------------------------------------------------- #
# Messenger — inter-agent messaging (v3.0, agents talk to each other)
# --------------------------------------------------------------------------- #


def _send_message_tool(registry: DispatchRegistry) -> ToolSpec:
    """Send a message FROM one roster agent TO another (or broadcast "*").

    Deterministic validation (Rule 2.8): both from and to must be real
    registered subagents (or BROADCAST for ``to``) — a typo'd or spoofed
    agent name is refused loudly, never silently routed. The bus is
    in-process and bounded; nothing here leaves the machine (Rule 2.2:
    no external send is claimed).

    NOTE on naming: this function is deliberately NOT called
    ``_send_message`` to avoid any chance of shadowing — the registry
    already has a comms ``draft_message``/``send_draft`` pair, and the
    mail subagent owns the IMAP ``read_inbox`` name. Tool names must be
    globally unique.
    """

    def _handler(arguments: dict[str, Any]) -> str:
        from_agent = (arguments.get("from_agent") or "").strip()
        to_agent = (arguments.get("to_agent") or "").strip()
        subject = (arguments.get("subject") or "").strip()
        body = (arguments.get("body") or "").strip()
        if not from_agent:
            return "ERROR: send_message requires 'from_agent'."
        if from_agent not in registry.subagent_names:
            return (
                f"REFUSED: unknown sender {from_agent!r} — send_message can "
                "only speak for a real roster agent."
            )
        if not to_agent:
            return "ERROR: send_message requires 'to_agent' (a subagent or '*')."
        if to_agent != BROADCAST and to_agent not in registry.subagent_names:
            return (
                f"REFUSED: unknown recipient {to_agent!r} — messages go to "
                "real roster agents or '*'."
            )
        if not body:
            return "ERROR: send_message requires a non-empty 'body'."
        msg = get_message_bus().post(
            from_agent=from_agent,
            to_agent=to_agent,
            subject=subject or "(no subject)",
            body=body,
        )
        target = "the whole roster (broadcast)" if to_agent == BROADCAST else to_agent
        return (
            f"MESSAGE SENT: {from_agent} -> {target} ({msg['id']}) "
            f"subject={subject or '(no subject)'} — delivered on the "
            "inter-agent bus."
        )

    return ToolSpec(
        name="send_message",
        description=(
            "Send a message from ONE roster agent to ANOTHER (or broadcast "
            "to the whole roster with to_agent='*') on the inter-agent bus. "
            "Use to route information between agents mid-task, e.g. research "
            "sends its findings to markets. Both agents must be real roster "
            "members. Internal bus only — nothing is sent outside the machine."
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_agent": {"type": "string", "description": "sending subagent name"},
                "to_agent": {"type": "string", "description": "recipient subagent name, or '*' to broadcast"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["from_agent", "to_agent", "body"],
        },
        handler=_handler,
    )


def _read_agent_inbox_tool(registry: DispatchRegistry) -> ToolSpec:
    """Read the INTER-AGENT inbox for ONE roster agent (direct + broadcast).

    Deterministic: returns the REAL messages from the bus, newest first,
    with unread counts. Honest "inbox empty" when there is nothing.

    Named ``_read_agent_inbox_tool`` (NOT ``_read_inbox_tool``) because the
    ``mail`` subagent already owns that module-level name for its IMAP
    handler — a second binding would shadow the mail tool at registry build
    time (reviewer-caught).
    """

    def _handler(arguments: dict[str, Any]) -> str:
        agent = (arguments.get("agent") or "").strip()
        if not agent:
            return "ERROR: read_agent_inbox requires 'agent'."
        if agent not in registry.subagent_names:
            return (
                f"REFUSED: unknown agent {agent!r} — read_agent_inbox reads "
                "the inbox of a real roster agent."
            )
        try:
            limit = int(arguments.get("limit", 10))
        except (TypeError, ValueError):
            return "ERROR: limit must be an integer."
        bus = get_message_bus()
        rows = bus.inbox(agent, limit=max(1, min(limit, 50)))
        # v3.0: reading the inbox marks these messages read FOR THIS AGENT —
        # a broadcast stays unread for every other agent until it reads it,
        # so one agent's read never clears another's badge (reviewer-caught).
        for m in rows:
            bus.mark_read(m["id"], agent)
            m["read"] = True
        unread = bus.unread_count(agent)
        if not rows:
            return f"INBOX ({agent}): empty — no messages yet (honest)."
        lines = []
        for m in rows:
            tag = "UNREAD" if not m["read"] else "read  "
            dest = "broadcast" if m["to"] == BROADCAST else m["to"]
            lines.append(
                f"- [{tag}] {m['id']} {m['from']} -> {dest} "
                f"({m['at'][11:19]}) {m['subject']}\n    {m['body'][:200]}"
            )
        head = f"INBOX ({agent}): {len(rows)} shown, {unread} unread — "
        return head + "\n".join(lines)

    return ToolSpec(
        name="read_agent_inbox",
        description=(
            "Read the INTER-AGENT inbox for a roster subagent: direct messages "
            "and broadcasts from the message bus, newest first, with unread "
            "counts. Use to see what other agents sent to this one (including "
            "live feed broadcasts). Note: this is the agent-to-agent bus, NOT "
            "the mail subagent's IMAP read_inbox."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "whose inter-agent inbox to read"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["agent"],
        },
        handler=_handler,
    )


def _build_messenger_subagent(registry: DispatchRegistry) -> Subagent:
    """The messenger subagent (v3.0): inter-agent messaging tools."""
    return _subagent(
        "messenger",
        "Both",
        "Inter-agent messaging — sends messages between roster agents on the bus.",
        [_send_message_tool(registry), _read_agent_inbox_tool(registry)],
    )


def _build_memory_subagent(registry: DispatchRegistry) -> Subagent:
    """The memory subagent (v2.x): vault + SQLite FTS5 store + self-review."""

    def _daily_digest_tool(arguments: dict[str, Any]) -> str:
        """v4.0 Phase 13: honest self-review over the real bus traffic."""
        from dourmouse.self_improve import build_daily_digest

        digest = build_daily_digest(registry)
        lines = [
            f"SELF-REVIEW // {digest['generated_at']} // {digest['message_count']} bus msgs",
            "",
        ]
        for name, s in digest["agents"].items():
            last = s["last_sent_at"] or "never"
            top = f" via {s['top_activity']}" if s["top_activity"] else ""
            lines.append(
                f"- {name}: sent {s['sent']}, recv {s['received']}, last {last}{top}"
            )
        lines.append("")
        lines.append("SUGGESTIONS:")
        lines.extend(f"- {sug}" for sug in digest["suggestions"])
        return "\n".join(lines)

    return _subagent(
        "memory",
        "Both",
        "Reads/writes the Obsidian vault + a SQLite FTS5 long-term store, "
        "and runs the honest daily self-review (Phase 13).",
        [
            ToolSpec(
                name="remember",
                description=(
                    "Store a fact/note in the LONG-TERM memory store "
                    "(SQLite FTS5, source/title/body). MANDATORY when the "
                    "user says 'remember X' — never just acknowledge; call "
                    "this tool so the fact actually persists. Also use when "
                    "a durable fact must survive beyond this conversation."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "default": "agent"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["title", "body"],
                },
                handler=_remember_tool,
            ),
            ToolSpec(
                name="recall",
                description=(
                    "Full-text search the LONG-TERM memory store (SQLite "
                    "FTS5 ranking) for facts/notes previously remembered "
                    "or ingested from sessions/vault. Use to recall what "
                    "was established before."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
                handler=_recall_tool,
            ),
            ToolSpec(
                name="memory_search_semantic",
                description=(
                    "SEMANTIC search of the LONG-TERM memory store: local "
                    "embedding similarity (DOURMOUSE_EMBED=1), falling back "
                    "honestly to FTS5 keyword recall when embeddings are "
                    "off/unavailable. Use to recall by MEANING rather than "
                    "exact words."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
                handler=_semantic_recall_tool,
            ),
            ToolSpec(
                name="search_vault",
                description=(
                    "Search the Obsidian vault for notes containing a query "
                    "(filesystem-backed until Phase 2 MCP fix)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
                handler=_search_vault_tool,
            ),
            ToolSpec(
                name="read_note",
                description="Read one note (.md) from the Obsidian vault.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=_read_note_tool,
            ),
            # v8.15: gated — silently overwrites the user's real vault notes
            # with no diff shown (unlike write_file, which is workspace-
            # sandboxed and shows one on overwrite; see its docstring).
            ToolSpec(
                name="write_note",
                description=(
                    "Write (create/overwrite) one note in the Obsidian vault. "
                    "REQUIRES human confirmation before it overwrites anything."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                handler=_write_note_tool,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: (
                    f"Write vault note {a.get('path', '?')!r} "
                    f"({len(a.get('content', ''))} chars)? This overwrites any "
                    "existing content at that path with no diff shown."
                ),
            ),
            ToolSpec(
                name="daily_digest",
                description=(
                    "Run the daily SELF-REVIEW (Phase 13): honest per-agent "
                    "stats from the inter-agent bus (messages sent/received, "
                    "top activity, last activity) plus conservative improvement "
                    "suggestions. Zero fabrication — a silent agent is reported "
                    "as silent."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_daily_digest_tool,
            ),
            ToolSpec(
                name="query_shared_memory",
                description=(
                    "Read-only search across the SHARED memory sources "
                    "configured on this machine: the local Ollama-embedded "
                    "store (DOURMOUSE_GLOBAL_MEMORY=1) and/or the desktop's "
                    "much larger spatial vault (DOURMOUSE_SPATIAL_VAULT_PATH), "
                    "when either is set. Every roster agent can call this — "
                    "the same shared knowledge base regardless of which "
                    "backend (nvidia/deepseek/claude/codex/ollama/qwen/glm/"
                    "kimi) is answering. Honestly reports NOT CONFIGURED "
                    "when neither source is enabled — never a silent empty "
                    "result."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
                handler=_query_shared_memory_tool,
                permission=Permission.REGULAR,
            ),
        ],
    )


# --------------------------------------------------------------------------- #
# Roster builder
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Multi-backend coding tools (v2.4) — NVIDIA / Freebuff DeepSeek / Claude
# --------------------------------------------------------------------------- #

_BACKEND_LABELS = {
    "ollama": "local Ollama",
    "nvidia": "NVIDIA NIM",
    "deepseek": "DeepSeek (Freebuff key or NVIDIA NIM)",
    "codex": "OpenAI Codex API",
    "claude": "Claude Code CLI",
}


def _make_code_tool(backend: str) -> ToolSpec:
    """Build a ToolSpec that routes a coding task through ONE LLM backend.

    Tool names are globally unique per backend (code_nvidia / code_deepseek /
    code_claude) because the registry rejects cross-agent collisions. Each
    handler reports configuration/execution failures honestly — a missing key
    or CLI is NOT CONFIGURED, never a fabricated answer (Rule 2.2).
    """
    label = _BACKEND_LABELS[backend]

    def handler(arguments: dict[str, Any]) -> str:
        from dourmouse import code_backends

        task = (arguments.get("task") or "").strip()
        if not task:
            return f"ERROR: code_{backend} requires a non-empty 'task'."
        try:
            timeout = int(arguments.get("timeout_seconds", 120))
        except (TypeError, ValueError):
            return "ERROR: timeout_seconds must be an integer."
        cwd = (arguments.get("cwd") or "").strip() or str(_PROJECT_ROOT)
        try:
            result = code_backends.run_code_task(
                backend, task, cwd=cwd, timeout=timeout
            )
        except RuntimeError as exc:
            return f"CODE {backend.upper()} (reported honestly): {exc}"
        return f"CODE {backend.upper()} RESULT ({label}):\n{result}"

    return ToolSpec(
        name=f"code_{backend}",
        description=(
            f"Run a real coding task through the {label} LLM backend and "
            f"return its REAL output. Honest NOT CONFIGURED if the backend "
            f"has no credentials/CLI."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "coding task"},
                "cwd": {"type": "string", "default": str(_PROJECT_ROOT)},
                "timeout_seconds": {"type": "integer", "default": 120},
            },
            "required": ["task"],
        },
        handler=handler,
    )


# --------------------------------------------------------------------------- #
# Live-intelligence tools (v2.3) — news / markets / mail / tasks
# --------------------------------------------------------------------------- #

def _news_headlines_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import live_feeds

    try:
        max_results = int(arguments.get("max_results", 10))
    except (TypeError, ValueError):
        return "ERROR: max_results must be an integer."
    try:
        items = live_feeds.news_headlines(max_results)
    except (RuntimeError, OSError, ValueError) as exc:
        return net_errors.report(
            exc,
            what="today's headlines",
            source="news_headlines",
            suggestion="You can ask me to web_search for a specific story instead.",
        )
    lines = [
        f"- {it['title']} [{it['source']}] {it['published']}" for it in items
    ]
    return "LIVE NEWS HEADLINES (Google News, keyless):\n" + "\n".join(lines)


def _news_search_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import live_feeds

    query = (arguments.get("query") or "").strip()
    if not query:
        return "ERROR: news_search requires a non-empty 'query'."
    try:
        max_results = int(arguments.get("max_results", 10))
    except (TypeError, ValueError):
        return "ERROR: max_results must be an integer."
    try:
        items = live_feeds.news_search(query, max_results)
    except (RuntimeError, OSError, ValueError) as exc:
        return net_errors.report(
            exc,
            what=f"news about {query}",
            source="news_search",
            suggestion="You can ask me to web_search for it instead.",
            extra={"query": query},
        )
    lines = [f"- {it['title']} [{it['source']}] {it['published']}" for it in items]
    return (
        f"LIVE NEWS RESULTS for {query!r} (Google News, keyless):\n"
        + "\n".join(lines)
    )


def _stock_quote_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import live_feeds

    symbol = (arguments.get("symbol") or "").strip()
    if not symbol:
        return "ERROR: stock_quote requires a 'symbol' (e.g. AAPL)."
    try:
        q = live_feeds.stock_quote(symbol)
    except (RuntimeError, OSError, ValueError) as exc:
        # A 404 here usually means the model routed a non-ticker question
        # (a sports score, a country) into the markets agent. Say so, and
        # point at the tool that can actually answer it.
        kind = net_errors.classify(exc)
        if kind is net_errors.ErrorKind.NOT_FOUND:
            suggestion = (
                f"{symbol!r} doesn't look like a tradeable ticker — "
                "use web_search if you meant something other than a stock."
            )
        else:
            suggestion = None
        return net_errors.report(
            exc,
            what=f"a quote for {symbol}",
            source="stock_quote",
            suggestion=suggestion,
            extra={"symbol": symbol},
        )
    return (
        f"QUOTE {q['symbol']}: ${q['price']} {q['currency']} "
        f"(day {q['day_low']}–{q['day_high']}, 52wk {q['week52_low']}–{q['week52_high']})"
    )


def _market_movers_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import live_feeds

    direction = (arguments.get("direction") or "gainers").strip()
    try:
        count = int(arguments.get("count", 10))
    except (TypeError, ValueError):
        return "ERROR: count must be an integer."
    try:
        rows = live_feeds.market_movers(direction, count)
    except (RuntimeError, OSError, ValueError) as exc:
        return net_errors.report(
            exc,
            what=f"today's top {direction}",
            source="market_movers",
            extra={"direction": direction},
        )
    lines = []
    for i, r in enumerate(rows, 1):
        pct = r["change_pct"]
        pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else str(pct)
        lines.append(f"{i}. {r['symbol']} {r['name']} ${r['price']} ({pct_s})")
    return f"TOP DAY {direction.upper()} (Yahoo Finance, keyless):\n" + "\n".join(lines)


def _read_inbox_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import live_feeds

    try:
        max_items = int(arguments.get("max_items", 10))
    except (TypeError, ValueError):
        return "ERROR: max_items must be an integer."
    try:
        messages = live_feeds.read_inbox(max_items)
    except RuntimeError as exc:
        return f"INBOX (reported honestly): {exc}"
    if not messages:
        return "INBOX: no messages (honest)."
    lines = [
        f"- from {m['from_']} | {m['subject']} | {m['date']}\n    {m['snippet']}"
        for m in messages
    ]
    return "INBOX (latest first):\n" + "\n".join(lines)


def _list_tasks_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import live_feeds

    include_done = bool(arguments.get("include_done", True))
    try:
        tasks = live_feeds.list_tasks(include_done)
    except RuntimeError as exc:
        return f"TASKS (reported honestly): {exc}"
    if not tasks:
        return "TASKS: none (honest)."
    lines = [
        f"- [{('x' if t['done'] else ' ')}] {t['id']} {t['title']} "
        f"(created {t['created_at']})"
        for t in tasks
    ]
    return "TASKS:\n" + "\n".join(lines)


def _add_task_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import live_feeds

    title = (arguments.get("title") or "").strip()
    if not title:
        return "ERROR: add_task requires a non-empty 'title'."
    try:
        task = live_feeds.add_task(title)
    except RuntimeError as exc:
        return f"TASK ADD FAILED: {exc}"
    return f"TASK ADDED: {task['id']} — {task['title']}"


def _complete_task_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import live_feeds

    task_id = (arguments.get("task_id") or "").strip()
    if not task_id:
        return "ERROR: complete_task requires a 'task_id'."
    try:
        changed = live_feeds.complete_task(task_id)
    except RuntimeError as exc:
        return f"TASK COMPLETE FAILED: {exc}"
    if not changed:
        return f"TASK {task_id}: not found or already done (nothing changed)."
    return f"TASK COMPLETE: {task_id}"


def _schedule_recurring_tool(arguments: dict[str, Any]) -> str:
    """User-defined recurring workflow: schedule a real tool call to repeat.

    The model maps the user's intent to a concrete tool+args ONCE here;
    the SchedulerRunner executes it deterministically afterwards (Rule 2.8).
    """
    from dourmouse import schedules

    tool = (arguments.get("tool") or "").strip()
    args = arguments.get("arguments") or {}
    schedule_text = (arguments.get("schedule_text") or "").strip()
    if not tool:
        return "ERROR: schedule_recurring requires a 'tool' name."
    if not schedule_text:
        return "ERROR: schedule_recurring requires a 'schedule_text' (e.g. 'every Monday at 9:00')."
    if not isinstance(args, dict):
        return "ERROR: 'arguments' must be a JSON object of tool arguments."
    registry = build_general_registry()
    tool_spec = registry.lookup(tool)
    if tool_spec is None:
        return (
            f"ERROR: no such tool: {tool!r}. List the registry first to pick a "
            "real tool name. Nothing was scheduled."
        )
    if tool_spec.permission is not Permission.REGULAR:
        # SchedulerRunner fires unattended (Rule 2.8) — no human is present to
        # answer a confirmation prompt when a schedule comes due. A gated tool
        # scheduled here would either silently never run (safe but confusing)
        # or, worse, bypass its own gate if the runner ever calls the handler
        # directly. Refuse at creation time instead, with an honest reason
        # (Rule 2.2), so the model/human learns immediately, not on a missed
        # 3am run six weeks from now.
        return (
            f"ERROR: cannot schedule {tool!r} — it requires human confirmation "
            f"({tool_spec.permission.value}), which isn't available for "
            "unattended scheduled runs. Only regular-tier tools can be "
            "scheduled. Nothing was scheduled."
        )
    try:
        spec = schedules.parse_schedule(schedule_text)
    except ValueError as exc:
        return f"SCHEDULE REJECTED: {exc}"
    store = schedules.Schedules()
    entry = store.add(tool, args, spec, schedule_text)
    return (
        f"SCHEDULED {entry['id']}: {tool} {schedules.describe_spec(spec)} — "
        f"next run {schedules.describe_next_run(entry)}. The scheduler runner "
        "executes it deterministically; cancel with cancel_schedule "
        f"({entry['id']})."
    )


def _list_schedules_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import schedules

    store = schedules.Schedules()
    entries = store.list()
    if not entries:
        return "SCHEDULES: none."
    lines = []
    for e in entries:
        state = "" if e.get("enabled") else " (disabled)"
        spec = e.get("spec") or {}
        lines.append(
            f"- {e.get('id')}{state}: {e.get('tool')} "
            f"{schedules.describe_spec(spec)} — next {schedules.describe_next_run(e)}"
        )
    return "SCHEDULES:\n" + "\n".join(lines)


def _cancel_schedule_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import schedules

    sid = (arguments.get("schedule_id") or "").strip()
    if not sid:
        return "ERROR: cancel_schedule requires a 'schedule_id'."
    if schedules.Schedules().remove(sid):
        return f"SCHEDULE CANCELLED: {sid}"
    return f"SCHEDULE {sid}: not found (nothing cancelled)."


def _subagent(name: str, domain: str, description: str, tools: list[ToolSpec]) -> Subagent:
    return Subagent(name=name, domain=domain, description=description, tools=tuple(tools))


def _build_delegate_tool(registry: DispatchRegistry) -> ToolSpec:
    """The orchestrator's own tool: spawn a NESTED dispatch run (self-dispatch).

    Reads the active DispatchContext (pushed by run_dispatch_messages) to
    reuse the parent's client, config, confirmation gate, and event sink, and
    to enforce the deterministic recursion guards: max depth and a shared
    total-delegate budget. The nested run is a REAL run_dispatch_messages
    against the same registry — it can call any roster tool, including its
    own delegate_task (bounded by the guards), and its transcript events ride
    the parent's event sink so the UI streams them live.
    """

    def _delegate_task(arguments: dict[str, Any]) -> str:
        ctx = current_dispatch_context(registry)
        if ctx is None:
            return (
                "ERROR: delegate_task requires an active dispatch context "
                "(it can only be called from inside a dispatch run)."
            )
        task = (arguments.get("task") or "").strip()
        if not task:
            return "ERROR: delegate_task requires a non-empty 'task'."
        target = (arguments.get("subagent") or "").strip()
        if target and target not in registry.subagent_names:
            return (
                f"ERROR: unknown subagent {target!r} — cannot delegate. "
                f"Known: {', '.join(sorted(registry.subagent_names))}"
            )
        # Deterministic recursion guards (Rule 2.8).
        if ctx.depth >= ctx.max_depth:
            return (
                f"REFUSED: maximum delegate depth ({ctx.max_depth}) reached — "
                "no deeper nesting allowed."
            )
        if not ctx.consume_delegate():
            return (
                f"REFUSED: delegate budget exhausted (max {ctx.max_delegates} "
                "nested runs per top-level request)."
            )
        try:
            nested_turns = max(1, min(int(arguments.get("max_turns", 5)), 8))
        except (TypeError, ValueError):
            return "ERROR: max_turns must be an integer."

        job_id = None
        if ctx.jobs is not None:
            # Record the TRUE parent chain: this job's parent is the job the
            # current context belongs to (None at top level), so the audit log
            # is a real tree, not a flat list with depth numbers.
            job_id = ctx.jobs.spawn(
                task=task,
                subagent=target or None,
                depth=ctx.depth + 1,
                parent_id=ctx.current_job_id,
            )
        nested_prompt = (
            f"[ROUTING DIRECTIVE] Complete this task using ONLY the "
            f"'{target}' subagent and its tools. TASK: {task}"
            if target
            else task
        )
        # Shared truth (spec: state management & consistent context): the
        # nested agent receives the parent run's recent conversation so it
        # knows what the parent already learned/decided — it does not start
        # from a blank slate.
        if ctx.parent_context:
            nested_prompt += (
                "\n\n[PARENT CONTEXT — read this; it is what the parent "
                "conversation already established]\n" + ctx.parent_context
            )
        # v3.1 per-agent models: when the nested run is routed AT one
        # subagent, it runs on THAT agent's configured NVIDIA model (e.g.
        # DOURMOUSE_MODEL_RESEARCH_INFO), resolved deterministically — never a
        # guess (Rule 2.8). Free sub-orchestration keeps the parent's model.
        nested_model = None
        if target and ctx.config is not None:
            nested_model = ctx.config.model_for_agent(target)
        nested_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": nested_prompt},
        ]
        try:
            report = run_dispatch_messages(
                nested_messages,
                registry,
                max_turns=nested_turns,
                client=ctx.client,
                config=ctx.config,
                confirmation_gate=ctx.confirmation_gate,
                event_sink=ctx.event_sink,
                job_tracker=ctx.jobs,
                depth=ctx.depth + 1,
                max_depth=ctx.max_depth,
                budget=ctx.budget,
                max_delegates=ctx.max_delegates,
                current_job_id=job_id,
                cost_budget=ctx.cost_budget,
                dlp=ctx.dlp,
                rbac=ctx.rbac,
                model=nested_model,
                # v8.12: the ROUTING DIRECTIVE text above already says
                # "using ONLY the 'target' subagent" — forced_agent makes
                # that authoritative instead of re-derived by the general
                # planner, which a comma-heavy task description could
                # otherwise fool into scoping the WRONG agents (traced
                # live: the nested run got no web_search tool, had nothing
                # but its own delegate_task available, and recursed to the
                # depth limit for an empty answer). None when no target was
                # given — a free sub-orchestration still plans normally.
                forced_agent=target or None,
            )
        except Exception as exc:  # honest failure surface (Rule 2.2)
            if ctx.jobs is not None and job_id:
                ctx.jobs.finish(job_id, error=f"nested dispatch failed: {exc}")
            return f"ERROR: nested dispatch failed: {exc}"

        final_text = (report.get("final_text") or "").strip()
        if ctx.jobs is not None and job_id:
            ctx.jobs.finish(job_id, result=final_text)
        truncated = final_text[-_DELEGATE_RESULT_CAP:]
        if len(final_text) > _DELEGATE_RESULT_CAP:
            truncated += "\n[result truncated]"
        head = (
            f"DELEGATED TASK {job_id or '(untracked)'} — "
            f"target={target or 'any'}, depth={ctx.depth + 1} — COMPLETE.\n"
        )
        return head + (truncated or "(no final text — see job for transcript)")

    return ToolSpec(
        name="delegate_task",
        description=(
            "Spawn a NESTED agent run against the same roster (self-dispatch): "
            "a fresh sub-orchestration that can use any registered tool, "
            "depth-bounded and audit-logged as a job. Pass 'subagent' to "
            "route the whole nested run at ONE agent (focus-style), or omit "
            "it for a free sub-orchestration. Use for large or clearly "
            "separable work; otherwise call roster tools directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "subagent": {"type": "string", "default": ""},
                "max_turns": {"type": "integer", "default": 5},
            },
            "required": ["task"],
        },
        handler=_delegate_task,
    )


def build_general_registry() -> DispatchRegistry:
    """Assemble the General-domain subagents (v2.0 Section 4).

    Six Section-4 agents, the laptop-wide ``system`` subagent (full
    filesystem/terminal access with the deterministic danger guard), and the
    ``orchestrator`` subagent whose delegate_task tool lets the lead agent
    spawn NESTED dispatch runs (self-dispatch, depth/budget bounded).
    """
    registry = DispatchRegistry()
    path_note = _sandbox_path_note()

    registry.register_subagent(
        _subagent(
            "orchestrator",
            "Both",
            "Lead orchestrator — spawns nested agent runs via delegate_task.",
            [_build_delegate_tool(registry)],
        )
    )

    registry.register_subagent(
        _subagent(
            "research_info",
            "General",
            "Web search, synthesis, fact-finding (keyless live Wikipedia search).",
            [
                ToolSpec(
                    name="web_search",
                    description=(
                        "Search the live web for a query and return real result "
                        "titles/snippets. Use for fact-finding."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "search query"},
                            "max_results": {"type": "integer", "default": 5},
                        },
                        "required": ["query"],
                    },
                    handler=_web_search_tool,
                ),
                ToolSpec(
                    name="fetch_url",
                    description=(
                        "Fetch a web page and return its readable text "
                        "(crudely HTML-stripped). Use to read a specific URL "
                        "the user or a search result pointed at."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "max_chars": {"type": "integer", "default": 8000},
                        },
                        "required": ["url"],
                    },
                    handler=_fetch_url_tool,
                ),
                ToolSpec(
                    name="open_url",
                    description=(
                        "Open a URL in the user's default browser. Local, "
                        "harmless — use when the user wants to look at a page."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                    handler=_open_url_tool,
                ),
            ],
        )
    )

    registry.register_subagent(
        _subagent(
            "comms",
            "General",
            "Drafts emails/messages. Draft only — sending requires confirmation.",
            [
                ToolSpec(
                    name="draft_message",
                    description=(
                        "Create a message draft (email/slack/etc). Returns a real "
                        "draft saved to the workspace; NEVER sends anything."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "channel": {"type": "string", "default": "email"},
                            "recipient": {"type": "string"},
                            "subject": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["body"],
                    },
                    handler=_draft_message_tool,
                ),
                ToolSpec(
                    name="send_draft",
                    description=(
                        "Send a drafted message. REQUIRES human confirmation; "
                        "currently NOT CONFIGURED (no channel backend yet)."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "channel": {"type": "string", "default": "email"},
                            "recipient": {"type": "string"},
                            "subject": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["body"],
                    },
                    handler=_send_draft_tool,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: (
                        f"Send a {a.get('channel', 'email')} message "
                        f"to {a.get('recipient', '(unspecified)')} with subject "
                        f"{a.get('subject', '(none)')}?"
                    ),
                ),
            ],
        )
    )

    registry.register_subagent(
        _subagent(
            "scheduling",
            "General",
            "Reads calendar, proposes times. Read only — booking requires confirmation.",
            [
                ToolSpec(
                    name="propose_time_slots",
                    description=(
                        "Deterministically propose free time slots for a meeting "
                        "of a given duration over the next N days."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "duration_minutes": {"type": "integer", "default": 30},
                            "days_ahead": {"type": "integer", "default": 5},
                            "start_hour": {"type": "integer", "default": 9},
                            "end_hour": {"type": "integer", "default": 17},
                        },
                    },
                    handler=_propose_time_slots_tool,
                ),
                ToolSpec(
                    name="list_calendar_events",
                    description=(
                        "List calendar events (read-only). Currently NOT "
                        "CONFIGURED until a calendar backend is wired."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_list_calendar_events_tool,
                ),
            ],
        )
    )

    def _sheets_read_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import sheets_read

        try:
            return sheets_read(
                arguments.get("spreadsheet_id", ""),
                arguments.get("sheet", "Sheet1"),
                arguments.get("max_rows", 50),
                arguments.get("max_cols", 20),
            )
        except RuntimeError as exc:
            return f"SHEETS READ (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network/parse failures, readable
            return f"SHEETS READ FAILED: {type(exc).__name__}: {exc}"

    def _drive_download_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import drive_download

        try:
            return drive_download(arguments.get("file_id", ""), arguments.get("dest", ""))
        except RuntimeError as exc:
            return f"DRIVE DOWNLOAD (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network failures, readable
            return f"DRIVE DOWNLOAD FAILED: {type(exc).__name__}: {exc}"

    def _drive_create_doc_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import drive_create_doc

        try:
            return drive_create_doc(
                arguments.get("title", ""),
                arguments.get("content", ""),
            )
        except RuntimeError as exc:
            return f"DRIVE DOC CREATE (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network failures, readable
            return f"DRIVE DOC CREATE FAILED: {type(exc).__name__}: {exc}"

    def _slides_create_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import slides_create

        try:
            return slides_create(
                arguments.get("title", ""),
                arguments.get("slides"),
            )
        except RuntimeError as exc:
            return f"SLIDES CREATE (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network failures, readable
            return f"SLIDES CREATE FAILED: {type(exc).__name__}: {exc}"

    registry.register_subagent(
        _subagent(
            "docs",
            "General",
            "Google Sheets, Drive, and Slides — reads link-shared Sheets, "
            "downloads link-shared Drive items, creates Google Docs and "
            "Slides presentations in the SIGNED-IN user's Drive (real write, "
            "requires confirmation + the Google sign-in with Drive write "
            "scope).",
            [
                ToolSpec(
                    name="sheets_read",
                    description=(
                        "Read a Google Sheet's values as rows (read-only). "
                        "Needs the spreadsheet ID from the URL and the sheet "
                        "name; works when the sheet is shared 'Anyone with "
                        "the link can view'. Private sheets report the exact "
                        "fix instead of fabricating data."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "spreadsheet_id": {"type": "string", "description": "the token in the sheet URL between /d/ and /edit"},
                            "sheet": {"type": "string", "default": "Sheet1"},
                            "max_rows": {"type": "integer", "default": 50},
                            "max_cols": {"type": "integer", "default": 20},
                        },
                        "required": ["spreadsheet_id"],
                    },
                    handler=_sheets_read_h,
                ),
                ToolSpec(
                    name="drive_download",
                    description=(
                        "Download a link-shared Google Drive item by its ID "
                        "(read-only). Works when the item is shared 'Anyone "
                        "with the link'; private items report the exact fix. "
                        "Writes into the uploads folder unless a dest path "
                        "is given."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "file_id": {"type": "string", "description": "the token in the file URL between /d/ and /view"},
                            "dest": {"type": "string", "description": "optional save path (default: uploads sandbox)"},
                        },
                        "required": ["file_id"],
                    },
                    handler=_drive_download_h,
                ),
                ToolSpec(
                    name="drive_create_doc",
                    description=(
                        "Create a Google Doc in the SIGNED-IN user's Google "
                        "Drive with the given title and optional content, and "
                        "return its open link. REAL write — REQUIRES human "
                        "confirmation. Needs the Google sign-in with Drive "
                        "write scope (GOOGLE_OAUTH_FULL_SCOPES=1); reports NOT "
                        "CONFIGURED honestly without a signed-in user."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "content": {"type": "string", "description": "optional text body for the doc"},
                        },
                        "required": ["title"],
                    },
                    handler=_drive_create_doc_h,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: (
                        f"Create a Google Doc titled {a.get('title', '?')!r} "
                        f"in your Drive ({(a.get('content') or '')[:80]}...)?"
                    ),
                ),
                ToolSpec(
                    name="slides_create",
                    description=(
                        "Create a Google Slides presentation in the SIGNED-IN "
                        "user's Drive with a title and a list of slides, each "
                        "{title, body}. Returns the open link. REAL write — "
                        "REQUIRES human confirmation. Needs the Google "
                        "sign-in with Drive write scope "
                        "(GOOGLE_OAUTH_FULL_SCOPES=1); reports NOT CONFIGURED "
                        "honestly without a signed-in user."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "slides": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "body": {"type": "string"},
                                    },
                                },
                                "description": "list of {title, body} slides",
                            },
                        },
                        "required": ["title"],
                    },
                    handler=_slides_create_h,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: (
                        f"Create a Slides deck titled {a.get('title', '?')!r} "
                        f"({len(a.get('slides') or [])} slides) in your Drive?"
                    ),
                ),
            ],
        )
    )

    registry.register_subagent(
        _subagent(
            "dev_coding",
            "General",
            "Writes, tests, debugs code. Deploy/publish requires confirmation.",
            [
                ToolSpec(
                    name="run_python",
                    description=(
                        "Execute a Python snippet in a sandboxed workspace "
                        "subprocess and return the REAL stdout/stderr/exit code."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "timeout_seconds": {"type": "integer", "default": 30},
                        },
                        "required": ["code"],
                    },
                    handler=_run_python_tool,
                ),
                ToolSpec(
                    name="read_file",
                    description="Read a text file from the workspace sandbox." + path_note,
                    parameters={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    handler=_read_file_tool,
                ),
                ToolSpec(
                    name="write_file",
                    description=(
                        "Write a text file inside the workspace sandbox. When the "
                        "file already exists, the result includes a unified diff "
                        "of exactly what changed." + path_note
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                    handler=_write_file_tool,
                ),
                ToolSpec(
                    name="search_files",
                    description=(
                        "grep-style content search across the workspace sandbox "
                        "(file:line:match). Use to find where something is "
                        "defined, referenced, or mentioned before editing." + path_note
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "path": {"type": "string", "default": "."},
                            "max_results": {"type": "integer", "default": 50},
                        },
                        "required": ["query"],
                    },
                    handler=_search_files_tool,
                ),
                ToolSpec(
                    name="diff_preview",
                    description=(
                        "Show a unified diff of a PROPOSED write against the "
                        "current file, WITHOUT writing anything. Use before "
                        "overwriting an existing file." + path_note
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                    handler=_diff_preview_tool,
                ),
                ToolSpec(
                    name="edit_file",
                    description=(
                        "Targeted str-replace edit inside one workspace file. "
                        "old_str must occur EXACTLY once; ambiguous or missing "
                        "matches are refused (no silent multi-match edits). "
                        "Returns the resulting unified diff." + path_note
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_str": {"type": "string"},
                            "new_str": {"type": "string"},
                        },
                        "required": ["path", "old_str", "new_str"],
                    },
                    handler=_edit_file_tool,
                ),
                ToolSpec(
                    name="claude_code",
                    description=(
                        "Delegate a coding task to the user's REAL Claude Code "
                        "CLI (headless `claude -p` mode) and return its real "
                        "output. Use for complex code work, debugging, or "
                        "codebase reasoning that benefits from Claude Code. "
                        "Requires the 'claude' CLI on PATH or CLAUDE_CODE_CLI "
                        "set — honestly NOT CONFIGURED if not found. Note: "
                        "headless mode runs with default permissions, so "
                        "permission-gated file edits are typically declined."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "cwd": {"type": "string", "default": str(_PROJECT_ROOT)},
                            "timeout_seconds": {"type": "integer", "default": 300},
                        },
                        "required": ["task"],
                    },
                    handler=_claude_code_tool,
                ),
                ToolSpec(
                    name="codex_code",
                    description=(
                        "Delegate a coding task to the user's REAL Codex CLI "
                        "(headless `codex exec` mode, using the ChatGPT login "
                        "already in ~/.codex/auth.json — no API key needed) "
                        "and return its real output. Use for complex code work "
                        "that benefits from Codex. Requires the 'codex' CLI on "
                        "PATH or CODEX_CLI set — honestly NOT CONFIGURED if "
                        "not found. Usage limits surface honestly at run "
                        "time. Note: `codex exec` runs with the CLI's "
                        "configured sandbox permissions (~/.codex/config.toml) "
                        "— file writes follow that policy, so permission-gated "
                        "edits may be declined."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "cwd": {"type": "string", "default": str(_PROJECT_ROOT)},
                            "timeout_seconds": {"type": "integer", "default": 300},
                        },
                        "required": ["task"],
                    },
                    handler=_codex_code_tool,
                ),
                ToolSpec(
                    name="deploy",
                    description=(
                        "Deploy or publish code. REQUIRES confirmation; "
                        "currently NOT CONFIGURED."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                    },
                    handler=_deploy_tool,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: f"Deploy to {a.get('target', '(unspecified)')}?",
                ),
            ],
)
    )

    registry.register_subagent(
        _subagent(
            "admin_ops",
            "General",
            "File organization, cleanup. Deletion requires per-item confirmation.",
            [
                ToolSpec(
                    name="list_files",
                    description=(
                        "List files/dirs in the workspace sandbox (read-only). "
                        "The sandbox root IS the workspace; pass '.' or omit "
                        "path to list it, or pass a path RELATIVE to the root "
                        "(e.g. 'docs/'). Never guess absolute paths — they are "
                        "refused." + path_note
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"path": {"type": "string", "default": "."}},
                    },
                    handler=_list_files_tool,
                ),
                ToolSpec(
                    name="delete_file",
                    description=(
                        "Delete ONE file inside the workspace sandbox. "
                        "REQUIRES per-item human confirmation before it runs." + path_note
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    handler=_delete_file_tool,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: f"Permanently delete workspace file {a.get('path')!r}?",
                ),
            ],
        )
    )

    registry.register_subagent(build_system_subagent())

    registry.register_subagent(_build_messenger_subagent(registry))

    registry.register_subagent(_build_memory_subagent(registry))

    # -- v2.4 coding subagents, one per LLM backend -------------------- #
    # v4.0: code_ollama routes to the local Ollama server (keyless, free,
    # fully local) — added BEFORE nvidia so the roster stays deterministic.
    registry.register_subagent(
        _subagent(
            "code_ollama",
            "Coding",
            "Coding via the local Ollama LLM backend (keyless, zero API spend).",
            [_make_code_tool("ollama")],
        )
    )
    registry.register_subagent(
        _subagent(
            "code_nvidia",
            "Coding",
            "Coding via the NVIDIA NIM LLM backend (OpenAI-compatible).",
            [_make_code_tool("nvidia")],
        )
    )
    registry.register_subagent(
        _subagent(
            "code_deepseek",
            "Coding",
            "Coding via the Freebuff free DeepSeek backend (OpenAI-compatible).",
            [_make_code_tool("deepseek")],
        )
    )
    registry.register_subagent(
        _subagent(
            "code_codex",
            "Coding",
            "Coding via the OpenAI Codex API (OpenAI-compatible).",
            [_make_code_tool("codex")],
        )
    )
    registry.register_subagent(
        _subagent(
            "code_claude",
            "Coding",
            "Coding via your real Claude Code CLI (claude -p).",
            [_make_code_tool("claude")],
        )
    )

    # -- v4.0 ATLAS command-centre agent ------------------------------- #
    # Real telemetry about the ATLAS quant repo: status, FX-archive
    # bootstrap progress, deliverables. Deterministic (Rule 2.8), honest
    # NOT CONFIGURED when ATLAS_REPO_PATH is unset (Rule 2.2).
    from dourmouse.atlas_ops import build_atlas_tool_specs

    registry.register_subagent(
        _subagent(
            "atlas",
            "Projects",
            "ATLAS quant repo telemetry — status, FX bootstrap progress, deliverables.",
            build_atlas_tool_specs(),
        )
    )

    # -- v5.5 Freebuff read agent -------------------------------------- #
    # Real access to the user's Freebuff Desktop app (loopback API):
    # read tools (account, projects, thread conversations, notes, skills,
    # git changes) + the v5.11 write tool freebuff_dispatch (create ONE
    # new thread + post ONE prompt so a real Freebuff agent runs the task
    # there). Honest NOT CONFIGURED when the app is not running/authed
    # (Rule 2.2).
    from dourmouse.freebuff_bridge import build_freebuff_tool_specs

    registry.register_subagent(
        _subagent(
            "freebuff",
            "Projects",
            "Freebuff Desktop — read threads/projects/notes/skills AND dispatch tasks into new Freebuff threads.",
            build_freebuff_tool_specs(),
        )
    )

    # v5.12 World Monitor — real-time global intelligence (worldmonitor.app):
    # market data, country risk/briefs, conflict events, news intelligence,
    # natural disasters, cyber threats, sanctions, forecasts, supply-chain.
    # Keyless: status + tool catalog. Keyed: generic call_tool to all 59 MCP
    # tools. Honest NOT CONFIGURED without WORLDMONITOR_API_KEY (Rule 2.2).
    # v5.27: + world_pulse / world_pulse_details — Dourmouse's OWN self-
    # hosted keyless monitor (dourmouse/world_pulse.py, no SDK, no key).
    from dourmouse.worldmonitor import build_worldmonitor_tool_specs

    def _world_pulse_h(arguments: dict[str, Any]) -> str:
        from dourmouse.world_pulse import world_pulse_snapshot

        try:
            snap = world_pulse_snapshot()
        except Exception as exc:  # noqa: BLE001 - snapshot must never raise
            return f"WORLD PULSE FAILED: {type(exc).__name__}: {exc}"
        lines = [
            f"WORLD PULSE {snap.get('pulse_score')} {snap.get('pulse_label')} "
            f"({snap.get('generated_at', '')[:16]} UTC)",
        ]
        for name, src in snap.get("sources", {}).items():
            state = (
                f"{src.get('count', 0)} items · {src.get('latency_ms', '?')}ms"
                if src.get("ok")
                else f"OFFLINE — {src.get('error', 'no error')}"
            )
            lines.append(f"- {name.upper()}: {state}")
        lines.append("ITEMS:")
        for kind, items in snap.get("items", {}).items():
            for it in items[:3]:
                sev = f"[{it.get('severity')}] " if it.get("severity") else ""
                lines.append(f"- {kind.upper()} {sev}{it.get('title', '')}")
        return "\n".join(lines)

    def _world_pulse_details_h(arguments: dict[str, Any]) -> str:
        from dourmouse.world_pulse import world_pulse_details

        try:
            det = world_pulse_details(arguments.get("source", ""))
        except Exception as exc:  # noqa: BLE001
            return f"WORLD PULSE DETAILS FAILED: {type(exc).__name__}: {exc}"
        if not det.get("ok"):
            return f"WORLD PULSE DETAILS (reported honestly): {det.get('error')}"
        lines = [
            f"SOURCE {det['source'].upper()} — {det.get('label', '')}",
            f"HEALTH: {det['health']}",
        ]
        for it in det.get("items", []):
            sev = f"[{it.get('severity')}] " if it.get("severity") else ""
            lines.append(f"- {sev}{it.get('title', '')} {it.get('link', '')}")
        return "\n".join(lines)

    # v8.20: two more real, deterministic tools on top of World Pulse —
    # neither calls an LLM; both compose the same real snapshot/geo data
    # already produced above into a different shape.
    def _world_brief_h(arguments: dict[str, Any]) -> str:
        from dourmouse.world_brief import generate_brief
        from dourmouse.world_pulse import world_pulse_snapshot

        try:
            brief = generate_brief(world_pulse_snapshot())
        except Exception as exc:  # noqa: BLE001 - must never raise into the model
            return f"WORLD BRIEF FAILED: {type(exc).__name__}: {exc}"
        return brief.get("text", "")

    def _world_correlations_h(arguments: dict[str, Any]) -> str:
        from dourmouse.world_correlation import find_correlations
        from dourmouse.world_pulse import world_pulse_geo

        try:
            pairs = find_correlations(world_pulse_geo())
        except Exception as exc:  # noqa: BLE001
            return f"WORLD CORRELATIONS FAILED: {type(exc).__name__}: {exc}"
        if not pairs:
            return "No cross-channel correlations within the current threshold right now."
        lines = ["CROSS-CHANNEL PROXIMITY (closest first):"]
        for p in pairs[:10]:
            a, b = p["a"], p["b"]
            lines.append(
                f"- {p['distance_km']:.0f} km apart: [{a['chan']}] {a['title']} "
                f"<-> [{b['chan']}] {b['title']}"
            )
        return "\n".join(lines)

    registry.register_subagent(
        _subagent(
            "worldmonitor",
            "Intelligence",
            "World Monitor — real-time global intelligence: markets, country risk, conflicts, disasters, cyber, sanctions, forecasts. Includes the SELF-HOSTED keyless World Pulse feed.",
            build_worldmonitor_tool_specs()
            + [
                ToolSpec(
                    name="world_pulse",
                    description=(
                        "Dourmouse's OWN world monitor: the self-hosted keyless "
                        "snapshot of markets (Yahoo movers + key quotes), world "
                        "news (Google News), disasters (GDACS), cyber advisories "
                        "(CISA), conflict/humanitarian updates (ReliefWeb) and "
                        "macro (World Bank), plus the internal pulse score. Real "
                        "data, no API key, failure-isolated per source."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_world_pulse_h,
                ),
                ToolSpec(
                    name="world_pulse_details",
                    description=(
                        "Drill into ONE World Pulse source for its raw items: "
                        "markets, news, disasters, cyber, conflict, or macro. "
                        "Returns the items with links plus the source's health."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "source": {"type": "string", "description": "markets|news|disasters|cyber|conflict|macro"}
                        },
                        "required": ["source"],
                    },
                    handler=_world_pulse_details_h,
                ),
                ToolSpec(
                    name="world_brief",
                    description=(
                        "A short, real, deterministic written brief ('what "
                        "happened') composed from the current World Pulse "
                        "snapshot — pulse score, the most severe real items "
                        "per channel, and any channel that's OFFLINE. Never "
                        "an LLM summary; every fact in it traces to real "
                        "data in the snapshot."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_world_brief_h,
                ),
                ToolSpec(
                    name="world_correlations",
                    description=(
                        "Real pairs of located World Pulse items from "
                        "DIFFERENT channels that are geographically close "
                        "(e.g. a wildfire near a flight path), closest "
                        "first. Reports proximity only — never a causal "
                        "claim."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_world_correlations_h,
                ),
            ],
        )
    )

    # -- v5.7 Spotify wrappers ---------------------------------------- #
    # Thin adapters around dourmouse.spotify_services — the module already
    # returns honest text (NOT CONFIGURED / NOT LINKED / real API errors),
    # so the wrapper only normalizes exceptions to text (Rule 2.2).
    def _spotify_wrap(fn):
        def _call(arguments: dict[str, Any]) -> str:
            try:
                return fn(arguments)
            except RuntimeError as exc:
                return f"SPOTIFY (reported honestly): {exc}"
        return _call

    def _spotify_link_tool(arguments: dict[str, Any]) -> str:
        from dourmouse.spotify_services import spotify_login

        return spotify_login(background=True)

    def _spotify_now_playing_tool(arguments: dict[str, Any]) -> str:
        from dourmouse.spotify_services import now_playing

        return now_playing()

    def _spotify_state_tool(arguments: dict[str, Any]) -> str:
        from dourmouse.spotify_services import playback_state

        return playback_state()

    def _spotify_control_tool(arguments: dict[str, Any]) -> str:
        from dourmouse.spotify_services import playback_control

        return playback_control(str(arguments.get("action") or ""))

    def _spotify_play_tool(arguments: dict[str, Any]) -> str:
        from dourmouse.spotify_services import play_uri

        return play_uri(str(arguments.get("uri") or ""))

    def _spotify_search_tool(arguments: dict[str, Any]) -> str:
        from dourmouse.spotify_services import search_tracks

        return search_tracks(
            str(arguments.get("query") or ""),
            int(arguments.get("limit", 5)),
        )

    def _spotify_top_tool(arguments: dict[str, Any]) -> str:
        from dourmouse.spotify_services import top_tracks

        return top_tracks(
            str(arguments.get("time_range") or "medium_term"),
            int(arguments.get("limit", 5)),
        )

    def _spotify_recent_tool(arguments: dict[str, Any]) -> str:
        from dourmouse.spotify_services import recently_played

        return recently_played(int(arguments.get("limit", 10)))

    def _spotify_playlists_tool(arguments: dict[str, Any]) -> str:
        from dourmouse.spotify_services import list_playlists

        return list_playlists(int(arguments.get("limit", 20)))

    # -- v6.0 forex-data pipeline agent -------------------------------- #
    # Real telemetry from the forex research pipeline (FOREX_DATA_PATH):
    # data inventory, the validated commodity-seasonal strategy + live
    # paper calendar, upcoming economic events, the paper log, and IBKR
    # gateway reachability. Deterministic (Rule 2.8), honest NOT
    # CONFIGURED when FOREX_DATA_PATH is unset (Rule 2.2).
    from dourmouse.forex_ops import build_forex_tool_specs

    registry.register_subagent(
        _subagent(
            "forex",
            "Projects",
            "forex-data pipeline telemetry — inventory, seasonal strategy, events, paper log, IBKR gateway.",
            build_forex_tool_specs(),
        )
    )

    # -- v8.2 Trading 212 broker agent -------------------------------- #
    # Real account + order access via the official T212 Public API
    # (demo/live, X-Api-Token). Paper-first: t212_order refuses without an
    # explicit paper_confirm=true, and live is double-gated. Honest NOT
    # CONFIGURED without T212_API_KEY (Rule 2.2). Equity/ISA scope only —
    # the API does not expose CFDs, so the seasonal CFD legs stay manual
    # or via IBKR.
    from dourmouse.trading212_ops import build_t212_tool_specs

    registry.register_subagent(
        _subagent(
            "t212",
            "Projects",
            "Trading 212 broker — real account summary, open positions, portfolio, and paper-first order placement (demo/live, double-gated).",
            build_t212_tool_specs(),
        )
    )

    # -- v8.3 MetaTrader 5 paper broker agent ------------------------- #
    # Low-friction paper venue: free MT5 demo accounts come with real-time
    # quotes and simulated fills (no data subscriptions, no futures margin
    # floors), and MT5 brokers commonly list the ag CFDs the seasonal
    # strategy trades. Paper-first: mt5_order refuses without
    # paper_confirm=true; live accounts are double-gated.
    from dourmouse.mt5_ops import build_mt5_tool_specs

    registry.register_subagent(
        _subagent(
            "mt5",
            "Projects",
            "MetaTrader 5 paper broker — status, seasonal-universe symbol availability, live quotes, and paper-first orders on a free demo account (no subscriptions, no margin floors).",
            build_mt5_tool_specs(),
        )
    )

    # -- v8.0 ATLAS Terminal agent ------------------------------------ #
    # What the ATLAS Terminal (streamlit, atlas_terminal/) shows right now.
    from dourmouse.atlas_ui_ops import build_atlas_ui_tool_specs

    registry.register_subagent(
        _subagent(
            "atlas_ui",
            "Projects",
            "ATLAS Terminal status — what the streamlit terminal would show now (validation, next trade, events, paper, IBKR).",
            build_atlas_ui_tool_specs(),
        )
    )

    # -- v8.1 ATLAS Command Center ------------------------------------- #
    # RUN the real research pipeline from here: validation suite, walk-
    # forward, backtest, paper log, calendar, events refresh. Also owns
    # the locked STANDARD (reports/validation_standard.json).
    from dourmouse.atlas_command import build_atlas_cmd_tool_specs

    registry.register_subagent(
        _subagent(
            "atlas_cmd",
            "Projects",
            "ATLAS Command Center — run the research pipeline (validation suite, backtest, paper log, calendar) and read the locked standard.",
            build_atlas_cmd_tool_specs(),
        )
    )

    # -- v2.3 preloaded live-intelligence agents ----------------------- #
    registry.register_subagent(
        _subagent(
            "news",
            "Live",
            "Live news feed — keyless Google News headlines on demand.",
            [
                ToolSpec(
                    name="news_headlines",
                    description=(
                        "Fetch LIVE top headlines (keyless Google News RSS). "
                        "Returns real {title, source, published} rows. Use for "
                        "'what's happening now' — no search engine needed."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "max_results": {"type": "integer", "default": 10},
                        },
                    },
                    handler=_news_headlines_tool,
                ),
                ToolSpec(
                    name="news_search",
                    description=(
                        "Search LIVE news for a specific topic, event, team, "
                        "match or person (keyless Google News RSS). Use this "
                        "for sports scores and results, elections, and any "
                        "'what happened with X' question. This is the correct "
                        "tool for anything current that is NOT a stock — "
                        "stock_quote only understands tradeable tickers."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "What to look for, e.g. "
                                    "'Bangladesh vs Australia cricket score'."
                                ),
                            },
                            "max_results": {"type": "integer", "default": 10},
                        },
                        "required": ["query"],
                    },
                    handler=_news_search_tool,
                ),
            ],
        )
    )

    registry.register_subagent(
        _subagent(
            "markets",
            "Live",
            "Live market data — Yahoo Finance quotes + top day gainers/losers.",
            [
                ToolSpec(
                    name="stock_quote",
                    description=(
                        "Real quote for one stock symbol (Yahoo Finance, keyless): "
                        "price, currency, day range, 52-week range. Returns REAL "
                        "market data, never fabricated numbers."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string", "description": "e.g. AAPL"},
                        },
                        "required": ["symbol"],
                    },
                    handler=_stock_quote_tool,
                ),
                ToolSpec(
                    name="market_movers",
                    description=(
                        "Top day GAINERS or LOSERS / biggest movers today "
                        "(Yahoo Finance screener, keyless, instant): symbol, "
                        "name, price, change, change_pct. USE THIS for any "
                        "market-movers / top-gainers / top-losers / "
                        "biggest-movers / hot-stocks request — do NOT route "
                        "it to web search."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "direction": {
                                "type": "string",
                                "default": "gainers",
                                "description": "'gainers' or 'losers'",
                            },
                            "count": {"type": "integer", "default": 10},
                        },
                    },
                    handler=_market_movers_tool,
                ),
            ],
        )
    )

    # -- v5.7 Spotify music agent -------------------------------------- #
    # The user's Spotify account: read-only taste/history tools plus
    # confirmation-gated playback control (changes state on the account, so a
    # human always approves — Rule 2.9). Honest NOT CONFIGURED until a Client
    # ID is set and the account is linked once (PKCE login).
    registry.register_subagent(
        _subagent(
            "music",
            "Media",
            "Spotify — now playing, playback control, search, top/recent tracks, playlists.",
            [
                ToolSpec(
                    name="spotify_link",
                    description=(
                        "One-time linking to the user's Spotify account: opens the "
                        "browser for approval (background). Requires SPOTIFY_CLIENT_ID "
                        "to be set first. Re-run after linking to check."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_spotify_link_tool,
                ),
                ToolSpec(
                    name="spotify_now_playing",
                    description=(
                        "What is CURRENTLY playing on the linked Spotify account "
                        "(track, artists, progress) — or honestly 'nothing playing'."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_spotify_wrap(_spotify_now_playing_tool),
                ),
                ToolSpec(
                    name="spotify_playback_state",
                    description=(
                        "Current Spotify playback state: device, shuffle, repeat, "
                        "volume, and the selected track."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_spotify_wrap(_spotify_state_tool),
                ),
                ToolSpec(
                    name="spotify_playback_control",
                    description=(
                        "CONTROL Spotify playback: next | previous | pause | resume | "
                        "volume <0-100>. Confirmation-gated (changes the user's "
                        "playback). Requires Spotify Premium."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "description": "next | previous | pause | resume | volume <0-100>"},
                        },
                        "required": ["action"],
                    },
                    handler=_spotify_control_tool,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: (
                        f"Control Spotify playback: {a.get('action', '?')}"
                    ),
                ),
                ToolSpec(
                    name="spotify_play",
                    description=(
                        "Start playback of a spotify: track/album/playlist URI on an "
                        "active device. Confirmation-gated. Requires Spotify Premium. "
                        "NEVER invent a URI: call spotify_playlists (for the user's "
                        "playlists) or spotify_search (for tracks/albums/artists) "
                        "FIRST and use the exact URI they return. Fabricated playlist "
                        "URIs fail with an honest error listing the real playlists."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string", "description": "e.g. spotify:track:4uLU6hMCjMI75M1A2tKUQC"},
                        },
                        "required": ["uri"],
                    },
                    handler=_spotify_play_tool,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: f"Play on Spotify: {a.get('uri', '?')}",
                ),
                ToolSpec(
                    name="spotify_search",
                    description=(
                        "Search Spotify for TRACKS/ALBUMS/ARTISTS (public "
                        "catalog only). Returns real matches with spotify: "
                        "URIs you can play. NOTE: the user's OWN playlists are "
                        "NOT searchable here — for 'my playlist' requests use "
                        "spotify_playlists, which returns the user's actual "
                        "playlists with exact URIs."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "e.g. daft punk"},
                            "limit": {"type": "integer", "default": 5},
                        },
                        "required": ["query"],
                    },
                    handler=_spotify_search_tool,
                ),
                ToolSpec(
                    name="spotify_top_tracks",
                    description=(
                        "The user's most-played tracks (short_term | medium_term | "
                        "long_term) — listening-habit analytics."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "time_range": {"type": "string", "default": "medium_term"},
                            "limit": {"type": "integer", "default": 5},
                        },
                    },
                    handler=_spotify_top_tool,
                ),
                ToolSpec(
                    name="spotify_recently_played",
                    description=(
                        "The user's recently played tracks, newest first — what "
                        "they've been listening to lately."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "default": 10},
                        },
                    },
                    handler=_spotify_recent_tool,
                ),
                ToolSpec(
                    name="spotify_playlists",
                    description=(
                        "The user's OWN Spotify playlists with track counts and "
                        "exact URIs. MANDATORY when the user asks to play or "
                        "list 'my playlist' / 'my playlists' — the user's "
                        "playlists are NOT in spotify_search results (search "
                        "only returns public Spotify playlists, never the "
                        "user's private ones). Look the playlist up here FIRST "
                        "and pass its exact URI to spotify_play."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "default": 20,
                                        "description": "Number of playlists to "
                                        "return. When the user names a SPECIFIC "
                                        "playlist, use the default (20) or "
                                        "larger — a small limit (e.g. 1) hides "
                                        "the playlist you are looking for."},
                        },
                    },
                    handler=_spotify_playlists_tool,
                ),
            ],
        )
    )

    registry.register_subagent(
        _subagent(
            "rnd",
            "Live",
            "R&D — pulls live news/market intel and researches for the roster itself.",
            [
                ToolSpec(
                    name="research_news",
                    description=(
                        "Fetch LIVE top headlines (keyless Google News RSS) for "
                        "R&D research scans. Same live data as the news agent's "
                        "news_headlines."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "max_results": {"type": "integer", "default": 10},
                        },
                    },
                    handler=_news_headlines_tool,
                ),
                ToolSpec(
                    name="research_quote",
                    description="Real quote for one symbol (Yahoo Finance, keyless).",
                    parameters={
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                        },
                        "required": ["symbol"],
                    },
                    handler=_stock_quote_tool,
                ),
                ToolSpec(
                    name="research_movers",
                    description="Top day gainers/losers (Yahoo Finance screener, keyless).",
                    parameters={
                        "type": "object",
                        "properties": {
                            "direction": {"type": "string", "default": "gainers"},
                            "count": {"type": "integer", "default": 10},
                        },
                    },
                    handler=_market_movers_tool,
                ),
                ToolSpec(
                    name="research_web_search",
                    description=(
                        "Search the live web (keyless) for research — the roster's "
                        "own R&D loop uses this to study new capabilities."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "max_results": {"type": "integer", "default": 5},
                        },
                        "required": ["query"],
                    },
                    handler=_web_search_tool,
                ),
                ToolSpec(
                    name="research_fetch_url",
                    description="Fetch a web page and return readable text (research reads).",
                    parameters={
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "max_chars": {"type": "integer", "default": 8000},
                        },
                        "required": ["url"],
                    },
                    handler=_fetch_url_tool,
                ),
            ],
        )
    )

    def _gmail_search_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import gmail_search

        try:
            return gmail_search(arguments.get("query", ""), arguments.get("max_results", 10))
        except RuntimeError as exc:
            return f"GMAIL SEARCH (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network/IMAP failures, readable
            return f"GMAIL SEARCH FAILED: {type(exc).__name__}: {exc}"

    def _gmail_read_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import gmail_read

        try:
            return gmail_read(arguments.get("message_id", ""))
        except RuntimeError as exc:
            return f"GMAIL READ (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network/IMAP failures, readable
            return f"GMAIL READ FAILED: {type(exc).__name__}: {exc}"

    def _gmail_archive_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import gmail_archive

        try:
            return gmail_archive(arguments.get("message_id", ""))
        except RuntimeError as exc:
            return f"GMAIL ARCHIVE (reported honestly): {exc}"

    def _gmail_trash_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import gmail_trash

        try:
            return gmail_trash(arguments.get("message_id", ""))
        except RuntimeError as exc:
            return f"GMAIL TRASH (reported honestly): {exc}"

    def _gmail_untrash_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import gmail_untrash

        try:
            return gmail_untrash(arguments.get("message_id", ""))
        except RuntimeError as exc:
            return f"GMAIL UNTRASH (reported honestly): {exc}"

    def _gmail_send_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import gmail_send

        try:
            return gmail_send(
                arguments.get("to", ""),
                arguments.get("subject", ""),
                arguments.get("body", ""),
            )
        except RuntimeError as exc:
            return f"GMAIL SEND (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network/SMTP failures, readable
            return f"GMAIL SEND FAILED: {type(exc).__name__}: {exc}"

    def _email_identity_status_h() -> str:
        from dourmouse.email_identity import identity_status

        try:
            s = identity_status()
        except Exception as exc:  # noqa: BLE001 - env reads, never fatal
            return f"EMAIL IDENTITY FAILED: {type(exc).__name__}: {exc}"
        lines = [
            f"IDENTITY: {s['name']}",
            f"BASE ACCOUNT: {s['base_address'] or '(none configured)'}",
            f"OWN ADDRESS: {s['own_address'] or '(none — set DOURMOUSE_EMAIL_ADDRESS or configure Gmail)'}",
            f"SENDER MODE: {s['sender_mode']}",
        ]
        if s["smtp_identity"]:
            m = s["smtp_identity"]
            lines.append(f"DEDICATED SMTP: {m['from']} via {m['host']}:{m['port']} (tls={m['tls']})")
        if s["alias_note"]:
            lines.append(f"ALIAS NOTE: {s['alias_note']}")
        return "\n".join(lines)

    def _email_own_send_h(arguments: dict[str, Any]) -> str:
        from dourmouse.email_identity import email_send_via_smtp

        try:
            return email_send_via_smtp(
                arguments.get("to", ""),
                arguments.get("subject", ""),
                arguments.get("body", ""),
            )
        except RuntimeError as exc:
            return f"EMAIL OWN SEND (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network/SMTP failures, readable
            return f"EMAIL OWN SEND FAILED: {type(exc).__name__}: {exc}"

    def _drive_search_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import drive_search

        try:
            return drive_search(arguments.get("query", ""), arguments.get("max_results", 10))
        except RuntimeError as exc:
            return f"DRIVE SEARCH (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network failures, readable
            return f"DRIVE SEARCH FAILED: {type(exc).__name__}: {exc}"

    def _drive_read_h(arguments: dict[str, Any]) -> str:
        from dourmouse.google_services import drive_read

        try:
            return drive_read(arguments.get("file_id", ""))
        except RuntimeError as exc:
            return f"DRIVE READ (reported honestly): {exc}"
        except Exception as exc:  # noqa: BLE001 - network failures, readable
            return f"DRIVE READ FAILED: {type(exc).__name__}: {exc}"

    # ---- Browser agent (v5.25): real headless Chrome via Playwright --------
    def _browser_h(tool: str) -> Callable[[dict[str, Any]], str]:
        def _handler(arguments: dict[str, Any]) -> str:
            from dourmouse.browser_agent import (
                browser_back,
                browser_click,
                browser_creds_forget,
                browser_creds_list,
                browser_creds_store,
                browser_extract,
                browser_fill,
                browser_fill_form,
                browser_open,
                browser_press,
                browser_screenshot,
                browser_select,
                browser_signin,
                browser_snapshot,
                browser_submit,
                browser_wait,
            )

            _FN = {
                "open": browser_open,
                "snapshot": browser_snapshot,
                "fill": browser_fill,
                "fill_form": browser_fill_form,
                "click": browser_click,
                "select": browser_select,
                "press": browser_press,
                "submit": browser_submit,
                "wait": browser_wait,
                "back": browser_back,
                "extract": browser_extract,
                "screenshot": browser_screenshot,
                "creds_store": browser_creds_store,
                "creds_list": browser_creds_list,
                "creds_forget": browser_creds_forget,
                "signin": browser_signin,
            }
            try:
                return _FN[tool](arguments)
            except RuntimeError as exc:
                return f"BROWSER (reported honestly): {exc}"
            except Exception as exc:  # noqa: BLE001 - driver failures, readable
                return f"BROWSER FAILED: {type(exc).__name__}: {exc}"

        return _handler

    _b_confirm = lambda a: (  # noqa: E731 - shared confirm prompt builder
        f"{a.get('note', 'Submit the active form')} — site: "
        f"{a.get('site', 'current page')}? (browser agent)"
    )

    registry.register_subagent(
        _subagent(
            "browser",
            "General",
            "Drives a real headless Chrome (Playwright + system Google Chrome) "
            "to open pages, fill forms, sign up and log in. Submitting forms, "
            "logging in, and storing credentials always require confirmation.",
            [
                ToolSpec(
                    name="browser_open",
                    description=(
                        "Open a URL in the agent's real Chrome and report the "
                        "page: URL, title, and interactive elements. Only "
                        "http(s) URLs are ever opened. Use first, then drive "
                        "the page with browser_fill / browser_click / "
                        "browser_submit."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                    handler=_browser_h("open"),
                ),
                ToolSpec(
                    name="browser_snapshot",
                    description=(
                        "Read the CURRENT page state: URL, title, and every "
                        "interactive element with its label/placeholder/value "
                        "plus a text sample. Use before filling or clicking so "
                        "you target real labels."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"max_elements": {"type": "integer", "default": 60}},
                    },
                    handler=_browser_h("snapshot"),
                ),
                ToolSpec(
                    name="browser_fill",
                    description=(
                        "Fill ONE form field on the current page by its label, "
                        "placeholder, button name, CSS selector (prefix 'css:'), "
                        "or visible text."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "target": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["target", "value"],
                    },
                    handler=_browser_h("fill"),
                ),
                ToolSpec(
                    name="browser_fill_form",
                    description=(
                        "Fill MANY form fields at once for signup flows: an "
                        "object of label -> value (labels from browser_snapshot). "
                        "Nothing is submitted until browser_submit (which needs "
                        "confirmation)."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"fields": {"type": "object", "description": "label -> value map"}},
                        "required": ["fields"],
                    },
                    handler=_browser_h("fill_form"),
                ),
                ToolSpec(
                    name="browser_click",
                    description=(
                        "Click an element on the current page by its label, "
                        "button name, link text, CSS selector (prefix 'css:'), "
                        "or visible text. Reports the resulting page."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                    },
                    handler=_browser_h("click"),
                ),
                ToolSpec(
                    name="browser_select",
                    description=(
                        "Choose an option in a <select> dropdown by its label "
                        "and the option's value or label."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "target": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["target", "value"],
                    },
                    handler=_browser_h("select"),
                ),
                ToolSpec(
                    name="browser_press",
                    description=(
                        "Send a keyboard key to the page (Enter, Tab, Escape, "
                        "ArrowDown...). Pressing Enter in a field submits the "
                        "form — that is confirmation-gated like browser_submit."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"key": {"type": "string"}},
                        "required": ["key"],
                    },
                    handler=_browser_h("press"),
                ),
                ToolSpec(
                    name="browser_submit",
                    description=(
                        "Submit the active form (login, signup, search). "
                        "REQUIRES human confirmation before anything is "
                        "submitted; reports the resulting page."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"note": {"type": "string", "description": "what is being submitted"}},
                    },
                    handler=_browser_h("submit"),
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=_b_confirm,
                ),
                ToolSpec(
                    name="browser_wait",
                    description=(
                        "Wait a fixed number of milliseconds for the page "
                        "(animations, redirects, CAPTCHA-adjacent delays)."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"ms": {"type": "integer", "default": 1000}},
                    },
                    handler=_browser_h("wait"),
                ),
                ToolSpec(
                    name="browser_back",
                    description="Go back one page in the agent's Chrome.",
                    parameters={"type": "object", "properties": {}},
                    handler=_browser_h("back"),
                ),
                ToolSpec(
                    name="browser_extract",
                    description=(
                        "Extract the visible text of an element (by label, CSS "
                        "with 'css:' prefix, or text) — e.g. read an article "
                        "behind a login, or pull a confirmation code off a page."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                    },
                    handler=_browser_h("extract"),
                ),
                ToolSpec(
                    name="browser_screenshot",
                    description=(
                        "Capture a PNG of the current page and save it to the "
                        "app's data dir; view it at /api/browser/screenshot. "
                        "Use to show the user what the agent sees."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"name": {"type": "string", "default": "latest"}},
                    },
                    handler=_browser_h("screenshot"),
                ),
                ToolSpec(
                    name="browser_creds_store",
                    description=(
                        "Store login credentials for a site in the local 0600 "
                        "vault so browser_signin can use them later. REQUIRES "
                        "confirmation. Passwords are never shown again."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "site": {"type": "string", "description": "domain or https URL"},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                        },
                        "required": ["site", "username", "password"],
                    },
                    handler=_browser_h("creds_store"),
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=_b_confirm,
                ),
                ToolSpec(
                    name="browser_creds_list",
                    description=(
                        "List which sites have stored credentials — usernames "
                        "only, passwords are never shown."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_browser_h("creds_list"),
                ),
                ToolSpec(
                    name="browser_creds_forget",
                    description=(
                        "Remove stored credentials for a site. REQUIRES "
                        "confirmation."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"site": {"type": "string"}},
                        "required": ["site"],
                    },
                    handler=_browser_h("creds_forget"),
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=_b_confirm,
                ),
                ToolSpec(
                    name="browser_signin",
                    description=(
                        "Log in to a site using its stored credentials: opens "
                        "the site, fills the username/password fields from the "
                        "vault, submits, and reports where the page landed. "
                        "REQUIRES confirmation — it performs a real login."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"site": {"type": "string", "description": "domain or https URL"}},
                        "required": ["site"],
                    },
                    handler=_browser_h("signin"),
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=_b_confirm,
                ),
            ],
        )
    )

    # ---- Compute node (v5.26): the Dell is infrastructure, not DOURMOUSE --
    def _server_status_h(arguments: dict[str, Any]) -> str:
        from dourmouse.remote_server import server_status

        try:
            s = server_status()
        except Exception as exc:  # noqa: BLE001 - status must never raise
            return f"SERVER STATUS FAILED: {type(exc).__name__}: {exc}"
        if not s.get("online"):
            return (
                f"COMPUTE NODE OFFLINE ({s.get('url')}) — {s.get('error') or 'no response'}. "
                "Local AI remains in charge (automatic failover)."
            )
        return (
            f"COMPUTE NODE ONLINE ({s.get('url')})\n"
            f"NODE: {s.get('node') or '?'}\n"
            f"MODEL: {s.get('model') or '?'}\n"
            f"OLLAMA: {'up' if s.get('ollama') else 'down'}\n"
            f"VERSION: {s.get('version') or '?'}\n"
            f"LATENCY: {s.get('latency_ms') or '?'}ms"
        )

    def _server_generate_h(arguments: dict[str, Any]) -> str:
        from dourmouse.remote_server import DourmouseServerClient

        try:
            result = DourmouseServerClient().generate(
                arguments.get("prompt", ""),
                system=arguments.get("system"),
                temperature=arguments.get("temperature"),
                max_tokens=arguments.get("max_tokens"),
            )
        except Exception as exc:  # noqa: BLE001 - client never raises, belt+braces
            return f"SERVER GENERATE FAILED: {type(exc).__name__}: {exc}"
        if not result.get("success"):
            return f"SERVER GENERATE (reported honestly): {result.get('error')}"
        return (
            f"COMPUTE NODE RESPONSE ({result.get('node') or '?'} · "
            f"{result.get('model') or '?'} · {result.get('latency_ms') or '?'}ms):\n"
            f"{result['response']}"
        )

    def _server_chat_h(arguments: dict[str, Any]) -> str:
        from dourmouse.remote_server import DourmouseServerClient

        try:
            result = DourmouseServerClient().chat(
                arguments.get("messages", []),
                temperature=arguments.get("temperature"),
            )
        except Exception as exc:  # noqa: BLE001
            return f"SERVER CHAT FAILED: {type(exc).__name__}: {exc}"
        if not result.get("success"):
            return f"SERVER CHAT (reported honestly): {result.get('error')}"
        return (
            f"COMPUTE NODE RESPONSE ({result.get('node') or '?'} · "
            f"{result.get('model') or '?'} · {result.get('latency_ms') or '?'}ms):\n"
            f"{result['response']}"
        )

    def _server_offload_h(arguments: dict[str, Any]) -> str:
        """Offload one inference to the compute node, falling back to the
        LOCAL Ollama on any failure. This is the transparent failover seam:
        the Dell offline never breaks a request, and the response says which
        path served it (never a fabricated result)."""
        from dourmouse.remote_server import generate_with_fallback, local_ollama_fallback

        prompt = (arguments.get("prompt") or "").strip()
        if not prompt:
            return "ERROR: server_offload requires a prompt."
        system = arguments.get("system")
        temperature = arguments.get("temperature")
        try:
            result = generate_with_fallback(
                prompt,
                lambda p: local_ollama_fallback(p, system=system),
                system=system,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001
            return f"SERVER OFFLOAD FAILED: {type(exc).__name__}: {exc}"
        if not result.get("success"):
            return f"SERVER OFFLOAD FAILED (both paths): {result.get('error')}"
        via = "COMPUTE NODE" if result.get("via") == "server" else "LOCAL AI (Dell offline — failover)"
        return (
            f"OFFLOAD RESPONSE · {via} · {result.get('latency_ms') or '?'}ms:\n"
            f"{result['response']}"
        )

    registry.register_subagent(
        _subagent(
            "compute",
            "General",
            "DOURMOUSE compute node (the Dell): LAN inference offload to "
            "Qwen3 1.7B with automatic fallback to the local AI. The Dell "
            "is infrastructure, never a second DOURMOUSE.",
            [
                ToolSpec(
                    name="server_status",
                    description=(
                        "Report the DOURMOUSE compute node (Dell): online/offline, "
                        "node name, model, Ollama status, version and response "
                        "latency. Read-only, cached, never raises."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_server_status_h,
                ),
                ToolSpec(
                    name="server_generate",
                    description=(
                        "Generate text on the DOURMOUSE compute node (Dell): "
                        "send a prompt (+ optional system instruction, "
                        "temperature, max_tokens) to the LAN Qwen3 1.7B node "
                        "and return its response with latency. Reports the "
                        "node offline honestly when unreachable — use "
                        "server_offload instead when a local fallback is wanted."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "system": {"type": "string"},
                            "temperature": {"type": "number"},
                            "max_tokens": {"type": "integer"},
                        },
                        "required": ["prompt"],
                    },
                    handler=_server_generate_h,
                ),
                ToolSpec(
                    name="server_chat",
                    description=(
                        "Chat on the DOURMOUSE compute node (Dell): pass an "
                        "OpenAI-format messages list to the LAN Qwen3 1.7B "
                        "node and return its reply with latency. Honest error "
                        "when the node is offline."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "messages": {
                                "type": "array",
                                "items": {"type": "object"},
                                "description": "[{role: system|user|assistant, content}]",
                            },
                            "temperature": {"type": "number"},
                        },
                        "required": ["messages"],
                    },
                    handler=_server_chat_h,
                ),
                ToolSpec(
                    name="server_offload",
                    description=(
                        "Offload one inference to the compute node with AUTOMATIC "
                        "fallback: tries the Dell Qwen3 1.7B; on ANY failure "
                        "(offline, timeout, error) it transparently uses the "
                        "LOCAL Ollama and says which path served the answer. "
                        "Use for heavy or local-AI-inference requests the main "
                        "machine should not spend its own tokens on."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "system": {"type": "string"},
                            "temperature": {"type": "number"},
                        },
                        "required": ["prompt"],
                    },
                    handler=_server_offload_h,
                ),
            ],
        )
    )

    registry.register_subagent(
        _subagent(
            "mail",
            "Live",
            "Inbox + Gmail + Drive — IMAP read_inbox, Gmail search/read/send, and "
            "Drive search/read, all on the signed-in Google account. Sending "
            "always requires confirmation.",
            [
                ToolSpec(
                    name="read_inbox",
                    description=(
                        "Read the latest N messages from IMAP INBOX (read-only). "
                        "Requires DOURMOUSE_IMAP_HOST / DOURMOUSE_IMAP_USER / "
                        "DOURMOUSE_IMAP_PASS env vars; otherwise reports NOT "
                        "CONFIGURED honestly. Never sends or deletes."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "max_items": {"type": "integer", "default": 10},
                        },
                    },
                    handler=_read_inbox_tool,
                ),
                ToolSpec(
                    name="gmail_search",
                    description=(
                        "Search the signed-in Google user's Gmail for messages "
                        "by subject/from/body words. Works with the Google "
                        "sign-in (gmail.readonly scope); otherwise uses the "
                        "shared App-Password setup or reports NOT CONFIGURED "
                        "honestly."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "max_results": {"type": "integer", "default": 10},
                        },
                        "required": ["query"],
                    },
                    handler=_gmail_search_h,
                ),
                ToolSpec(
                    name="gmail_read",
                    description=(
                        "Read ONE Gmail message by its id/uid (from "
                        "gmail_search). Read-only; works on the signed-in "
                        "Google account or the shared App-Password setup."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "message_id": {"type": "string", "description": "numeric uid from gmail_search"},
                        },
                        "required": ["message_id"],
                    },
                    handler=_gmail_read_h,
                ),
                ToolSpec(
                    name="drive_search",
                    description=(
                        "Search the signed-in Google user's Drive (read-only) "
                        "by name/content words — newest first. Needs the user's "
                        "Google sign-in (drive.readonly scope); otherwise "
                        "reports NOT CONFIGURED honestly. Never deletes."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string",
                                       "description": "name/content words, e.g. 'q3 report' (empty = browse recent files)"},
                            "max_results": {"type": "integer", "default": 10},
                        },
                    },
                    handler=_drive_search_h,
                ),
                ToolSpec(
                    name="drive_read",
                    description=(
                        "Read ONE file's text content from the signed-in "
                        "Google user's Drive by its id (from drive_search). "
                        "Docs/Sheets are exported as text; oversized binaries "
                        "are refused honestly. Read-only."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "file_id": {"type": "string",
                                          "description": "file id from drive_search"},
                        },
                        "required": ["file_id"],
                    },
                    handler=_drive_read_h,
                ),
                ToolSpec(
                    name="gmail_send",
                    description=(
                        "Send an email FROM your Gmail account. ALWAYS requires "
                        "human confirmation of recipient + subject before any "
                        "message leaves."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "to": {"type": "string"},
                            "subject": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["to", "subject", "body"],
                    },
                    handler=_gmail_send_h,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: (
                        f"Send Gmail to {a.get('to', '?')!r} with subject "
                        f"{a.get('subject', '')!r}? (body: "
                        f"{(a.get('body') or '')[:140]}...)"
                    ),
                ),
                ToolSpec(
                    name="gmail_archive",
                    description=(
                        "Remove one email from the INBOX (it stays in All Mail "
                        "and is fully searchable). Nothing is deleted and it is "
                        "reversible. Needs the message id from gmail_search."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "message_id": {
                                "type": "string",
                                "description": "Gmail message id, as returned by gmail_search.",
                            }
                        },
                        "required": ["message_id"],
                    },
                    handler=_gmail_archive_h,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: (
                        f"Archive Gmail message {a.get('message_id', '?')} "
                        "(leaves the inbox, stays in All Mail, nothing deleted)?"
                    ),
                ),
                ToolSpec(
                    name="gmail_trash",
                    description=(
                        "Move one email to Trash, recoverable for 30 days. This "
                        "is NOT permanent deletion — Dourmouse cannot permanently "
                        "delete mail. Use gmail_untrash to undo. Needs the message "
                        "id from gmail_search."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "message_id": {
                                "type": "string",
                                "description": "Gmail message id, as returned by gmail_search.",
                            }
                        },
                        "required": ["message_id"],
                    },
                    handler=_gmail_trash_h,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: (
                        f"Move Gmail message {a.get('message_id', '?')} to Trash? "
                        "Recoverable for 30 days."
                    ),
                ),
                ToolSpec(
                    name="gmail_untrash",
                    description=(
                        "Restore an email from Trash back to the inbox. The undo "
                        "for gmail_trash."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "message_id": {"type": "string"},
                        },
                        "required": ["message_id"],
                    },
                    handler=_gmail_untrash_h,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: (
                        f"Restore Gmail message {a.get('message_id', '?')} from Trash?"
                    ),
                ),
                ToolSpec(
                    name="email_identity_status",
                    description=(
                        "Report Dourmouse's own mail identity: display name, "
                        "own address (default the +dourmouse receiving alias), "
                        "and whether a dedicated SMTP mailbox is configured. "
                        "Read-only."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=lambda a: _email_identity_status_h(),
                ),
                ToolSpec(
                    name="email_own_send",
                    description=(
                        "Send an email FROM Dourmouse's OWN dedicated identity "
                        "(the configured SMTP mailbox, e.g. you+dourmouse@... "
                        "or a real dedicated address). ALWAYS requires "
                        "confirmation; reports NOT CONFIGURED honestly when no "
                        "dedicated mailbox is set up yet."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "to": {"type": "string"},
                            "subject": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["to", "subject", "body"],
                    },
                    handler=_email_own_send_h,
                    permission=Permission.REQUIRES_CONFIRMATION,
                    confirm_prompt=lambda a: (
                        f"Send mail FROM the Dourmouse identity to "
                        f"{a.get('to', '?')!r} with subject "
                        f"{a.get('subject', '')!r}? (body: "
                        f"{(a.get('body') or '')[:140]}...)"
                    ),
                ),
            ],
        )
    )

    registry.register_subagent(
        _subagent(
            "tasks",
            "Live",
            "Local task list — deterministic CRUD in the workspace (tasks.json).",
            [
                ToolSpec(
                    name="list_tasks",
                    description=(
                        "List the local task list (id, title, created_at, done), "
                        "oldest first."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "include_done": {"type": "boolean", "default": True},
                        },
                    },
                    handler=_list_tasks_tool,
                ),
                ToolSpec(
                    name="add_task",
                    description="Add one task to the local task list.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                    handler=_add_task_tool,
                ),
                ToolSpec(
                    name="complete_task",
                    description="Mark one task done by its id.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                        },
                        "required": ["task_id"],
                    },
                    handler=_complete_task_tool,
                ),
                ToolSpec(
                    name="schedule_recurring",
                    description=(
                        "Schedule a real tool call to repeat automatically "
                        "('do this every Monday'). Pass the exact tool name "
                        "and its arguments you want repeated, plus a plain-"
                        "English schedule: 'every Monday at 9:00', 'daily at "
                        "8:30', 'every 30 minutes', 'weekly'. Returns the "
                        "schedule id; the runner executes it deterministically "
                        "(no model in the loop) and persists across restarts."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string", "description": "an existing tool name in this system"},
                            "arguments": {"type": "object", "description": "the tool's arguments as a JSON object"},
                            "schedule_text": {"type": "string", "description": "'every Monday at 9:00', 'daily at 8:30', 'every 30 minutes'"},
                        },
                        "required": ["tool", "arguments", "schedule_text"],
                    },
                    handler=_schedule_recurring_tool,
                ),
                ToolSpec(
                    name="list_schedules",
                    description=(
                        "List every recurring schedule (id, tool, when, next "
                        "run, last run)."
                    ),
                    parameters={"type": "object", "properties": {}},
                    handler=_list_schedules_tool,
                ),
                ToolSpec(
                    name="cancel_schedule",
                    description="Stop a recurring schedule by its id (from list_schedules).",
                    parameters={
                        "type": "object",
                        "properties": {
                            "schedule_id": {"type": "string"},
                        },
                        "required": ["schedule_id"],
                    },
                    handler=_cancel_schedule_tool,
                ),
            ],
        )
    )

    # -- 3D & UI Design agent ------------------------------------------ #
    # Real spec-generation + cataloguing tools for the desktop
    # spatial_ai_library scaffold (ui_components/ui_manifest.json,
    # 3d_models/). Deterministic (Rule 2.8), honestly NOT a mesh/CAD
    # generator (Rule 2.2) — see design_3d_ops.py's module docstring.
    from dourmouse.design_3d_ops import build_design_3d_tool_specs

    registry.register_subagent(
        _subagent(
            "design_3d",
            "General",
            "3D & UI Design — generates/describes UI component and "
            "primitive-level 3D model specs and catalogues them into a "
            "ui_manifest.json-shaped manifest at a configurable path. "
            "NOT a renderer, CAD engine, or mesh generator.",
            build_design_3d_tool_specs(),
        )
    )

    # -- v5.8 artifact renderer ---------------------------------------- #
    # Every research/coding/report agent can publish a structured artifact
    # (markdown / table / series) rendered beside the chat — the biggest
    # 'next level' gap vs Claude Cowork (live tables, equity curves,
    # formatted reports instead of raw text). The tool writes into the
    # shared ArtifactStore; the web UI renders it live via /api/artifacts.
    # The orchestrator deliberately stays single-tool (delegate_task) —
    # publish_artifact rides the agents that actually produce output.
    from dourmouse.artifacts import build_artifact_tool_spec

    _artifact_spec = build_artifact_tool_spec()
    for _name in ("research_info", "dev_coding", "rnd", "atlas"):
        registry.extend_subagent(_name, _artifact_spec)

    # -- shared RAG tool (query_shared_memory) -------------------------- #
    # "a shared database all LLMs can use": query_shared_memory is
    # registered above on the memory subagent (its natural home, next to
    # remember/recall/memory_search_semantic) then extended to EVERY other
    # subagent here via the SAME ToolSpec object identity (matching
    # extend_subagent's own is-identity contract above) — unlike
    # publish_artifact, which deliberately rides only the report-producing
    # agents, this one is meant to be reachable from anywhere, since any
    # backend answering any subagent's tool-calling turn may want it.
    # The orchestrator is excluded on purpose: it deliberately stays
    # single-tool (delegate_task only — see the artifact-tool comment
    # above), so it is not extended here either.
    _shared_memory_spec = registry.lookup("query_shared_memory")
    if _shared_memory_spec is not None:
        for _sub in registry.all_subagents():
            if _sub.name == "orchestrator":
                continue
            registry.extend_subagent(_sub.name, _shared_memory_spec)

    return registry
