"""FD2 fixes for repo_index: P4-48 (confinement, credentials, DLP), P4-49 (duplicate headings),
P4-50 (prune on a store whose reads and writes differ)."""

from __future__ import annotations

import pytest

from dourmouse import repo_index as ri
from dourmouse.memory_store import LocalFallbackMemoryStore, MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "mem.db"))
    monkeypatch.setenv("DOURMOUSE_LEARN", "1")
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("DOURMOUSE_REPO_SCAN_ROOTS", raising=False)
    monkeypatch.delenv("ATLAS_REPO_PATH", raising=False)


# --- P4-48 ----------------------------------------------------------------- #
def test_scan_tool_refuses_a_directory_outside_the_project_roots(tool_env, tmp_path):
    outside = tmp_path / "somewhere" / "dotfiles"
    outside.mkdir(parents=True)
    (outside / "README.md").write_text("# hi\n")
    out = ri._repo_scan_tool({"path": str(outside)})
    assert "REPO INDEX (honest)" in out
    assert "outside" in out
    store = MemoryStore(tmp_path / "mem.db")
    assert store.count() == 0
    store.close()


def test_scan_tool_refuses_home(tool_env, tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    home.mkdir()
    (home / "README.md").write_text("# private\n")
    monkeypatch.setenv("HOME", str(home))
    out = ri._repo_scan_tool({"path": "~"})
    assert "outside" in out
    store = MemoryStore(tmp_path / "mem.db")
    assert store.count() == 0
    store.close()


def test_scan_tool_accepts_a_configured_root(tool_env, tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "README.md").write_text("# hi\nthing\n")
    monkeypatch.setenv("DOURMOUSE_REPO_SCAN_ROOTS", str(tmp_path))
    out = ri._repo_scan_tool({"path": str(proj)})
    assert "REPO INDEXED" in out


def test_scan_tool_accepts_the_workspace(tool_env, tmp_path):
    ws = tmp_path / "ws" / "proj"
    ws.mkdir(parents=True)
    (ws / "README.md").write_text("# hi\nthing\n")
    out = ri._repo_scan_tool({"path": str(ws)})
    assert "REPO INDEXED" in out


def test_credential_named_files_are_not_collected(tmp_path):
    for name in (
        "credentials.json", "secrets.yaml", "service_account.json", "client_secret_1.json",
        "tokens.json", "auth.json", "id_rsa", "prod.pem", "my.key", ".npmrc", ".netrc",
        "api_keys.txt", "password.txt",
    ):
        (tmp_path / name).write_text("x = 1\n")
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "config").write_text("[default]\n")
    (tmp_path / "README.md").write_text("# ok\n")
    (tmp_path / "config.toml").write_text("name = 'ok'\n")
    names = sorted(p.name for p in ri._collect_files(tmp_path))
    assert names == ["README.md", "config.toml"]


def test_digests_are_dlp_redacted_before_storage(store, tmp_path):
    secret = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 3
    (tmp_path / "notes.txt").write_text(f"deploy notes\nANTHROPIC_API_KEY={secret}\n")
    ri.scan_repo(store, tmp_path)
    bodies = " ".join(f["body"] for f in store.all_facts())
    assert secret not in bodies
    assert "REDACTED" in bodies


# --- P4-49 ----------------------------------------------------------------- #
def test_duplicate_changelog_headings_are_all_indexed(store, tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Fixed\n- first fix alpha\n\n## Fixed\n- second fix bravo\n\n## Fixed\n- third fix charlie\n"
    )
    stats = ri.scan_repo(store, tmp_path)
    bodies = " ".join(f["body"] for f in store.all_facts())
    assert "alpha" in bodies and "bravo" in bodies and "charlie" in bodies
    assert stats["unchanged"] == 0
    again = ri.scan_repo(store, tmp_path)  # idempotent, nothing overwritten
    assert again["updated"] == 0 and again["added"] == 0 and again["removed"] == 0
    assert again["unchanged"] == 3


# --- P4-50 ----------------------------------------------------------------- #
class _FakeRemote:
    """Remote whose reads work, writes work, and a full dump is unsupported."""

    def __init__(self):
        self.facts = {}

    def remember(self, source, title, body):
        self.facts[(source, title)] = body
        return "ok"

    def get(self, source, title):
        b = self.facts.get((source, title))
        return None if b is None else {"source": source, "title": title, "body": b}

    def count(self, source=None):
        return len([k for k in self.facts if source in (None, k[0])])

    def all_facts(self):
        from dourmouse.memory_store import RemoteMemoryStoreUnavailable

        raise RemoteMemoryStoreUnavailable("full dump unsupported")

    def close(self):
        pass


def test_scan_with_a_fallback_store_does_not_prune_from_the_wrong_store(tmp_path):
    local = MemoryStore(tmp_path / "local.db")
    # a PENDING SYNC repo fact left locally during an outage
    local.remember("repo:proj", "[PENDING SYNC] gone.md", "META mtime=1 size=1\nKIND=markdown\nPATH=gone.md\n\nx")
    remote = _FakeRemote()
    store = LocalFallbackMemoryStore(remote, lambda: local)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("# hello\n")
    stats = ri.scan_repo(store, proj, source="repo:proj")  # must not raise AttributeError
    assert ("repo:proj", "README.md") in remote.facts
    assert stats["removed"] == 0
    assert "prune_skipped" in stats
    # the local pending fact is untouched
    assert local.get("repo:proj", "[PENDING SYNC] gone.md") is not None
    local.close()
