"""Tests for dourmouse/study_agent.py — the Study tab's sandboxed, real
file access to the user's study resource folder (backlog #9)."""

from __future__ import annotations

import pytest

from dourmouse.study_agent import (
    StudyPathError,
    list_study_files,
    read_study_file,
    study_folder_status,
    study_root,
)


@pytest.fixture
def study_dir(tmp_path, monkeypatch):
    root = tmp_path / "MYP data folder"
    root.mkdir()
    (root / "notes.txt").write_text("real study notes", encoding="utf-8")
    (root / ".DS_Store").write_text("junk", encoding="utf-8")
    sub = root / "Textbooks"
    sub.mkdir()
    (sub / "chapter1.txt").write_text("chapter one content", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("should never be reachable", encoding="utf-8")
    monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(root))
    return root


class TestStudyRoot:
    def test_env_override_is_honored(self, study_dir):
        assert study_root() == study_dir

    def test_status_reports_real_existence(self, study_dir):
        status = study_folder_status()
        assert status["exists"] is True
        assert status["path"] == str(study_dir)

    def test_status_is_honest_when_folder_is_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(tmp_path / "does-not-exist"))
        status = study_folder_status()
        assert status["exists"] is False


class TestListStudyFiles:
    def test_lists_real_entries_excluding_dotfiles(self, study_dir):
        result = list_study_files()
        names = {e["name"] for e in result["entries"]}
        assert "notes.txt" in names
        assert "Textbooks" in names
        assert ".DS_Store" not in names

    def test_lists_a_real_subfolder(self, study_dir):
        result = list_study_files("Textbooks")
        names = {e["name"] for e in result["entries"]}
        assert "chapter1.txt" in names

    def test_refuses_a_path_traversal_attempt(self, study_dir):
        with pytest.raises(StudyPathError):
            list_study_files("../")

    def test_refuses_missing_folder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_STUDY_DIR", str(tmp_path / "gone"))
        with pytest.raises(StudyPathError):
            list_study_files()


class TestReadStudyFile:
    def test_reads_a_real_file(self, study_dir):
        result = read_study_file("notes.txt")
        assert result["content"] == "real study notes"
        assert result["truncated"] is False

    def test_reads_a_file_in_a_subfolder(self, study_dir):
        result = read_study_file("Textbooks/chapter1.txt")
        assert result["content"] == "chapter one content"

    def test_refuses_a_path_traversal_attempt_to_escape_the_folder(self, study_dir):
        with pytest.raises(StudyPathError):
            read_study_file("../outside.txt")

    def test_refuses_an_absolute_path_outside_the_folder(self, study_dir, tmp_path):
        with pytest.raises(StudyPathError):
            read_study_file(str(tmp_path / "outside.txt"))

    def test_refuses_a_directory(self, study_dir):
        with pytest.raises(StudyPathError):
            read_study_file("Textbooks")

    def test_reads_a_real_pdf(self, study_dir):
        """Real gap found live-testing this session: this folder's own
        real content is mostly PDFs (textbooks, past assessments), and a
        plain UTF-8 read always refused them as binary — the feature's
        most obvious real use never worked."""
        from dourmouse.tests.test_extract import make_pdf

        pdf_bytes = make_pdf(["Chapter 1: Real study content."])
        (study_dir / "textbook.pdf").write_bytes(pdf_bytes)
        result = read_study_file("textbook.pdf")
        assert "Chapter 1" in result["content"]
        assert "real study content" in result["content"].lower()

    def test_pdf_without_pypdf_is_an_honest_studypatherror(self, study_dir, monkeypatch):
        (study_dir / "textbook.pdf").write_bytes(b"%PDF-1.4\nnot a real pdf reader target")

        def fake_extract_pdf_text(path):
            raise RuntimeError("pypdf is not installed")

        import dourmouse.extract as extract_mod

        monkeypatch.setattr(extract_mod, "extract_pdf_text", fake_extract_pdf_text)
        with pytest.raises(StudyPathError, match="pypdf"):
            read_study_file("textbook.pdf")

    def test_truncates_long_content_and_says_so(self, study_dir):
        big = study_dir / "big.txt"
        big.write_text("x" * 50, encoding="utf-8")
        result = read_study_file("big.txt", max_chars=10)
        assert len(result["content"]) == 10
        assert result["truncated"] is True
