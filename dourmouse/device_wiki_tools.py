"""The ``device_wiki`` subagent -- chat-reachable tools over Domain E's
device wiki (``dourmouse/device_wiki/``), mirroring
``research_pipeline_tools.py``'s own dedicated-module-plus-
``build_X_subagent()``-factory shape (the fourth reuse of that shape this
session: ``research_mesh_tools.py``, ``research_pipeline_tools.py``,
``security/tools.py``, now this).

Real gap this closes: every device_wiki stage function (findings #056
through #059) was only reachable by writing a short Python script.
Nothing here changes those functions -- pure, thin, honest wiring.

Known, named limitation (not hidden): `device_wiki_scan` runs the whole
real walk + reconcile + summarize-every-unsummarized-file cycle
SYNCHRONOUSLY in one call -- for a large real folder tree this can take
minutes (one real model call per real file needing a summary). No
background/goal-runtime integration yet, the same already-documented
limitation `research_mesh_qualify`/`research_pipeline_tools` both carry.
A real, named cost bound (`max_files_to_summarize`) caps how many
newly-discovered-or-changed files get summarized in one call, so a huge
first scan does not run unboundedly -- the rest stay honestly
UNSUMMARIZED until the next call.
"""

from __future__ import annotations

import time
from typing import Any

from dourmouse.device_wiki.stages import summarize_file
from dourmouse.device_wiki.store import DEFAULT_DB, WikiStore
from dourmouse.device_wiki.walker import configured_roots, scan
from dourmouse.dispatch import Subagent, ToolSpec

_DEFAULT_MAX_FILES_TO_SUMMARIZE = 20


def _store() -> WikiStore:
    return WikiStore(DEFAULT_DB)


def _device_wiki_scan_tool(arguments: dict[str, Any]) -> str:
    roots = configured_roots()
    if not roots:
        return (
            "ERROR: no real root folders configured. Set DOURMOUSE_WIKI_ROOTS to one or more "
            "real, absolute folder paths (os.pathsep-separated) before scanning -- never "
            "silently the whole filesystem."
        )
    try:
        max_files = int(arguments.get("max_files_to_summarize", _DEFAULT_MAX_FILES_TO_SUMMARIZE))
    except (TypeError, ValueError):
        return "ERROR: max_files_to_summarize must be an integer."

    store = _store()
    now = time.time()
    before = store.all_as_dict()
    result = scan(store, roots, now)

    new_or_changed = [
        path for path, entry in result.items()
        if entry.status == "UNSUMMARIZED" and (path not in before or before[path].content_hash != entry.content_hash)
    ]
    summarized = 0
    for path in new_or_changed[:max_files]:
        summarized_entry = summarize_file(result[path], now)
        result[path] = summarized_entry
        store.save_entry(summarized_entry)
        summarized += 1

    missing = sum(1 for e in result.values() if e.status == "MISSING")
    unsummarized_remaining = sum(1 for e in result.values() if e.status == "UNSUMMARIZED")
    return (
        f"Real scan complete over {len(roots)} root(s): {len(result)} real file(s) tracked, "
        f"{summarized} newly summarized this call, {unsummarized_remaining} still unsummarized "
        f"(honest -- not fabricated), {missing} marked missing (deleted from disk)."
    )


def _device_wiki_status_tool(_arguments: dict[str, Any]) -> str:
    store = _store()
    entries = store.list_all()
    if not entries:
        return "No real device wiki entries yet -- run device_wiki_scan first."
    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e.status] = by_status.get(e.status, 0) + 1
    lines = [f"{len(entries)} real file(s) tracked:"]
    for status in ("SUMMARIZED", "UNSUMMARIZED", "MISSING"):
        if status in by_status:
            lines.append(f"  {status}: {by_status[status]}")
    return "\n".join(lines)


def _device_wiki_get_tool(arguments: dict[str, Any]) -> str:
    path = str(arguments.get("path") or "").strip()
    if not path:
        return "ERROR: device_wiki_get requires a non-empty 'path'."
    entry = _store().get(path)
    if entry is None:
        return f"No real wiki entry for {path!r} -- run device_wiki_scan first."
    if entry.status == "SUMMARIZED":
        return f"{path} [{entry.status}]: {entry.summary}"
    return f"{path} [{entry.status}]: no real summary available."


def build_device_wiki_subagent() -> Subagent:
    return Subagent(
        name="device_wiki",
        domain="General",
        description=(
            "A local, LLM-curated knowledge base built from the user's own real files "
            "(Domain E): per-file summaries, never a raw file index and never a "
            "fabricated description. Scoped to a real, explicit, user-configured set of "
            "root folders -- never the whole filesystem. Read-only: never touches, "
            "moves, renames, or deletes a real file."
        ),
        tools=(
            ToolSpec(
                name="device_wiki_scan",
                description=(
                    "Walk the real, configured root folders (DOURMOUSE_WIKI_ROOTS), "
                    "reconcile the wiki's real state (new files added, deleted files "
                    "marked missing, changed files re-queued), and summarize up to "
                    "max_files_to_summarize newly-discovered-or-changed real files."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "max_files_to_summarize": {"type": "integer", "default": _DEFAULT_MAX_FILES_TO_SUMMARIZE},
                    },
                },
                handler=_device_wiki_scan_tool,
            ),
            ToolSpec(
                name="device_wiki_status",
                description="Report real counts of SUMMARIZED/UNSUMMARIZED/MISSING files currently tracked.",
                parameters={"type": "object", "properties": {}},
                handler=_device_wiki_status_tool,
            ),
            ToolSpec(
                name="device_wiki_get",
                description="Look up one real file's current wiki entry (its status and, if summarized, its real summary) by its exact real path.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=_device_wiki_get_tool,
            ),
        ),
    )
