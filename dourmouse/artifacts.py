"""Artifact renderer backend (v5.8) — structured results beside the chat.

The biggest 'next level' gap vs Claude Cowork was flat text answers. This
module gives Dourmouse real rendered artifacts: agents publish structured
content (markdown reports, tables, series/equity curves) and the HUD renders
them beside the conversation instead of dumping raw text.

Three kinds (validated at publish time, Rule 2.2 — nothing fabricated):

- ``markdown``  content = the raw markdown body (headings, tables, code,
  lists — rendered safely in the HUD)
- ``table``     content = ``{"columns": [...], "rows": [[...]]}``
- ``series``    content = ``{"labels": [...], "values": [...]}`` (an equity
  curve / line chart; labels are x-axis ticks, values are the line)

The store is a bounded, thread-safe singleton (like the message bus): tools
publish into it, the web UI reads it via GET /api/artifacts, and an optional
*sink* lets the chat SSE stream push a live ``artifact`` event the moment one
is published mid-run — the HUD renders it without polling.

Honesty: publish validates shape and size BEFORE storing and raises
ValueError with the real reason; the tool wrapper surfaces that as an ERROR
result. A raising sink is swallowed — rendering must never break dispatch.
The store itself is in-memory and session-scoped: artifacts are renderings,
not durable facts (long-term memory stays the source of truth).
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dourmouse.dispatch import ToolSpec

# Bounded store: keep the most recent N artifacts (a chat could otherwise
# grow this without limit; 60 is plenty for a session).
_MAX_ARTIFACTS = 60

# Per-kind content caps so a huge report can never bloat the page or the
# model context when re-sent.
_MAX_MARKDOWN_CHARS = 60_000
_MAX_TABLE_ROWS = 1_000
_MAX_TABLE_COLS = 30
_MAX_SERIES_POINTS = 2_000

_ARTIFACT_KINDS = ("markdown", "table", "series")


class ArtifactStore:
    """Thread-safe, bounded store of rendered artifacts.

    ``set_sink(fn)`` attaches an observer called with
    ``{"type": "artifact", "artifact": {...}}`` whenever an artifact is
    published (used by the chat SSE stream for live rendering). Only one
    sink at a time — the web server swaps it per active request, exactly
    like ``WebConfirmationGate.set_emit``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        self._next_id = 1
        self._sink: Callable[[dict[str, Any]], None] | None = None

    # -- lifecycle -------------------------------------------------------- #

    def set_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        with self._lock:
            self._sink = sink

    def publish(
        self, kind: str, title: str, content: Any
    ) -> dict[str, Any]:
        """Validate + store one artifact; returns the stored record.

        Raises ValueError with the REAL reason on bad kind/shape (Rule 2.2).
        """
        kind = (kind or "").strip().lower()
        if kind not in _ARTIFACT_KINDS:
            raise ValueError(
                f"artifact kind must be one of {_ARTIFACT_KINDS}, got {kind!r}"
            )
        title = (title or "").strip()
        if not title:
            raise ValueError("artifact title must be a non-empty string")
        content = self._validate(kind, content)
        with self._lock:
            record = {
                "id": f"art-{self._next_id}",
                "kind": kind,
                "title": title[:200],
                "content": content,
                "created": time.time(),
            }
            self._next_id += 1
            self._items.append(record)
            if len(self._items) > _MAX_ARTIFACTS:
                del self._items[: len(self._items) - _MAX_ARTIFACTS]
            sink = self._sink
        if sink is not None:
            try:
                sink({"type": "artifact", "artifact": dict(record)})
            except Exception:  # noqa: BLE001, S110 - a raising sink never breaks publish
                pass
        return dict(record)

    def list(self, limit: int = 40) -> list[dict[str, Any]]:
        """Newest-first list (full records; the renderer needs all fields)."""
        with self._lock:
            items = list(reversed(self._items))
        return items[: max(1, min(int(limit), 100))]

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in self._items:
                if item["id"] == artifact_id:
                    return dict(item)
        return None

    def clear(self) -> int:
        with self._lock:
            n = len(self._items)
            self._items.clear()
        return n

    # -- validation ------------------------------------------------------- #

    def _validate(self, kind: str, content: Any) -> Any:
        if kind == "markdown":
            if not isinstance(content, str):
                raise ValueError("markdown artifact content must be a string")
            return content[:_MAX_MARKDOWN_CHARS]
        if kind == "table":
            if not isinstance(content, dict):
                raise ValueError("table artifact content must be JSON {columns, rows}")
            columns = content.get("columns")
            rows = content.get("rows")
            if not isinstance(columns, list) or not all(
                isinstance(c, str) for c in columns
            ):
                # TRY004 ignored: the tool wrapper's contract catches
                # ValueError for ALL validation failures (Rule 2.2).
                raise ValueError("table 'columns' must be a list of strings")
            if not isinstance(rows, list):
                # TRY004 ignored: ValueError is the tool boundary contract.
                raise ValueError("table 'rows' must be a list of lists")
            columns = columns[:_MAX_TABLE_COLS]
            clean_rows = []
            for row in rows[:_MAX_TABLE_ROWS]:
                if not isinstance(row, (list, tuple)):
                    # TRY004 ignored: ValueError is the tool boundary contract.
                    raise ValueError("each table row must be a list")  # noqa: TRY004
                clean_rows.append(
                    [str(v)[:500] for v in list(row)[:_MAX_TABLE_COLS]]
                )
            return {"columns": columns, "rows": clean_rows}
        # series
        if not isinstance(content, dict):
            # TRY004 ignored: ValueError is the tool boundary contract.
            raise ValueError(  # noqa: TRY004
                "series artifact content must be JSON {labels, values}"
            )
        labels = content.get("labels")
        values = content.get("values")
        if not isinstance(labels, list) or not isinstance(values, list):
            # TRY004 ignored: ValueError is the tool boundary contract.
            raise ValueError(  # noqa: TRY004
                "series 'labels' and 'values' must be lists"
            )
        if len(labels) != len(values):
            raise ValueError("series 'labels' and 'values' must have equal length")
        if not labels:
            raise ValueError("series must have at least one point")
        nums: list[float] = []
        for v in values[:_MAX_SERIES_POINTS]:
            try:
                num = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"series value {v!r} is not numeric")
            if not math.isfinite(num):
                # NaN/Inf pass float() but would render a broken SVG chart.
                raise ValueError(f"series value {v!r} is not finite")
            nums.append(num)
        labels = [str(l)[:80] for l in labels[:_MAX_SERIES_POINTS]]
        return {"labels": labels, "values": nums}


# --------------------------------------------------------------------------- #
# Default singleton (like get_message_bus) — tools and the web UI share it.
# --------------------------------------------------------------------------- #
_DEFAULT_STORE: ArtifactStore | None = None
_DEFAULT_LOCK = threading.Lock()


def default_store() -> ArtifactStore:
    """The process-wide artifact store (created lazily, safe under threads)."""
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = ArtifactStore()
        return _DEFAULT_STORE


def reset_default_store() -> None:
    """Replace the singleton (hermetic tests call this per-fixture)."""
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        _DEFAULT_STORE = None


# --------------------------------------------------------------------------- #
# Tool — publish one artifact from an agent run.
# --------------------------------------------------------------------------- #
def publish_artifact_tool(arguments: dict[str, Any]) -> str:
    """publish_artifact(kind, title, content) -> honest status text.

    ``content`` is a plain string for kind=markdown, or a JSON string
    describing {columns, rows} / {labels, values} for table/series. The
    model gets one back-text; the artifact itself appears in the HUD panel.
    """
    kind = str(arguments.get("kind") or "").strip()
    title = str(arguments.get("title") or "").strip()
    raw_content = arguments.get("content")
    if isinstance(raw_content, str) and kind in ("table", "series"):
        try:
            raw_content = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            return (
                f"ERROR: artifact kind={kind} needs content as a JSON string: {exc}"
            )
    try:
        record = default_store().publish(kind, title, raw_content)
    except ValueError as exc:
        return f"ERROR: artifact not published — {exc}"
    return (
        f"ARTIFACT PUBLISHED: [{record['id']}] {record['kind']} — "
        f"{record['title']} (rendered in the ARTIFACTS panel)"
    )


def build_artifact_tool_spec() -> ToolSpec:
    """The publish_artifact ToolSpec (imported lazily by the roster)."""
    from dourmouse.dispatch import ToolSpec

    return ToolSpec(
        name="publish_artifact",
        description=(
            "Publish a structured artifact rendered beside the chat: "
            "kind='markdown' (content is the markdown body), kind='table' "
            "(content is JSON {'columns': [...], 'rows': [[...]]}), or "
            "kind='series' (content is JSON {'labels': [...], 'values': [...]} "
            "— an equity curve / line chart). Use this INSTEAD of dumping a "
            "report as raw text: the HUD renders it as a live artifact."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(_ARTIFACT_KINDS),
                    "description": "markdown | table | series",
                },
                "title": {
                    "type": "string",
                    "description": "short human title for the artifact panel",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "markdown body, or a JSON string for table/series"
                    ),
                },
            },
            "required": ["kind", "title", "content"],
        },
        handler=publish_artifact_tool,
    )
