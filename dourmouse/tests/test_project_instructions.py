"""dourmouse/project_instructions.py -- Dourmouse's own CLAUDE.md-equivalent
(Domain H). Real file, real workspace, no mocked filesystem."""

from __future__ import annotations

from dourmouse.project_instructions import (
    PROJECT_INSTRUCTIONS_FILENAME,
    load_project_instructions,
)


def test_no_file_is_honest_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    assert load_project_instructions() == ""


def test_real_file_is_read_and_stripped(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    (tmp_path / PROJECT_INSTRUCTIONS_FILENAME).write_text(
        "\n  Always answer in haiku.  \n"
    )
    assert load_project_instructions() == "Always answer in haiku."


def test_whitespace_only_file_is_honest_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    (tmp_path / PROJECT_INSTRUCTIONS_FILENAME).write_text("   \n\n   ")
    assert load_project_instructions() == ""


def test_oversized_file_is_capped_with_a_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    (tmp_path / PROJECT_INSTRUCTIONS_FILENAME).write_text("x" * 20_000)
    text = load_project_instructions()
    assert len(text) < 20_000
    assert text.endswith("[project instructions truncated]")


def test_unreadable_path_is_honest_empty_not_a_crash(monkeypatch, tmp_path):
    # A directory in place of the file: read_text raises OSError (IsADirectoryError).
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    (tmp_path / PROJECT_INSTRUCTIONS_FILENAME).mkdir()
    assert load_project_instructions() == ""
