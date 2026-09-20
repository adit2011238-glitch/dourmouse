"""dourmouse/device_wiki/stages.py -- real file reads (against real
temp files) and a mocked model call (same _FakeSession/_install_chat_fake
pattern as test_research_pipeline.py's own convention, a local
reconstruction per this codebase's established per-file-doubles rule)."""

from __future__ import annotations

import dourmouse.chat as chat_module
from dourmouse.device_wiki.core import with_new_entry
from dourmouse.device_wiki.stages import (
    _strip_internal_diagnostics,
    read_file_for_summary,
    summarize_entry,
    summarize_file,
)

_REAL_PLAN_STEP_DIAGNOSTIC = (
    "\n\n[DOURMOUSE: plan step(s) not executed via tools -- no tools were available this turn.]"
)


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


class TestReadFileForSummary:
    def test_a_real_text_file_is_read(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("real file content")
        assert read_file_for_summary(str(f)) == "real file content"

    def test_a_missing_file_is_honestly_none(self, tmp_path):
        assert read_file_for_summary(str(tmp_path / "never-created.txt")) is None

    def test_a_directory_is_honestly_none(self, tmp_path):
        assert read_file_for_summary(str(tmp_path)) is None

    def test_a_real_binary_file_is_honestly_none(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"\x00\x01\x02real binary data")
        assert read_file_for_summary(str(f)) is None

    def test_a_real_empty_file_is_honestly_none(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert read_file_for_summary(str(f)) is None

    def test_a_whitespace_only_file_is_honestly_none(self, tmp_path):
        f = tmp_path / "blank.txt"
        f.write_text("   \n\n   ")
        assert read_file_for_summary(str(f)) is None

    def test_content_is_truncated_to_the_real_cost_bound(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 50_000)
        result = read_file_for_summary(str(f))
        assert len(result) == 8000


class TestSummarizeEntry:
    def test_a_real_summary_is_applied(self, monkeypatch):
        _install_chat_fake(monkeypatch, ["A real summary of the file's content."])
        entry = with_new_entry("/a.txt", "hash1", 100, now=1000.0)
        result = summarize_entry(entry, "real content", now=2000.0)
        assert result.status == "SUMMARIZED"
        assert result.summary == "A real summary of the file's content."

    def test_the_real_path_and_content_reach_the_model(self, monkeypatch):
        _install_chat_fake(monkeypatch, ["summary"])
        entry = with_new_entry("/real/path.txt", "hash1", 100, now=1000.0)
        summarize_entry(entry, "the real unique content marker", now=2000.0)
        assert "/real/path.txt" in _FakeSession.calls[0]
        assert "the real unique content marker" in _FakeSession.calls[0]

    def test_a_genuinely_empty_model_reply_is_an_honest_failed_summary(self, monkeypatch):
        _install_chat_fake(monkeypatch, [""])
        entry = with_new_entry("/a.txt", "hash1", 100, now=1000.0)
        result = summarize_entry(entry, "real content", now=2000.0)
        assert result.status == "UNSUMMARIZED"
        assert result.summary == ""

    def test_a_leaked_diagnostic_is_stripped_from_the_real_summary(self, monkeypatch):
        _install_chat_fake(monkeypatch, ["A real summary." + _REAL_PLAN_STEP_DIAGNOSTIC])
        entry = with_new_entry("/a.txt", "hash1", 100, now=1000.0)
        result = summarize_entry(entry, "real content", now=2000.0)
        assert result.summary == "A real summary."
        assert "[DOURMOUSE" not in result.summary


class TestStripInternalDiagnostics:
    def test_strips_the_real_leaked_suffix(self):
        assert _strip_internal_diagnostics("real text" + _REAL_PLAN_STEP_DIAGNOSTIC) == "real text"

    def test_leaves_real_text_with_no_diagnostic_untouched(self):
        assert _strip_internal_diagnostics("real text") == "real text"


class TestSummarizeFile:
    def test_a_real_readable_file_is_summarized(self, monkeypatch, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("real file content")
        _install_chat_fake(monkeypatch, ["a real summary"])
        entry = with_new_entry(str(f), "hash1", 100, now=1000.0)
        result = summarize_file(entry, now=2000.0)
        assert result.status == "SUMMARIZED"
        assert result.summary == "a real summary"

    def test_a_binary_file_never_reaches_the_model(self, monkeypatch, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"\x00\x01real binary")
        _install_chat_fake(monkeypatch, ["should never be used"])
        entry = with_new_entry(str(f), "hash1", 100, now=1000.0)
        result = summarize_file(entry, now=2000.0)
        assert result.status == "UNSUMMARIZED"
        assert _FakeSession.calls == []

    def test_a_deleted_file_never_reaches_the_model(self, monkeypatch, tmp_path):
        _install_chat_fake(monkeypatch, ["should never be used"])
        entry = with_new_entry(str(tmp_path / "gone.txt"), "hash1", 100, now=1000.0)
        result = summarize_file(entry, now=2000.0)
        assert result.status == "UNSUMMARIZED"
        assert _FakeSession.calls == []
