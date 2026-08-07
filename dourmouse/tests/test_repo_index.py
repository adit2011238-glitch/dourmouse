"""P6 repo-index tests — fake-repo scan, idempotency, source scoping, honesty.

Fully hermetic: ``scan_repo`` is called with an explicit tmp root (no
ATLAS_REPO_PATH needed) except for the NOT-CONFIGURED test, which clears it.
No network, no git (Rules 2.1 / 2.8).
"""

from __future__ import annotations

import os

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse.memory_store import MemoryStore
from dourmouse.repo_index import (
    _repo_scan_tool,
    _repo_search_tool,
    _repo_status_tool,
    load_scan_meta,
    repo_search,
    repo_status,
    save_scan_meta,
    scan_repo,
    source_for_root,
)


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


def _make_fake_repo(root):
    """A small ATLAS-shaped repo: docs, source, tests + noise to exclude."""
    (root / "atlas").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "deliverables").mkdir()
    (root / ".git").mkdir()
    (root / ".venv").mkdir()
    (root / "data" / "fx_archive").mkdir(parents=True)
    (root / "README.md").write_text("# ATLAS\nQuant research platform.\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 2026-07-01\n- Tightened daily loss limit to 3%.\n",
        encoding="utf-8",
    )
    (root / "atlas" / "engine.py").write_text(
        '"""Core backtest engine."""\n\n\ndef run_backtest(strategy: str) -> float:\n    """Run one backtest."""\n    return 1.0\n',
        encoding="utf-8",
    )
    (root / "tests" / "test_engine.py").write_text(
        '"""Engine tests."""\n\n\ndef test_run():\n    assert True\n',
        encoding="utf-8",
    )
    (root / "deliverables" / "report.json").write_text(
        '{"strategy": "scalp", "sharpe": 1.4}', encoding="utf-8"
    )
    # noise that must be excluded:
    (root / ".venv" / "lib.py").write_text("def noise(): pass\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "data" / "fx_archive" / "x.bi5").write_bytes(b"\x00\x01\x02")
    (root / "big.bin").write_bytes(b"\x00" * 600_000)  # > size cap + binary


class TestScan:
    def test_ingests_docs_source_and_reports(self, store, tmp_path):
        _make_fake_repo(tmp_path)
        stats = scan_repo(store, tmp_path)
        assert stats["added"] >= 5  # README, CHANGELOG, engine.py, test, report.json
        assert stats["total_facts"] == store.count(source="repo")

    def test_excludes_venv_git_data_and_binary(self, store, tmp_path):
        _make_fake_repo(tmp_path)
        scan_repo(store, tmp_path)
        facts = store.all_facts()
        titles = {f["title"] for f in facts}
        assert not any("lib.py" in t for t in titles), ".venv must be excluded"
        assert not any(t.startswith(".git") for t in titles), ".git must be excluded"
        assert not any("fx_archive" in t for t in titles), "data tree must be excluded"
        assert not any("big.bin" in t for t in titles), "oversized/binary must be skipped"
        assert any("report.json" in t for t in titles), "deliverables are still ingested"

    def test_changelog_split_into_section_facts(self, store, tmp_path):
        """Decisions buried mid-file must be first-class retrievable facts."""
        (tmp_path / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## v9 — Phase 5: Risk management layer\n- Tightened daily loss limit to 3%.\n\n"
            "## v10 — Data integrity\n- Removed survivorship bias.\n",
            encoding="utf-8",
        )
        scan_repo(store, tmp_path)
        assert store.get("repo", "CHANGELOG.md: v9 — Phase 5: Risk management layer") is not None
        assert store.get("repo", "CHANGELOG.md: v10 — Data integrity") is not None
        hits = repo_search(store, "survivorship bias")
        assert hits and hits[0]["title"].startswith("CHANGELOG.md: v10"), \
            "a mid-file section must be retrievable on its own"

    def test_changelog_without_sections_falls_back_to_flat(self, store, tmp_path):
        """A changelog with no '## ' headings must not vanish from the index
        (reviewer-caught: sectioning returned [] -> file skipped AND pruned)."""
        (tmp_path / "CHANGELOG.md").write_text(
            "# Changelog\n\n- Tightened daily loss limit to 3%.\n- Added the FX archive.\n",
            encoding="utf-8",
        )
        stats = scan_repo(store, tmp_path)
        assert stats["added"] == 1
        assert store.get("repo", "CHANGELOG.md") is not None

    def test_transient_skip_keeps_prior_facts(self, store, tmp_path, monkeypatch):
        """A file that produced facts but is transiently unreadable on a later
        scan must KEEP its facts — a flaky scan never destroys knowledge."""
        from dourmouse import repo_index

        (tmp_path / "README.md").write_text("# Atlas\nThe risk layer lives here.\n", encoding="utf-8")
        scan_repo(store, tmp_path)
        assert store.get("repo", "README.md") is not None
        # simulate a transient failure: nothing can be digested this run
        monkeypatch.setattr(repo_index, "_digest_many", lambda path: [])
        rescan = scan_repo(store, tmp_path)
        assert rescan["skipped"] >= 1
        assert rescan["removed"] == 0
        assert store.get("repo", "README.md") is not None, \
            "facts must survive a transiently-failed scan"

    def test_python_skeleton_keeps_signatures(self, store, tmp_path):
        _make_fake_repo(tmp_path)
        scan_repo(store, tmp_path)
        engine = store.get("repo", "atlas/engine.py")
        assert engine is not None
        assert "run_backtest" in engine["body"]
        assert "Core backtest engine" in engine["body"]

    def test_scan_is_idempotent(self, store, tmp_path):
        _make_fake_repo(tmp_path)
        scan_repo(store, tmp_path)
        second = scan_repo(store, tmp_path)
        assert second["added"] == 0
        assert second["updated"] == 0
        assert second["removed"] == 0
        assert second["unchanged"] > 0
        assert second["total_facts"] == store.count(source="repo")

    def test_changed_file_is_updated(self, store, tmp_path):
        """A changed changelog re-ingests: the new section lands, the old one
        is pruned (per-section facts make this exact)."""
        _make_fake_repo(tmp_path)
        scan_repo(store, tmp_path)
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## 2026-07-02\n- Added a feature.\n", encoding="utf-8")
        old = os.stat(changelog).st_mtime
        os.utime(changelog, (old + 10, old + 10))
        second = scan_repo(store, tmp_path)
        assert second["added"] + second["updated"] >= 1
        assert store.get("repo", "CHANGELOG.md: 2026-07-02") is not None
        assert store.get("repo", "CHANGELOG.md: 2026-07-01") is None

    def test_not_configured_without_repo_path(self, store, monkeypatch):
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        from dourmouse.atlas_ops import AtlasNotConfiguredError

        with pytest.raises(AtlasNotConfiguredError):
            scan_repo(store)  # no root -> must read ATLAS_REPO_PATH and fail honestly

    def test_deleted_files_are_pruned(self, store, tmp_path):
        """A re-scan after a file is deleted must drop its stale repo fact."""
        _make_fake_repo(tmp_path)
        scan_repo(store, tmp_path)
        assert store.count(source="repo") > 0
        (tmp_path / "CHANGELOG.md").unlink()
        rescan = scan_repo(store, tmp_path)
        assert rescan["removed"] >= 1
        assert store.get("repo", "CHANGELOG.md: 2026-07-01") is None


class TestSearch:
    def test_repo_search_scoped_to_repo_source(self, store, tmp_path):
        _make_fake_repo(tmp_path)
        scan_repo(store, tmp_path)
        store.remember("agent", "unrelated", "apple pie recipe")
        hits = repo_search(store, "apple")
        assert hits == []  # the non-repo fact must not leak in
        hits = repo_search(store, "loss limit")
        assert hits, "CHANGELOG's risk change must be findable"
        assert all(h["source"] == "repo" for h in hits)

    def test_repo_status_counts(self, store, tmp_path):
        _make_fake_repo(tmp_path)
        scan_repo(store, tmp_path)
        assert repo_status(store)["source"] == "repo"
        assert repo_status(store)["facts"] == store.count(source="repo")


class TestRosterWiring:
    def test_memory_agent_has_semantic_tool(self):
        registry = build_general_registry()
        mem = registry.get_subagent("memory")
        assert mem is not None
        names = {t.name for t in mem.tools}
        assert "memory_search_semantic" in names

    def test_atlas_agent_has_repo_tools(self):
        registry = build_general_registry()
        atlas = registry.get_subagent("atlas")
        assert atlas is not None
        names = {t.name for t in atlas.tools}
        assert {"atlas_repo_scan", "atlas_repo_search", "atlas_repo_status"} <= names


# --------------------------------------------------------------------------- #
# Multi-project scoping: explicit source keys + per-tool path/source params
# --------------------------------------------------------------------------- #


class TestMultiProjectScoping:
    def test_scan_with_explicit_source_stays_scoped(self, store, tmp_path):
        _make_fake_repo(tmp_path)
        stats = scan_repo(store, tmp_path, source="repo:myproj")
        assert stats["source"] == "repo:myproj"
        assert stats["total_facts"] > 0
        assert (
            repo_status(store, source="repo:myproj")["facts"]
            == stats["total_facts"]
        )
        assert repo_status(store)["facts"] == 0  # default 'repo' untouched

    def test_two_sources_coexist(self, store, tmp_path):
        _make_fake_repo(tmp_path)
        a = scan_repo(store, tmp_path, source="repo:proj-a")
        b = scan_repo(store, tmp_path, source="repo:proj-b")
        assert a["total_facts"] == b["total_facts"] > 0
        assert repo_status(store, source="repo:proj-a")["facts"] == a["total_facts"]
        assert repo_status(store, source="repo:proj-b")["facts"] == b["total_facts"]

    def test_search_scoped_to_one_source(self, store, tmp_path):
        """Scoping must be airtight: querying the DEFAULT scope after
        indexing a derived project must NOT leak its facts. (An FTS5 column
        filter would — source:"repo" token-matches repo:proj-a — so scoping
        is an exact f.source = ? equality; this test pins that.)"""
        _make_fake_repo(tmp_path)
        scan_repo(store, tmp_path, source="repo:proj-a")
        hits = repo_search(store, "loss limit", source="repo:proj-a")
        assert hits and all(h["source"] == "repo:proj-a" for h in hits)
        assert repo_search(store, "loss limit") == []  # default scope clean

    def test_prune_only_touches_its_own_source(self, store, tmp_path):
        def _titles(src: str) -> set[str]:
            return {f["title"] for f in store.all_facts() if f["source"] == src}

        _make_fake_repo(tmp_path)
        scan_repo(store, tmp_path, source="repo:proj-b")
        (tmp_path / "atlas" / "engine.py").unlink()
        # A fresh scan under a DIFFERENT source (no engine.py in the tree)
        # must not prune proj-b's fact for it.
        scan_repo(store, tmp_path, source="repo:proj-a")
        assert "atlas/engine.py" not in _titles("repo:proj-a")
        assert "atlas/engine.py" in _titles("repo:proj-b")  # untouched
        # Rescanning proj-b over the changed tree prunes only proj-b's fact.
        scan_repo(store, tmp_path, source="repo:proj-b")
        assert "atlas/engine.py" not in _titles("repo:proj-b")
        assert "README.md" in _titles("repo:proj-a") and "README.md" in _titles("repo:proj-b")

    def test_source_for_root_derives_slug(self, tmp_path):
        p = tmp_path / "My Proj!"
        p.mkdir()
        assert source_for_root(p) == "repo:my-proj"
        weird = tmp_path / "!!!"  # slug empty -> fallback
        weird.mkdir()
        assert source_for_root(weird) == "repo:project"

    def test_underscore_and_hyphen_folders_never_collide(self, store, tmp_path):
        """The prune deletes by exact source — so two distinct folder names
        must never collapse into one scope. 'my_proj' and 'my-proj' share
        every relative title; a shared source would let one rescan delete
        the other's facts."""
        under = tmp_path / "my_proj"
        hyphen = tmp_path / "my-proj"
        under.mkdir(); hyphen.mkdir()
        assert source_for_root(under) != source_for_root(hyphen)
        assert source_for_root(under) == "repo:my_proj"
        assert source_for_root(hyphen) == "repo:my-proj"
        # both scan cleanly into their own scopes without cross-pruning
        _make_fake_repo(under); _make_fake_repo(hyphen)
        a = scan_repo(store, under, source=source_for_root(under))
        b = scan_repo(store, hyphen, source=source_for_root(hyphen))
        assert a["total_facts"] == b["total_facts"] > 0
        # and their sidecar meta files are distinct too
        save_scan_meta(store, a, under, source=source_for_root(under))
        save_scan_meta(store, b, hyphen, source=source_for_root(hyphen))
        assert (
            load_scan_meta(store, source=source_for_root(under))["root"]
            == str(under)
        )
        assert (
            load_scan_meta(store, source=source_for_root(hyphen))["root"]
            == str(hyphen)
        )

    def test_scan_meta_per_source(self, store, tmp_path):
        stats = {"scanned": 1, "added": 1, "updated": 0, "skipped": 0,
                 "unchanged": 0, "removed": 0, "total_facts": 1}
        save_scan_meta(store, stats, tmp_path, source="repo:myproj")
        assert load_scan_meta(store, source="repo:myproj") is not None
        assert load_scan_meta(store) is None  # default scope has its own file


class TestToolsMultiProject:
    @pytest.fixture
    def tool_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "mem.db"))
        monkeypatch.setenv("DOURMOUSE_LEARN", "1")
        yield

    def test_scan_tool_path_derives_source(self, tool_store, tmp_path):
        _make_fake_repo(tmp_path)
        out = _repo_scan_tool({"path": str(tmp_path)})
        assert "REPO INDEXED (source=" in out
        assert f"source='{source_for_root(tmp_path)}'" in out
        assert "total facts in memory" in out

    def test_scan_tool_invalid_path_is_honest(self, tool_store, tmp_path):
        out = _repo_scan_tool({"path": str(tmp_path / "nope")})
        assert out.startswith("REPO INDEX (honest): ERROR")

    def test_scan_tool_default_uses_env(self, tool_store, tmp_path, monkeypatch):
        _make_fake_repo(tmp_path)
        monkeypatch.setenv("ATLAS_REPO_PATH", str(tmp_path))
        out = _repo_scan_tool({})
        assert "REPO INDEXED (source=repo)" in out
        assert "root:" in out

    def test_search_tool_source_param(self, tool_store, tmp_path):
        _make_fake_repo(tmp_path)
        _repo_scan_tool({"path": str(tmp_path)})
        src = source_for_root(tmp_path)
        out = _repo_search_tool({"query": "loss limit", "source": src})
        assert "REPO SEARCH RESULTS" in out
        assert f"source={src!r}" in out
        # the default 'repo' scope has nothing indexed -> honest no-match
        out2 = _repo_search_tool({"query": "loss limit"})
        assert "no matches" in out2

    def test_search_tool_path_resolves_source(self, tool_store, tmp_path):
        _make_fake_repo(tmp_path)
        _repo_scan_tool({"path": str(tmp_path)})
        out = _repo_search_tool({"query": "loss limit", "path": str(tmp_path)})
        assert "REPO SEARCH RESULTS" in out

    def test_status_tool_source_param(self, tool_store, tmp_path):
        _make_fake_repo(tmp_path)
        _repo_scan_tool({"path": str(tmp_path)})
        src = source_for_root(tmp_path)
        out = _repo_status_tool({"source": src})
        assert "facts indexed" in out
        assert f"source='{src}'" in out
        assert _repo_status_tool({}).endswith("source='repo').")
