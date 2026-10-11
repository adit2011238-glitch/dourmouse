"""Real, I/O- and model-touching stage functions for the device wiki
(Domain E, step 3), built on the pure data model in core.py (finding
#056). One real reuse of already-proven machinery: the same tool-less
``ChatSession(DispatchRegistry(), session_file=None)`` primitive used by
``_verify_completion``/``_verify_goal_criteria``/``RealBrain.answer`` and
every ``research_pipeline`` stage function -- no new call path invented.

Harsh acceptance test 1 (never fabricate a summary, an honest "could not
summarize" marker for a binary/unreadable file, never silence) is
enforced here: `read_file_for_summary` honestly returns ``None`` for a
binary or unreadable file, and `summarize_file` turns that into
``with_failed_summary`` rather than ever calling the model on content
that was never really read.
"""

from __future__ import annotations

import re
from pathlib import Path

from .core import WikiEntry, with_failed_summary, with_summary
from .walker import is_secret_name

#: A real, named cost bound -- the same reasoning research_pipeline's own
#: `max_sources_per_sub_question` cap uses: summarizing an unbounded file
#: is an uncontrolled real cost per file, not a free call.
_MAX_CONTENT_CHARS = 8000
#: How many raw bytes to sniff for a null byte before deciding a file is
#: binary -- large enough to catch a real binary file's own header,
#: small enough to never read a genuinely huge file just to classify it.
_BINARY_SNIFF_BYTES = 8000

# Same real, live-caught dispatch leak research_pipeline/stages.py's own
# _strip_internal_diagnostics fixes (finding #046) -- a local copy per
# this codebase's own established "local reconstruction, not a cross-
# domain import" convention (test_dispatch.py's own FakeClient doubles,
# research_pipeline's own copy), not a shared utility module.
_DIAGNOSTIC_SUFFIX_RE = re.compile(r"\n\n\[DOURMOUSE:.*\]\s*\Z", re.DOTALL)


def _strip_internal_diagnostics(text: str) -> str:
    return _DIAGNOSTIC_SUFFIX_RE.sub("", text).rstrip()


_SUMMARY_PROMPT = (
    "Summarize this real file's content in 2-3 concise sentences, for a personal "
    "knowledge-base wiki entry a user will browse later. Describe what the file "
    "actually contains -- never invent details not present in the text below.\n\n"
    "FILE: {path}\n\n"
    "CONTENT:\n{content}\n\n"
    "Reply with ONLY the summary text, nothing else."
)


def read_file_for_summary(path: str) -> str | None:
    """Real, honest file read for summarization. Returns ``None`` (never
    raises) for anything that is not genuinely real, readable text: a
    missing/unreadable file, a real binary file (a null byte in the
    first `_BINARY_SNIFF_BYTES` real bytes -- the standard, cheap
    real-world heuristic), or a real file with zero readable content.
    Truncates to `_MAX_CONTENT_CHARS` -- a real, named cost bound, not an
    attempt to summarize an unbounded file in full."""
    p = Path(path)
    try:
        # Same rule as the walker: never read a symlink or a secret-named file, even for an entry an older
        # scan stored, because this text is posted to a model.
        if p.is_symlink() or is_secret_name(p.name) or not p.is_file():
            return None
        with p.open("rb") as fh:
            # One bounded read serves both the binary sniff and the content: a multi-GB log is never loaded
            # whole just to be cut to _MAX_CONTENT_CHARS. 4 bytes per char is the UTF-8 maximum.
            head = fh.read(max(_BINARY_SNIFF_BYTES, _MAX_CONTENT_CHARS * 4))
        if b"\x00" in head[:_BINARY_SNIFF_BYTES]:
            return None
        text = head.decode("utf-8", errors="replace")
    except OSError:
        return None
    text = text.strip()
    return text[:_MAX_CONTENT_CHARS] if text else None


def summarize_entry(entry: WikiEntry, content: str, now: float) -> WikiEntry:
    """One real, tool-less ChatSession call. `content` is the real text
    `read_file_for_summary` already produced -- this function never reads
    the file itself, so a caller can unit-test summarization and file-
    reading independently, matching this codebase's own established
    "narrow, single-purpose real functions" discipline."""
    from dourmouse.chat import ChatSession
    from dourmouse.dispatch import DispatchRegistry

    session = ChatSession(DispatchRegistry(), session_file=None)
    result = session.ask(
        _SUMMARY_PROMPT.format(path=entry.path, content=content),
        force_plain_dispatch=True,
    )
    text = _strip_internal_diagnostics((result.get("final_text") or "").strip())
    if not text:
        return with_failed_summary(entry, now)
    return with_summary(entry, text, now)


def summarize_file(entry: WikiEntry, now: float) -> WikiEntry:
    """The real, end-to-end per-file summarization step: read the real
    file, and only call the model on content that was genuinely read --
    a binary or unreadable file gets an honest `with_failed_summary`
    without ever reaching the model (harsh acceptance test 1)."""
    content = read_file_for_summary(entry.path)
    if content is None:
        return with_failed_summary(entry, now)
    return summarize_entry(entry, content, now)
