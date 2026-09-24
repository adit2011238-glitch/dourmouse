"""dourmouse/device_wiki_tools.py -- the device_wiki subagent, chat-
reachable wiring over Domain E's already-tested stages/store/walker.
Real store (a real SQLite file per test via monkeypatched default_db()) and
real temp directories; the model call itself is mocked (same
_FakeSession/_install_chat_fake convention as test_research_pipeline.py)."""

from __future__ import annotations

import pytest

import dourmouse.chat as chat_module
import dourmouse.device_wiki_tools as dwt
from dourmouse.device_wiki.store import WikiStore
from dourmouse.device_wiki.walker import ROOTS_ENV


class _FakeSession:
    calls: list[str] = []
    responses: list[object] = []

    def __init__(self, registry, session_file=None):
        pass

    def ask(self, prompt, force_plain_dispatch=False):
        type(self).calls.append(prompt)
        responses = type(self).responses
        scripted = responses[0] if len(responses) == 1 else responses.pop(0)
        return {"final_text": scripted}


def _install_chat_fake(monkeypatch, responses: list[str]):
    _FakeSession.calls = []
    _FakeSession.responses = list(responses)
    monkeypatch.setattr(chat_module, "ChatSession", _FakeSession)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(dwt, "default_db", lambda: tmp_path / "wiki.db")


def _tool(name: str):
    sub = dwt.build_device_wiki_subagent()
    for t in sub.tools:
        if t.name == name:
            return t
    raise AssertionError(f"no tool named {name!r}")


class TestDeviceWikiScanTool:
    def test_no_configured_roots_is_an_honest_error(self, monkeypatch):
        monkeypatch.delenv(ROOTS_ENV, raising=False)
        out = _tool("device_wiki_scan").handler({})
        assert "ERROR" in out
        assert "DOURMOUSE_WIKI_ROOTS" in out

    def test_a_real_scan_summarizes_new_files(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("real content a")
        (root / "b.txt").write_text("real content b")
        monkeypatch.setenv(ROOTS_ENV, str(root))
        _install_chat_fake(monkeypatch, ["summary a", "summary b"])
        out = _tool("device_wiki_scan").handler({})
        assert "2 real file(s) tracked" in out
        assert "2 newly summarized" in out
        assert "0 still unsummarized" in out

    def test_max_files_to_summarize_caps_real_model_calls(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        for i in range(3):
            (root / f"f{i}.txt").write_text(f"real content {i}")
        monkeypatch.setenv(ROOTS_ENV, str(root))
        _install_chat_fake(monkeypatch, ["a real summary"])
        out = _tool("device_wiki_scan").handler({"max_files_to_summarize": 1})
        assert "1 newly summarized" in out
        assert "2 still unsummarized" in out
        assert len(_FakeSession.calls) == 1

    def test_invalid_max_files_is_an_honest_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ROOTS_ENV, str(tmp_path))
        out = _tool("device_wiki_scan").handler({"max_files_to_summarize": "not-a-number"})
        assert "ERROR" in out

    def test_a_real_deletion_is_reported_as_missing(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        f = root / "a.txt"
        f.write_text("real content")
        monkeypatch.setenv(ROOTS_ENV, str(root))
        _install_chat_fake(monkeypatch, ["a real summary"])
        _tool("device_wiki_scan").handler({})
        f.unlink()
        out = _tool("device_wiki_scan").handler({})
        assert "1 marked missing" in out


class TestDeviceWikiStatusTool:
    def test_no_entries_yet_is_honest(self):
        out = _tool("device_wiki_status").handler({})
        assert "No real device wiki entries yet" in out

    def test_reports_real_status_counts(self, tmp_path, monkeypatch):
        from dourmouse.device_wiki.core import with_new_entry, with_summary

        store = WikiStore(tmp_path / "wiki.db")
        monkeypatch.setattr(dwt, "default_db", lambda: tmp_path / "wiki.db")
        store.save_entry(with_summary(with_new_entry("/a.txt", "h1", 1, now=1000.0), "s", now=1000.0))
        store.save_entry(with_new_entry("/b.txt", "h2", 2, now=1000.0))
        out = _tool("device_wiki_status").handler({})
        assert "SUMMARIZED: 1" in out
        assert "UNSUMMARIZED: 1" in out


class TestDeviceWikiGetTool:
    def test_empty_path_is_an_honest_error(self):
        assert "ERROR" in _tool("device_wiki_get").handler({"path": "  "})

    def test_unknown_path_is_honest(self):
        out = _tool("device_wiki_get").handler({"path": "/never-scanned.txt"})
        assert "No real wiki entry" in out

    def test_a_real_summarized_entry_is_returned(self, tmp_path, monkeypatch):
        from dourmouse.device_wiki.core import with_new_entry, with_summary

        store = WikiStore(tmp_path / "wiki.db")
        monkeypatch.setattr(dwt, "default_db", lambda: tmp_path / "wiki.db")
        store.save_entry(with_summary(with_new_entry("/a.txt", "h1", 1, now=1000.0), "a real summary", now=1000.0))
        out = _tool("device_wiki_get").handler({"path": "/a.txt"})
        assert "SUMMARIZED" in out
        assert "a real summary" in out


class TestBuildDeviceWikiSubagent:
    def test_registers_all_real_tools(self):
        sub = dwt.build_device_wiki_subagent()
        assert sub.name == "device_wiki"
        assert {t.name for t in sub.tools} == {
            "device_wiki_scan", "device_wiki_status", "device_wiki_get",
        }

    def test_no_tool_writes_to_a_real_file(self):
        """Domain E's own harsh acceptance test 3: the wiki is read-only,
        never touches/moves/renames/deletes a real file."""
        import inspect

        for tool in dwt.build_device_wiki_subagent().tools:
            source = inspect.getsource(tool.handler)
            assert "unlink" not in source
            assert "os.remove" not in source
            assert "shutil.move" not in source
            assert ".write_text(" not in source
            assert ".write_bytes(" not in source
