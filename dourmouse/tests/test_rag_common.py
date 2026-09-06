"""dourmouse/rag_common.py tests — the shared content_hash() primitive.

Covers: determinism, the trivial-whitespace normalization it intentionally
applies, real-content sensitivity, and that memory_store.py and
bulk_ingest.py actually import THIS function rather than each holding
their own copy (the fragmentation this module exists to close).
"""

from __future__ import annotations

from dourmouse.rag_common import content_hash


def test_deterministic():
    assert content_hash("hello world") == content_hash("hello world")


def test_normalizes_surrounding_whitespace():
    assert content_hash("hello world") == content_hash("  hello world  \n")


def test_normalizes_trailing_line_whitespace():
    assert content_hash("line one\nline two") == content_hash("line one   \nline two\t")


def test_real_content_difference_changes_hash():
    assert content_hash("hello world") != content_hash("hello world!")


def test_case_sensitive():
    assert content_hash("Hello") != content_hash("hello")


def test_returns_hex_sha256():
    digest = content_hash("some content")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_memory_store_uses_shared_primitive():
    from dourmouse import memory_store

    assert memory_store._content_hash is content_hash


def test_bulk_ingest_uses_shared_primitive():
    from dourmouse import bulk_ingest

    assert bulk_ingest.content_hash is content_hash
