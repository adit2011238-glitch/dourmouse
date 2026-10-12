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
  (never content), under ``<workspace>/privacy/egress.jsonl``.

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
    # Apps, screen and browser pages
    "browser_snapshot": "screen", "browser_extract": "screen", "browser_screenshot": "screen",
    "browser_click": "screen", "browser_type": "screen", "browser_fill": "screen", "browser_fill_form": "screen",
    "browser_select": "screen", "browser_press": "screen", "browser_submit": "screen", "browser_wait": "screen",
    "browser_back": "screen", "browser_media": "screen",
    "app_driver_snapshot": "screen", "app_driver_click": "screen", "app_driver_type": "screen",
    "app_driver_press_key": "screen", "app_driver_scroll": "screen",
    "clipboard_get": "screen", "security_browser_history": "screen",
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
})

#: Tools that read local files or run local code: their arguments are checked
#: for credential paths (T3).
_LOCAL_READERS = frozenset({
    "read_path", "read_file", "review_read_file", "study_read_file", "extract_pdf", "extract_receipt",
    "read_upload", "run_command", "run_privileged_command", "run_python", "run_python_host",
    "compute_run_python", "edit_file", "search_files", "review_search_files", "study_search_files",
})


def classify(name: str, arguments: Any, agent: str | None = None) -> tuple[str, str | None]:
    """``(tier, source)`` of a tool's result. T3 is decided separately by
    ``t3_reason`` because it depends on the result's content too."""
    name = (name or "").strip()
    if name in _PATH_TOOLS:
        path = _first_path(arguments)
        if path and _inside_workspace(path):
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

_NEVER_SEND: set[str] = set()
_never_lock = threading.Lock()


def register_never_send(value: str) -> None:
    """A value that must never reach a model, matched exactly (the server
    registers its per-launch owner secret here at start)."""
    if value and len(value) >= 16:
        with _never_lock:
            _NEVER_SEND.add(value)


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
    body = text or ""
    if _PRIVATE_KEY_BLOCK.search(body):
        return "a private key"
    if _COOKIE_FILE.search(body):
        return "browser cookies"
    if _KEYCHAIN_DUMP.search(body):
        return "Keychain items"
    with _never_lock:
        secrets = tuple(_NEVER_SEND)
    if any(s in body for s in secrets):
        return "the owner secret"
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


class PlaceholderMap:
    """Value <-> placeholder for one chat, in memory only."""

    MAX_VALUES = 2000

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_value: dict[tuple[str, str], str] = {}
        self._by_placeholder: dict[str, tuple[str, str]] = {}
        self._next: Counter[str] = Counter()

    def placeholder(self, kind: str, value: str) -> str:
        with self._lock:
            key = (kind, value)
            found = self._by_value.get(key)
            if found:
                return found
            if len(self._by_placeholder) >= self.MAX_VALUES:
                return f"[{kind}]"  # full: still never sent, just not restorable
            self._next[kind] += 1
            ph = f"[{kind}_{self._next[kind]}]"
            self._by_value[key] = ph
            self._by_placeholder[ph] = key
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

    def restore_arguments(self, value: Any) -> Any:
        """A tool call's arguments with placeholders filled in, at any depth,
        except card and bank numbers."""
        if isinstance(value, str):
            return self._restore(value, _NOT_FOR_ARGUMENTS)
        if isinstance(value, dict):
            return {k: self.restore_arguments(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore_arguments(v) for v in value]
        return value

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


def placeholder_map(chat: str) -> PlaceholderMap:
    """The placeholder map of one chat (kept for the 64 most recent chats)."""
    key = chat or "default"
    with _maps_lock:
        found = _MAPS.get(key)
        if found is None:
            found = PlaceholderMap()
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

PRIVATE_CHATS_LIMIT = 500
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
            return {}
        self.note = note
        return data or {}

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
                chats[chat] = _utc_now()
                if len(chats) > PRIVATE_CHATS_LIMIT:
                    for old in sorted(chats, key=lambda k: chats[k])[: len(chats) - PRIVATE_CHATS_LIMIT]:
                        chats.pop(old, None)
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
#: A bridge started without a map (Codex starts its own) numbers from here,
#: so its placeholders can never be read as one of a chat's own.
_BRIDGE_UNMAPPED_START = 9000


def bridge_handoff() -> tuple[dict[str, str], Path]:
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
    return env, Path(name)


def bridge_collect(path: Path) -> None:
    """Merge what the bridge added to the map, then remove the file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        mapping_for_current_chat().merge(data)
    Path(path).unlink(missing_ok=True)


def bridge_turn() -> TurnPrivacy:
    """The privacy state of one tool call inside the bridge process."""
    chat = os.environ.get(BRIDGE_CHAT_ENV, "").strip() or "default"
    private = os.environ.get(BRIDGE_PRIVATE_ENV) == "1" or PrivacyStore().private_for(chat)
    mapping = PlaceholderMap()
    path = os.environ.get(BRIDGE_MAP_ENV, "")
    loaded = False
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            mapping.merge(data)
            loaded = True
    if not loaded:
        for kind in ("CARD", "IBAN", "ID", "EMAIL", "PHONE", "ADDRESS"):
            mapping._next[kind] = _BRIDGE_UNMAPPED_START
    return TurnPrivacy(chat=chat, private=private, placeholders=mapping, provider="the CLI's provider")


def bridge_save(turn: TurnPrivacy) -> None:
    path = os.environ.get(BRIDGE_MAP_ENV, "")
    if path and os.path.exists(path):
        from dourmouse.state_file import atomic_write_text

        atomic_write_text(Path(path), json.dumps(turn.placeholders.export()), mode=0o600)


# --------------------------------------------------------------------------- #
# The gate (one function for the dispatch loop and the MCP bridge)
# --------------------------------------------------------------------------- #

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
    return (f"Send your {info['label']} ({info['what']}) to {provider} to answer this and later requests "
            f"(identifiers are swapped for placeholders first; change it any time in SETTINGS, PRIVACY)?")


def asker_for(confirmation_gate: Any) -> Asker | None:
    """How to ask the owner through a run's confirmation channel.

    The web gate's ``ask_owner`` shows a real card that auto approve never
    answers, and returns None when nobody could answer: that answer is
    stored. A plain callable (the terminal, a background goal) approves this
    one request only. No gate: nobody can be asked."""
    if confirmation_gate is None:
        return None
    ask_owner = getattr(confirmation_gate, "ask_owner", None)
    if callable(ask_owner):
        def ask(prompt: str) -> tuple[bool | None, bool]:
            answer = ask_owner(prompt)
            return answer, answer is not None
        return ask

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
) -> str:
    """The text of a tool result that may be sent to the cloud model."""
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
    long_ok = text.startswith(_LONG_STATUS_PREFIXES) and len(text) < 4000
    is_status = (text.startswith(_NOT_CONTENT_PREFIXES) and len(text) < 600) or long_ok
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

    redacted, counts = PiiRedactor(owner_emails()).redact(text, turn.placeholders)
    if counts:
        turn.redactions.update(counts)
        redacted += ("\n[PRIVACY: personal identifiers were replaced by placeholders such as [EMAIL_1]. Use them "
                     "exactly as written; Dourmouse fills in the real values on this Mac.]")
    return redacted


# --------------------------------------------------------------------------- #
# Egress ledger (PRIV-4)
# --------------------------------------------------------------------------- #

EGRESS_MAX_BYTES = 1_000_000
EGRESS_KEEP = 3
_egress_lock = threading.Lock()
_egress_failures = 0


def egress_path() -> Path:
    from dourmouse.config import workspace_dir

    return workspace_dir() / "privacy" / "egress.jsonl"


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
    tiers = dict.fromkeys(TIERS, 0)
    if turn is not None:
        tiers.update({k: int(v) for k, v in turn.tiers.items()})
    row = {
        "ts": _utc_now(),
        "provider": provider_label(backend),
        "backend": backend,
        "model": str(model or "")[:120],
        "origin": origin,
        "chat": _chat_id(turn.chat) if turn is not None else "",
        "tiers": tiers,
        "sources": sorted(turn.sources) if turn is not None else [],
        "redactions": dict(sorted(turn.redactions.items())) if turn is not None else {},
        "private": bool(turn.private) if turn is not None else False,
        "bytes": len(data),
        "messages": count,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    line = json.dumps(row, sort_keys=True) + "\n"
    path = egress_path()
    try:
        with _egress_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
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
