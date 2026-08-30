"""Diff-parsing self-correction loop (Aider-AI/aider architecture port,
part 3/4).

Aider's architect/editor split lets a model express a change as a unified
diff or a SEARCH/REPLACE block, applies it, and feeds a real parse/apply
error straight back into the model's context on failure so it can retry —
without a human in the loop for what is really just a syntax typo.

This ports the same two input formats and the same self-correction
property, adapted to how Dourmouse's dispatch loop already works: the
tool handlers here do the parsing + atomic apply + rollback + real syntax
check themselves and return an honest, actionable result string. Because
the dispatch loop already re-prompts the model with each tool_result
(dourmouse/dispatch.py's normal multi-turn tool-calling), a precise
failure message IS the self-correction feedback loop — no separate retry
machinery needed, the SAME mechanism that already lets a model react to
"CONFIRMATION REQUIRED" or "ERROR: old_str not found" reacts to this too.

Every apply is atomic: on any failure (a hunk that won't match, a result
that fails to parse after applying), the file is restored to its exact
original bytes — never left half-patched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ApplyResult:
    ok: bool
    message: str
    diff: str = ""


# --------------------------------------------------------------------------- #
# SEARCH/REPLACE blocks (Aider's own primary format)
# --------------------------------------------------------------------------- #

_SR_BLOCK_RE = re.compile(
    r"<{5,9}\s*SEARCH\s*\n(?P<search>.*?)\n?={5,9}\s*\n(?P<replace>.*?)\n?>{5,9}\s*REPLACE",
    re.DOTALL,
)


@dataclass
class SearchReplaceBlock:
    search: str
    replace: str


def parse_search_replace_blocks(text: str) -> list[SearchReplaceBlock]:
    """Every ``<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE`` block, in
    the order they appear. An empty ``search`` means "append" (Aider's own
    convention for a brand-new file / appending to the end) — callers
    decide what that means for their target.
    """
    return [
        SearchReplaceBlock(m.group("search"), m.group("replace"))
        for m in _SR_BLOCK_RE.finditer(text)
    ]


def apply_search_replace(path: Path, blocks_text: str) -> ApplyResult:
    """Apply every SEARCH/REPLACE block in ``blocks_text`` to ``path``,
    atomically — all blocks must match cleanly or NONE are applied.

    Same uniqueness discipline as general_roster.py's edit_file: a
    ``search`` that matches zero or more-than-one times is refused rather
    than guessing, so an ambiguous patch never silently edits the wrong
    occurrence.
    """
    blocks = parse_search_replace_blocks(blocks_text)
    if not blocks:
        return ApplyResult(
            False,
            "PATCH REFUSED: no SEARCH/REPLACE blocks found — expected "
            "'<<<<<<< SEARCH\\n...\\n=======\\n...\\n>>>>>>> REPLACE'. "
            "Nothing was changed.",
        )
    if not path.is_file():
        return ApplyResult(
            False, f"PATCH REFUSED: no such file: {path}. Nothing was changed."
        )
    original = path.read_text(encoding="utf-8", errors="replace")
    working = original
    for i, block in enumerate(blocks, 1):
        if not block.search.strip():
            working = working + ("" if working.endswith("\n") else "\n") + block.replace
            continue
        count = working.count(block.search)
        if count == 0:
            return ApplyResult(
                False,
                f"PATCH FAILED at block {i}/{len(blocks)}: SEARCH text not found "
                f"in {path.name}. Nothing was changed (all-or-nothing apply). "
                "The SEARCH block must match the file's CURRENT content "
                "exactly, including whitespace — re-read the file and retry "
                "with an exact excerpt.",
            )
        if count > 1:
            return ApplyResult(
                False,
                f"PATCH FAILED at block {i}/{len(blocks)}: SEARCH text matches "
                f"{count} places in {path.name} — refusing an ambiguous edit. "
                "Nothing was changed. Include more surrounding context in "
                "SEARCH so it matches exactly once.",
            )
        working = working.replace(block.search, block.replace, 1)
    diff = _unified_diff_text(original, working, path.name)
    check = _syntax_error_note(path, working)
    if check:
        return ApplyResult(
            False,
            f"PATCH REFUSED: applying it would leave {path.name} with a "
            f"syntax error — {check}. Nothing was changed (the file was "
            "never written). Fix the SEARCH/REPLACE content and retry.",
        )
    path.write_text(working, encoding="utf-8")
    return ApplyResult(
        True,
        f"PATCHED {path.name} ({len(blocks)} block(s) applied).",
        diff,
    )


# --------------------------------------------------------------------------- #
# Unified diff (the other format models commonly produce)
# --------------------------------------------------------------------------- #

@dataclass
class _Hunk:
    old_start: int
    old_lines: list[str]
    new_lines: list[str]


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def _parse_unified_diff(diff_text: str) -> list[_Hunk]:
    hunks: list[_Hunk] = []
    old_lines: list[str] = []
    new_lines: list[str] = []
    old_start = 0
    in_hunk = False
    for line in diff_text.splitlines():
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if in_hunk:
                hunks.append(_Hunk(old_start, old_lines, new_lines))
            old_start = int(m.group(1))
            old_lines, new_lines = [], []
            in_hunk = True
            continue
        if not in_hunk:
            continue  # file headers (---/+++) and anything before the first hunk
        if line.startswith("-"):
            old_lines.append(line[1:])
        elif line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith(" ") or line == "":
            old_lines.append(line[1:] if line else "")
            new_lines.append(line[1:] if line else "")
        # a bare "\ No newline at end of file" marker etc. is ignored
    if in_hunk:
        hunks.append(_Hunk(old_start, old_lines, new_lines))
    return hunks


def _find_hunk_position(file_lines: list[str], hunk: _Hunk) -> int | None:
    """Where ``hunk.old_lines`` actually sits in ``file_lines``.

    Tries the diff's OWN stated line number first (fast path, correct
    whenever the file hasn't drifted); falls back to scanning the whole
    file for an exact match — the same tolerance real `patch` and Aider's
    own applier have for a model that got the line numbers slightly wrong
    but the CONTEXT right.
    """
    n = len(hunk.old_lines)
    if n == 0:
        return hunk.old_start - 1 if hunk.old_start >= 1 else 0
    claimed = hunk.old_start - 1
    if 0 <= claimed <= len(file_lines) - n and file_lines[claimed:claimed + n] == hunk.old_lines:
        return claimed
    for i in range(0, len(file_lines) - n + 1):
        if file_lines[i:i + n] == hunk.old_lines:
            return i
    return None


def apply_unified_diff(path: Path, diff_text: str) -> ApplyResult:
    """Apply a unified diff to ``path``, atomically. Every hunk must find
    an exact context match somewhere in the file or NONE are applied.
    """
    hunks = _parse_unified_diff(diff_text)
    if not hunks:
        return ApplyResult(
            False,
            "PATCH REFUSED: no valid unified-diff hunks found (expected "
            "'@@ -a,b +c,d @@' headers). Nothing was changed.",
        )
    if not path.is_file():
        return ApplyResult(
            False, f"PATCH REFUSED: no such file: {path}. Nothing was changed."
        )
    original = path.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines()
    had_trailing_newline = original.endswith("\n")
    # Apply hunks back-to-front by position so earlier positions in the
    # file stay valid as later ones shift line counts.
    positioned: list[tuple[int, _Hunk]] = []
    for i, hunk in enumerate(hunks, 1):
        pos = _find_hunk_position(lines, hunk)
        if pos is None:
            return ApplyResult(
                False,
                f"PATCH FAILED at hunk {i}/{len(hunks)}: its context does not "
                f"match {path.name}'s current content anywhere in the file. "
                "Nothing was changed (all-or-nothing apply). Re-read the "
                "file and regenerate the diff against its actual content.",
            )
        positioned.append((pos, hunk))
    for pos, hunk in sorted(positioned, key=lambda ph: ph[0], reverse=True):
        lines[pos:pos + len(hunk.old_lines)] = hunk.new_lines
    new_text = "\n".join(lines) + ("\n" if had_trailing_newline or lines else "")
    check = _syntax_error_note(path, new_text)
    if check:
        return ApplyResult(
            False,
            f"PATCH REFUSED: applying it would leave {path.name} with a "
            f"syntax error — {check}. Nothing was changed. Fix the diff "
            "and retry.",
        )
    diff = _unified_diff_text(original, new_text, path.name)
    path.write_text(new_text, encoding="utf-8")
    return ApplyResult(True, f"PATCHED {path.name} ({len(hunks)} hunk(s) applied).", diff)


# --------------------------------------------------------------------------- #
# Shared: diff rendering + real syntax verification (self-correction input)
# --------------------------------------------------------------------------- #

def _unified_diff_text(before: str, after: str, name: str) -> str:
    import difflib

    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile=name, tofile=name, lineterm=""
        )
    )


def _syntax_error_note(path: Path, new_text: str) -> str | None:
    """None if ``new_text`` parses cleanly as ``path``'s language (or the
    language is unsupported/unknown — no check is not a failure); a real,
    actionable message otherwise. Python gets an exact line/offset via
    ``ast.parse``; every tree-sitter-supported language gets a coarser
    but still real has-error check.
    """
    if path.suffix.lower() == ".py":
        import ast

        try:
            ast.parse(new_text, filename=str(path))
        except SyntaxError as exc:
            return f"line {exc.lineno}, col {exc.offset}: {exc.msg}"
        return None
    try:
        from dourmouse.repo_map import language_for, _parser_for  # deferred: optional dep
    except Exception:  # noqa: BLE001 - tree-sitter not installed: skip the check, don't fail
        return None
    lang = language_for(path)
    if lang is None:
        return None
    try:
        parser = _parser_for(lang)
        tree = parser.parse(new_text.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - a parser hiccup must not block a real patch
        return None
    if tree.root_node.has_error:
        return "the resulting file does not parse (tree-sitter reported a syntax error)"
    return None
