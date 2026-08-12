"""v4.1 (P6+) Project Memory panel tests — /api/repo endpoints, scan meta,
repo_facts listing, honest NOT CONFIGURED, and HUD wiring.

Hermetic: real HTTP against a temp store (DOURMOUSE_LEARN defaults on and is
pinned to 1), a fake ATLAS repo under tmp_path for the scan path, no network.
"""

from __future__ import annotations

import http.client
import json
import os
import threading

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse.memory_store import MemoryStore
from dourmouse.repo_index import (
    load_scan_meta,
    repo_facts,
    save_scan_meta,
    scan_repo,
)

_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "ui")


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


def _make_fake_repo(root):
    """A small ATLAS-shaped repo (same shape as test_repo_index)."""
    (root / "atlas").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "deliverables").mkdir()
    (root / ".git").mkdir()
    (root / ".venv").mkdir()
    (root / "README.md").write_text(
        "# ATLAS\nQuant research platform. The scalping engine lives here.\n",
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 2026-07-01\n- Tightened daily loss limit to 3%.\n",
        encoding="utf-8",
    )
    (root / "atlas" / "engine.py").write_text(
        '"""Core backtest engine."""\n\n\ndef run_backtest(strategy: str) -> float:\n'
        '    """Run one backtest."""\n    return 1.0\n',
        encoding="utf-8",
    )
    (root / "tests" / "test_engine.py").write_text(
        '"""Engine tests."""\n\n\ndef test_run():\n    assert True\n',
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# repo_facts + scan-meta sidecar (unit level)
# --------------------------------------------------------------------------- #


class TestRepoFactsAndMeta:
    def test_repo_facts_newest_first_with_path_and_kind(self, store, tmp_path):
        _make_fake_repo(tmp_path / "repo")
        scan_repo(store, tmp_path / "repo")
        facts = repo_facts(store, limit=50)
        assert facts, "a scanned repo must produce facts"
        # newest-first: all_facts is id-ascending, the last ingest is last
        assert facts[0]["path"] == "tests/test_engine.py"
        assert facts[0]["kind"] == "python"
        assert facts[0]["title"].startswith("tests/test_engine.py")
        kinds = {f["kind"] for f in facts}
        assert kinds >= {"python", "markdown"}
        for f in facts:
            assert f["path"]

    def test_repo_facts_honors_limit(self, store, tmp_path):
        _make_fake_repo(tmp_path / "repo")
        scan_repo(store, tmp_path / "repo")
        assert len(repo_facts(store, limit=2)) <= 2

    def test_scan_meta_roundtrip_and_missing(self, store, tmp_path):
        assert load_scan_meta(store) is None  # never scanned -> honest None
        stats = {"scanned": 5, "added": 4, "updated": 1, "skipped": 1,
                 "unchanged": 0, "removed": 0, "total_facts": 4}
        save_scan_meta(store, stats, tmp_path)
        meta = load_scan_meta(store)
        assert meta is not None
        assert meta["root"] == str(tmp_path)
        assert meta["total_facts"] == 4
        assert meta["when"]

    def test_scan_meta_never_pollutes_fact_count(self, store, tmp_path):
        _make_fake_repo(tmp_path / "repo")
        stats = scan_repo(store, tmp_path / "repo")
        before = store.count(source="repo")
        assert before == stats["total_facts"] > 0
        save_scan_meta(store, stats, tmp_path)
        assert store.count(source="repo") == before  # sidecar, not a fact

    def test_corrupt_meta_loads_as_none(self, store, tmp_path):
        path = str(store.db_path) + ".repo-meta.json"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert load_scan_meta(store) is None

    def test_concurrent_saves_never_corrupt_meta(self, store, tmp_path):
        """Unique tmp names + lock: two racing saves (threaded server) can
        interleave freely yet the sidecar must always stay valid JSON with
        one of the two payloads — never a torn write."""
        import threading

        base = {"scanned": 0, "added": 0, "updated": 0, "skipped": 0,
                "unchanged": 0, "removed": 0, "total_facts": 0}
        errors: list[Exception] = []

        def writer(tag: str, n: int) -> None:
            try:
                for i in range(20):
                    save_scan_meta(store, {**base, "scanned": n + i}, tmp_path)
            except Exception as exc:  # noqa: BLE001 -- test surface: a crash must be visible
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t, t * 100)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        meta = load_scan_meta(store)
        assert meta is not None
        assert set(meta) >= {"when", "root", "scanned", "total_facts"}

    def test_scan_never_ingests_its_own_sidecar(self, store, tmp_path):
        """A .repo-meta.json inside the scanned root must be skipped — the
        scan must never index its own store sidecar (self-ingestion loop)."""
        _make_fake_repo(tmp_path / "repo")
        (tmp_path / "repo" / "mem.db.repo-meta.json").write_text(
            '{"when": "x"}', encoding="utf-8"
        )
        stats = scan_repo(store, tmp_path / "repo")
        titles = {f["title"] for f in store.all_facts() if f["source"] == "repo"}
        assert stats["total_facts"] == stats["scanned"]
        assert not any("repo-meta" in t for t in titles)


# --------------------------------------------------------------------------- #
# /api/repo endpoints over real HTTP
# --------------------------------------------------------------------------- #


@pytest.fixture
def server(tmp_path, monkeypatch):
    from dourmouse.webui import run_server

    monkeypatch.setenv("DOURMOUSE_LEARN", "1")
    registry = build_general_registry()
    store = MemoryStore(tmp_path / "mem.db")
    srv = run_server(registry, port=0, memory=store)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _host, port = srv.server_address[:2]
    yield int(port), store
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=3)
    store.close()


@pytest.fixture
def bare_server(monkeypatch):
    """Server with NO memory store — the honest NOT CONFIGURED path."""
    from dourmouse.webui import run_server

    monkeypatch.setenv("DOURMOUSE_LEARN", "1")
    registry = build_general_registry()
    srv = run_server(registry, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _host, port = srv.server_address[:2]
    yield int(port)
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=3)


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = json.loads(resp.read().decode())
    conn.close()
    return resp.status, body


def _post(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path)
    resp = conn.getresponse()
    body = json.loads(resp.read().decode())
    conn.close()
    return resp.status, body


def _seed_repo_fact(store, title="README.md", body_extra="the scalping engine"):
    store.remember(
        "repo",
        title,
        f"META mtime=1 size=1\nKIND=markdown\nPATH={title}\n\n# ATLAS\n{body_extra}",
    )


class TestRepoStatusEndpoint:
    def test_status_not_configured_without_memory(self, bare_server):
        status, body = _get(bare_server, "/api/repo")
        assert status == 200
        assert body["configured"] is False
        assert "NOT CONFIGURED" in body["error"]

    def test_status_reports_facts_recent_and_last_scan(self, server):
        port, store = server
        _seed_repo_fact(store)
        status, body = _get(port, "/api/repo")
        assert status == 200
        assert body["configured"] is True
        assert body["facts"] == 1
        assert body["last_scan"] is None  # never scanned -> honest
        assert body["recent"] and body["recent"][0]["path"] == "README.md"

    def test_status_reflects_scan_meta(self, server, tmp_path):
        port, store = server
        _make_fake_repo(tmp_path / "repo")
        stats = scan_repo(store, tmp_path / "repo")
        save_scan_meta(store, stats, tmp_path / "repo")
        _status, body = _get(port, "/api/repo")
        assert body["facts"] == stats["total_facts"]
        assert body["last_scan"]["root"] == str(tmp_path / "repo")
        assert body["last_scan"]["when"]


class TestRepoSearchEndpoint:
    def test_search_scoped_to_repo_source(self, server):
        port, store = server
        _seed_repo_fact(store, title="README.md")
        store.remember("feedback", "fb-1", "the scalping engine is good")  # noise
        status, body = _get(port, "/api/repo?q=scalping")
        assert status == 200
        assert body["configured"] is True
        assert body["query"] == "scalping"
        assert body["hits"], "the repo fact must match"
        assert all(h["source"] == "repo" for h in body["hits"])
        assert all("README.md" in h["title"] for h in body["hits"])

    def test_search_no_match_is_honest(self, server):
        port, store = server
        _seed_repo_fact(store, body_extra="forex volatility")
        _status, body = _get(port, "/api/repo?q=zzzzzzz")
        assert body["hits"] == []

    def test_blank_query_returns_status_shape(self, server):
        port, store = server
        _seed_repo_fact(store)
        _status, body = _get(port, "/api/repo?q=%20%20")
        assert body["configured"] is True
        assert "facts" in body  # status shape, not search


class TestRepoScanEndpoint:
    def test_scan_ingests_writes_meta(self, server, tmp_path, monkeypatch):
        port, store = server
        _make_fake_repo(tmp_path / "repo")
        monkeypatch.setenv("ATLAS_REPO_PATH", str(tmp_path / "repo"))
        status, body = _post(port, "/api/repo/scan")
        assert status == 200
        assert body["ok"] is True
        assert body["stats"]["scanned"] > 0
        assert body["stats"]["total_facts"] > 0
        assert body["root"] == str(tmp_path / "repo")
        meta = load_scan_meta(store)
        assert meta and meta["total_facts"] == body["stats"]["total_facts"]
        _status, get_body = _get(port, "/api/repo")
        assert get_body["facts"] == body["stats"]["total_facts"]

    def test_scan_is_idempotent_over_http(self, server, tmp_path, monkeypatch):
        port, _store = server
        _make_fake_repo(tmp_path / "repo")
        monkeypatch.setenv("ATLAS_REPO_PATH", str(tmp_path / "repo"))
        _status, first = _post(port, "/api/repo/scan")
        _status, second = _post(port, "/api/repo/scan")
        assert second["stats"]["added"] == 0
        assert second["stats"]["total_facts"] == first["stats"]["total_facts"]

    def test_scan_not_configured_without_path(self, server, monkeypatch):
        port, _store = server
        monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)
        status, body = _post(port, "/api/repo/scan")
        assert status == 409
        assert body["ok"] is False
        assert "NOT CONFIGURED" in body["error"]

    def test_scan_not_configured_without_memory(self, bare_server):
        status, body = _post(bare_server, "/api/repo/scan")
        assert status == 409
        assert body["ok"] is False


# --------------------------------------------------------------------------- #
# HUD wiring
# --------------------------------------------------------------------------- #


class TestHudWiring:
    def test_project_memory_panel_present(self):
        html = os.path.join(_UI_DIR, "index.html")
        with open(html, encoding="utf-8") as fh:
            text = fh.read()
        for needle in (
            'id="repofacts"',
            'id="reposearch"',
            'id="rescanBtn"',
            'id="repometa"',
            'id="repocount"',
            "PROJECT MEMORY",
        ):
            assert needle in text

    def test_panel_js_wired(self):
        with open(os.path.join(_UI_DIR, "index.html"), encoding="utf-8") as fh:
            text = fh.read()
        assert "fetch('/api/repo')" in text
        assert "fetch('/api/repo?q=' + encodeURIComponent(q))" in text
        assert "fetch('/api/repo/scan', { method: 'POST' })" in text
        assert "pollRepo()" in text
