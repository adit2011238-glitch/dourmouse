"""Reference lists and in-text citations in APA 7 (the default, the owner's
choice on 2026-11-13) or MLA 9, built only from what the indexes returned.

Known limits, stated rather than guessed around: titles are kept exactly as the
index gives them (APA asks for sentence case, which cannot be produced
reliably without knowing which words are proper nouns), and a missing volume,
issue or page range is left out rather than invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .papers import Paper

STYLES = ("apa", "mla")
DEFAULT_STYLE = "apa"

_PARTICLES = frozenset({"van", "von", "de", "der", "den", "la", "le", "du", "da", "di", "del", "dos", "das",
                        "ten", "ter", "bin", "al", "el"})
_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"})


@dataclass(frozen=True)
class Name:
    given: str
    surname: str
    suffix: str = ""


def parse_name(raw: str) -> Name:
    raw = " ".join((raw or "").split())
    if "," in raw:
        surname, _, given = raw.partition(",")
        return Name(given.strip(), surname.strip())
    parts = raw.split(" ")
    suffix = ""
    if len(parts) > 2 and parts[-1].lower() in _SUFFIXES:
        suffix = parts.pop()
    if len(parts) == 1:
        return Name("", parts[0], suffix)
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in _PARTICLES:
        i -= 1
    return Name(" ".join(parts[:i]), " ".join(parts[i:]), suffix)


def initials(given: str) -> str:
    out: list[str] = []
    for part in given.replace(".", ". ").split():
        if "-" in part.strip("."):
            out.append("-".join(f"{p[0]}." for p in part.strip(".").split("-") if p))
        elif part[0].isalpha():
            out.append(f"{part[0]}.")
    return " ".join(out)


def _apa_author(n: Name) -> str:
    s = f"{n.surname}, {initials(n.given)}".rstrip(", ") if n.given else n.surname
    return f"{s}, {n.suffix}" if n.suffix else s


def _apa_authors(names: list[Name]) -> str:
    if not names:
        return ""
    parts = [_apa_author(n) for n in names]
    if len(parts) == 1:
        return parts[0]
    if len(parts) > 20:
        return ", ".join(parts[:19]) + ", . . . " + parts[-1]
    return ", ".join(parts[:-1]) + ", & " + parts[-1]


def _mla_authors(names: list[Name]) -> str:
    if not names:
        return ""
    first = f"{names[0].surname}, {names[0].given}".rstrip(", ") + (f", {names[0].suffix}" if names[0].suffix else "")
    if len(names) == 1:
        return first
    if len(names) == 2:
        second = f"{names[1].given} {names[1].surname}".strip()
        return f"{first}, and {second}"
    return f"{first}, et al"


def _end(s: str, mark: str = ".") -> str:
    s = s.strip()
    return s if s.endswith((".", "?", "!")) else s + mark


def _link(p: Paper) -> str:
    if p.doi:
        return f"https://doi.org/{p.doi}"
    if p.arxiv_id:
        return f"https://doi.org/10.48550/arXiv.{p.arxiv_id}"
    return p.landing_url


def reference(p: Paper, style: str = DEFAULT_STYLE, year_suffix: str = "") -> str:
    """One reference-list entry, in Markdown (italics with *...*)."""
    names = [parse_name(a) for a in p.authors if a.strip()]
    year = f"{p.year}{year_suffix}" if p.year else "n.d." + (f"-{year_suffix}" if year_suffix else "")
    preprint = p.status == "preprint"
    if style == "mla":
        out = []
        authors = _mla_authors(names)
        if authors:
            out.append(_end(authors))
        out.append(f"\"{_end(p.title)}\"")
        container = "arXiv" if preprint and p.arxiv_id else p.venue
        bits = [f"*{container}*"] if container else []
        if p.volume and not preprint:
            bits.append(f"vol. {p.volume}")
        if p.issue and not preprint:
            bits.append(f"no. {p.issue}")
        bits.append(str(p.year) if p.year else "n.d.")
        if p.pages and not preprint:
            bits.append(f"pp. {p.pages}")
        link = _link(p).replace("https://", "")
        if link:
            bits.append(link)
        out.append(_end(", ".join(bits)))
        return " ".join(out)
    if style != "apa":
        raise ValueError(f"unknown citation style {style!r} (use one of {STYLES})")
    authors = _apa_authors(names)
    head = f"{_end(authors)} ({year})." if authors else f"{_end(p.title)} ({year})."
    title_part = "" if not authors else (f"*{p.title.strip()}* [Preprint]." if preprint else _end(p.title))
    if preprint:
        source = "arXiv." if p.arxiv_id else (_end(p.venue) if p.venue else "")
    else:
        src = f"*{p.venue}*" if p.venue else ""
        if src and p.volume:
            src += f", *{p.volume}*" + (f"({p.issue})" if p.issue else "")
        if src and p.pages:
            src += f", {p.pages}"
        source = _end(src) if src else ""
    link = _link(p)
    return " ".join(x for x in (head, title_part, source, link) if x)


def in_text(p: Paper, style: str = DEFAULT_STYLE, year_suffix: str = "") -> str:
    """The parenthetical's inner text: "Cepeda et al., 2008" (APA) or "Cepeda et al." (MLA)."""
    names = [parse_name(a) for a in p.authors if a.strip()]
    if not names:
        who = f"\"{' '.join(p.title.split()[:4])}\""
    elif len(names) == 1:
        who = names[0].surname
    elif len(names) == 2:
        joiner = " & " if style == "apa" else " and "
        who = names[0].surname + joiner + names[1].surname
    else:
        who = f"{names[0].surname} et al."
    if style == "mla":
        return who
    year = f"{p.year}{year_suffix}" if p.year else "n.d." + (f"-{year_suffix}" if year_suffix else "")
    return f"{who}, {year}"


def _sort_key(p: Paper) -> tuple[str, int, str]:
    first = parse_name(p.authors[0]).surname.lower() if p.authors else p.title.lower()
    return (re.sub(r"[^a-z0-9 ]", "", first), p.year or 0, p.title.lower())


def reference_list(papers: list[Paper], style: str = DEFAULT_STYLE) -> tuple[list[str], dict[str, str]]:
    """(sorted entries, in-text citation by paper key). Two works that would
    cite identically (same authors and year) get a, b suffixes in both."""
    if style not in STYLES:
        raise ValueError(f"unknown citation style {style!r} (use one of {STYLES})")
    unique = list({p.key: p for p in papers}.values())
    ordered = sorted(unique, key=_sort_key)
    plain = {p.key: in_text(p, "apa") for p in ordered}
    clash: dict[str, list[Paper]] = {}
    for p in ordered:
        clash.setdefault(plain[p.key], []).append(p)
    suffix: dict[str, str] = {}
    for group in clash.values():
        if len(group) > 1:
            for i, p in enumerate(sorted(group, key=lambda x: x.title.lower())):
                suffix[p.key] = "abcdefghijklmnopqrstuvwxyz"[i % 26]
    entries = [reference(p, style, suffix.get(p.key, "")) for p in ordered]
    cites = {p.key: in_text(p, style, suffix.get(p.key, "")) for p in ordered}
    if style == "mla":  # MLA tells same-author works apart by a short title, not a letter
        for key in suffix:
            p = next(x for x in ordered if x.key == key)
            cites[key] = f"{cites[key]}, \"{' '.join(p.title.split()[:3])}\""
    return entries, cites
