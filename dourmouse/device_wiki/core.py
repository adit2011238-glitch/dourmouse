"""Pure data model for the device wiki (Domain E, step 1). No I/O, no
clock read internally (every timestamp is a real value the caller
passes in, matching research_pipeline/core.py's and sentry.py's own
"caller owns the clock" testing convention), no model call -- fully
unit-testable, mirroring this codebase's own established pure-logic-
first pattern (research_mesh/core.py, research_pipeline/core.py,
exams.py's own citation gate).

One `WikiEntry` per real file on disk. Never deleted -- a file removed
from disk gets `status="MISSING"` (harsh acceptance test 2: the wiki
must reflect a real deletion on its next scan, never show a stale entry
forever), the same "never silently vanish, always terminal-but-visible"
discipline `research_pipeline.core.Claim`'s own REJECTED status and
`goals.py`'s own terminal states already establish.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: Real lifecycle a WikiEntry can be in. UNSUMMARIZED is the honest
#: state for a genuinely-not-yet-summarized OR could-not-summarize file
#: (harsh acceptance test 1: never fabricate a summary, an honest
#: "could not summarize" marker is this same status, never silence).
WIKI_ENTRY_STATUSES = frozenset({"UNSUMMARIZED", "SUMMARIZED", "MISSING"})


@dataclass(frozen=True)
class WikiEntry:
    """One real file this wiki has seen. `content_hash` is a real
    `hashlib.sha256` of the file's own bytes (computed by the caller,
    e.g. the walker in step 4) -- used by `reconcile()` to detect a real
    on-disk content change and correctly re-trigger summarization rather
    than trusting a stale summary of content that no longer exists."""

    path: str
    content_hash: str
    size_bytes: int
    status: str
    summary: str = ""
    summarized_at: float | None = None
    last_seen: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in WIKI_ENTRY_STATUSES:
            raise ValueError(f"unknown wiki entry status: {self.status!r}")
        if not self.path.strip():
            raise ValueError("a wiki entry must reference a real, non-empty path")


def with_new_entry(path: str, content_hash: str, size_bytes: int, now: float) -> WikiEntry:
    """A real file seen for the first time -- honestly UNSUMMARIZED until
    a real summarizer call (step 3) actually runs."""
    return WikiEntry(
        path=path, content_hash=content_hash, size_bytes=size_bytes,
        status="UNSUMMARIZED", last_seen=now,
    )


def with_summary(entry: WikiEntry, summary: str, now: float) -> WikiEntry:
    """A real, non-fabricated summary lands here -- raises on an empty
    summary rather than silently accepting one (harsh acceptance test 1:
    a real summary or an honest UNSUMMARIZED marker, never a hollow
    success)."""
    if not summary.strip():
        raise ValueError("a real summary must be non-empty text")
    return replace(entry, summary=summary, status="SUMMARIZED", summarized_at=now, last_seen=now)


def with_failed_summary(entry: WikiEntry, now: float) -> WikiEntry:
    """A real, honest "could not summarize" outcome (binary/unreadable
    file, a genuinely failed model call) -- stays UNSUMMARIZED, never
    fabricates placeholder text. Distinct function from `with_new_entry`
    so a caller's intent (this is a re-attempt, not a first sighting) is
    explicit at the call site, even though the resulting state is the
    same real honest non-summary."""
    return replace(entry, last_seen=now)


def mark_missing(entry: WikiEntry, now: float) -> WikiEntry:
    """A real file this wiki once indexed that no longer exists on disk.
    Never deleted from the wiki's own record -- kept, terminal, visible,
    matching `research_pipeline.core.Claim`'s own REJECTED-but-kept
    discipline. `last_seen` is deliberately NOT bumped to `now` here: it
    should keep recording the last time this file was genuinely observed
    ON DISK, not the time its absence was noticed."""
    return replace(entry, status="MISSING")


def reconcile(
    existing: dict[str, WikiEntry],
    found: dict[str, tuple[str, int]],
    now: float,
) -> dict[str, WikiEntry]:
    """The real, pure reconciliation step a walker (step 4) drives on
    every scan: `found` is `{path: (content_hash, size_bytes)}` for every
    real file the walker actually found on disk THIS scan.

    - A path in `found` but not in `existing` is a real new file --
      `with_new_entry`.
    - A path in both, with the SAME `content_hash`, is untouched content
      -- only `last_seen` advances (the existing summary, if any, is
      still honest and correct).
    - A path in both, with a DIFFERENT `content_hash`, is a real on-disk
      content change -- reverts to a fresh UNSUMMARIZED entry rather than
      keeping a summary of content that no longer exists (a stale summary
      silently presented as current would be a real, undetected
      fabrication risk).
    - A path in `existing` but NOT in `found` is a real deletion (harsh
      acceptance test 2) -- `mark_missing`, never silently dropped from
      the returned dict.
    A previously-MISSING file that reappears (the same real path, found
    again) is treated as a real new sighting -- if its content hash still
    matches what was on record, its status self-heals from MISSING back
    to SUMMARIZED (a real prior summary, preserved on `mark_missing`, is
    still honest and valid) or UNSUMMARIZED (no real summary was ever
    produced), derived from whether a real summary is present rather than
    from the stale `MISSING` status itself; if the content differs, it is
    re-summarized like any other real content change.
    """
    result: dict[str, WikiEntry] = {}
    for path, (content_hash, size_bytes) in found.items():
        old = existing.get(path)
        if old is None or old.content_hash != content_hash:
            result[path] = with_new_entry(path, content_hash, size_bytes, now)
        else:
            status = "SUMMARIZED" if old.summary else "UNSUMMARIZED"
            result[path] = replace(old, last_seen=now, status=status)
    for path, old in existing.items():
        if path not in found:
            result[path] = mark_missing(old, now)
    return result
