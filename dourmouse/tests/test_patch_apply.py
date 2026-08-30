"""patch_apply.py — Aider port part 3/4: diff-parsing + self-correction.

Real files, real parsing, real syntax checks — no mocking the thing under
test.
"""

from __future__ import annotations

from dourmouse import patch_apply


class TestSearchReplaceParsing:
    def test_parses_one_block(self):
        text = (
            "<<<<<<< SEARCH\n"
            "old line\n"
            "=======\n"
            "new line\n"
            ">>>>>>> REPLACE\n"
        )
        blocks = patch_apply.parse_search_replace_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].search == "old line"
        assert blocks[0].replace == "new line"

    def test_parses_multiple_blocks(self):
        text = (
            "<<<<<<< SEARCH\na\n=======\nA\n>>>>>>> REPLACE\n"
            "some prose in between\n"
            "<<<<<<< SEARCH\nb\n=======\nB\n>>>>>>> REPLACE\n"
        )
        blocks = patch_apply.parse_search_replace_blocks(text)
        assert [b.search for b in blocks] == ["a", "b"]
        assert [b.replace for b in blocks] == ["A", "B"]

    def test_no_blocks_in_plain_text(self):
        assert patch_apply.parse_search_replace_blocks("just some text") == []


class TestApplySearchReplace:
    def test_applies_a_single_block(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("def add(a, b):\n    return a - b\n")
        patch = (
            "<<<<<<< SEARCH\n"
            "    return a - b\n"
            "=======\n"
            "    return a + b\n"
            ">>>>>>> REPLACE\n"
        )
        result = patch_apply.apply_search_replace(f, patch)
        assert result.ok
        assert f.read_text() == "def add(a, b):\n    return a + b\n"

    def test_refuses_when_search_not_found(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("def add(a, b):\n    return a + b\n")
        patch = "<<<<<<< SEARCH\nnonexistent\n=======\nx\n>>>>>>> REPLACE\n"
        result = patch_apply.apply_search_replace(f, patch)
        assert not result.ok
        assert "not found" in result.message
        assert f.read_text() == "def add(a, b):\n    return a + b\n"  # untouched

    def test_refuses_ambiguous_multi_match(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("x = 1\nx = 1\n")
        patch = "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
        result = patch_apply.apply_search_replace(f, patch)
        assert not result.ok
        assert "ambiguous" in result.message.lower()
        assert f.read_text() == "x = 1\nx = 1\n"  # untouched

    def test_all_or_nothing_across_multiple_blocks(self, tmp_path):
        """Block 1 would apply cleanly; block 2 can't be found. Neither
        must land — this is the atomicity guarantee."""
        f = tmp_path / "m.py"
        f.write_text("a = 1\nb = 2\n")
        patch = (
            "<<<<<<< SEARCH\na = 1\n=======\na = 100\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nnope\n=======\nx\n>>>>>>> REPLACE\n"
        )
        result = patch_apply.apply_search_replace(f, patch)
        assert not result.ok
        assert f.read_text() == "a = 1\nb = 2\n"

    def test_self_correction_refuses_a_syntax_breaking_edit(self, tmp_path):
        """The real self-correction property: a patch that would leave the
        file syntactically broken is refused BEFORE it's written, with a
        precise, actionable line/col — not applied-then-discovered-broken."""
        f = tmp_path / "m.py"
        f.write_text("def add(a, b):\n    return a + b\n")
        patch = (
            "<<<<<<< SEARCH\n"
            "def add(a, b):\n"
            "=======\n"
            "def add(a, b:\n"  # missing closing paren -> syntax error
            ">>>>>>> REPLACE\n"
        )
        result = patch_apply.apply_search_replace(f, patch)
        assert not result.ok
        assert "syntax error" in result.message
        assert "line" in result.message
        assert f.read_text() == "def add(a, b):\n    return a + b\n"  # never written

    def test_no_blocks_found_is_refused_not_silent(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("x = 1\n")
        result = patch_apply.apply_search_replace(f, "no blocks here")
        assert not result.ok
        assert "no SEARCH/REPLACE blocks" in result.message

    def test_missing_file_is_refused(self, tmp_path):
        patch = "<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n"
        result = patch_apply.apply_search_replace(tmp_path / "nope.py", patch)
        assert not result.ok
        assert "no such file" in result.message.lower()

    def test_successful_apply_includes_a_real_diff(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("x = 1\n")
        patch = "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
        result = patch_apply.apply_search_replace(f, patch)
        assert result.ok
        assert "-x = 1" in result.diff
        assert "+x = 2" in result.diff


class TestApplyUnifiedDiff:
    def _diff(self, old: str, new: str, name="m.py") -> str:
        import difflib

        return "\n".join(
            difflib.unified_diff(
                old.splitlines(), new.splitlines(), fromfile=name, tofile=name, lineterm=""
            )
        )

    def test_applies_a_clean_diff(self, tmp_path):
        f = tmp_path / "m.py"
        old = "def add(a, b):\n    return a - b\n"
        new = "def add(a, b):\n    return a + b\n"
        f.write_text(old)
        result = patch_apply.apply_unified_diff(f, self._diff(old, new))
        assert result.ok
        assert f.read_text() == new

    def test_refuses_when_context_does_not_match(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("totally different content\n")
        diff = self._diff("def add(a, b):\n    return a - b\n", "def add(a, b):\n    return a + b\n")
        result = patch_apply.apply_unified_diff(f, diff)
        assert not result.ok
        assert "does not match" in result.message
        assert f.read_text() == "totally different content\n"

    def test_tolerates_a_wrong_line_number_with_right_context(self, tmp_path):
        """Same tolerance real `patch`/Aider have: the diff's stated line
        number can be stale as long as the CONTEXT still matches
        somewhere in the file."""
        f = tmp_path / "m.py"
        content = "\n".join([f"line{i}" for i in range(20)]) + "\n"
        f.write_text(content)
        # Hand-craft a hunk claiming the wrong start line but correct context.
        diff = (
            "--- m.py\n+++ m.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-line10\n"
            "+CHANGED\n"
        )
        result = patch_apply.apply_unified_diff(f, diff)
        assert result.ok
        assert "CHANGED" in f.read_text()
        assert "line10\n" not in f.read_text()

    def test_no_hunks_found_is_refused(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("x = 1\n")
        result = patch_apply.apply_unified_diff(f, "not a diff at all")
        assert not result.ok
        assert "no valid unified-diff hunks" in result.message

    def test_multi_hunk_diff_applies_all(self, tmp_path):
        f = tmp_path / "m.py"
        old = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"
        new = "a = 100\nb = 2\nc = 3\nd = 400\ne = 5\n"
        f.write_text(old)
        result = patch_apply.apply_unified_diff(f, self._diff(old, new))
        assert result.ok
        assert f.read_text() == new

    def test_self_correction_refuses_syntax_breaking_diff(self, tmp_path):
        f = tmp_path / "m.py"
        old = "def add(a, b):\n    return a + b\n"
        new = "def add(a, b\n    return a + b\n"  # missing ):
        f.write_text(old)
        result = patch_apply.apply_unified_diff(f, self._diff(old, new))
        assert not result.ok
        assert "syntax error" in result.message
        assert f.read_text() == old


class TestSyntaxCheckAcrossLanguages:
    def test_python_reports_exact_location(self, tmp_path):
        note = patch_apply._syntax_error_note(tmp_path / "m.py", "def f(:\n    pass\n")
        assert note is not None
        assert "line" in note

    def test_python_clean_code_has_no_note(self, tmp_path):
        assert patch_apply._syntax_error_note(tmp_path / "m.py", "def f():\n    pass\n") is None

    def test_unsupported_extension_skips_the_check(self, tmp_path):
        assert patch_apply._syntax_error_note(tmp_path / "m.md", "not # valid ((( python") is None

    def test_javascript_broken_syntax_is_caught(self, tmp_path):
        import pytest

        pytest.importorskip("tree_sitter_language_pack")
        note = patch_apply._syntax_error_note(tmp_path / "m.js", "function f( {\n  return\n}\n")
        assert note is not None
