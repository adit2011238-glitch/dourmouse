"""Tree-sitter repo mapping (Aider-AI/aider architecture port, part 2/4).

Aider's repomap.py builds a compressed structural map of a codebase
(classes, functions, signatures — no bodies) so a model gets real
awareness of a large project without the full source ever entering its
context window. This is the same idea, scoped to what Dourmouse actually
needs: real parsing via tree-sitter (never regex heuristics pretending to
be a parser), one map per requested root, capped and honest about what it
dropped.

Deterministic (Rule 2.8): the same tree, walked the same way, every time.
No LLM judgment anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Directories never worth mapping — build output, dependencies, VCS
#: internals. Matched by exact path-component name at any depth.
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".next", ".turbo", "target", ".idea", ".vscode", "egg-info",
}

#: file suffix -> tree-sitter grammar name (tree_sitter_language_pack's
#: naming). Deliberately starts narrow (this codebase's own real
#: languages) rather than claiming support for every language Aider
#: covers — each addition here needs its _LANG_DEFS entry to actually be
#: correct, not just "the parser loaded without crashing".
_LANG_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
}

#: Per-language: which node types are "definitions" worth a map line, and
#: which child node type marks where the body starts (the header text is
#: everything from the definition node's start up to that child's start —
#: this is what keeps a multi-line signature intact while dropping the
#: body). container_types are definitions that may themselves nest more
#: definitions (classes) — walked into; the rest are leaves.
@dataclass(frozen=True)
class _LangDef:
    def_types: frozenset[str]
    container_types: frozenset[str]
    body_child_type: str


_LANG_DEFS: dict[str, _LangDef] = {
    "python": _LangDef(
        def_types=frozenset({"function_definition", "class_definition"}),
        container_types=frozenset({"class_definition"}),
        body_child_type="block",
    ),
    "javascript": _LangDef(
        def_types=frozenset({"function_declaration", "class_declaration", "method_definition"}),
        container_types=frozenset({"class_declaration"}),
        body_child_type="statement_block",
    ),
    "typescript": _LangDef(
        def_types=frozenset({"function_declaration", "class_declaration", "method_definition", "interface_declaration"}),
        container_types=frozenset({"class_declaration", "interface_declaration"}),
        body_child_type="statement_block",
    ),
    "tsx": _LangDef(
        def_types=frozenset({"function_declaration", "class_declaration", "method_definition", "interface_declaration"}),
        container_types=frozenset({"class_declaration", "interface_declaration"}),
        body_child_type="statement_block",
    ),
    "go": _LangDef(
        def_types=frozenset({"function_declaration", "method_declaration", "type_declaration"}),
        container_types=frozenset(),
        body_child_type="block",
    ),
    "rust": _LangDef(
        def_types=frozenset({"function_item", "struct_item", "impl_item", "trait_item"}),
        container_types=frozenset({"impl_item", "trait_item"}),
        body_child_type="declaration_list",
    ),
    "java": _LangDef(
        def_types=frozenset({"method_declaration", "class_declaration", "interface_declaration"}),
        container_types=frozenset({"class_declaration", "interface_declaration"}),
        body_child_type="class_body",
    ),
    "ruby": _LangDef(
        def_types=frozenset({"method", "class", "module"}),
        container_types=frozenset({"class", "module"}),
        body_child_type="body_statement",
    ),
}


def supported_languages() -> tuple[str, ...]:
    return tuple(sorted(_LANG_DEFS))


def language_for(path: Path) -> str | None:
    return _LANG_BY_SUFFIX.get(path.suffix.lower())


def _parser_for(lang: str) -> Any:
    from tree_sitter_language_pack import get_parser  # deferred: optional dep

    return get_parser(lang)


def _header_text(node: Any, source: bytes, lang_def: _LangDef) -> str:
    """Everything from ``node``'s start up to its body child's start,
    decoded and whitespace-trimmed — the signature, never the body."""
    body_start = node.end_byte
    for child in node.children:
        if child.type == lang_def.body_child_type:
            body_start = child.start_byte
            break
    raw = source[node.start_byte:body_start]
    return raw.decode("utf-8", errors="replace").strip().rstrip(":").strip()


def _walk(node: Any, source: bytes, lang_def: _LangDef, depth: int, out: list[tuple[int, str]]) -> None:
    for child in node.children:
        if child.type in lang_def.def_types:
            out.append((depth, _header_text(child, source, lang_def)))
            if child.type in lang_def.container_types:
                _walk(child, source, lang_def, depth + 1, out)
            # A definition that is not a container (a plain function) may
            # still have nested children worth descending into structurally
            # (decorators aside) but never DEFINITIONS of interest beyond
            # its own signature — skip descending to avoid double-counting
            # a class's methods as if they were top-level.
            continue
        _walk(child, source, lang_def, depth, out)


def map_file(path: Path, *, max_chars: int = 4000) -> str | None:
    """One file's structural map, or None if unsupported/unparseable.

    Returns "" (empty, not None) for a supported file with zero
    definitions — honest signal that the file was looked at and is just
    data/config/empty, not skipped.
    """
    lang = language_for(path)
    if lang is None:
        return None
    lang_def = _LANG_DEFS[lang]
    try:
        source = path.read_bytes()
    except OSError:
        return None
    try:
        parser = _parser_for(lang)
        tree = parser.parse(source)
    except Exception:  # noqa: BLE001 - a grammar mismatch must not crash the whole map
        return None
    lines: list[tuple[int, str]] = []
    _walk(tree.root_node, source, lang_def, 0, lines)
    rendered = "\n".join("  " * d + h for d, h in lines)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars].rstrip() + f"\n  … truncated ({len(lines)} definitions total)"
    return rendered


def iter_source_files(root: Path, *, max_files: int = 400):
    """Every file under ``root`` with a supported suffix, skipping the
    directories in _SKIP_DIRS at any depth. Deterministic order (sorted),
    capped at ``max_files`` — the caller is told honestly when the cap bit
    (see generate_repo_map)."""
    found: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in p.parts):
            continue
        # v13.1 (live-caught, real bug): on a non-APFS volume (this repo's
        # own — an external exFAT/SMB-style drive), macOS scatters
        # AppleDouble shadow files ("._foo.py") next to every real one —
        # binary resource-fork data that still matches the ".py" suffix
        # filter. Unfiltered, these flooded a real map with dozens of
        # "(no definitions found)" entries and pushed real files out past
        # the char budget. Any dotfile is skipped, not just "._" — a
        # hidden file is never a source file worth mapping.
        if p.name.startswith("."):
            continue
        if language_for(p) is None:
            continue
        found.append(p)
        if len(found) >= max_files:
            break
    return found


def generate_repo_map(
    root: Path,
    *,
    max_files: int = 400,
    max_total_chars: int = 12_000,
    max_chars_per_file: int = 1500,
) -> str:
    """A compressed, structural map of every supported source file under
    ``root``: relative path, then its classes/functions/methods with real
    signatures, no bodies. Honest about both caps it applies (file count,
    total size) — never a silent truncation (house convention: no silent
    caps in a workflow/tool result).
    """
    root = root.resolve()
    if not root.is_dir():
        return f"REPO MAP: {root} is not a directory."
    files = iter_source_files(root, max_files=max_files)
    if not files:
        return f"REPO MAP: no supported source files under {root} ({', '.join(supported_languages())})."
    sections: list[str] = []
    total = 0
    dropped = 0
    for f in files:
        body = map_file(f, max_chars=max_chars_per_file)
        if body is None:
            continue
        rel = f.relative_to(root)
        section = f"{rel}:\n{body}" if body else f"{rel}: (no definitions found)"
        if total + len(section) > max_total_chars:
            dropped += 1
            continue
        sections.append(section)
        total += len(section)
    header = f"REPO MAP: {root} — {len(sections)} file(s), {total} chars"
    if len(files) >= max_files:
        header += f" (file scan capped at {max_files}; more files may exist)"
    if dropped:
        header += f" ({dropped} file(s) dropped to stay under {max_total_chars} chars)"
    return header + "\n\n" + "\n\n".join(sections)
