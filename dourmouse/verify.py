"""Verified completion (plan PL-07, finding 208).

A tool that changes something used to report "WROTE x" and the model repeated it. Nothing looked. This module
reads the result back after a state-changing tool runs and gives every such call one of three outcomes, decided
by the app and never by the model:

* ``verified``     the change is there (the file holds the text, the deleted file is gone).
* ``not_checked``  the tool changes something Dourmouse cannot read back (a sent message, a calendar entry).
* ``failed``       the tool said it worked and the read-back says it did not.

The dispatcher appends one plain line to the tool result, stores the outcome on the transcript entry and in the
ledger (``action.verified``), and ``audit_final_answer`` adds a note when the final answer says "I sent / saved /
deleted" with no verified step of that kind behind it. Read-only tools get no outcome.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERIFIED = "verified"
NOT_CHECKED = "not_checked"
FAILED = "failed"
STATUSES = (VERIFIED, NOT_CHECKED, FAILED)
LABELS = {VERIFIED: "Verified", NOT_CHECKED: "Not checked", FAILED: "Failed"}

#: A result that starts like this is the tool (or the dispatcher) already saying nothing changed.
_NOTHING_CHANGED = (
    "ERROR", "REFUSED", "DECLINED", "BLOCKED", "CONFIRMATION REQUIRED", "NOT CONFIGURED", "NOT SENT",
    "CANCELLED", "UNDO REFUSED", "Withheld",
)

#: Tool class per state-changing tool. Claims in a final answer are matched against these classes.
TOOL_CLASS: dict[str, str] = {
    "write_path": "save", "apply_patch": "save", "apply_search_replace": "save", "write_file": "save",
    "edit_file": "save",
    "delete_path": "delete", "delete_file": "delete",
    "send_draft": "send", "send_message": "send",
    "create_calendar_event": "calendar",
    "schedule_recurring": "schedule",
    "add_task": "task", "add_tasks": "task", "create_goal": "task",
    "send_app_keystrokes": "keys",
}
#: Tools in TOOL_CLASS with no read-back; they are always "not checked", with the reason shown to the owner.
_NO_READ_BACK = {
    "send_draft": "a sent message cannot be read back",
    "send_message": "a sent message cannot be read back",
    "create_calendar_event": "the calendar was not read back",
    "schedule_recurring": "the schedule was not read back",
    "add_task": "the task list was not read back",
    "add_tasks": "the task list was not read back",
    "create_goal": "the goal was not read back",
    "send_app_keystrokes": "what the other app did with the keys was not read",
}


@dataclass(frozen=True)
class Outcome:
    status: str
    detail: str
    tool: str = ""
    tool_class: str = ""

    def line(self) -> str:
        return f"[Dourmouse check: {LABELS[self.status]}. {self.detail}]"

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail, "tool": self.tool, "class": self.tool_class}


def _outcome(status: str, detail: str, tool: str) -> Outcome:
    return Outcome(status, detail, tool, TOOL_CLASS.get(tool, ""))


def _system_path(arguments: dict[str, Any]) -> Path | None:
    path = Path(str(arguments.get("path") or "")).expanduser()
    return path if path.is_absolute() else None


def _workspace_path(arguments: dict[str, Any]) -> Path | None:
    from dourmouse import general_roster as gr

    try:
        return gr._safe_resolve(gr._workspace_root(), arguments.get("path", ""))
    except ValueError:
        return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _check_written(tool: str, path: Path | None, arguments: dict[str, Any]) -> Outcome:
    if path is None:
        return _outcome(NOT_CHECKED, "the path could not be resolved to read it back", tool)
    text = _read(path)
    if text is None:
        return _outcome(FAILED, f"{path.name} is not readable after the write", tool)
    if text != str(arguments.get("content", "")):
        return _outcome(FAILED, f"{path.name} does not hold the text that was written", tool)
    return _outcome(VERIFIED, f"read {path.name} back and it holds the text ({len(text)} characters)", tool)


def _verify_write_path(tool: str, arguments: dict[str, Any], result: str) -> Outcome | None:
    return _check_written(tool, _system_path(arguments), arguments) if result.startswith("WROTE") else None


def _verify_write_file(tool: str, arguments: dict[str, Any], result: str) -> Outcome | None:
    if not result.startswith(("WROTE", "UPDATED")):
        return None
    return _check_written(tool, _workspace_path(arguments), arguments)


def _check_contains(tool: str, path: Path | None, wanted: Iterable[str]) -> Outcome:
    if path is None:
        return _outcome(NOT_CHECKED, "the path could not be resolved to read it back", tool)
    text = _read(path)
    if text is None:
        return _outcome(FAILED, f"{path.name} is not readable after the edit", tool)
    missing = [piece for piece in wanted if piece and piece not in text]
    if missing:
        return _outcome(FAILED, f"{path.name} does not contain the new text ({len(missing)} piece(s) missing)", tool)
    return _outcome(VERIFIED, f"read {path.name} back and the new text is in it", tool)


def _verify_search_replace(tool: str, arguments: dict[str, Any], result: str) -> Outcome | None:
    from dourmouse.patch_apply import parse_search_replace_blocks

    if result.startswith(_NOTHING_CHANGED) or "REFUSED" in result[:40]:
        return None
    blocks = parse_search_replace_blocks(str(arguments.get("patch") or ""))
    if not blocks:
        return None
    return _check_contains(tool, _system_path(arguments), [b.replace.replace("\r\n", "\n") for b in blocks])


def _verify_patch(tool: str, arguments: dict[str, Any], result: str) -> Outcome | None:
    if result.startswith(_NOTHING_CHANGED) or "REFUSED" in result[:40]:
        return None
    added = [
        line[1:] for line in str(arguments.get("diff") or "").replace("\r\n", "\n").split("\n")
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
    ]
    if not added:
        return _outcome(NOT_CHECKED, "the diff adds no lines, so there is nothing to look for", tool)
    return _check_contains(tool, _system_path(arguments), added)


def _verify_edit_file(tool: str, arguments: dict[str, Any], result: str) -> Outcome | None:
    if result.startswith(_NOTHING_CHANGED):
        return None
    return _check_contains(tool, _workspace_path(arguments), [str(arguments.get("new_str") or "")])


def _check_gone(tool: str, path: Path | None) -> Outcome:
    if path is None:
        return _outcome(NOT_CHECKED, "the path could not be resolved to look for it", tool)
    if path.exists():
        return _outcome(FAILED, f"{path.name} is still there after the delete", tool)
    return _outcome(VERIFIED, f"looked for {path.name} and it is gone", tool)


def _verify_delete_path(tool: str, arguments: dict[str, Any], result: str) -> Outcome | None:
    return _check_gone(tool, _system_path(arguments)) if result.startswith("DELETED") else None


def _verify_delete_file(tool: str, arguments: dict[str, Any], result: str) -> Outcome | None:
    return _check_gone(tool, _workspace_path(arguments)) if result.startswith("DELETED") else None


_Verifier = Callable[[str, dict[str, Any], str], "Outcome | None"]
VERIFIERS: dict[str, _Verifier] = {
    "write_path": _verify_write_path,
    "write_file": _verify_write_file,
    "apply_search_replace": _verify_search_replace,
    "apply_patch": _verify_patch,
    "edit_file": _verify_edit_file,
    "delete_path": _verify_delete_path,
    "delete_file": _verify_delete_file,
}


def verify(tool: str, arguments: dict[str, Any], result: str) -> Outcome | None:
    """The outcome for one finished call, or None for a read-only tool or a call that changed nothing."""
    if tool not in TOOL_CLASS or not isinstance(arguments, dict):
        return None
    text = str(result or "")
    if text.startswith(_NOTHING_CHANGED):
        return None
    checker = VERIFIERS.get(tool)
    if checker is not None:
        try:
            return checker(tool, arguments, text)
        except Exception as exc:  # noqa: BLE001 - a failing check must not read as a pass, nor break the call
            return _outcome(NOT_CHECKED, f"the check itself failed ({type(exc).__name__})", tool)
    return _outcome(NOT_CHECKED, _NO_READ_BACK.get(tool, "no read-back exists for this tool"), tool)


# -- hand-off from the dispatcher to the loop that builds the transcript entry ---------------------------------- #

_last = threading.local()


def remember(outcome: Outcome | None) -> None:
    _last.value = outcome


def take_last() -> Outcome | None:
    """The outcome of the call this thread just ran, once."""
    outcome = getattr(_last, "value", None)
    _last.value = None
    return outcome


# -- the final answer audit ---------------------------------------------------------------------------------- #

_CLAIMS: dict[str, re.Pattern[str]] = {
    "send": re.compile(r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+(?:just\s+|already\s+)?(?:sent|emailed|messaged|replied)\b"
                       r"|\b(?:email|message|draft|reply)\s+(?:has been|was|is)\s+sent\b", re.I),
    "save": re.compile(r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+(?:just\s+|already\s+)?(?:saved|written|wrote|created the file|updated|edited|patched)\b"
                       r"|\b(?:file|changes?)\s+(?:has|have|was|were|is|are)\s+(?:been\s+)?(?:saved|written|updated)\b", re.I),
    "delete": re.compile(r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+(?:just\s+|already\s+)?(?:deleted|removed)\b"
                         r"|\b(?:file|it)\s+(?:has been|was|is)\s+(?:deleted|removed)\b", re.I),
    "calendar": re.compile(r"\b(?:i(?:'ve| have)?)\s+(?:just\s+|already\s+)?(?:scheduled|booked|added (?:it|the event) to your calendar)\b", re.I),
}
_CLAIM_WORDS = {"send": "sent", "save": "saved", "delete": "deleted", "calendar": "scheduled"}


def claimed_classes(answer: str) -> list[str]:
    return [cls for cls, pattern in _CLAIMS.items() if pattern.search(answer or "")]


def audit_final_answer(answer: str, outcomes: Iterable[dict[str, Any] | Outcome | None]) -> str:
    """A note for the end of the answer for each claim with no verified step of that class behind it, or ""."""
    done: set[str] = set()
    tried: set[str] = set()
    for item in outcomes:
        if item is None:
            continue
        data = item.as_dict() if isinstance(item, Outcome) else item
        cls = str(data.get("class") or "")
        tried.add(cls)
        if data.get("status") == VERIFIED:
            done.add(cls)
    notes = []
    for cls in claimed_classes(answer):
        if cls in done:
            continue
        word = _CLAIM_WORDS[cls]
        if cls in tried:
            notes.append(f"this answer says something was {word}, but no step of that kind was verified; treat it as unconfirmed")
        else:
            notes.append(f"this answer says something was {word}, but no step of that kind ran in this turn; treat it as unconfirmed")
    if not notes:
        return ""
    return "\n\n[Dourmouse check: " + "; ".join(notes) + "]"


# -- counts for the Security and Agents Activity views ---------------------------------------------------------- #

def day_counts(events: Iterable[dict[str, Any]], since_ts: float) -> dict[str, Any]:
    """Counts of ``action.verified`` events since ``since_ts``: per status and per tool."""
    counts = dict.fromkeys(STATUSES, 0)
    by_tool: dict[str, dict[str, int]] = {}
    for event in events:
        if event.get("kind") != "action.verified" or float(event.get("ts") or 0) < since_ts:
            continue
        status = str((event.get("payload") or {}).get("status") or "")
        if status not in counts:
            continue
        counts[status] += 1
        tool = str(event.get("subject_id") or "")
        by_tool.setdefault(tool, dict.fromkeys(STATUSES, 0))[status] += 1
    return {"counts": counts, "total": sum(counts.values()), "by_tool": by_tool}
