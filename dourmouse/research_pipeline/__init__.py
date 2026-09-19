"""Structured research pipeline (Domain G, docs/COMMERCIAL_GRADE_MASTER_
REQUIREMENTS.md): question -> plan -> source discovery -> evidence
extraction -> ... -> synthesis, with real provenance on every claim --
never a bare assertion with no traceable source.

This is the first, smallest real piece per this domain's own build plan:
the real data model and persisted store, mirroring research_mesh/store.py's
own proven one-row-JSON-body SQLite shape (finding #033) rather than
inventing new persistence plumbing. Stage functions (plan/extract/
synthesize) land as real, separate, incremental follow-on commits, each
independently tested and live-verified, matching how every other domain
this session was built -- not as one large, harder-to-verify piece.
"""

from .core import Claim, Contradiction, ResearchRecord, Stage
from .store import ResearchStore

__all__ = ["Claim", "Contradiction", "ResearchRecord", "Stage", "ResearchStore"]
