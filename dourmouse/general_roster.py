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
from typing import Any

from dourmouse.dispatch import (
    DispatchRegistry,
    Permission,
    Subagent,
    ToolSpec,
    current_dispatch_context,
    run_dispatch_messages,
    system_message,
)
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
    max_results = int(arguments.get("max_results", 5))
    errors: list[str] = []
    # General search first (DuckDuckGo), Wikipedia as a reliable fallback.
    try:
        ddg = _duckduckgo_search(query, max_results)
        if ddg is not None:
            return ddg
        errors.append("DuckDuckGo returned no parseable results")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        errors.append(f"DuckDuckGo failed: {exc}")
    try:
        return _wikipedia_search(query, max_results)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        errors.append(f"Wikipedia failed: {exc}")
    return f"WEB SEARCH FAILED (reported honestly): {'; '.join(errors)}"


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
        return f"FETCH FAILED (reported honestly): {exc}"
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
    return (
        "NOT CONFIGURED: no calendar backend wired yet (read-only design — "
        "booking would require confirmation once it exists). No events "
        "fabricated."
    )


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
    return target.read_text(errors="replace")


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
                for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
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
    old = target.read_text(errors="replace").splitlines()
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
    text = target.read_text(errors="replace")
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
            text = p.read_text(errors="replace")
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
    return target.read_text(errors="replace")


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
                    "(SQLite FTS5, source/title/body). Use when the user "
                    "says 'remember X' or when a durable fact must survive "
                    "beyond this conversation."
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
            ToolSpec(
                name="write_note",
                description="Write (create/overwrite) one note in the Obsidian vault.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                handler=_write_note_tool,
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
    except RuntimeError as exc:
        return f"NEWS FEED FAILED (reported honestly): {exc}"
    lines = [
        f"- {it['title']} [{it['source']}] {it['published']}" for it in items
    ]
    return "LIVE NEWS HEADLINES (Google News, keyless):\n" + "\n".join(lines)


def _stock_quote_tool(arguments: dict[str, Any]) -> str:
    from dourmouse import live_feeds

    symbol = (arguments.get("symbol") or "").strip()
    if not symbol:
        return "ERROR: stock_quote requires a 'symbol' (e.g. AAPL)."
    try:
        q = live_feeds.stock_quote(symbol)
    except RuntimeError as exc:
        return f"QUOTE FAILED (reported honestly): {exc}"
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
    except RuntimeError as exc:
        return f"MARKET MOVERS FAILED (reported honestly): {exc}"
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
                    description="List files/dirs in the workspace sandbox (read-only)." + path_note,
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
                        "Top day GAINERS or LOSERS (Yahoo Finance screener, "
                        "keyless): symbol, name, price, change, change_pct."
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

    registry.register_subagent(
        _subagent(
            "mail",
            "Live",
            "Inbox + Gmail — IMAP read_inbox, and Gmail search/read/send via your "
            "Google account (App Password). Sending always requires confirmation.",
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
                        "Search Gmail (your Google account) for messages by "
                        "subject/from/body words. Needs GOOGLE_GMAIL_USER + "
                        "GOOGLE_GMAIL_APP_PASSWORD in .env; otherwise reports "
                        "NOT CONFIGURED honestly."
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
                        "Read ONE Gmail message by its uid (from gmail_search). "
                        "Read-only; needs the same Gmail env vars."
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
            ],
        )
    )

    return registry
