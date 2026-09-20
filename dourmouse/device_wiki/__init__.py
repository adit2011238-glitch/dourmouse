"""Device wiki (Domain E, docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md
section 7): a local, LLM-curated knowledge base built from the user's own
files -- per-file summaries, never a raw file index and never a
fabricated description.

This is the first, smallest real piece per this domain's own build plan:
the pure data model, no I/O, no model call. The real SQLite store, the
summarizer (reusing the tool-less ChatSession primitive), the walker
(explicit root-folder allowlist, never the whole filesystem), the chat
tool, and the UI page land as real, separate, incremental follow-on
commits, each independently tested and live-verified -- the exact same
sequence `research_pipeline` was built in (finding #042 onward).
"""

from .core import WikiEntry, mark_missing, reconcile, with_failed_summary, with_new_entry, with_summary
from .stages import read_file_for_summary, summarize_entry, summarize_file
from .store import DEFAULT_DB, WikiStore
from .walker import ROOTS_ENV, configured_roots, scan, walk_roots

__all__ = [
    "WikiEntry", "mark_missing", "reconcile", "with_failed_summary", "with_new_entry", "with_summary",
    "read_file_for_summary", "summarize_entry", "summarize_file",
    "DEFAULT_DB", "WikiStore",
    "ROOTS_ENV", "configured_roots", "scan", "walk_roots",
]
