"""dourmouse/device_wiki/store.py -- real SQLite persistence, one row per
real file. Every test uses a real on-disk file (tmp_path), same
convention as test_research_pipeline.py's own TestResearchStore."""

from __future__ import annotations

from dourmouse.device_wiki.core import mark_missing, with_new_entry, with_summary
from dourmouse.device_wiki.store import WikiStore


class TestWikiStore:
    def test_a_saved_entry_round_trips_exactly(self, tmp_path):
        store = WikiStore(tmp_path / "wiki.db")
        entry = with_summary(with_new_entry("/a.txt", "hash1", 100, now=1000.0), "real summary", now=2000.0)
        store.save_entry(entry)
        loaded = store.get("/a.txt")
        assert loaded == entry

    def test_get_for_an_unknown_path_is_honestly_none(self, tmp_path):
        store = WikiStore(tmp_path / "wiki.db")
        assert store.get("/never-saved.txt") is None

    def test_saving_the_same_path_twice_updates_not_duplicates(self, tmp_path):
        store = WikiStore(tmp_path / "wiki.db")
        store.save_entry(with_new_entry("/a.txt", "hash1", 100, now=1000.0))
        store.save_entry(with_summary(with_new_entry("/a.txt", "hash1", 100, now=1000.0), "real summary", now=2000.0))
        assert len(store.list_all()) == 1
        assert store.get("/a.txt").status == "SUMMARIZED"

    def test_save_all_persists_a_real_batch(self, tmp_path):
        store = WikiStore(tmp_path / "wiki.db")
        entries = {
            "/a.txt": with_new_entry("/a.txt", "h1", 1, now=1000.0),
            "/b.txt": with_new_entry("/b.txt", "h2", 2, now=1000.0),
        }
        store.save_all(entries)
        assert {e.path for e in store.list_all()} == {"/a.txt", "/b.txt"}

    def test_list_all_filters_by_status(self, tmp_path):
        store = WikiStore(tmp_path / "wiki.db")
        summarized = with_summary(with_new_entry("/a.txt", "h1", 1, now=1000.0), "real", now=1000.0)
        unsummarized = with_new_entry("/b.txt", "h2", 2, now=1000.0)
        store.save_all({"/a.txt": summarized, "/b.txt": unsummarized})
        assert [e.path for e in store.list_all("SUMMARIZED")] == ["/a.txt"]
        assert [e.path for e in store.list_all("UNSUMMARIZED")] == ["/b.txt"]

    def test_a_missing_entry_persists_its_missing_status(self, tmp_path):
        store = WikiStore(tmp_path / "wiki.db")
        entry = with_new_entry("/a.txt", "h1", 1, now=1000.0)
        store.save_entry(mark_missing(entry, now=2000.0))
        assert store.get("/a.txt").status == "MISSING"

    def test_all_as_dict_matches_reconciles_own_existing_shape(self, tmp_path):
        store = WikiStore(tmp_path / "wiki.db")
        store.save_entry(with_new_entry("/a.txt", "h1", 1, now=1000.0))
        result = store.all_as_dict()
        assert set(result.keys()) == {"/a.txt"}
        assert result["/a.txt"].path == "/a.txt"

    def test_store_persists_across_instances(self, tmp_path):
        path = tmp_path / "wiki.db"
        WikiStore(path).save_entry(with_new_entry("/a.txt", "h1", 1, now=1000.0))
        assert WikiStore(path).get("/a.txt") is not None
