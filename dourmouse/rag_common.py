"""dourmouse/rag_common.py — shared primitives for the RAG storage stack.

RAG de-fragmentation (backlog #1 continuation, Cycle 1 was dedup-only and
left the actual fragmentation unresolved): before this file, content-hash
dedup logic lived privately inside dourmouse/memory_store.py
(``_content_hash``) with no shared primitive, so dourmouse/bulk_ingest.py
had nothing to call and relied entirely on memory_store.remember()'s
internal dedup — any second RAG-writing path would have had to reinvent
the hash or silently skip dedup. This module gives the whole RAG stack
(memory_store.py today; bulk_ingest.py and any future writer) one
canonical, shared ``content_hash()``.

Scope note: dourmouse/desktop_rag.py and dourmouse/shared_rag.py (the
separate pgvector-backed ``hybrid_chunks`` store, 1M+ rows) are explicitly
OUT of scope here — deliberately not touched this cycle. That store needs
its own dedicated de-fragmentation pass; folding it in here would be
riskier than this cycle's mandate.
"""

from __future__ import annotations

import hashlib


def content_hash(body: str) -> str:
    """Stable hash of a fact/document body for exact-content dedup.

    Normalizes only trivial formatting noise (surrounding whitespace, and
    trailing whitespace per line) so re-ingesting byte-identical content
    that merely picked up a stray trailing space or blank line still
    matches — NOT a fuzzy/semantic hash. Two bodies with any real content
    difference get different hashes and are both kept.
    """
    normalized = "\n".join(line.rstrip() for line in body.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()
