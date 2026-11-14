"""Reading a paper found by paper search (PL-10 T1, step 2).

A paper is read from its open-access PDF when one can be fetched: arXiv's copy
first (the most reliable), then the links OpenAlex and Semantic Scholar give.
Every fetch goes through ``acquire.fetch_document``, so it is SSRF-guarded,
polite (robots.txt and per-host spacing), stored raw by its SHA-256, and a PDF
is turned into text by the existing ``extract.extract_pdf_text`` (the
``extract_pdf`` tool's own reader). When no PDF can be read (paywalled, robots
refusal, a landing page instead of a PDF, a scanned PDF with no text) the paper
is read from its abstract only, and ``PaperText.note`` says why: a card made
from an abstract must say so.

The text is split into its sections (Abstract, Introduction, Methods, Results,
Discussion, ...). The reference list is kept out of what the model reads and
out of what a quote may be checked against: a quote from another paper's title
is not this paper's finding.

Verbatim checking of a PDF quote: PDF text has ligatures, line-break
hyphenation, curly quotes and odd spacing that a faithful quotation does not
reproduce. ``quote_in_text`` applies ONE normalisation to both the quote and
the paper (Unicode NFKC, typographic quotes and dashes to ASCII, soft hyphens
removed, whitespace collapsed) and accepts a line-break hyphen either joined or
kept. It is still a character-for-character match; nothing fuzzy.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.error
from dataclasses import dataclass, field
from typing import Any

from .papers import Paper

#: Characters of a paper's body shown to a model (about 15k tokens).
PROMPT_CHARS = 60_000
#: PDF links tried per paper before falling back to the abstract.
MAX_PDF_ATTEMPTS = 3
#: Text kept per paper. Past this the reading is marked truncated, never whole.
MAX_TEXT_CHARS = 600_000

_PAGE_MARKER = re.compile(r"^--- page (\d+) ---$", re.M)
_HEADINGS = (
    "abstract", "introduction", "background", "related work", "literature review", "theoretical framework",
    "method", "methods", "methodology", "materials and methods", "methods and materials", "participants",
    "experimental setup", "experiment", "experiments", "study 1", "study 2", "data", "data and methods",
    "results", "results and discussion", "findings", "analysis", "discussion", "general discussion",
    "conclusion", "conclusions", "concluding remarks", "limitations", "future work", "summary",
    "references", "bibliography", "works cited", "literature cited", "acknowledgements", "acknowledgments",
    "acknowledgement", "acknowledgment", "appendix", "supplementary material",
)
_REFERENCE_SECTIONS = ("references", "bibliography", "works cited", "literature cited")
_HEADING_RE = re.compile(
    r"^\s*(?:(?:\d{1,2}(?:\.\d{1,2})*|[IVX]{1,5}|[A-H])[.)]?\s+)?"
    r"(" + "|".join(re.escape(h) for h in sorted(_HEADINGS, key=len, reverse=True)) + r")"
    r"(?:\s+[A-Z0-9])?\s*[.:]?\s*$",
    re.I,
)
_QUOTE_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "­": None, "​": None, "﻿": None,
})


@dataclass(frozen=True)
class Section:
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class PaperText:
    paper_key: str
    #: "pdf", "abstract" or "none" (no PDF and no abstract: nothing to read).
    read_from: str
    #: The PDF actually read, or the paper's own URL for an abstract.
    source_url: str
    final_url: str = ""
    raw_sha256: str = ""
    text: str = field(default="", repr=False)
    sections: tuple[Section, ...] = ()
    #: (page number, offset in ``text`` where that page starts).
    pages: tuple[tuple[int, int], ...] = ()
    #: Why the PDF was not read (each attempt), or that the text was cut.
    note: str = ""
    truncated: bool = False

    def section_text(self, name: str) -> str:
        return "\n\n".join(self.text[s.start:s.end].strip() for s in self.sections if s.name == name)

    def body(self) -> str:
        """Everything except the reference list."""
        if not self.sections:
            return self.text
        return "\n\n".join(
            f"[{s.name}]\n{self.text[s.start:s.end].strip()}"
            for s in self.sections if s.name.lower() not in _REFERENCE_SECTIONS
        )

    def checkable_text(self) -> str:
        """What a quote is verified against: the body without our section labels."""
        if not self.sections:
            return self.text
        return "\n\n".join(self.text[s.start:s.end] for s in self.sections
                           if s.name.lower() not in _REFERENCE_SECTIONS)

    def locate(self, quote: str) -> str:
        """"Results, PDF page 4" for a quote, or "" when it is not in the body."""
        target = normalize_for_match(quote)
        if not target:
            return ""
        where: list[str] = []
        for s in self.sections:
            if s.name.lower() in _REFERENCE_SECTIONS:
                continue
            if _contains(self.text[s.start:s.end], target):
                where.append(s.name)
                break
        if self.read_from == "pdf" and self.pages:
            bounds = [start for _, start in self.pages] + [len(self.text)]
            page = next((f"PDF page {num}" for i, (num, _) in enumerate(self.pages)
                         if _contains(self.text[bounds[i]:bounds[i + 1]], target)), "")
            if not page:  # a quote that runs over a page break
                page = next((f"PDF pages {num}-{self.pages[i + 1][0]}" for i, (num, _) in enumerate(self.pages[:-1])
                             if _contains(self.text[bounds[i]:bounds[i + 2]], target)), "")
            if page:
                where.append(page)
        elif self.read_from == "abstract":
            where = ["Abstract"]
        return ", ".join(where)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_key": self.paper_key, "read_from": self.read_from, "source_url": self.source_url,
            "final_url": self.final_url, "raw_sha256": self.raw_sha256, "note": self.note,
            "truncated": self.truncated, "chars": len(self.text),
            "sections": [s.name for s in self.sections],
        }


def normalize_for_match(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTE_MAP)
    return " ".join(t.split())


def _variants(text: str) -> tuple[str, str]:
    """The text with each line-break hyphen joined ("experi-\\nment" ->
    "experiment") and kept ("long-\\nterm" -> "long-term")."""
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTE_MAP)
    joined = re.sub(r"(\w)-[ \t]*\n\s*(\w)", r"\1\2", t)
    kept = re.sub(r"(\w)-[ \t]*\n\s*(\w)", r"\1-\2", t)
    return " ".join(joined.split()), " ".join(kept.split())


def _contains(text: str, normalized_quote: str) -> bool:
    joined, kept = _variants(text)
    return normalized_quote in joined or normalized_quote in kept


def quote_in_text(quote: str, text: str) -> bool:
    """True when ``quote`` appears in ``text`` character for character, after
    the one normalisation described in the module docstring."""
    q = normalize_for_match(quote)
    return bool(q) and _contains(text, q)


def split_pages(extracted: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    """``extract_pdf_text`` output without its "--- page N ---" markers, and
    where each page starts in the result."""
    out: list[str] = []
    pages: list[tuple[int, int]] = []
    pos = 0
    matches = list(_PAGE_MARKER.finditer(extracted))
    if not matches:
        return extracted.strip(), ()
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(extracted)
        chunk = extracted[start:end].strip("\n")
        pages.append((int(m.group(1)), pos))
        out.append(chunk)
        pos += len(chunk) + 2
    return "\n\n".join(out), tuple(pages)


def find_sections(text: str) -> tuple[Section, ...]:
    """Sections from heading lines. Text before the first heading is "Front
    matter". A "References" heading in the first fifth of the paper is a table
    of contents line, not the reference list, and is ignored."""
    heads: list[tuple[int, str]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        m = _HEADING_RE.match(stripped) if 0 < len(stripped) <= 60 else None
        if m:
            name = m.group(1).strip().title()
            early_refs = name.lower() in _REFERENCE_SECTIONS and pos < len(text) * 0.2
            if not early_refs and (not heads or heads[-1][1] != name):
                heads.append((pos, name))
        elif not heads and re.match(r"^abstract[\s.:\-]", stripped, re.I):
            heads.append((pos, "Abstract"))  # "Abstract. We show ..." runs on in its own line
        pos += len(line)
    if not heads:
        return (Section("Body", 0, len(text)),) if text.strip() else ()
    sections: list[Section] = []
    if heads[0][0] > 0 and text[:heads[0][0]].strip():
        sections.append(Section("Front matter", 0, heads[0][0]))
    for i, (start, name) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        sections.append(Section(name, start, end))
    return tuple(sections)


def _abstract_reading(paper: Paper, note: str) -> PaperText:
    if not paper.abstract.strip():
        return PaperText(paper.key, "none", paper.url, note=(note + "; " if note else "") + "no abstract either")
    text = paper.abstract.strip()
    return PaperText(paper.key, "abstract", paper.url, text=text, sections=(Section("Abstract", 0, len(text)),),
                     note=note or "no open-access PDF link")


def read_paper(paper: Paper, *, fetch: Any = None, max_attempts: int = MAX_PDF_ATTEMPTS) -> PaperText:
    """Read ``paper`` from its first readable open-access PDF, else its abstract."""
    from dourmouse import net_guard

    from .acquire import UnsupportedContent, fetch_document
    from .politeness import RobotsDisallowed

    fetch = fetch or (lambda url: fetch_document(url, render=False))
    tried: list[str] = []
    for url in list(paper.oa_pdf_urls)[:max_attempts]:
        try:
            doc = fetch(url)
        except RobotsDisallowed:
            tried.append(f"{url}: refused by the site's robots.txt")
            continue
        except net_guard.FetchRefused as exc:
            tried.append(f"{url}: refused ({exc})")
            continue
        except UnsupportedContent as exc:
            tried.append(f"{url}: {str(exc)[:160]}")
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            tried.append(f"{url}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        if doc.kind != "pdf":
            tried.append(f"{url}: answered with a {doc.kind} page, not a PDF")
            continue
        text, pages = split_pages(doc.text)
        if len(text.strip()) < 500:
            tried.append(f"{url}: the PDF has almost no text layer")
            continue
        truncated = bool(doc.truncated) or len(text) > MAX_TEXT_CHARS
        text = text[:MAX_TEXT_CHARS]
        note = "the PDF was longer than the reading limit; only the start was read" if truncated else ""
        return PaperText(paper.key, "pdf", url, final_url=doc.final_url, raw_sha256=doc.raw_sha256, text=text,
                         sections=find_sections(text), pages=pages, note=note, truncated=truncated)
    why = "; ".join(tried) if tried else "no open-access PDF link"
    return _abstract_reading(paper, why)


def store_reading(reading: PaperText, paper: Paper, library: Any, *, job_id: str = "") -> list[str]:
    """Each section of the reading into the library (references excluded), with
    its paper key, section and how it was read. Returns the passage ids."""
    ids: list[str] = []
    meta_base = {"paper_key": reading.paper_key, "title": paper.title, "read_from": reading.read_from,
                 "raw_sha256": reading.raw_sha256, "kind": "paper_text", "job_id": job_id}
    for s in reading.sections or (Section("Body", 0, len(reading.text)),):
        if s.name.lower() in _REFERENCE_SECTIONS:
            continue
        chunk = reading.text[s.start:s.end].strip()
        if chunk:
            ids.extend(library.add(paper.url or reading.paper_key, chunk, {**meta_base, "section": s.name}))
    return ids
