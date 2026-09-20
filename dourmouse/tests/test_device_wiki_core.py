"""dourmouse/device_wiki/core.py -- pure data model, no I/O, no model
call. Every test is hermetic; the caller passes in an explicit `now`,
matching research_pipeline/core.py's own testing convention."""

from __future__ import annotations

import pytest

from dourmouse.device_wiki.core import (
    WikiEntry,
    mark_missing,
    reconcile,
    with_failed_summary,
    with_new_entry,
    with_summary,
)


class TestWikiEntry:
    def test_an_unknown_status_is_rejected(self):
        with pytest.raises(ValueError):
            WikiEntry(path="/a", content_hash="h", size_bytes=1, status="NOT_A_REAL_STATUS")

    def test_an_empty_path_is_rejected(self):
        with pytest.raises(ValueError):
            WikiEntry(path="  ", content_hash="h", size_bytes=1, status="UNSUMMARIZED")


class TestWithNewEntry:
    def test_a_new_file_is_honestly_unsummarized(self):
        entry = with_new_entry("/a/b.txt", "hash1", 100, now=1000.0)
        assert entry.status == "UNSUMMARIZED"
        assert entry.summary == ""
        assert entry.last_seen == 1000.0


class TestWithSummary:
    def test_a_real_summary_marks_the_entry_summarized(self):
        entry = with_new_entry("/a/b.txt", "hash1", 100, now=1000.0)
        summarized = with_summary(entry, "A real summary of the file.", now=2000.0)
        assert summarized.status == "SUMMARIZED"
        assert summarized.summary == "A real summary of the file."
        assert summarized.summarized_at == 2000.0
        assert summarized.last_seen == 2000.0

    def test_an_empty_summary_is_never_accepted(self):
        entry = with_new_entry("/a/b.txt", "hash1", 100, now=1000.0)
        with pytest.raises(ValueError):
            with_summary(entry, "   ", now=2000.0)


class TestWithFailedSummary:
    def test_a_failed_summary_stays_honestly_unsummarized(self):
        entry = with_new_entry("/a/b.txt", "hash1", 100, now=1000.0)
        failed = with_failed_summary(entry, now=2000.0)
        assert failed.status == "UNSUMMARIZED"
        assert failed.summary == ""
        assert failed.last_seen == 2000.0


class TestMarkMissing:
    def test_a_missing_file_is_marked_but_never_dropped(self):
        entry = with_summary(with_new_entry("/a/b.txt", "hash1", 100, now=1000.0), "real summary", now=1000.0)
        missing = mark_missing(entry, now=2000.0)
        assert missing.status == "MISSING"
        assert missing.summary == "real summary"  # never lost -- kept, terminal, visible

    def test_last_seen_is_not_bumped_for_a_missing_file(self):
        """last_seen must keep recording the last time the file was
        genuinely ON DISK, not the time its absence was noticed."""
        entry = with_new_entry("/a/b.txt", "hash1", 100, now=1000.0)
        missing = mark_missing(entry, now=2000.0)
        assert missing.last_seen == 1000.0


class TestReconcile:
    def test_a_real_new_file_is_added(self):
        result = reconcile({}, {"/a.txt": ("hash1", 10)}, now=1000.0)
        assert set(result.keys()) == {"/a.txt"}
        assert result["/a.txt"].status == "UNSUMMARIZED"

    def test_an_unchanged_file_only_advances_last_seen(self):
        existing = {"/a.txt": with_summary(with_new_entry("/a.txt", "hash1", 10, now=1000.0), "real summary", now=1000.0)}
        result = reconcile(existing, {"/a.txt": ("hash1", 10)}, now=2000.0)
        assert result["/a.txt"].status == "SUMMARIZED"
        assert result["/a.txt"].summary == "real summary"
        assert result["/a.txt"].last_seen == 2000.0

    def test_a_real_content_change_reverts_to_unsummarized(self):
        existing = {"/a.txt": with_summary(with_new_entry("/a.txt", "hash1", 10, now=1000.0), "stale summary", now=1000.0)}
        result = reconcile(existing, {"/a.txt": ("hash2", 20)}, now=2000.0)
        assert result["/a.txt"].status == "UNSUMMARIZED"
        assert result["/a.txt"].summary == ""  # the stale summary is never silently kept
        assert result["/a.txt"].content_hash == "hash2"

    def test_a_real_deletion_is_marked_missing_never_dropped(self):
        """Harsh acceptance test 2: a real deletion must be reflected on
        the next scan, not shown as a stale entry forever."""
        existing = {"/a.txt": with_new_entry("/a.txt", "hash1", 10, now=1000.0)}
        result = reconcile(existing, {}, now=2000.0)
        assert set(result.keys()) == {"/a.txt"}
        assert result["/a.txt"].status == "MISSING"

    def test_multiple_files_are_reconciled_independently(self):
        existing = {
            "/keep.txt": with_new_entry("/keep.txt", "h1", 1, now=1000.0),
            "/gone.txt": with_new_entry("/gone.txt", "h2", 2, now=1000.0),
        }
        found = {
            "/keep.txt": ("h1", 1),
            "/new.txt": ("h3", 3),
        }
        result = reconcile(existing, found, now=2000.0)
        assert set(result.keys()) == {"/keep.txt", "/gone.txt", "/new.txt"}
        assert result["/gone.txt"].status == "MISSING"
        assert result["/new.txt"].status == "UNSUMMARIZED"
        assert result["/keep.txt"].last_seen == 2000.0

    def test_a_missing_file_that_reappears_with_the_same_content_self_heals(self):
        summarized = with_summary(with_new_entry("/a.txt", "hash1", 10, now=1000.0), "real summary", now=1000.0)
        missing = mark_missing(summarized, now=2000.0)
        result = reconcile({"/a.txt": missing}, {"/a.txt": ("hash1", 10)}, now=3000.0)
        assert result["/a.txt"].status == "SUMMARIZED"
        assert result["/a.txt"].summary == "real summary"

    def test_a_missing_unsummarized_file_that_reappears_stays_unsummarized(self):
        entry = with_new_entry("/a.txt", "hash1", 10, now=1000.0)
        missing = mark_missing(entry, now=2000.0)
        result = reconcile({"/a.txt": missing}, {"/a.txt": ("hash1", 10)}, now=3000.0)
        assert result["/a.txt"].status == "UNSUMMARIZED"

    def test_empty_existing_and_empty_found_is_a_real_empty_result(self):
        assert reconcile({}, {}, now=1000.0) == {}
