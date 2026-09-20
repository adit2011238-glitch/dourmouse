"""dourmouse/device_wiki/walker.py -- real filesystem I/O against real
temp directories, never mocked (the walk/hash logic IS the thing under
test, matching test_security_platform_adapter.py's own "real data only"
discipline where it makes sense, adapted for real local files instead of
captured command output)."""

from __future__ import annotations

from pathlib import Path

from dourmouse.device_wiki.store import WikiStore
from dourmouse.device_wiki.walker import ROOTS_ENV, configured_roots, scan, walk_roots


class TestConfiguredRoots:
    def test_unset_env_is_an_honest_empty_list(self, monkeypatch):
        monkeypatch.delenv(ROOTS_ENV, raising=False)
        assert configured_roots() == []

    def test_a_real_configured_root_is_returned(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ROOTS_ENV, str(tmp_path))
        assert configured_roots() == [tmp_path]

    def test_a_nonexistent_configured_root_is_silently_excluded(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ROOTS_ENV, str(tmp_path / "does-not-exist"))
        assert configured_roots() == []

    def test_multiple_real_roots_are_all_returned(self, monkeypatch, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv(ROOTS_ENV, f"{a}{__import__('os').pathsep}{b}")
        assert set(configured_roots()) == {a, b}


class TestWalkRoots:
    def test_finds_every_real_file(self, tmp_path):
        (tmp_path / "a.txt").write_text("real content a")
        (tmp_path / "b.txt").write_text("real content b")
        found = walk_roots([tmp_path])
        assert set(found.keys()) == {str(tmp_path / "a.txt"), str(tmp_path / "b.txt")}

    def test_content_hash_is_a_real_sha256_of_the_real_file(self, tmp_path):
        import hashlib

        content = "real, specific content"
        f = tmp_path / "a.txt"
        f.write_text(content)
        found = walk_roots([tmp_path])
        assert found[str(f)][0] == hashlib.sha256(content.encode("utf-8")).hexdigest()

    def test_size_bytes_is_the_real_file_size(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("12345")
        found = walk_roots([tmp_path])
        assert found[str(f)][1] == 5

    def test_skips_known_system_and_dotfile_directories(self, tmp_path):
        (tmp_path / "real.txt").write_text("real")
        skip_dir = tmp_path / "node_modules"
        skip_dir.mkdir()
        (skip_dir / "junk.js").write_text("skip me")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("skip me too")
        found = walk_roots([tmp_path])
        assert set(found.keys()) == {str(tmp_path / "real.txt")}

    def test_skips_a_real_oversized_file_entirely(self, tmp_path):
        small = tmp_path / "small.txt"
        small.write_text("small")
        big = tmp_path / "big.txt"
        big.write_bytes(b"x" * 1000)
        found = walk_roots([tmp_path], max_file_size=500)
        assert set(found.keys()) == {str(small)}

    def test_multiple_real_roots_are_all_walked(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "one.txt").write_text("1")
        (b / "two.txt").write_text("2")
        found = walk_roots([a, b])
        assert set(found.keys()) == {str(a / "one.txt"), str(b / "two.txt")}

    def test_a_real_nested_subdirectory_is_walked(self, tmp_path):
        nested = tmp_path / "sub" / "deeper"
        nested.mkdir(parents=True)
        (nested / "f.txt").write_text("deep real content")
        found = walk_roots([tmp_path])
        assert str(nested / "f.txt") in found

    def test_zero_real_roots_is_an_honest_empty_result(self):
        assert walk_roots([]) == {}


class TestScan:
    def test_a_real_first_scan_persists_every_real_file(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("real content")
        store = WikiStore(tmp_path / "wiki.db")
        result = scan(store, [root], now=1000.0)
        assert set(result.keys()) == {str(root / "a.txt")}
        assert store.get(str(root / "a.txt")) is not None

    def test_a_real_deletion_is_reflected_on_the_next_scan(self, tmp_path):
        """Harsh acceptance test 2, end to end through the real store."""
        root = tmp_path / "root"
        root.mkdir()
        f = root / "a.txt"
        f.write_text("real content")
        store = WikiStore(tmp_path / "wiki.db")
        scan(store, [root], now=1000.0)
        f.unlink()
        result = scan(store, [root], now=2000.0)
        assert result[str(f)].status == "MISSING"
        assert store.get(str(f)).status == "MISSING"

    def test_a_real_content_change_is_reflected_on_the_next_scan(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        f = root / "a.txt"
        f.write_text("original real content")
        store = WikiStore(tmp_path / "wiki.db")
        first = scan(store, [root], now=1000.0)
        f.write_text("changed real content")
        result = scan(store, [root], now=2000.0)
        assert result[str(f)].status == "UNSUMMARIZED"
        assert result[str(f)].content_hash != first[str(f)].content_hash

    def test_a_new_root_file_appearing_later_is_picked_up(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        store = WikiStore(tmp_path / "wiki.db")
        scan(store, [root], now=1000.0)
        (root / "new.txt").write_text("appeared later")
        result = scan(store, [root], now=2000.0)
        assert str(root / "new.txt") in result
        assert result[str(root / "new.txt")].status == "UNSUMMARIZED"
