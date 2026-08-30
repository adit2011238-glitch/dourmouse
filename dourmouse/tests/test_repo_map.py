"""repo_map.py — Aider port part 2/4: tree-sitter structural codebase map.

Real tree-sitter parsing against real temp source files — no mocking the
parser, since the whole point is proving the actual grammar walk works.
"""

from __future__ import annotations

import pytest

tree_sitter_language_pack = pytest.importorskip("tree_sitter_language_pack")

from dourmouse import repo_map


class TestLanguageDetection:
    def test_known_suffixes(self):
        from pathlib import Path

        assert repo_map.language_for(Path("x.py")) == "python"
        assert repo_map.language_for(Path("x.js")) == "javascript"
        assert repo_map.language_for(Path("x.ts")) == "typescript"

    def test_unknown_suffix_is_none(self):
        from pathlib import Path

        assert repo_map.language_for(Path("x.md")) is None
        assert repo_map.language_for(Path("x.json")) is None


class TestMapFilePython:
    def test_extracts_function_signature(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("def add(a: int, b: int = 1) -> int:\n    return a + b\n")
        out = repo_map.map_file(f)
        assert out == "def add(a: int, b: int = 1) -> int"

    def test_extracts_class_and_nested_methods_indented(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text(
            "class Foo:\n"
            "    def bar(self, x):\n"
            "        return x\n"
            "    def baz(self):\n"
            "        pass\n"
        )
        out = repo_map.map_file(f)
        lines = out.splitlines()
        assert lines[0] == "class Foo"
        assert lines[1] == "  def bar(self, x)"
        assert lines[2] == "  def baz(self)"

    def test_never_includes_the_body(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("def secret() -> None:\n    api_key = 'sk-should-not-leak'\n")
        out = repo_map.map_file(f)
        assert "sk-should-not-leak" not in out

    def test_multiline_signature_stays_one_logical_header(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text(
            "def wide(\n    a,\n    b,\n) -> None:\n    pass\n"
        )
        out = repo_map.map_file(f)
        assert out.startswith("def wide(")
        assert "pass" not in out

    def test_empty_file_returns_empty_string_not_none(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        assert repo_map.map_file(f) == ""

    def test_file_with_no_defs_returns_empty_string(self, tmp_path):
        f = tmp_path / "consts.py"
        f.write_text("X = 1\nY = 2\n")
        assert repo_map.map_file(f) == ""

    def test_unsupported_suffix_returns_none(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# hi\n")
        assert repo_map.map_file(f) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert repo_map.map_file(tmp_path / "nope.py") is None

    def test_caps_per_file_output_and_says_so(self, tmp_path):
        f = tmp_path / "big.py"
        body = "\n".join(f"def fn_{i}(): pass" for i in range(500))
        f.write_text(body)
        out = repo_map.map_file(f, max_chars=200)
        assert len(out) < 300  # header + truncation note, not the full 500
        assert "truncated" in out


class TestMapFileJavaScript:
    def test_extracts_function_and_class(self, tmp_path):
        f = tmp_path / "m.js"
        f.write_text(
            "function greet(name) {\n  return 'hi ' + name;\n}\n"
            "class Widget {\n  render() {\n    return null;\n  }\n}\n"
        )
        out = repo_map.map_file(f)
        assert "function greet(name)" in out
        assert "class Widget" in out
        assert "render()" in out


class TestGenerateRepoMap:
    def test_maps_a_small_tree(self, tmp_path):
        (tmp_path / "a.py").write_text("def one(): pass\n")
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "b.py").write_text("class Two:\n    def m(self): pass\n")
        out = repo_map.generate_repo_map(tmp_path)
        assert "a.py" in out
        assert "def one()" in out
        assert str(sub.name) in out or "b.py" in out
        assert "class Two" in out

    def test_skips_skip_dirs(self, tmp_path):
        (tmp_path / "real.py").write_text("def x(): pass\n")
        skip = tmp_path / "node_modules"
        skip.mkdir()
        (skip / "vendored.py").write_text("def hidden(): pass\n")
        out = repo_map.generate_repo_map(tmp_path)
        assert "real.py" in out
        assert "vendored.py" not in out
        assert "hidden" not in out

    def test_skips_hidden_and_appledouble_files(self, tmp_path):
        """v13.1 (live-caught, real bug): on a non-APFS volume macOS
        scatters AppleDouble shadow files ("._foo.py") that still match
        the source-suffix filter — unfiltered, they flooded a real map
        with junk 'no definitions found' entries and pushed real files
        out past the char budget."""
        (tmp_path / "real.py").write_text("def x(): pass\n")
        (tmp_path / "._real.py").write_bytes(b"\x00\x05Mac OS X\x00\x02")
        (tmp_path / ".hidden.py").write_text("def y(): pass\n")
        out = repo_map.generate_repo_map(tmp_path)
        assert "._real.py" not in out
        assert ".hidden.py" not in out

    def test_empty_directory_is_honest(self, tmp_path):
        out = repo_map.generate_repo_map(tmp_path)
        assert "no supported source files" in out

    def test_not_a_directory_is_honest(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def x(): pass\n")
        out = repo_map.generate_repo_map(f)
        assert "not a directory" in out

    def test_total_char_cap_is_disclosed_not_silent(self, tmp_path):
        for i in range(30):
            (tmp_path / f"f{i}.py").write_text(
                "\n".join(f"def fn_{j}_{i}(a, b, c, d): pass" for j in range(40))
            )
        out = repo_map.generate_repo_map(tmp_path, max_total_chars=500)
        assert "dropped" in out

    def test_file_count_cap_is_disclosed_not_silent(self, tmp_path):
        for i in range(10):
            (tmp_path / f"f{i}.py").write_text("def x(): pass\n")
        out = repo_map.generate_repo_map(tmp_path, max_files=3, max_total_chars=100_000)
        assert "capped at 3" in out

    def test_deterministic_across_runs(self, tmp_path):
        (tmp_path / "a.py").write_text("def a(): pass\n")
        (tmp_path / "b.py").write_text("def b(): pass\n")
        first = repo_map.generate_repo_map(tmp_path)
        second = repo_map.generate_repo_map(tmp_path)
        assert first == second
