"""Tests for dourmouse/skills.py (Domain H, piece 3: Skills-as-modular-
capability-packages). Hermetic: every loader test uses its own tmp_path
skills directory, never the real dourmouse/skills/.
"""

from __future__ import annotations

from dourmouse.skills import (
    Skill,
    load_skills,
    relevant_skills,
    skill_context_block,
)


def _write_skill(root, dirname: str, *, name: str, description: str = "", keywords: str = "", body: str = "") -> None:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nkeywords: {keywords}\n---\n{body}",
        encoding="utf-8",
    )


class TestLoadSkills:
    def test_missing_directory_returns_empty_not_an_error(self, tmp_path):
        assert load_skills(tmp_path / "does_not_exist") == []

    def test_loads_a_well_formed_skill(self, tmp_path):
        _write_skill(
            tmp_path, "pdf-forms", name="pdf-forms",
            description="Fill PDF forms", keywords="pdf, form, fillable",
            body="# PDF Forms\nHow to fill a PDF form.",
        )
        skills = load_skills(tmp_path)
        assert len(skills) == 1
        s = skills[0]
        assert s.name == "pdf-forms"
        assert s.description == "Fill PDF forms"
        assert s.keywords == ("pdf", "form", "fillable")
        assert "How to fill a PDF form." in s.body

    def test_skips_a_directory_with_no_skill_md(self, tmp_path):
        (tmp_path / "not-a-skill").mkdir()
        (tmp_path / "not-a-skill" / "readme.txt").write_text("nothing here")
        assert load_skills(tmp_path) == []

    def test_skips_a_skill_md_with_no_frontmatter(self, tmp_path):
        d = tmp_path / "broken"
        d.mkdir()
        (d / "SKILL.md").write_text("just some markdown, no frontmatter at all")
        assert load_skills(tmp_path) == []

    def test_skips_a_skill_md_with_no_name_field(self, tmp_path):
        d = tmp_path / "broken"
        d.mkdir()
        (d / "SKILL.md").write_text("---\ndescription: x\n---\nbody")
        assert load_skills(tmp_path) == []

    def test_multiple_skills_sorted_by_name(self, tmp_path):
        _write_skill(tmp_path, "zzz", name="zzz-skill", keywords="z")
        _write_skill(tmp_path, "aaa", name="aaa-skill", keywords="a")
        skills = load_skills(tmp_path)
        assert [s.name for s in skills] == ["aaa-skill", "zzz-skill"]

    def test_keywords_are_lowercased_and_stripped(self, tmp_path):
        _write_skill(tmp_path, "s", name="s", keywords=" PDF , Form ,fillable ")
        skills = load_skills(tmp_path)
        assert skills[0].keywords == ("pdf", "form", "fillable")


class TestRelevantSkills:
    def _skills(self):
        return [
            Skill(name="pdf-forms", description="", keywords=("pdf", "form"), body="pdf body", path=None),
            Skill(name="excel-export", description="", keywords=("excel", "spreadsheet"), body="excel body", path=None),
        ]

    def test_no_match_returns_empty(self):
        assert relevant_skills("what's the weather today", self._skills()) == []

    def test_keyword_in_prompt_matches(self):
        result = relevant_skills("please fill out this pdf form for me", self._skills())
        assert [s.name for s in result] == ["pdf-forms"]

    def test_partial_word_does_not_falsely_match(self):
        # "pdfs" contains "pdf" as a substring but is a different token --
        # whole-token matching only, no substring false positives.
        result = relevant_skills("I love pdfs", self._skills())
        assert result == []

    def test_more_keyword_overlap_ranks_first(self):
        skills = [
            Skill(name="one-match", description="", keywords=("pdf",), body="", path=None),
            Skill(name="two-match", description="", keywords=("pdf", "form"), body="", path=None),
        ]
        result = relevant_skills("fill this pdf form", skills)
        assert [s.name for s in result] == ["two-match", "one-match"]

    def test_default_loads_from_real_skills_dir_without_crashing(self):
        # Whatever is actually shipped today (possibly nothing) -- just
        # confirm the default path doesn't raise.
        relevant_skills("anything")


class TestSkillContextBlock:
    def _skills(self):
        return [Skill(name="pdf-forms", description="", keywords=("pdf",), body="Fill PDF forms like this.", path=None)]

    def test_empty_when_nothing_relevant(self):
        assert skill_context_block("what's the weather", self._skills()) == ""

    def test_includes_the_real_skill_body_when_relevant(self):
        block = skill_context_block("help me with a pdf", self._skills())
        assert "[SKILL: pdf-forms]" in block
        assert "Fill PDF forms like this." in block

    def test_multiple_relevant_skills_all_included(self):
        skills = [
            Skill(name="a", description="", keywords=("alpha",), body="A body", path=None),
            Skill(name="b", description="", keywords=("beta",), body="B body", path=None),
        ]
        block = skill_context_block("alpha and beta together", skills)
        assert "[SKILL: a]" in block and "A body" in block
        assert "[SKILL: b]" in block and "B body" in block
