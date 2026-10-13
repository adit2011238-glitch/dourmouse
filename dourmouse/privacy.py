"""The privacy gate for a cloud-only app (PRIV-1 to PRIV-5).

Every model Dourmouse uses runs in the cloud, so anything a model sees leaves
this Mac. This module is how Dourmouse sends less and keeps a record:

* **Tiers.** Each tool result is tagged when it enters a turn: T0 public,
  T1 personal, T2 sensitive (needs the owner's consent for its source), T3
  never sent (password vault, Keychain, keys, .env, cookies, the owner
  secret). ``classify`` and ``t3_reason``.
* **Placeholders.** Card and IBAN numbers, ID numbers, phone numbers, street
  addresses and other people's email addresses become ``[EMAIL_1]`` and so on
  before a result is sent (``governance.PiiRedactor``); the mapping stays in
  memory on this Mac per chat (``placeholder_map``) and the real values come
  back in the reply and in tool arguments.
* **Consent and Private mode.** ``PrivacyStore``: one small JSON in the
  settings folder. Only the owner's SETTINGS routes write it.
* **Egress ledger.** ``record_egress``: one metadata line per model request
  (never content), under ``<settings folder>/privacy/egress.jsonl`` as a hash chain.

``gate_tool_result`` is the one function the dispatch loop and the MCP bridge
call for every tool result. See ~/Documents/DOURMOUSE/PRIV_DESIGN_NOTE.md.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import shlex
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TIERS = ("T0", "T1", "T2", "T3")

#: The consent sources, in the order the SETTINGS screen shows them.
SOURCES: dict[str, dict[str, str]] = {
    "mail": {"label": "Mail", "what": "message subjects, snippets and bodies"},
    "memory": {"label": "Memory", "what": "notes and facts from your memory vault"},
    "documents": {"label": "Documents and Drive", "what": "the text of documents, sheets and Drive files"},
    "finance": {"label": "Finance", "what": "account balances, positions and orders"},
    "contacts": {"label": "Contacts", "what": "names, numbers and addresses from your contacts"},
    "files": {"label": "Files outside the workspace",
              "what": "file content and command output from this Mac outside the workspace folder"},
    "screen": {"label": "Apps, screen and browser pages",
               "what": "page text, window contents and the clipboard"},
}

#: Agents whose tool results are T2 unless a tool below says otherwise.
#: (Replaces model_delegation's old local-only list: these agents were promised
#: local handling that a cloud-only app cannot give.)
SENSITIVE_AGENTS = frozenset({
    "mail", "comms", "messenger", "docs", "memory", "companion",
    "admin_ops", "system", "tasks", "scheduling", "compute",
    "browser", "panel_control", "freebuff", "design_3d",
    "apps", "app_driver",
    "markets", "forex", "mt5", "t212",
})

#: The consent source an unlisted tool of a sensitive agent falls under.
_AGENT_SOURCE = {
    "mail": "mail", "comms": "mail", "messenger": "mail",
    "docs": "documents", "tasks": "documents", "scheduling": "documents", "design_3d": "documents",
    "memory": "memory", "companion": "memory",
    "admin_ops": "files", "system": "files", "compute": "files", "freebuff": "files",
    "browser": "screen", "panel_control": "screen", "apps": "screen", "app_driver": "screen",
    "markets": "finance", "forex": "finance", "mt5": "finance", "t212": "finance",
}

_T2_TOOLS: dict[str, str] = {
    # Mail
    "read_inbox": "mail", "gmail_search": "mail", "gmail_read": "mail",
    # Memory
    "recall": "memory", "memory_search_semantic": "memory", "search_vault": "memory", "read_note": "memory",
    "daily_digest": "memory", "query_shared_memory": "memory",
    # Documents and Drive
    "drive_read": "documents", "drive_download": "documents", "sheets_read": "documents",
    "device_wiki_get": "documents",
    # Finance
    "t212_account": "finance", "t212_positions": "finance", "t212_portfolio": "finance",
    "forex_ibkr": "finance", "forex_paper": "finance", "forex_report": "finance", "forex_inventory": "finance",
    "mt5_status": "finance",
    # Files outside the workspace, command output
    "run_command": "files", "run_privileged_command": "files", "run_python_host": "files",
    "compute_run_python": "files", "freebuff_read_thread": "files", "freebuff_notes": "files",
    "freebuff_dispatch": "files",
    # H-FIXP-1: these two run the Claude or Codex CLI with its own file and shell tools on (the owner approves
    # each call), so their output can be anything those tools read.
    "claude_code": "files", "codex_code": "files",
    # Apps, screen and browser pages
    "browser_snapshot": "screen", "browser_extract": "screen", "browser_screenshot": "screen",
    "browser_click": "screen", "browser_type": "screen", "browser_fill": "screen", "browser_fill_form": "screen",
    "browser_select": "screen", "browser_press": "screen", "browser_submit": "screen", "browser_wait": "screen",
    "browser_back": "screen", "browser_media": "screen",
    "app_driver_snapshot": "screen", "app_driver_click": "screen", "app_driver_type": "screen",
    "app_driver_press_key": "screen", "app_driver_scroll": "screen",
    "clipboard_get": "screen", "security_browser_history": "screen",
    # Names of downloaded files, devices on the network and open ports (R5-16).
    "security_downloads": "files", "security_known_devices": "files", "security_external_peers": "files",
    "list_exposed_services": "files",
}

#: Tools whose tier depends on where the file is: T1 inside the workspace,
#: else T2 under the named source.
_PATH_TOOLS: dict[str, str] = {
    "read_path": "files", "extract_pdf": "files", "extract_receipt": "documents", "study_read_file": "documents",
}

_T0_TOOLS = frozenset({
    "web_search", "fetch_url", "open_url", "research_news", "research_quote", "research_movers",
    "research_web_search", "research_fetch_url", "news_headlines", "news_search",
    "worldmonitor_status", "worldmonitor_catalog", "worldmonitor_call_tool", "world_pulse",
    "world_pulse_details", "world_brief", "world_correlations", "globe_control",
    "stock_quote", "market_movers", "mt5_quote", "mt5_universe", "forex_events", "forex_strategy",
    "generate_image", "spotify_search", "research_mesh_status", "research_mesh_qualify",
})
_T0_PREFIXES = ("research_",)

#: Status and name-list tools of sensitive agents (the plan names calendar
#: entries, tasks, goals and file names as T1).
_T1_TOOLS = frozenset({
    "list_calendar_events", "propose_time_slots", "create_calendar_event",
    "list_tasks", "add_task", "complete_task", "schedule_recurring", "list_schedules", "cancel_schedule",
    "list_files", "list_path", "delete_file", "librarian_find", "librarian_status", "librarian_proposals",
    "librarian_apply", "librarian_undo", "system_info", "check_connections", "repo_map", "write_path",
    "apply_search_replace", "apply_patch", "delete_path", "undo_last_change", "open_path", "open_file_preview",
    "player_play", "player_pause", "player_seek", "player_now_playing", "clipboard_set", "read_upload",
    "list_running_apps", "list_app_windows", "activate_app", "quit_app", "send_app_keystrokes", "press_app_key",
    "click_app_menu_item", "app_driver_status", "app_driver_stop",
    "list_panel_types", "open_panel", "close_panel", "move_panel", "resize_panel", "organize_panels",
    "generate_ui_component_spec", "generate_3d_model_spec", "list_manifest", "read_manifest_entry",
    "write_manifest_entry",
    "freebuff_status", "freebuff_projects", "freebuff_threads", "freebuff_skills", "freebuff_changes",
    "compute_job_status", "compute_jobs", "compute_environment",
    "open_browser_pane", "browser_open", "browser_pane_show", "browser_pane_hide", "browser_signin",
    "browser_creds_list", "browser_creds_store", "browser_creds_forget",
    "remember", "write_note", "draft_message", "send_draft", "send_message", "read_agent_inbox",
    "drive_search", "gmail_send", "gmail_archive", "gmail_trash", "gmail_bulk_trash", "gmail_untrash",
    "email_identity_status", "email_own_send", "sheets_create", "sheets_append", "drive_create_doc",
    "docs_append", "docs_insert_image", "slides_create", "drive_share",
    "t212_status", "t212_order", "mt5_order",
    "delegate_task", "delegate_parallel", "delegate_to_models", "query_desktop_vault",
    "get_goal_status", "list_goals", "device_wiki_status", "device_wiki_scan",
})

#: Tools that read local files or run local code: their arguments are checked
#: for credential paths (T3).
_LOCAL_READERS = frozenset({
    "read_path", "read_file", "review_read_file", "study_read_file", "extract_pdf", "extract_receipt",
    "read_upload", "run_command", "run_privileged_command", "run_python", "run_python_host",
    "compute_run_python", "edit_file", "search_files", "review_search_files", "study_search_files",
})


#: Agents whose tools not named above were looked at one by one and are T1 (the owner's own work items, file and
#: device names, coding helpers). A test walks the real registry and fails when an agent has a tool that is in no
#: tier map and whose agent is in neither this set nor SENSITIVE_AGENTS, so a new tool cannot be T1 by accident.
#: At run time only the agents in _FAIL_CLOSED_AGENTS (external MCP servers) default to T2.
_FAIL_CLOSED_AGENTS = frozenset({"mcp_tools"})
_T1_AGENTS = frozenset({
    "agent_smith", "code_claude", "code_codex", "code_deepseek", "code_nvidia", "code_ollama", "dev_coding",
    "device_wiki", "goals", "music", "reviewer", "study", "security", "research_info", "rnd",
})


def classify(name: str, arguments: Any, agent: str | None = None) -> tuple[str, str | None]:
    """``(tier, source)`` of a tool's result. T3 is decided separately by
    ``t3_reason`` because it depends on the result's content too."""
    name = (name or "").strip()
    if name in _PATH_TOOLS:
        path = _first_path(arguments)
        if path:
            protected = protected_workspace_source(path)
            if protected is not None:
                return "T2", protected
            if _inside_workspace(path):
                return "T1", None
        return "T2", _PATH_TOOLS[name]
    if name in _T2_TOOLS:
        return "T2", _T2_TOOLS[name]
    if name in _T0_TOOLS or name.startswith(_T0_PREFIXES):
        return "T0", None
    if name in _T1_TOOLS:
        return "T1", None
    owner = (agent or "").strip().lower()
    if owner in SENSITIVE_AGENTS:
        return "T2", _AGENT_SOURCE.get(owner, "files")
    if owner in _FAIL_CLOSED_AGENTS:
        # R5-16: fail closed. The external "mcp_tools" servers (a filesystem or mail server, say) can read
        # anything, so their results need the Files consent like a command's.
        return "T2", "files"
    return "T1", None


def hidden_in_private_mode(name: str, agent: str | None = None) -> bool:
    """True for a tool Private mode removes from the tool list: anything that
    can return T2 content (a path tool can, depending on its argument)."""
    return name in _PATH_TOOLS or classify(name, {}, agent)[0] == "T2"


def _first_path(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in ("path", "file", "file_path", "filename", "pdf_path", "src", "source"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


#: The workspace folders that hold the app's own state, memory and past chats, and the consent source of each
#: (R5-8). A path here is never "just a file inside the workspace".
_PROTECTED_DIR_SOURCE = {
    "memory": "memory", "sessions": "memory", "neuro": "memory",
    "device_wiki": "documents", "librarian": "documents", "office": "documents",
}


def protected_workspace_source(path: str) -> str | None:
    """The consent source of a path under one of the workspace's state folders (memory, sessions, device_wiki,
    auth, state ...), or None for any other path."""
    from dourmouse.config import workspace_dir
    from dourmouse.path_identity import is_within
    from dourmouse.sandbox import _PROTECTED_WORKSPACE_DIRS

    raw = os.path.expanduser(path)
    root = workspace_dir()
    candidate = raw if os.path.isabs(raw) else os.path.join(str(root), raw)
    for folder in _PROTECTED_WORKSPACE_DIRS:
        try:
            if is_within(candidate, root / folder):
                return _PROTECTED_DIR_SOURCE.get(folder, "files")
        except (OSError, ValueError):
            continue
    return None


def _inside_workspace(path: str) -> bool:
    from dourmouse.config import workspace_dir
    from dourmouse.path_identity import is_within

    raw = os.path.expanduser(path)
    root = workspace_dir()
    candidate = raw if os.path.isabs(raw) else os.path.join(str(root), raw)
    try:
        return is_within(candidate, root)
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# T3: never sent
# --------------------------------------------------------------------------- #

_KEYCHAIN_COMMAND = re.compile(
    r"\bsecurity\s+(?:find-(?:generic|internet|certificate)-password|dump-keychain|export)\b"
)
#: The header line, the footer line, or the secret scrubber's mark for the
#: header (it runs first and replaces only that one line, so the key body
#: itself still has to be withheld here).
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----(?:BEGIN|END) (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\[REDACTED:PRIVATE_KEY_BLOCK\]"
)
_COOKIE_FILE = re.compile(r"^# (?:Netscape )?HTTP Cookie File", re.MULTILINE)
_KEYCHAIN_DUMP = re.compile(r'keychain: "[^"\n]+\.keychain(?:-db)?"')
#: Names the gate adds to system_access's credential list for reading.
_EXTRA_SECRET_NAMES = re.compile(
    r"(?i)(?:\.keychain(?:-db)?|^cookies\.sqlite|^key[34]\.db|^logins\.json|^cookies\.binarycookies"
    r"|^login data|^web data|^local state)$"
)

#: Two or more KEY=value lines whose name says it is a secret: the content of a .env file, however it was read
#: (R5-17: a command such as `grep -r KEY ~/proj` names no secret path).
_DOTENV_SECRET_LINE = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?[A-Za-z][A-Za-z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_]*"
    r"[ \t]*=[ \t]*['\"]?[^\s'\"]{6,}"
)
#: A credential folder or file spelled inside a longer string (a command, or code that builds the path).
_SECRET_FRAGMENT = re.compile(
    r"(?i)(?:^|[\s/\\\"'`(),=:])(?:\.ssh|\.aws|\.gnupg|\.kube|keychains|id_(?:rsa|ed25519|ecdsa|dsa)|"
    r"\.netrc|\.git-credentials|\.pgpass|\.npmrc|login data|cookies\.sqlite|cookies\.binarycookies)(?=$|[\s/\\\"'`(),;:])"
    r"|(?:^|[\s/\\\"'`(),=:])\.env(?:\.[\w.-]+)?(?=$|[\s/\\\"'`(),;:])"
)
_NEVER_SEND: set[str] = set()
#: (length, sha256 hex) of never-send values. The MCP bridge process gets these instead of the values, so a
#: Claude or Codex process never holds the secret itself (R5-17).
_NEVER_HASHES: set[tuple[int, str]] = set()
_never_lock = threading.Lock()
BRIDGE_SECRET_ENV = "DOURMOUSE_PRIVACY_NEVER_SHA"  # noqa: S105 - an environment variable name, not a secret


def register_never_send(value: str) -> None:
    """A value that must never reach a model, matched exactly (the server
    registers its per-launch owner secret here at start)."""
    if value and len(value) >= 16:
        with _never_lock:
            _NEVER_SEND.add(value)
            _NEVER_HASHES.add((len(value), hashlib.sha256(value.encode("utf-8")).hexdigest()))


def register_never_send_hash(length: int, digest: str) -> None:
    if length >= 16 and re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        with _never_lock:
            _NEVER_HASHES.add((length, digest))


def _contains_hashed_secret(body: str) -> bool:
    with _never_lock:
        hashes = set(_NEVER_HASHES)
        exact = tuple(_NEVER_SEND)
    if any(s in body for s in exact):
        return True
    if not hashes:
        return False
    cap = 500_000
    windows = [body] if len(body) <= cap else [body[: cap // 2], body[-cap // 2:]]
    for text in windows:
        for length in {n for n, _ in hashes}:
            wanted = {h for n, h in hashes if n == length}
            for i in range(len(text) - length + 1):
                if hashlib.sha256(text[i:i + length].encode("utf-8")).hexdigest() in wanted:
                    return True
    return False


def _is_secret_path(token: str) -> bool:
    from dourmouse import system_access as sa

    token = token.strip().strip("'\"`,;()")
    if not token or ("/" not in token and not token.startswith((".", "~")) and not any(
            p.search(token) for p in sa._SENSITIVE_FILENAME_PATTERNS)):
        return False
    path = Path(os.path.expanduser(token))
    parts = [p.casefold() for p in path.parts]
    if any(c.casefold() in parts for c in sa._SENSITIVE_COMPONENTS):
        return True
    name = path.name
    if any(p.search(name) or p.search(name.casefold()) for p in sa._SENSITIVE_FILENAME_PATTERNS):
        return True
    if _EXTRA_SECRET_NAMES.search(name):
        return True
    if sa._is_cli_credential_file(path):
        return True
    text = str(path) + "/"
    return "/.config/gcloud/" in text or "/.config/gh/" in text


def _argument_tokens(arguments: Any) -> list[str]:
    from dourmouse.governance import _strings_in

    tokens: list[str] = []
    for value in _strings_in(arguments):
        try:
            tokens.extend(shlex.split(value))
        except ValueError:
            tokens.extend(value.split())
    return tokens


def t3_reason(name: str, arguments: Any, text: str) -> str | None:
    """Why this result must never be sent, or None."""
    if name in _LOCAL_READERS:
        for value in _strings_in_args(arguments):
            if _KEYCHAIN_COMMAND.search(value):
                return "Keychain items"
        for token in _argument_tokens(arguments):
            if _is_secret_path(token):
                return "a credential file (keys, .env, Keychain, browser cookies or tokens)"
        for value in _strings_in_args(arguments):
            if _SECRET_FRAGMENT.search(value):
                return "a credential file (keys, .env, Keychain, browser cookies or tokens)"
    body = text or ""
    if _PRIVATE_KEY_BLOCK.search(body):
        return "a private key"
    if _COOKIE_FILE.search(body):
        return "browser cookies"
    if _KEYCHAIN_DUMP.search(body):
        return "Keychain items"
    if _contains_hashed_secret(body):
        return "the owner secret"
    if len(_DOTENV_SECRET_LINE.findall(body)) >= 2:
        return "environment variables with secret names (a .env style file)"
    return None


def _strings_in_args(arguments: Any) -> list[str]:
    from dourmouse.governance import _strings_in

    return _strings_in(arguments)


# --------------------------------------------------------------------------- #
# Placeholders (PRIV-2)
# --------------------------------------------------------------------------- #

_PLACEHOLDER = re.compile(r"\[(CARD|IBAN|ID|EMAIL|PHONE|ADDRESS)_(\d{1,5})\]")
#: Never written back into a tool call: a card or bank number does not leave
#: through the model's actions either. The owner's reply still shows it.
_NOT_FOR_ARGUMENTS = frozenset({"CARD", "IBAN"})
_KINDS = ("CARD", "IBAN", "ID", "EMAIL", "PHONE", "ADDRESS")

_EVERYWHERE = frozenset({"*"})
_RECIPIENT_FIELDS = frozenset({"to", "cc", "bcc", "recipient", "recipients", "email", "emails", "attendee",
                               "attendees", "guest", "guests", "address", "share_with"})
#: Tool -> the fields that get real values back (R5-5). A tool not listed gets nothing back.
_RESTORE_FIELDS: dict[str, frozenset[str]] = {
    **dict.fromkeys(("gmail_send", "email_own_send", "draft_message", "send_draft", "send_message",
                     "drive_share", "create_calendar_event", "propose_time_slots", "gmail_search"),
                    _RECIPIENT_FIELDS),
    # Local stores on this Mac: the owner's own notes and tasks hold the real value.
    **dict.fromkeys(("write_note", "remember", "add_task"), _EVERYWHERE),
}


class PlaceholderMapFull(RuntimeError):
    """The chat holds as many distinct identifiers as one map keeps (R5-13): the value is not sent at all, since
    a bare ``[EMAIL]`` could never be restored."""


class PlaceholderMap:
    """Value <-> placeholder for one chat. A chat's map is also kept in a 0600 file in the settings folder
    (``persist``), so numbering survives a restart and the eviction of an old chat from memory: a
    placeholder already in a saved chat can never point at a different person later (R5-13)."""

    MAX_VALUES = 2000

    def __init__(self, persist: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._by_value: dict[tuple[str, str], str] = {}
        self._by_placeholder: dict[str, tuple[str, str]] = {}
        self._next: Counter[str] = Counter()
        self._persist = persist
        if persist is not None:
            try:
                data = json.loads(persist.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                self.merge(data)

    def _save(self) -> None:
        if self._persist is None:
            return
        from dourmouse.state_file import atomic_write_text

        try:
            atomic_write_text(self._persist, json.dumps(self.export()), mode=0o600)
        except OSError as exc:
            # Not hidden: a map that cannot be kept only costs restart consistency, and says so.
            print(f"[PRIVACY] the placeholder map could not be saved: {exc}")

    def placeholder(self, kind: str, value: str) -> str:
        with self._lock:
            key = (kind, value)
            found = self._by_value.get(key)
            if found:
                return found
            if len(self._by_placeholder) >= self.MAX_VALUES:
                raise PlaceholderMapFull(kind)
            self._next[kind] += 1
            ph = f"[{kind}_{self._next[kind]}]"
            self._by_value[key] = ph
            self._by_placeholder[ph] = key
            self._save()
            return ph

    def _restore(self, text: str, skip: frozenset[str]) -> str:
        if not text or "[" not in text:
            return text

        def sub(m: re.Match[str]) -> str:
            hit = self._by_placeholder.get(m.group(0))
            if hit is None or hit[0] in skip:
                return m.group(0)
            return hit[1]

        with self._lock:
            return _PLACEHOLDER.sub(sub, text)

    def restore_text(self, text: str) -> str:
        """Every placeholder back to its value (the owner's own view)."""
        return self._restore(text, frozenset())

    def restore_arguments(self, value: Any, tool: str = "") -> Any:
        """A tool call's arguments with placeholders filled in (R5-5).

        Only where the real value is needed and nothing leaves: the recipient
        fields of the mail, calendar and sharing tools get email addresses
        back, and a few local note tools get every kind except cards and bank
        numbers. Every other tool keeps the placeholders, above all the tools
        that fetch a URL, search, drive a page, run a command or hand text to
        another model: a planted instruction cannot make Dourmouse put an ID
        number or address into a URL or a delegated prompt."""
        fields = _RESTORE_FIELDS.get((tool or "").strip())
        if fields is _EVERYWHERE:
            return self._restore_deep(value, _NOT_FOR_ARGUMENTS)
        if fields:
            return self._restore_fields(value, fields)
        return value

    def _restore_deep(self, value: Any, skip: frozenset[str]) -> Any:
        if isinstance(value, str):
            return self._restore(value, skip)
        if isinstance(value, dict):
            return {k: self._restore_deep(v, skip) for k, v in value.items()}
        if isinstance(value, list):
            return [self._restore_deep(v, skip) for v in value]
        return value

    def _restore_fields(self, value: Any, fields: frozenset[str]) -> Any:
        if not isinstance(value, dict):
            return value
        only_email = frozenset(_KINDS) - {"EMAIL"}
        return {
            k: (self._restore_deep(v, only_email) if str(k).lower() in fields else v)
            for k, v in value.items()
        }

    def mask_text(self, text: str) -> str:
        """The opposite of ``restore_text``: every known real value back to its placeholder. Used for text that
        goes back into a chat's history after the owner's view restored it (R5-10)."""
        if not text:
            return text
        with self._lock:
            pairs = sorted(((val, ph) for ph, (_kind, val) in self._by_placeholder.items() if val),
                           key=lambda p: -len(p[0]))
        for value, ph in pairs:
            if value in text:
                text = text.replace(value, ph)
        return text

    def export(self) -> dict[str, Any]:
        with self._lock:
            return {"values": [[ph, kind, val] for ph, (kind, val) in self._by_placeholder.items()],
                    "next": dict(self._next)}

    def merge(self, data: dict[str, Any]) -> None:
        """Add entries another process minted (the MCP bridge), keeping ours."""
        with self._lock:
            for item in data.get("values") or []:
                if not (isinstance(item, list) and len(item) == 3 and all(isinstance(x, str) for x in item)):
                    continue
                ph, kind, val = item
                if ph in self._by_placeholder or not _PLACEHOLDER.fullmatch(ph):
                    continue
                self._by_placeholder[ph] = (kind, val)
                self._by_value.setdefault((kind, val), ph)
            for kind, n in (data.get("next") or {}).items():
                if isinstance(n, int) and n > self._next[kind]:
                    self._next[kind] = n


_MAPS: OrderedDict[str, PlaceholderMap] = OrderedDict()
_MAPS_LIMIT = 64
_maps_lock = threading.Lock()


_MAP_KEEP_DAYS = 90


def _map_file(key: str) -> Path | None:
    try:
        from dourmouse.config import user_config_dir

        folder = user_config_dir() / "privacy_maps"
        folder.mkdir(parents=True, exist_ok=True)
        _prune_map_files(folder)
        return folder / (hashlib.sha256(("dourmouse-map:" + key).encode("utf-8")).hexdigest()[:32] + ".json")
    except OSError:
        return None


def _prune_map_files(folder: Path) -> None:
    cutoff = time.time() - _MAP_KEEP_DAYS * 86400
    for old in folder.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            continue


def placeholder_map(chat: str) -> PlaceholderMap:
    """The placeholder map of one chat (kept in memory for the 64 most recent chats, and in a file for 90 days)."""
    key = chat or "default"
    with _maps_lock:
        found = _MAPS.get(key)
        if found is None:
            found = PlaceholderMap(persist=_map_file(key))
            _MAPS[key] = found
        _MAPS.move_to_end(key)
        while len(_MAPS) > _MAPS_LIMIT:
            _MAPS.popitem(last=False)
        return found


class StreamRestorer:
    """Restores placeholders in a live text stream. A chunk that ends inside
    something that may still become a placeholder is held back until the
    next chunk (at most 16 characters)."""

    _HOLD = 16

    def __init__(self, mapping: PlaceholderMap, emit: Callable[[str], None]) -> None:
        self._mapping = mapping
        self._emit = emit
        self._buf = ""

    def feed(self, chunk: str) -> None:
        self._buf += chunk or ""
        cut = len(self._buf)
        start = self._buf.rfind("[")
        if start != -1 and "]" not in self._buf[start:] and len(self._buf) - start < self._HOLD:
            cut = start
        ready, self._buf = self._buf[:cut], self._buf[cut:]
        if ready:
            self._emit(self._mapping.restore_text(ready))

    def finish(self) -> None:
        if self._buf:
            self._emit(self._mapping.restore_text(self._buf))
            self._buf = ""


def history_safe(text: str, mapping: PlaceholderMap) -> str:
    """Text that was shown to the owner (real values) made safe to put back into the model's history: known
    values return to their placeholders and any other identifier is swapped like a tool result (R5-10)."""
    if not text:
        return text
    from dourmouse.governance import PiiRedactor

    try:
        redacted, _ = PiiRedactor(owner_emails()).redact(mapping.mask_text(text), mapping)
    except PlaceholderMapFull:
        return "[PRIVACY: this text held more personal identifiers than the chat can swap and was left out.]"
    return redacted


def chat_sources() -> list[str]:
    """The sensitive sources the active chat's Claude or Codex session has read through the bridge."""
    with _chat_sources_lock:
        return sorted(_CHAT_SOURCES.get(_active_chat()) or ())


def owner_emails() -> set[str]:
    """The owner's own addresses, which are never swapped."""
    found = {e.strip().lower() for e in os.environ.get("DOURMOUSE_OWNER_EMAILS", "").split(",") if "@" in e}
    from dourmouse import email_identity

    for value in (email_identity.base_address(), email_identity.own_address(),
                  os.environ.get("DOURMOUSE_EMAIL_ADDRESS", "")):
        if value and "@" in value:
            found.add(value.strip().lower())
    return found


# --------------------------------------------------------------------------- #
# Consent, Private mode and the provider checklist (PRIV-1, PRIV-5)
# --------------------------------------------------------------------------- #

PRIVATE_CHATS_LIMIT = 5000
_store_lock = threading.Lock()

#: Every backend Dourmouse can send a request to.
PROVIDERS: tuple[dict[str, str], ...] = (
    {"id": "ollama_cloud", "label": "Ollama Cloud", "company": "Ollama", "key": "OLLAMA_API_KEY"},
    {"id": "gemini", "label": "Gemini", "company": "Google (AI Studio)", "key": "GEMINI_API_KEY"},
    {"id": "claude_cli", "label": "Claude CLI sign-in", "company": "Anthropic", "cli": "claude"},
    {"id": "codex_cli", "label": "Codex CLI", "company": "OpenAI", "cli": "codex"},
    {"id": "nvidia", "label": "NVIDIA NIM", "company": "NVIDIA", "key": "NVIDIA_API_KEY"},
    {"id": "local_proxy", "label": "FreeLLMAPI or OmniRoute",
     "company": "a proxy on this Mac that forwards to the providers it is set up with",
     "key": "FREELLMAPI_BASE_URL"},
)
_PROVIDER_IDS = frozenset(p["id"] for p in PROVIDERS)


def store_path() -> Path:
    from dourmouse.config import user_config_dir

    return user_config_dir() / "privacy.json"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class PrivacyStore:
    """Consent per source, Private mode, and the provider checklist.

    Read fresh on every question (the file is tiny and the MCP bridge, a
    separate process, reads the same file). A file that cannot be parsed is
    kept aside by ``state_file`` and every source counts as "ask"."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else store_path()
        self.note = ""

    def _read(self) -> dict[str, Any]:
        from dourmouse.state_file import StateUnreadable, load_json_or_aside

        try:
            data, note = load_json_or_aside(self.path, dict)
        except StateUnreadable as exc:
            self.note = str(exc)
            # R5-22: a file nobody can read says nothing about Private mode, so it stays on.
            return {"private_mode": True}
        self.note = note
        if note:
            # The file was corrupt and has been set aside. Consent goes back to "ask", but Private mode must not
            # silently turn off: the replacement file keeps it on until the owner changes it (SETTINGS shows the note).
            data = {"private_mode": True, "recovery_note": note}
            try:
                self._write(data)
            except OSError:
                pass
        return self._validated(data or {})

    @staticmethod
    def _validated(data: dict[str, Any]) -> dict[str, Any]:
        """Wrong types never raise inside the gate: a bad consent table is empty (every source asks), and a bad
        list of Private chats means Private mode is on for all of them."""
        out = dict(data)
        consent = out.get("consent")
        out["consent"] = {k: v for k, v in consent.items() if isinstance(k, str) and v in ("allow", "deny")} \
            if isinstance(consent, dict) else {}
        chats = out.get("private_chats")
        if chats is None:
            out["private_chats"] = {}
        elif not isinstance(chats, dict):
            out["private_chats"], out["private_mode"] = {}, True
        providers = out.get("providers")
        out["providers"] = providers if isinstance(providers, dict) else {}
        out["private_mode"] = bool(out.get("private_mode"))
        return out

    def _write(self, data: dict[str, Any]) -> None:
        from dourmouse.state_file import atomic_write_text

        data["version"] = 1
        atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)

    def snapshot(self) -> dict[str, Any]:
        return self._read()

    def consent(self, source: str) -> str | None:
        value = (self._read().get("consent") or {}).get(source)
        return value if value in ("allow", "deny") else None

    def set_consent(self, source: str, decision: str) -> None:
        if source not in SOURCES:
            raise ValueError(f"{source!r} is not a privacy source")
        if decision not in ("allow", "deny", "ask"):
            raise ValueError("the decision must be allow, deny or ask")
        with _store_lock:
            data = self._read()
            consent = dict(data.get("consent") or {})
            if decision == "ask":
                consent.pop(source, None)
            else:
                consent[source] = decision
            data["consent"] = consent
            self._write(data)

    def private_mode(self) -> bool:
        return bool(self._read().get("private_mode"))

    def set_private_mode(self, enabled: bool) -> None:
        with _store_lock:
            data = self._read()
            data["private_mode"] = bool(enabled)
            data.pop("recovery_note", None)  # the owner has now decided
            self._write(data)

    def chat_private(self, chat: str) -> bool:
        return bool(chat) and chat in (self._read().get("private_chats") or {})

    def private_for(self, chat: str) -> bool:
        data = self._read()
        return bool(data.get("private_mode")) or (bool(chat) and chat in (data.get("private_chats") or {}))

    def set_chat_private(self, chat: str, enabled: bool) -> None:
        chat = (chat or "").strip()
        if not chat or len(chat) > 200:
            raise ValueError("a chat id is required (at most 200 characters)")
        with _store_lock:
            data = self._read()
            chats = dict(data.get("private_chats") or {})
            if enabled:
                if chat not in chats and len(chats) >= PRIVATE_CHATS_LIMIT:
                    # R5-22: never drop an older chat's Private flag to make room; say so instead.
                    raise ValueError(f"{PRIVATE_CHATS_LIMIT} chats are already Private; turn some off first "
                                     "or use the global Private mode")
                chats[chat] = _utc_now()
            else:
                chats.pop(chat, None)
            data["private_chats"] = chats
            self._write(data)

    def set_provider_verified(self, provider: str, verified: bool) -> None:
        if provider not in _PROVIDER_IDS:
            raise ValueError(f"{provider!r} is not a backend Dourmouse uses")
        with _store_lock:
            data = self._read()
            providers = dict(data.get("providers") or {})
            providers[provider] = {"verified": bool(verified), "at": _utc_now()}
            data["providers"] = providers
            self._write(data)


def _provider_configured(p: dict[str, str]) -> bool:
    import shutil

    if "cli" in p:
        return shutil.which(p["cli"]) is not None
    name = p["key"]
    if os.environ.get(name, "").strip():
        return True
    from dourmouse.config import api_key_setting

    return bool(api_key_setting(name))


def provider_table(store: PrivacyStore | None = None) -> list[dict[str, Any]]:
    """Which company each backend sends to, whether it is set up, and
    whether the owner has ticked that they checked its retention and
    training settings (the code cannot check them)."""
    store = store or PrivacyStore()
    saved = store.snapshot().get("providers") or {}
    rows = []
    for p in PROVIDERS:
        mark = saved.get(p["id"]) or {}
        rows.append({
            "id": p["id"], "label": p["label"], "company": p["company"], "region": None,
            "configured": _provider_configured(p),
            "verified": bool(mark.get("verified")), "verified_at": mark.get("at"),
        })
    return rows


_BACKEND_LABELS = {
    "ollama_cloud": "Ollama Cloud (Ollama)",
    "ollama_local": "Ollama endpoint on this Mac",
    "gemini": "Gemini (Google)",
    "claude_cli": "Claude CLI (Anthropic)",
    "codex_cli": "Codex CLI (OpenAI)",
    "nvidia": "NVIDIA NIM (NVIDIA)",
    "local_proxy": "Proxy on this Mac (FreeLLMAPI or OmniRoute)",
}


def provider_label(backend: str) -> str:
    return _BACKEND_LABELS.get(backend, backend or "unknown provider")


def backend_for_base_url(base_url: str) -> str:
    """The backend id of an OpenAI-compatible or Ollama base URL."""
    url = (base_url or "").lower()
    if "ollama.com" in url:
        return "ollama_cloud"
    if "nvidia.com" in url:
        return "nvidia"
    if "googleapis.com" in url:
        return "gemini"
    if "127.0.0.1" in url or "localhost" in url or "[::1]" in url:
        return "ollama_local" if ":11434" in url else "local_proxy"
    return re.sub(r"^https?://", "", url).split("/")[0] or "unknown"


# --------------------------------------------------------------------------- #
# The turn: chat id, Private mode, counters, the placeholder map
# --------------------------------------------------------------------------- #

@dataclass
class TurnPrivacy:
    chat: str
    private: bool
    placeholders: PlaceholderMap
    provider: str = "the cloud model"
    tiers: Counter[str] = field(default_factory=Counter)
    sources: set[str] = field(default_factory=set)
    redactions: Counter[str] = field(default_factory=Counter)
    #: one-call answers from a plain confirmation callable (not stored)
    once: dict[str, bool] = field(default_factory=dict)
    query: str = ""
    #: bytes of gated tool results the MCP bridge served (bridge process only)
    served_bytes: int = 0
    #: the backend id requests of this turn go to (ollama_cloud, claude_cli ...); "" when unknown
    backend: str = ""


_TURN: contextvars.ContextVar[TurnPrivacy | None] = contextvars.ContextVar("dourmouse_privacy_turn", default=None)
_CHAT: contextvars.ContextVar[str] = contextvars.ContextVar("dourmouse_privacy_chat", default="")


def current_turn() -> TurnPrivacy | None:
    return _TURN.get()


def current_chat() -> str:
    return _CHAT.get()


@contextlib.contextmanager
def chat_scope(chat: str) -> Iterator[None]:
    """The chat (the UI's tab id) the requests in this block belong to."""
    token = _CHAT.set((chat or "").strip() or "default")
    try:
        yield
    finally:
        _CHAT.reset(token)


_chat_tokens = threading.local()


def enter_chat(chat: str) -> None:
    """``chat_scope`` for a request handler that cannot use ``with``; paired
    with ``leave_chat()`` in the request's finally (the project_scope pattern)."""
    _chat_tokens.token = _CHAT.set((chat or "").strip() or "default")


def leave_chat() -> None:
    token = getattr(_chat_tokens, "token", None)
    if token is not None:
        try:
            _CHAT.reset(token)
        except ValueError:  # set in a different context; clear instead
            _CHAT.set("")
        _chat_tokens.token = None


@contextlib.contextmanager
def turn_scope(chat: str = "", *, private: bool | None = None, query: str = "") -> Iterator[TurnPrivacy]:
    """The privacy state of one top-level request. A nested run (a delegate,
    a parallel branch, a delegated model task) inherits the active one, so a
    whole request tree shares one gate."""
    active = _TURN.get()
    if active is not None:
        yield active
        return
    key = (chat or "").strip() or _CHAT.get() or "default"
    if private is None:
        private = PrivacyStore().private_for(key)
    turn = TurnPrivacy(chat=key, private=bool(private), placeholders=placeholder_map(key), query=query)
    token = _TURN.set(turn)
    try:
        yield turn
    finally:
        _TURN.reset(token)


@contextlib.contextmanager
def use_turn(turn: TurnPrivacy) -> Iterator[TurnPrivacy]:
    """Make ``turn`` the active privacy state (and its chat the active chat) for this block. The MCP bridge uses
    it around each tool call, so a delegated run started from the bridge joins the bridge's gate (R5-6, R5-7)."""
    token = _TURN.set(turn)
    chat_token = _CHAT.set(turn.chat)
    try:
        yield turn
    finally:
        _CHAT.reset(chat_token)
        _TURN.reset(token)


def forward_refusal(target_backend: str) -> str | None:
    """A refusal text when this request has read a sensitive source and the text would be handed to a different
    company than the one the owner's consent was asked about (R5-6); None when it may go. Consent is given for
    "a cloud model" in the prompt, but the model-written task of a code or delegate tool may quote what was read,
    so it only goes to the request's own backend."""
    turn = _TURN.get()
    if turn is None or not turn.sources:
        return None
    if turn.backend and turn.backend == target_backend:
        return None
    return ("REFUSED: this request has already read "
            + ", ".join(SOURCES[s]["label"] for s in sorted(turn.sources) if s in SOURCES)
            + f", and a task for {provider_label(target_backend)} could carry it to a different company than "
            "the one you allowed. Ask for it in a new chat, or do the step with this chat's own model.")


def auto_injection_allowed(source: str = "memory") -> bool:
    """Context that is added without a tool call (shared memory before a
    Claude CLI task, global memory before a turn) is added only when the
    owner has allowed the source and Private mode is off. Never asks."""
    store = PrivacyStore()
    turn = _TURN.get()
    private = turn.private if turn is not None else store.private_for(_CHAT.get() or "default")
    return not private and store.consent(source) == "allow"


def mapping_for_current_chat() -> PlaceholderMap:
    turn = _TURN.get()
    return turn.placeholders if turn is not None else placeholder_map(_CHAT.get() or "default")


# --------------------------------------------------------------------------- #
# The MCP bridge runs in its own process (Claude and Codex start it)
# --------------------------------------------------------------------------- #

BRIDGE_CHAT_ENV = "DOURMOUSE_PRIVACY_CHAT"
BRIDGE_PRIVATE_ENV = "DOURMOUSE_PRIVACY_PRIVATE"
BRIDGE_MAP_ENV = "DOURMOUSE_PRIVACY_MAP"
BRIDGE_BACKEND_ENV = "DOURMOUSE_PRIVACY_BACKEND"
#: A bridge started without a map (Codex starts its own) numbers from here,
#: so its placeholders can never be read as one of a chat's own.
_BRIDGE_UNMAPPED_START = 9000


def bridge_handoff(backend: str = "") -> tuple[dict[str, str], Path]:
    """Environment for a CLI whose MCP bridge serves this chat, and the 0600
    temp file holding the chat's placeholder map for this one call."""
    import tempfile

    turn = _TURN.get()
    chat = turn.chat if turn is not None else (_CHAT.get() or "default")
    private = turn.private if turn is not None else PrivacyStore().private_for(chat)
    mapping = turn.placeholders if turn is not None else placeholder_map(chat)
    fd, name = tempfile.mkstemp(prefix="dourmouse-privacy-map-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:  # mkstemp creates it 0600
        json.dump(mapping.export(), fh)
    env = {BRIDGE_CHAT_ENV: chat, BRIDGE_PRIVATE_ENV: "1" if private else "0", BRIDGE_MAP_ENV: name}
    if backend:
        env[BRIDGE_BACKEND_ENV] = backend
    with _never_lock:
        hashes = sorted(_NEVER_HASHES)
    if hashes:
        env[BRIDGE_SECRET_ENV] = ",".join(f"{n}:{h}" for n, h in hashes)
    return env, Path(name)


_CHAT_SOURCES: dict[str, set[str]] = {}
_chat_sources_lock = threading.Lock()


def _active_chat() -> str:
    turn = _TURN.get()
    return turn.chat if turn is not None else (_CHAT.get() or "default")


def bridge_collect(path: Path, backend: str = "claude_cli") -> None:
    """Merge what the bridge added to the map, the sources and tiers it served (R5-11), write one ledger row
    for the results the bridge sent to the CLI's provider, then remove the file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        mapping_for_current_chat().merge(data)
        sources = [x for x in (data.get("sources") or []) if isinstance(x, str) and x in SOURCES]
        tiers = {k: int(v) for k, v in (data.get("tiers") or {}).items() if k in TIERS and isinstance(v, int)}
        served = int(data.get("served_bytes") or 0)
        if sources:
            with _chat_sources_lock:
                _CHAT_SOURCES.setdefault(_active_chat(), set()).update(sources)
        turn = _TURN.get()
        if turn is not None:
            turn.sources.update(sources)
            for k, v in tiers.items():
                turn.tiers[k] += v
        if served or sources or any(tiers.values()):
            record_egress(backend=backend, model="", text="", origin=f"{backend}_bridge_results",
                          tiers=tiers, sources=sources, size=served)
    Path(path).unlink(missing_ok=True)


def session_must_restart() -> bool:
    """True when the Claude session of the active chat holds results it may no longer keep: the chat is Private
    now, or a source it read has since been set to NEVER (R5-9). The caller starts a fresh session."""
    chat = _active_chat()
    with _chat_sources_lock:
        sources = set(_CHAT_SOURCES.get(chat) or ())
    if not sources:
        return False
    store = PrivacyStore()
    turn = _TURN.get()
    private = turn.private if turn is not None else store.private_for(chat)
    if private or any(store.consent(src) == "deny" for src in sources):
        with _chat_sources_lock:
            _CHAT_SOURCES.pop(chat, None)
        return True
    return False


def bridge_turn() -> TurnPrivacy:
    """The privacy state of one tool call inside the bridge process."""
    chat = os.environ.get(BRIDGE_CHAT_ENV, "").strip() or "default"
    private = os.environ.get(BRIDGE_PRIVATE_ENV) == "1" or PrivacyStore().private_for(chat)
    mapping = PlaceholderMap()
    path = os.environ.get(BRIDGE_MAP_ENV, "")
    loaded = False
    backend = os.environ.get(BRIDGE_BACKEND_ENV, "").strip()
    for item in os.environ.get(BRIDGE_SECRET_ENV, "").split(","):
        n, _, h = item.partition(":")
        if n.isdigit():
            register_never_send_hash(int(n), h)
    turn = TurnPrivacy(chat=chat, private=private, placeholders=mapping, provider="the CLI's provider",
                       backend=backend)
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            mapping.merge(data)
            loaded = True
            turn.sources.update(x for x in (data.get("sources") or []) if x in SOURCES)
            turn.tiers.update({k: int(v) for k, v in (data.get("tiers") or {}).items()
                               if k in TIERS and isinstance(v, int)})
            turn.served_bytes = int(data.get("served_bytes") or 0)
    if not loaded:
        for kind in ("CARD", "IBAN", "ID", "EMAIL", "PHONE", "ADDRESS"):
            mapping._next[kind] = _BRIDGE_UNMAPPED_START
    return turn


def bridge_save(turn: TurnPrivacy) -> None:
    path = os.environ.get(BRIDGE_MAP_ENV, "")
    if path and os.path.exists(path):
        from dourmouse.state_file import atomic_write_text

        data = turn.placeholders.export()
        data["sources"] = sorted(turn.sources)
        data["tiers"] = {k: int(v) for k, v in turn.tiers.items()}
        data["served_bytes"] = int(turn.served_bytes)
        atomic_write_text(Path(path), json.dumps(data), mode=0o600)


# --------------------------------------------------------------------------- #
# The gate (one function for the dispatch loop and the MCP bridge)
# --------------------------------------------------------------------------- #

class DispatcherText(str):
    """Text the dispatcher itself wrote (a refusal, a confirmation or an error line). A tool handler cannot make
    one, so only this type is exempt from the consent and size checks; text that merely starts with "ERROR" or
    "CONFIRMATION REQUIRED" is content like any other (R5-15)."""


T2_MAX_CHARS = 12_000
_NOT_CONTENT_PREFIXES = (
    "ERROR", "REFUSED", "NOT CONFIGURED", "DECLINED", "CONFIRMATION REQUIRED", "BLOCKED", "CANCELLED",
    "NOT SENT", "Withheld",
)

_LONG_STATUS_PREFIXES = ("CONFIRMATION REQUIRED", "DECLINED BY USER")

#: ``ask(prompt) -> (approved or None when nobody answered, remember the answer)``
Asker = Callable[[str], tuple[bool | None, bool]]


def consent_prompt(source: str, provider: str) -> str:
    info = SOURCES[source]
    return (f"Send your {info['label']} ({info['what']}) to {provider} to answer this and later requests? "
            f"Identifiers are swapped for placeholders first; names and free text are not. It is shared with "
            f"cloud models, not only this one, so change it any time in SETTINGS, PRIVACY.")


def asker_for(confirmation_gate: Any) -> Asker | None:
    """How to ask the owner through a run's confirmation channel.

    The web gate's ``ask_owner`` shows a real card that auto approve never
    answers, and returns None when nobody could answer: that answer is
    stored. A gate that marks itself ``interactive_consent`` (the terminal
    prompt, where the owner types the answer) approves this one request only.
    Any other callable (a goal runner, a test stub, a gate that auto approves
    or approves a ticket for one action) is never asked: consent is a privacy
    choice, not an action approval (R5-4). No gate: nobody can be asked."""
    if confirmation_gate is None:
        return None
    ask_owner = getattr(confirmation_gate, "ask_owner", None)
    if callable(ask_owner):
        def ask(prompt: str) -> tuple[bool | None, bool]:
            answer = ask_owner(prompt)
            return answer, answer is not None
        return ask
    if not getattr(confirmation_gate, "interactive_consent", False):
        return None

    def ask_plain(prompt: str) -> tuple[bool | None, bool]:
        return bool(confirmation_gate(prompt)), False
    return ask_plain


def _not_sent_consent(source: str) -> str:
    return (f"NOT SENT: the owner has not allowed {SOURCES[source]['label']} to be shared with a cloud model; "
            f"ask them to turn it on in SETTINGS, PRIVACY.")


def _not_sent_private(source: str) -> str:
    return (f"NOT SENT: Private mode is on for this chat, so {SOURCES[source]['label']} is not shared with a "
            f"cloud model. The owner can turn Private mode off in the chat toolbar or in SETTINGS, PRIVACY.")


def _minimise(text: str, query: str) -> str:
    """Cut an oversized T2 result to the paragraphs that share the most words
    with the request, kept in their original order."""
    if len(text) <= T2_MAX_CHARS:
        return text
    words = set(re.findall(r"[a-z0-9]{3,}", (query or "").lower()))
    parts = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) < 2:
        parts = text.splitlines(keepends=True)
    scored = sorted(range(len(parts)),
                    key=lambda i: (-len(words & set(re.findall(r"[a-z0-9]{3,}", parts[i].lower()))), i))
    keep: list[int] = []
    used = 0
    budget = T2_MAX_CHARS - 300
    for i in scored:
        size = len(parts[i]) + 2
        if used + size > budget:
            if not keep:
                keep.append(i)
                used = budget
            continue
        keep.append(i)
        used += size
    body = "\n\n".join(parts[i] for i in sorted(keep))[:budget]
    return (body + f"\n\n[PRIVACY: this result was {len(text):,} characters; only the {len(body):,} that match "
            "the request were sent. Ask for a specific part (one message, one section) to see more.]")


def gate_tool_result(
    name: str,
    arguments: Any,
    text: str,
    *,
    turn: TurnPrivacy,
    agent: str | None = None,
    ask: Asker | None = None,
    store: PrivacyStore | None = None,
    own_text: bool | None = None,
) -> str:
    """The text of a tool result that may be sent to the cloud model. ``own_text`` says the dispatcher wrote the
    text itself (default: the text is a ``DispatcherText``)."""
    if not text:
        return text
    reason = t3_reason(name, arguments, text)
    if reason is not None:
        turn.tiers["T3"] += 1
        return f"NOT SENT: this result holds {reason}, which never goes to a cloud model. It stayed on this Mac."
    tier, source = classify(name, arguments, agent)
    # The dispatcher's own confirmation and refusal messages carry the action's prompt text and can
    # be long (finding #179: a long privileged-command prompt was replaced by "NOT SENT"); private
    # data does not start with these phrases, so they get a larger cap than a bare ERROR line.
    trusted = isinstance(text, DispatcherText) if own_text is None else own_text
    long_ok = trusted and text.startswith(_LONG_STATUS_PREFIXES) and len(text) < 4000
    is_status = trusted and ((text.startswith(_NOT_CONTENT_PREFIXES) and len(text) < 600) or long_ok)
    if tier == "T2" and source is not None and not is_status:
        if turn.private:
            return _not_sent_private(source)
        store = store or PrivacyStore()
        decision = store.consent(source)
        if decision == "deny":
            return _not_sent_consent(source)
        if decision is None:
            approved = turn.once.get(source)
            if approved is None and ask is not None:
                approved, remember = ask(consent_prompt(source, turn.provider))
                if approved is not None:
                    turn.once[source] = approved
                    if remember:
                        store.set_consent(source, "allow" if approved else "deny")
            if not approved:
                return _not_sent_consent(source)
        text = _minimise(text, turn.query)
        turn.sources.add(source)
    turn.tiers[tier] += 1
    from dourmouse.governance import PiiRedactor

    try:
        redacted, counts = PiiRedactor(owner_emails()).redact(text, turn.placeholders)
    except PlaceholderMapFull:
        turn.tiers["T3"] += 1
        return ("NOT SENT: this chat already holds too many distinct personal identifiers to swap safely, so the "
                "result stayed on this Mac. Start a new chat to continue.")
    if counts:
        turn.redactions.update(counts)
        redacted += ("\n[PRIVACY: personal identifiers were replaced by placeholders such as [EMAIL_1]. Use them "
                     "exactly as written; Dourmouse fills in the real values on this Mac.]")
    return redacted


# --------------------------------------------------------------------------- #
# History: what a chat already holds when Private mode or a NEVER arrives (R5-9)
# --------------------------------------------------------------------------- #

PRIV_TAG = "_priv_source"
_DELEGATE_TOOLS = frozenset({"delegate_task", "delegate_parallel", "delegate_to_models"})


def result_sources(name: str, arguments: Any, text: str, agent: str | None, turn: TurnPrivacy) -> list[str]:
    """The consent sources a gated tool result (already passed to the model) carries, for tagging its message.
    A T2 result carries its own source; a delegated run's answer carries every source this request has read."""
    if not text or text.startswith("NOT SENT"):
        return []
    if (name or "") in _DELEGATE_TOOLS:
        return sorted(turn.sources)
    tier, source = classify(name, arguments, agent)
    return [source] if tier == "T2" and source else []


def strip_tags(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages as a provider should see them (no private bookkeeping keys)."""
    if not any(PRIV_TAG in m for m in messages):
        return messages
    return [{k: v for k, v in m.items() if k != PRIV_TAG} for m in messages]


def tag_new_messages(messages: list[dict[str, Any]], start: int, turn: TurnPrivacy) -> None:
    """After a top-level request: the assistant text produced after the first sensitive result of this request
    was written from it, so it carries the sources the request read."""
    seen = False
    for m in messages[start:]:
        if m.get(PRIV_TAG):
            seen = True
        elif seen and m.get("role") == "assistant" and turn.sources:
            m[PRIV_TAG] = sorted(turn.sources)


def scrub_history(messages: list[dict[str, Any]], *, private: bool, store: PrivacyStore | None = None) -> int:
    """Remove from the history what the chat may no longer send: when the chat is Private, every message that
    carries a sensitive source; otherwise the messages whose source the owner has since set to NEVER. The text
    is replaced by a one-line note; returns how many messages were changed (R5-9)."""
    if not any(PRIV_TAG in m for m in messages):
        return 0
    store = store or PrivacyStore()
    denied = {src for src in SOURCES if store.consent(src) == "deny"}
    changed = 0
    for m in messages:
        tags = m.get(PRIV_TAG)
        if not tags:
            continue
        tags = [tags] if isinstance(tags, str) else list(tags)
        gone = [t for t in tags if private or t in denied]
        if not gone:
            continue
        label = ", ".join(SOURCES[t]["label"] for t in gone if t in SOURCES) or "sensitive content"
        if m.get("role") == "tool":
            m["content"] = (f"[PRIVACY: an earlier result from {label} was removed from this chat's context "
                            f"because it is no longer shared with a cloud model.]")
        else:
            m["content"] = (f"[PRIVACY: an earlier answer that used {label} was removed from this chat's context "
                            f"because it is no longer shared with a cloud model.]")
            m.pop("tool_calls", None)
        m.pop(PRIV_TAG, None)
        changed += 1
    return changed


# --------------------------------------------------------------------------- #
# Egress ledger (PRIV-4)
# --------------------------------------------------------------------------- #

EGRESS_MAX_BYTES = 1_000_000
EGRESS_KEEP = 3
_egress_lock = threading.Lock()
_egress_failures = 0


def egress_path() -> Path:
    """The ledger lives in the settings folder, which the model's file and command tools cannot write (R5-18);
    it used to sit in the workspace, where they could append, forge or truncate it."""
    from dourmouse.config import user_config_dir

    return user_config_dir() / "privacy" / "egress.jsonl"


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:32]


def _last_line(path: Path) -> str:
    for candidate in (path, Path(f"{path}.1")):
        try:
            lines = [ln for ln in candidate.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except OSError:
            continue
        if lines:
            return lines[-1]
    return ""


def egress_chain_status() -> dict[str, Any]:
    """Whether the ledger rows still form the chain they were written in: each row names the hash of the row
    before it and the next sequence number, so a deleted, edited or inserted row shows (R5-18)."""
    path = egress_path()
    prev_line = ""
    prev_seq: int | None = None
    count = 0
    for candidate in [Path(f"{path}.{i}") for i in range(EGRESS_KEEP, 0, -1)] + [path]:
        if not candidate.exists():
            continue
        for raw in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                return {"ok": False, "rows": count, "broken_at": count + 1}
            count += 1
            if prev_seq is not None and (row.get("seq") != prev_seq + 1 or row.get("prev") != _line_hash(prev_line)):
                return {"ok": False, "rows": count, "broken_at": row.get("seq")}
            prev_line, prev_seq = raw, row.get("seq") if isinstance(row.get("seq"), int) else None
            if prev_seq is None:
                return {"ok": False, "rows": count, "broken_at": count}
    return {"ok": True, "rows": count, "broken_at": None}


def egress_failures() -> int:
    return _egress_failures


def _chat_id(chat: str) -> str:
    return hashlib.sha256(("dourmouse-chat:" + chat).encode("utf-8")).hexdigest()[:12] if chat else ""


def record_egress(
    *,
    backend: str,
    model: str = "",
    messages: list[dict[str, Any]] | None = None,
    text: str | None = None,
    origin: str = "",
    tiers: dict[str, int] | None = None,
    sources: list[str] | None = None,
    size: int | None = None,
) -> None:
    """One metadata line for one request to a model. Never the content: only
    its size and a SHA-256 of it."""
    global _egress_failures
    if messages is not None:
        payload = json.dumps(messages, default=str, sort_keys=True, ensure_ascii=False)
        count = len(messages)
    else:
        payload = text or ""
        count = 1
    data = payload.encode("utf-8", "replace")
    turn = _TURN.get()
    tier_row = dict.fromkeys(TIERS, 0)
    if tiers is not None:
        tier_row.update(tiers)  # what one batch of results carried, not the whole request tree's total
    elif turn is not None:
        tier_row.update({k: int(v) for k, v in turn.tiers.items()})
    row = {
        "ts": _utc_now(),
        "provider": provider_label(backend),
        "backend": backend,
        "model": str(model or "")[:120],
        "origin": origin,
        "chat": _chat_id(turn.chat if turn is not None else _CHAT.get()),
        "tiers": tier_row,
        "sources": sorted(sources) if sources is not None else (sorted(turn.sources) if turn is not None else []),
        "redactions": dict(sorted(turn.redactions.items())) if turn is not None else {},
        "private": bool(turn.private) if turn is not None else (
            bool(_CHAT.get()) and PrivacyStore().private_for(_CHAT.get())),
        "bytes": len(data) if size is None else int(size),
        "messages": count,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    path = egress_path()
    try:
        with _egress_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            last = _last_line(path)
            try:
                last_seq = int(json.loads(last).get("seq") or 0) if last else 0
            except (ValueError, AttributeError):
                last_seq = 0
            row["seq"] = last_seq + 1
            row["prev"] = _line_hash(last) if last else ""
            line = json.dumps(row, sort_keys=True) + "\n"
            if path.exists() and path.stat().st_size + len(line) > EGRESS_MAX_BYTES:
                _rotate(path)
            fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError as exc:
        # The request itself is not stopped by a full disk; the failure is
        # counted and shown in SECURITY next to the ledger, never hidden.
        _egress_failures += 1
        print(f"[PRIVACY] the egress ledger could not be written: {exc}")


def record_client_request(client: Any, model: str, messages: list[dict[str, Any]], origin: str,
                          base_url: str = "") -> None:
    """One ledger row for a request made through an OpenAI-compatible client that is not the dispatch loop's
    (the orchestrator, the ATLAS lab, the profile, a key check). H-PRIV-6."""
    url = base_url or str(getattr(client, "base_url", "") or "")
    record_egress(backend=backend_for_base_url(url), model=model, messages=messages, origin=origin)


def _rotate(path: Path) -> None:
    for i in range(EGRESS_KEEP, 0, -1):
        older = Path(f"{path}.{i}")
        newer = Path(f"{path}.{i - 1}") if i > 1 else path
        if i == EGRESS_KEEP and older.exists():
            older.unlink()
        if newer.exists():
            os.replace(newer, older)


def read_egress(limit: int = 100) -> list[dict[str, Any]]:
    """The newest ``limit`` rows, newest first."""
    path = egress_path()
    rows: list[dict[str, Any]] = []
    for candidate in [path] + [Path(f"{path}.{i}") for i in range(1, EGRESS_KEEP + 1)]:
        if len(rows) >= limit or not candidate.exists():
            continue
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        for raw in reversed(lines):
            if len(rows) >= limit:
                break
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows
