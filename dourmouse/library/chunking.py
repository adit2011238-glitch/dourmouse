"""Passage chunking for the library: about 400 words per passage with a small overlap, keeping page numbers and
headings (plan 2.1, finding 206). Pure text in, a list of ``Chunk`` out; nothing here touches a database."""

from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_WORDS = 400
OVERLAP_WORDS = 40
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_PAGE_MARK = re.compile(r"^\s*\[\[page\s+(\d+)\]\]\s*$", re.IGNORECASE)
_FORM_FEED = "\f"


@dataclass(frozen=True)
class Chunk:
    text: str
    page: int | None = None
    heading: str = ""


def page_marker(number: int) -> str:
    """The line a connector writes between pages so the chunker can keep page numbers."""
    return f"[[page {int(number)}]]"


def _units(text: str):
    """Yield (page, heading, paragraph) in reading order. A page changes at a ``[[page N]]`` line or a form feed."""
    page: int | None = None
    heading = ""
    auto_page = 1
    saw_form_feed = _FORM_FEED in text
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        if saw_form_feed and _FORM_FEED in block:
            parts = block.split(_FORM_FEED)
            for i, part in enumerate(parts):
                if i:
                    auto_page += 1
                    page = auto_page
                if part.strip():
                    yield from _lines_of(part, page, heading)
                    heading = _last_heading(part, heading)
            continue
        yield from _lines_of(block, page, heading)
        new_heading = _last_heading(block, heading)
        heading = new_heading
        marker = None
        for line in block.splitlines():
            m = _PAGE_MARK.match(line)
            if m:
                marker = int(m.group(1))
        if marker is not None:
            page = marker


def _last_heading(block: str, current: str) -> str:
    for line in block.splitlines():
        m = _HEADING.match(line)
        if m:
            current = m.group(1).strip()
    return current


def _lines_of(block: str, page: int | None, heading: str):
    kept = []
    h = heading
    for line in block.splitlines():
        if _PAGE_MARK.match(line):
            continue
        m = _HEADING.match(line)
        if m:
            h = m.group(1).strip()
        kept.append(line)
    para = "\n".join(kept).strip()
    if para:
        yield page, h, para


def chunk_text(text: str, target_words: int = TARGET_WORDS, overlap_words: int = OVERLAP_WORDS) -> list[Chunk]:
    """Split ``text`` into passages of at most ``target_words`` words. Paragraphs stay whole where they fit; a longer
    paragraph is cut on word boundaries. The last ``overlap_words`` of a passage start the next one when a paragraph
    had to be cut, so a sentence on the seam is found from either side. Each chunk keeps the page it starts on and
    the nearest heading above it."""
    if target_words < 20:
        raise ValueError("target_words must be at least 20")
    overlap_words = max(0, min(overlap_words, target_words // 4))
    out: list[Chunk] = []
    cur_words: list[str] = []
    cur_page: int | None = None
    cur_head = ""

    def flush() -> None:
        nonlocal cur_words
        if cur_words:
            out.append(Chunk(" ".join(cur_words), cur_page, cur_head))
        cur_words = []

    for page, heading, para in _units(text or ""):
        words = para.split()
        if not words:
            continue
        if len(words) > target_words:
            flush()
            start = 0
            while start < len(words):
                piece = words[start:start + target_words]
                out.append(Chunk(" ".join(piece), page, heading))
                if start + target_words >= len(words):
                    break
                start += target_words - overlap_words
            continue
        if cur_words and len(cur_words) + len(words) > target_words:
            flush()
        if not cur_words:
            cur_page, cur_head = page, heading
        cur_words.extend(words)
    flush()
    return out
