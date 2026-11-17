"""What starts an automation (plan 2.2, finding 207).

Four trigger types. ``time`` is the old clock and is judged by the scheduler (``schedules.next_run``). The other
three are events the runner polls once per tick:

* ``file_appears``: a new file shows up in one folder. The first look only records what is already there, so
  switching it on never fires for old files. Only names are listed; no file is opened.
* ``security_finding``: a new needs-you or heads-up item from the Security screen reaches the attention feed.
* ``mail_match``: switched off until plan 3.1 (``MAIL_TRIGGER_ENABLED``). It cannot be created and never fires.

A poll returns the events and the state to keep; it never runs anything and never raises on a missing folder, it
reports it in the state so the screen can say so.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

#: Mail reaches the automation runner only after the security work in plan 3.1. Flip this there, nowhere else.
MAIL_TRIGGER_ENABLED = False

TRIGGER_TYPES = ("time", "file_appears", "security_finding", "mail_match")
_MAX_SEEN = 2000
_PARTIAL = (".crdownload", ".part", ".download", ".tmp", ".partial")
#: Folders nobody should watch for new files: they hold keys and system state, and a name there is not a file the
#: owner meant to hand to an automation.
_BLOCKED_PARTS = (".ssh", ".gnupg", "keychains", ".aws", ".config/gcloud")
_BLOCKED_ROOTS = ("/system", "/private/etc", "/etc", "/usr", "/bin", "/sbin", "/var/db")


def clean_folder(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("choose the folder to watch")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("the folder must be a full path")
    resolved = os.path.realpath(path)
    low = resolved.lower()
    if any(low == r or low.startswith(r + "/") for r in _BLOCKED_ROOTS) or any(f"/{p}" in low for p in _BLOCKED_PARTS):
        raise ValueError("that folder holds keys or system files and cannot be watched")
    if not os.path.isdir(resolved):
        raise ValueError("that folder does not exist")
    return resolved


def clean_trigger(raw: Any, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """The stored shape of a trigger, or ValueError with plain words. ``spec`` is the parsed clock for ``time``."""
    if not isinstance(raw, dict):
        raise ValueError("a trigger is required")
    kind = str(raw.get("type") or "").strip()
    if kind not in TRIGGER_TYPES:
        raise ValueError("the trigger must be a time, a new file, or a security finding")
    if kind == "mail_match":
        # Not an accident: the mail trigger would feed mail text to an unattended run.
        raise ValueError("the mail trigger is not available yet; it waits for the security work in plan 3.1")
    if kind == "time":
        if not spec or not spec.get("kind"):
            raise ValueError("a time trigger needs a schedule")
        return {"type": "time"}
    if kind == "file_appears":
        pattern = str(raw.get("pattern") or "*").strip() or "*"
        if len(pattern) > 60 or "/" in pattern or "\\" in pattern:
            raise ValueError("the file pattern is one name pattern such as *.pdf")
        return {"type": "file_appears", "folder": clean_folder(raw.get("folder")), "pattern": pattern}
    return {"type": "security_finding"}


def describe(entry: dict[str, Any], describe_spec: Any) -> str:
    """The trigger in words, for the list row."""
    trigger = entry.get("trigger") or {}
    kind = trigger.get("type") or "time"
    if kind == "file_appears":
        folder = Path(str(trigger.get("folder") or "")).name or "a folder"
        pattern = trigger.get("pattern") or "*"
        return f"When a file appears in {folder}" if pattern == "*" else f"When {pattern} appears in {folder}"
    if kind == "security_finding":
        return "When Security finds something"
    if kind == "mail_match":
        return "When mail matches (not available yet)"
    spec = entry.get("spec") or {}
    text = describe_spec(spec) if spec.get("kind") else ""
    m = re.fullmatch(r"every (\d+) (minute|hour|day)\(s\)", text)
    if m:  # real plurals: "every hour", "every 30 minutes"
        text = f"every {m.group(2)}" if m.group(1) == "1" else f"every {m.group(1)} {m.group(2)}s"
    return text[:1].upper() + text[1:] if text else "No schedule"


def is_event(entry: dict[str, Any]) -> bool:
    return (entry.get("trigger") or {}).get("type") in ("file_appears", "security_finding", "mail_match")


def poll(entry: dict[str, Any], *, attention_store: Any = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(events, state changes). One event per poll at most: a burst of ten new files is one run, not ten."""
    trigger = entry.get("trigger") or {}
    state = entry.get("state") or {}
    kind = trigger.get("type")
    if kind == "mail_match":
        return [], {}  # off until 3.1, even if a row was written by hand
    if kind == "file_appears":
        return _poll_files(trigger, state)
    if kind == "security_finding":
        return _poll_security(state, attention_store)
    return [], {}


def _poll_files(trigger: dict[str, Any], state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    folder = str(trigger.get("folder") or "")
    pattern = str(trigger.get("pattern") or "*")
    try:
        names = sorted(
            n for n in os.listdir(folder)
            if not n.startswith(".") and not n.lower().endswith(_PARTIAL) and fnmatch.fnmatch(n, pattern)
            and os.path.isfile(os.path.join(folder, n))
        )
    except OSError as exc:
        # Said, not hidden: the row shows it and the run history is not touched.
        return [], {"watch_error": f"cannot read the folder ({exc.__class__.__name__})"}
    cleared = {"watch_error": ""}
    if "seen" not in state:
        return [], {**cleared, "seen": names[-_MAX_SEEN:]}
    known = set(state.get("seen") or [])
    fresh = [n for n in names if n not in known]
    keep = (list(state.get("seen") or []) + fresh)[-_MAX_SEEN:]
    if not fresh:
        return [], {**cleared, "seen": keep} if keep != state.get("seen") else cleared
    return [{"kind": "file_appears", "count": len(fresh), "names": fresh[:5]}], {**cleared, "seen": keep}


def _poll_security(state: dict[str, Any], store: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if store is None:
        from dourmouse import attention

        store = attention.get_attention_store()
    items = [i for i in store.items(include_done=True, limit=300) if i.get("source") == "security"
             and i.get("kind") in ("needs_you", "heads_up")]
    newest = max((int(i["id"]) for i in items), default=0)
    if "after_id" not in state:
        return [], {"after_id": newest}
    fresh = [i for i in items if int(i["id"]) > int(state["after_id"])]
    if not fresh:
        return [], {}
    return [{"kind": "security_finding", "count": len(fresh), "titles": [i["title"] for i in fresh[:3]]}], {"after_id": newest}
