"""Paper search over OpenAlex, Semantic Scholar and arXiv (PL-10 T1, step 1).

Before this module the research pipeline had no academic source at all:
``web_search`` is Wikipedia plus Brave and DuckDuckGo page scraping
(general_roster.py), so a research question could only ever be answered from
web pages. Here the three free scholarly indexes are queried directly:

* OpenAlex for breadth (works search, reviews sorted by citations, DOI lookup),
* Semantic Scholar for the citation graph (references and citing papers),
* arXiv for preprints and their PDFs.

Rules every request keeps:

* One request at a time per service, spaced by ``MIN_INTERVAL``: about one a
  second for OpenAlex and Semantic Scholar (the owner's limit), one every three
  seconds for arXiv (arXiv's own API terms ask for that, so theirs wins).
* A 429 is waited out once or twice when the service says for how long
  (``Retry-After``, capped), then reported. Every failure is reported per
  service in ``SearchResult.errors``; one service failing never hides the
  others' results and is never turned into "no papers found".
* Answers are cached on disk (``CACHE_TTL``) keyed by the URL WITHOUT any API
  key, because the keyless budgets are small (OpenAlex keyless use shares about
  a hundred searches a day per network address).
* Free API keys are optional: ``OPENALEX_API_KEY`` (sent as ``api_key``) and
  ``SEMANTIC_SCHOLAR_API_KEY`` (sent as ``x-api-key``). Nothing else about the
  owner is sent: no e-mail address, no name.

Results from all three are merged into one list deduplicated by DOI (and, so a
preprint and its published version are one paper, by arXiv id and by title plus
first author), then ranked by ``rank_papers``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from dourmouse import net_guard
from dourmouse.config import workspace_dir

OPENALEX = "openalex"
SEMANTIC_SCHOLAR = "semantic_scholar"
ARXIV = "arxiv"
#: The encyclopedia pass of the context brief goes through the same throttled, cached client.
WIKIPEDIA = "wikipedia"
SOURCES = (OPENALEX, SEMANTIC_SCHOLAR, ARXIV)
SOURCE_NAMES = {OPENALEX: "OpenAlex", SEMANTIC_SCHOLAR: "Semantic Scholar", ARXIV: "arXiv", WIKIPEDIA: "Wikipedia"}

USER_AGENT = "dourmouse-research/1.0"

#: Seconds between two requests to one service.
MIN_INTERVAL = {OPENALEX: 1.0, SEMANTIC_SCHOLAR: 1.0, ARXIV: 3.0, WIKIPEDIA: 1.0}
#: Longest Retry-After this client will sit through before reporting the 429.
MAX_RETRY_AFTER = 30.0
#: Retries after a 429 (so at most three requests per call).
MAX_429_RETRIES = 2
CACHE_TTL = 7 * 86400.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
ARXIV_MAX_TERMS = 5

_OPENALEX_BASE = "https://api.openalex.org"
_S2_BASE = "https://api.semanticscholar.org/graph/v1"
# The API host. arxiv.org/robots.txt disallows /api on the main site and
# export.arxiv.org is the host arXiv's API manual names for programmatic use.
_ARXIV_BASE = "https://export.arxiv.org/api/query"

_OPENALEX_SELECT = (
    "id,doi,display_name,publication_year,publication_date,type,cited_by_count,authorships,"
    "primary_location,best_oa_location,locations,abstract_inverted_index,biblio,ids"
)
_S2_FIELDS = (
    "title,authors,year,venue,externalIds,abstract,citationCount,openAccessPdf,publicationTypes,"
    "journal,publicationDate,url"
)

_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)
_ARXIV_ID_IN_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([^?#\s]+?)(?:v\d+)?(?:\.pdf)?$", re.I)
_ARXIV_VERSION = re.compile(r"v\d+$")


class PaperSourceError(RuntimeError):
    """One service could not answer (network, HTTP error, rate limit, bad body)."""

    def __init__(self, source: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{SOURCE_NAMES.get(source, source)}: {message}")
        self.source = source
        self.status = status


# ---------------------------------------------------------------- the record


@dataclass(frozen=True)
class Paper:
    """One scholarly work as the indexes describe it. Every field is what a
    service returned; nothing is filled in by guess."""

    title: str
    authors: tuple[str, ...] = ()
    year: int | None = None
    publication_date: str = ""
    venue: str = ""
    #: "journal", "conference", "repository" (a preprint server), "book", "" unknown.
    venue_type: str = ""
    #: The index's own type word ("article", "review", "preprint", "JournalArticle", ...).
    work_type: str = ""
    is_review: bool = False
    doi: str = ""
    arxiv_id: str = ""
    openalex_id: str = ""
    s2_id: str = ""
    abstract: str = ""
    cited_by: int | None = None
    #: Open-access PDF links, most reliable first (arXiv, then the indexes' own).
    oa_pdf_urls: tuple[str, ...] = ()
    landing_url: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    found_by: tuple[str, ...] = ()
    #: DOIs of other records merged into this one (a conference and a journal version).
    other_dois: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Stable identity: DOI, else arXiv id, else an index id, else the title."""
        if self.doi:
            return f"doi:{self.doi}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id}"
        if self.openalex_id:
            return f"openalex:{self.openalex_id}"
        if self.s2_id:
            return f"s2:{self.s2_id}"
        return "title:" + hashlib.sha256(normalize_title(self.title).encode("utf-8")).hexdigest()[:16]

    @property
    def status(self) -> str:
        """Where the work appeared: "journal", "conference", "preprint" or "unknown".
        A journal or conference venue is what a peer-reviewed publication looks
        like in the index; the index cannot prove review took place."""
        if self.venue_type in ("journal", "conference"):
            return self.venue_type
        if self.venue_type == "repository" or (self.arxiv_id and not self.venue_type and not self.doi):
            return "preprint"
        if self.work_type.lower() in ("preprint", "posted-content"):
            return "preprint"
        if self.work_type in ("JournalArticle",):
            return "journal"
        if self.work_type in ("Conference",):
            return "conference"
        return "unknown"

    @property
    def url(self) -> str:
        if self.doi:
            return f"https://doi.org/{self.doi}"
        if self.arxiv_id:
            return f"https://arxiv.org/abs/{self.arxiv_id}"
        return self.landing_url

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, tuple):
                d[k] = list(v)
        d["key"] = self.key
        d["status"] = self.status
        d["url"] = self.url
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Paper:
        names = {f.name for f in fields(cls)}
        clean: dict[str, Any] = {}
        for k, v in d.items():
            if k not in names:
                continue
            clean[k] = tuple(v) if isinstance(v, list) else v
        return cls(**clean)


@dataclass
class SearchResult:
    papers: list[Paper] = field(default_factory=list)
    #: service -> what went wrong. Never folded into "nothing found".
    errors: dict[str, str] = field(default_factory=dict)
    #: service -> how many records it returned before merging.
    counts: dict[str, int] = field(default_factory=dict)

    def note(self) -> str:
        parts = [f"{SOURCE_NAMES[s]} {n}" for s, n in self.counts.items()]
        line = "found: " + (", ".join(parts) if parts else "nothing queried")
        if self.errors:
            line += "; failed: " + "; ".join(f"{SOURCE_NAMES.get(s, s)} ({e})" for s, e in self.errors.items())
        return line


# ---------------------------------------------------------------- normalizing


def normalize_doi(value: str | None) -> str:
    """Lower-case bare DOI ("10.1/x"), from a DOI, a doi: string or a doi.org URL."""
    if not value:
        return ""
    v = str(value).strip()
    v = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", v, flags=re.I)
    v = v.strip().lower()
    return v if v.startswith("10.") and "/" in v else ""


def normalize_arxiv_id(value: str | None) -> str:
    """"1706.03762" from "1706.03762v7", "arXiv:1706.03762", an abs/pdf URL or
    the arXiv DataCite DOI 10.48550/arXiv.1706.03762."""
    if not value:
        return ""
    v = str(value).strip()
    m = _ARXIV_DOI.match(normalize_doi(v) or "")
    if m:
        v = m.group(1)
    m = _ARXIV_ID_IN_URL.search(v)
    if m:
        v = m.group(1)
    v = re.sub(r"^arxiv:\s*", "", v, flags=re.I)
    v = _ARXIV_VERSION.sub("", v.strip())
    return v.lower() if re.match(r"^(\d{4}\.\d{4,5}|[a-z\-]+(\.[a-z]{2})?/\d{7})$", v, re.I) else ""


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _surname(name: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    while len(parts) > 1 and parts[-1].rstrip(".").lower() in ("jr", "sr", "ii", "iii", "iv"):
        parts.pop()
    return normalize_title(parts[-1]) if parts else ""


def _title_key(p: Paper) -> str:
    t = normalize_title(p.title)
    if len(t.split()) < 4:
        return ""  # "Introduction" or "Editorial" would merge unrelated works
    first = _surname(p.authors[0]) if p.authors else ""
    return f"{t}|{first}"


def _openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        for pos in positions or ():
            if isinstance(pos, int) and 0 <= pos < 20000:
                slots[pos] = word
    return " ".join(slots[i] for i in sorted(slots))


def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _clean_text(v: Any) -> str:
    return " ".join(str(v or "").split())


# ---------------------------------------------------------------- parsing


def parse_openalex_work(w: dict[str, Any]) -> Paper:
    primary = w.get("primary_location") or {}
    source = primary.get("source") or {}
    venue_type = str(source.get("type") or "")
    if venue_type == "book series":
        venue_type = "book"
    pdfs: list[str] = []
    for loc in [w.get("best_oa_location") or {}, primary, *(w.get("locations") or [])]:
        url = (loc or {}).get("pdf_url")
        if url and url not in pdfs and str(url).lower().startswith(("http://", "https://")):
            pdfs.append(str(url))
    ids = w.get("ids") or {}
    doi_raw = normalize_doi(w.get("doi") or ids.get("doi"))
    arxiv = ""
    m = _ARXIV_DOI.match(doi_raw)
    if m:
        arxiv, doi_raw = normalize_arxiv_id(m.group(1)), ""
    for loc in w.get("locations") or []:
        if not arxiv:
            arxiv = normalize_arxiv_id((loc or {}).get("landing_page_url"))
    biblio = w.get("biblio") or {}
    first, last = biblio.get("first_page"), biblio.get("last_page")
    pages = f"{first}-{last}" if first and last and first != last else (str(first) if first else "")
    work_type = str(w.get("type") or "")
    openalex_id = str(w.get("id") or "").rsplit("/", 1)[-1]
    return Paper(
        title=_clean_text(w.get("display_name") or w.get("title")),
        authors=tuple(
            _clean_text((a.get("author") or {}).get("display_name"))
            for a in (w.get("authorships") or [])
            if (a.get("author") or {}).get("display_name")
        ),
        year=_int_or_none(w.get("publication_year")),
        publication_date=str(w.get("publication_date") or ""),
        venue=_clean_text(source.get("display_name")),
        venue_type=venue_type,
        work_type=work_type,
        is_review=work_type == "review",
        doi=doi_raw,
        arxiv_id=arxiv,
        openalex_id=openalex_id,
        abstract=_openalex_abstract(w.get("abstract_inverted_index")),
        cited_by=_int_or_none(w.get("cited_by_count")),
        oa_pdf_urls=tuple(pdfs),
        landing_url=str(primary.get("landing_page_url") or ""),
        volume=str(biblio.get("volume") or ""),
        issue=str(biblio.get("issue") or ""),
        pages=pages,
        publisher=_clean_text(source.get("host_organization_name")),
        found_by=(OPENALEX,),
    )


def parse_s2_paper(p: dict[str, Any]) -> Paper:
    ext = p.get("externalIds") or {}
    journal = p.get("journal") or {}
    types = [str(t) for t in (p.get("publicationTypes") or []) if t]
    doi = normalize_doi(ext.get("DOI"))
    arxiv = normalize_arxiv_id(ext.get("ArXiv"))
    m = _ARXIV_DOI.match(doi)
    if m:
        arxiv, doi = arxiv or normalize_arxiv_id(m.group(1)), ""
    venue_type = ""
    if "JournalArticle" in types or ("Review" in types and journal.get("name")):
        venue_type = "journal"
    elif "Conference" in types:
        venue_type = "conference"
    venue = _clean_text(journal.get("name") or p.get("venue"))
    if venue.lower() in ("arxiv.org", "arxiv"):
        venue_type = "repository"
    pdf = (p.get("openAccessPdf") or {}).get("url") or ""
    return Paper(
        title=_clean_text(p.get("title")),
        authors=tuple(_clean_text(a.get("name")) for a in (p.get("authors") or []) if a.get("name")),
        year=_int_or_none(p.get("year")),
        publication_date=str(p.get("publicationDate") or ""),
        venue=venue,
        venue_type=venue_type,
        work_type=",".join(types),
        is_review="Review" in types,
        doi=doi,
        arxiv_id=arxiv,
        s2_id=str(p.get("paperId") or ""),
        abstract=_clean_text(p.get("abstract")),
        cited_by=_int_or_none(p.get("citationCount")),
        oa_pdf_urls=(pdf,) if str(pdf).lower().startswith(("http://", "https://")) else (),
        landing_url=str(p.get("url") or ""),
        volume=str(journal.get("volume") or "").strip(),
        pages=str(journal.get("pages") or "").strip().replace(" ", ""),
        found_by=(SEMANTIC_SCHOLAR,),
    )


_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def parse_arxiv_feed(body: bytes) -> list[Paper]:
    """arXiv's Atom feed, parsed with defusedxml (no entity expansion)."""
    from defusedxml import ElementTree

    root = ElementTree.fromstring(body)
    out: list[Paper] = []
    for entry in root.findall(f"{_ATOM}entry"):
        raw_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        arxiv = normalize_arxiv_id(raw_id)
        title = _clean_text(entry.findtext(f"{_ATOM}title"))
        if not arxiv or not title or title.lower() == "error":
            continue  # arXiv reports query errors as an entry titled "Error"
        published = (entry.findtext(f"{_ATOM}published") or "").strip()
        doi = normalize_doi(entry.findtext(f"{_ARXIV_NS}doi"))
        journal_ref = _clean_text(entry.findtext(f"{_ARXIV_NS}journal_ref"))
        out.append(Paper(
            title=title,
            authors=tuple(
                _clean_text(a.findtext(f"{_ATOM}name")) for a in entry.findall(f"{_ATOM}author")
                if _clean_text(a.findtext(f"{_ATOM}name"))
            ),
            year=_int_or_none(published[:4]),
            publication_date=published[:10],
            venue="arXiv",
            # A journal_ref says the work was also published elsewhere; the
            # record here is still the preprint, so it stays a repository copy.
            venue_type="repository",
            work_type="preprint" + (f" (journal ref: {journal_ref})" if journal_ref else ""),
            doi=doi if not _ARXIV_DOI.match(doi) else "",
            arxiv_id=arxiv,
            abstract=_clean_text(entry.findtext(f"{_ATOM}summary")),
            oa_pdf_urls=(f"https://arxiv.org/pdf/{arxiv}",),
            landing_url=f"https://arxiv.org/abs/{arxiv}",
            found_by=(ARXIV,),
        ))
    return out


# ---------------------------------------------------------------- merging


def _prefer(a: str, b: str) -> str:
    return a or b


def _merge_two(a: Paper, b: Paper) -> Paper:
    """Union of what two records of the same work say. Text fields keep the
    first non-empty value (OpenAlex records are merged first), the abstract
    keeps the longer one, the citation count the larger, and every PDF link is
    kept with arXiv's first."""
    doi_a, doi_b = a.doi, b.doi
    doi = doi_a or doi_b
    others = set(a.other_dois) | set(b.other_dois)
    if doi_a and doi_b and doi_a != doi_b:
        # Two published versions under one arXiv id or title: keep the journal one.
        keep = doi_b if (b.venue_type == "journal" and a.venue_type != "journal") else doi_a
        others |= {doi_a, doi_b} - {keep}
        doi = keep
    pdfs: list[str] = []
    arxiv = a.arxiv_id or b.arxiv_id
    if arxiv:
        pdfs.append(f"https://arxiv.org/pdf/{arxiv}")
    for u in (*a.oa_pdf_urls, *b.oa_pdf_urls):
        if u not in pdfs and not (arxiv and _ARXIV_ID_IN_URL.search(u)):
            pdfs.append(u)
    venue_rank = {"journal": 3, "conference": 2, "book": 1, "repository": 0, "": -1}
    best_venue = a if venue_rank.get(a.venue_type, -1) >= venue_rank.get(b.venue_type, -1) else b
    cites = [c for c in (a.cited_by, b.cited_by) if c is not None]
    return replace(
        a,
        title=_prefer(a.title, b.title),
        authors=a.authors if len(a.authors) >= len(b.authors) else b.authors,
        year=a.year if a.year is not None else b.year,
        publication_date=_prefer(a.publication_date, b.publication_date),
        venue=best_venue.venue or a.venue or b.venue,
        venue_type=best_venue.venue_type,
        work_type=_prefer(a.work_type, b.work_type),
        is_review=a.is_review or b.is_review,
        doi=doi,
        arxiv_id=arxiv,
        openalex_id=_prefer(a.openalex_id, b.openalex_id),
        s2_id=_prefer(a.s2_id, b.s2_id),
        abstract=a.abstract if len(a.abstract) >= len(b.abstract) else b.abstract,
        cited_by=max(cites) if cites else None,
        oa_pdf_urls=tuple(pdfs),
        landing_url=_prefer(a.landing_url, b.landing_url),
        volume=_prefer(best_venue.volume, a.volume or b.volume),
        issue=_prefer(best_venue.issue, a.issue or b.issue),
        pages=_prefer(best_venue.pages, a.pages or b.pages),
        publisher=_prefer(a.publisher, b.publisher),
        found_by=tuple(dict.fromkeys((*a.found_by, *b.found_by))),
        other_dois=tuple(sorted(others - {doi})),
    )


def merge_papers(papers: Iterable[Paper]) -> list[Paper]:
    """Deduplicate by DOI, arXiv id, and title plus first author (any shared
    key joins two records), keeping first-seen order."""
    items = [p for p in papers if p.title]
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[str, int] = {}
    for i, p in enumerate(items):
        for k in (f"doi:{p.doi}" if p.doi else "", f"arxiv:{p.arxiv_id}" if p.arxiv_id else "",
                  f"title:{_title_key(p)}" if _title_key(p) else ""):
            if not k:
                continue
            if k in seen:
                ra, rb = find(seen[k]), find(i)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
            else:
                seen[k] = i
    groups: dict[int, Paper] = {}
    order: list[int] = []
    for i, p in enumerate(items):
        r = find(i)
        if r in groups:
            groups[r] = _merge_two(groups[r], p)
        else:
            groups[r] = p
            order.append(r)
    return [groups[r] for r in order]


# ---------------------------------------------------------------- ranking


@dataclass(frozen=True)
class RankScore:
    total: float
    relevance: float
    citations_per_year: float
    recency: float
    venue: float

    def to_dict(self) -> dict[str, float]:
        return {k: round(v, 4) for k, v in asdict(self).items()}


#: Weights of the four signals the plan names. Relevance dominates: a highly
#: cited paper about something else is not evidence for this question.
RANK_WEIGHTS = {"relevance": 0.55, "citations_per_year": 0.2, "recency": 0.1, "venue": 0.15}
_VENUE_SCORE = {"journal": 1.0, "conference": 0.9, "preprint": 0.55, "unknown": 0.5}


def query_terms(*texts: str) -> frozenset[str]:
    from .answer_critic import _tokens

    words: set[str] = set()
    for t in texts:
        words |= _tokens(t or "")[0]
    return frozenset(words)


def citations_per_year(p: Paper, now_year: int) -> float | None:
    if p.cited_by is None or p.year is None:
        return None
    return p.cited_by / max(1, now_year - p.year + 1)


def rank_papers(papers: Iterable[Paper], terms: frozenset[str], *, now_year: int) -> list[tuple[Paper, RankScore]]:
    """Papers best first. Relevance is the share of the query's content words
    found in the title and abstract (a title hit counts double); citations per
    year are log-scaled against the best in the set; recency halves every eight
    years; venue is journal or conference over preprint over unknown."""
    from .answer_critic import _tokens

    plist = list(papers)
    cpys = [citations_per_year(p, now_year) for p in plist]
    top_cpy = max([c for c in cpys if c is not None] or [0.0])
    scored: list[tuple[Paper, RankScore]] = []
    for p, cpy in zip(plist, cpys, strict=True):
        title_words = _tokens(p.title)[0]
        body_words = _tokens(p.abstract)[0]
        if terms:
            hits = sum((2.0 if t in title_words else 1.0 if t in body_words else 0.0) for t in terms)
            relevance = min(1.0, hits / (2.0 * len(terms)) * 1.5)
        else:
            relevance = 0.0
        cites = math.log1p(cpy) / math.log1p(top_cpy) if cpy is not None and top_cpy > 0 else 0.0
        recency = 0.5 ** (max(0, now_year - p.year) / 8.0) if p.year is not None else 0.0
        venue = _VENUE_SCORE.get(p.status, 0.5)
        total = (RANK_WEIGHTS["relevance"] * relevance + RANK_WEIGHTS["citations_per_year"] * cites
                 + RANK_WEIGHTS["recency"] * recency + RANK_WEIGHTS["venue"] * venue)
        scored.append((p, RankScore(total, relevance, cites, recency, venue)))
    scored.sort(key=lambda ps: (-ps[1].total, ps[0].title))
    return scored


# ---------------------------------------------------------------- transport


class Throttle:
    """At most one request per ``interval`` seconds, across threads."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._next - self._clock()
            if delay > 0:
                self._sleep(delay)
            self._next = self._clock() + self.interval


#: Process-wide: every job and every chat tool shares one gate per service.
THROTTLES: dict[str, Throttle] = {s: Throttle(interval) for s, interval in MIN_INTERVAL.items()}

#: (status, headers, body) for a GET. Tests replace it; production goes
#: through the SSRF guard like every other fetch in this codebase.
Transport = Callable[[str, dict[str, str], float], tuple[int, dict[str, str], bytes]]


def _guarded_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 -- scheme checked by net_guard
    try:
        with net_guard.guarded_urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            return int(getattr(resp, "status", 200) or 200), dict(resp.headers.items()), body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
        except Exception:  # noqa: BLE001 -- the status is the finding; the body is a courtesy
            body = b""
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, body


def _retry_after(headers: dict[str, str], attempt: int) -> float:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return max(0.0, float(str(v).strip()))
            except ValueError:
                break
    return 2.0 * (2 ** attempt)


def _redact(url: str) -> str:
    """The URL with any api_key parameter removed: the cache key, and what an
    error message may show."""
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "api_key"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


def default_cache_dir() -> Path:
    """Resolved per call (finding #084), never at import."""
    return workspace_dir() / "research_pipeline" / "paper_api_cache"


class PaperClient:
    """The three services behind one throttled, cached client."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        cache_dir: str | Path | None = None,
        use_cache: bool = True,
        cache_ttl: float = CACHE_TTL,
        throttles: dict[str, Throttle] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        openalex_key: str | None = None,
        s2_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._transport = transport or _guarded_get
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._use_cache = use_cache
        self._ttl = cache_ttl
        self._throttles = throttles if throttles is not None else THROTTLES
        self._sleep = sleep
        self._clock = clock
        self._openalex_key = openalex_key if openalex_key is not None else os.environ.get("OPENALEX_API_KEY", "").strip()
        self._s2_key = s2_key if s2_key is not None else os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
        self._timeout = timeout
        #: Requests actually sent per service (cache hits excluded): a job's cost.
        self.requests_sent: dict[str, int] = dict.fromkeys(MIN_INTERVAL, 0)

    # -- plumbing

    def keyed(self) -> dict[str, bool]:
        return {OPENALEX: bool(self._openalex_key), SEMANTIC_SCHOLAR: bool(self._s2_key), ARXIV: False}

    def _cache_path(self, url: str) -> Path:
        root = self._cache_dir if self._cache_dir is not None else default_cache_dir()
        digest = hashlib.sha256(_redact(url).encode("utf-8")).hexdigest()
        return root / digest[:2] / f"{digest}.json"

    def _cache_get(self, url: str) -> bytes | None:
        if not self._use_cache:
            return None
        path = self._cache_path(url)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self._clock() - float(entry.get("at", 0)) > self._ttl:
            return None
        body = entry.get("body")
        return body.encode("utf-8") if isinstance(body, str) else None

    def _cache_put(self, url: str, body: bytes) -> None:
        if not self._use_cache:
            return
        path = self._cache_path(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"url": _redact(url), "at": self._clock(),
                                       "body": body.decode("utf-8", errors="replace")}), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # a cache that cannot be written only costs a repeat request later

    def _get(self, source: str, url: str, headers: dict[str, str] | None = None) -> bytes:
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        for attempt in range(MAX_429_RETRIES + 1):
            self._throttles[source].wait()
            self.requests_sent[source] = self.requests_sent.get(source, 0) + 1
            try:
                status, resp_headers, body = self._transport(url, hdrs, self._timeout)
            except net_guard.FetchRefused as exc:
                raise PaperSourceError(source, f"refused: {exc}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise PaperSourceError(source, f"{type(exc).__name__}: {exc}") from exc
            if status == 429:
                wait = _retry_after(resp_headers, attempt)
                if attempt < MAX_429_RETRIES and wait <= MAX_RETRY_AFTER:
                    self._sleep(wait)
                    continue
                hint = " (no API key: the keyless allowance is shared and small)" if not self.keyed().get(source) else ""
                raise PaperSourceError(source, f"rate limited (HTTP 429){hint}", 429)
            if status >= 400:
                snippet = body[:200].decode("utf-8", errors="replace").strip()
                raise PaperSourceError(source, f"HTTP {status}" + (f": {snippet}" if snippet else ""), status)
            if len(body) > MAX_RESPONSE_BYTES:
                raise PaperSourceError(source, "response too large")
            self._cache_put(url, body)
            return body
        raise PaperSourceError(source, "rate limited (HTTP 429)", 429)  # pragma: no cover - loop always returns or raises

    def _json(self, source: str, url: str, headers: dict[str, str] | None = None) -> Any:
        body = self._get(source, url, headers)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PaperSourceError(source, f"unreadable answer: {exc}") from exc

    def _openalex_url(self, path: str, params: dict[str, Any]) -> str:
        p = dict(params)
        if self._openalex_key:
            p["api_key"] = self._openalex_key
        return f"{_OPENALEX_BASE}{path}?{urllib.parse.urlencode(p)}"

    def _s2_headers(self) -> dict[str, str]:
        return {"x-api-key": self._s2_key} if self._s2_key else {}

    # -- OpenAlex

    def openalex_search(self, query: str, *, per_page: int = 25, reviews_only: bool = False,
                        sort: str = "") -> list[Paper]:
        """Works matching ``query``. ``reviews_only`` restricts to OpenAlex's
        review type and searches titles and abstracts only, so a citation sort
        cannot lift a famous paper that merely mentions the topic in its text."""
        query = " ".join(query.split())
        if not query:
            return []
        params: dict[str, Any] = {"per-page": max(1, min(int(per_page), 50)), "select": _OPENALEX_SELECT}
        safe = query.replace(",", " ").replace(":", " ")
        if reviews_only:
            params["filter"] = f"type:review,title_and_abstract.search:{safe}"
        else:
            params["search"] = query
        if sort:
            params["sort"] = sort
        data = self._json(OPENALEX, self._openalex_url("/works", params))
        return [parse_openalex_work(w) for w in (data.get("results") or []) if isinstance(w, dict)]

    def openalex_work(self, *, doi: str = "", openalex_id: str = "") -> Paper | None:
        """One work by DOI or OpenAlex id; None when OpenAlex has no such work."""
        if doi:
            path = "/works/doi:" + urllib.parse.quote(normalize_doi(doi), safe="/")
        elif openalex_id:
            path = "/works/" + urllib.parse.quote(openalex_id.rsplit("/", 1)[-1], safe="")
        else:
            raise ValueError("doi or openalex_id is required")
        try:
            data = self._json(OPENALEX, self._openalex_url(path, {"select": _OPENALEX_SELECT}))
        except PaperSourceError as exc:
            if exc.status == 404:
                return None
            raise
        return parse_openalex_work(data) if isinstance(data, dict) and data.get("id") else None

    # -- Semantic Scholar

    def s2_search(self, query: str, *, limit: int = 20) -> list[Paper]:
        query = " ".join(query.split())
        if not query:
            return []
        url = f"{_S2_BASE}/paper/search?" + urllib.parse.urlencode(
            {"query": query, "limit": max(1, min(int(limit), 100)), "fields": _S2_FIELDS})
        data = self._json(SEMANTIC_SCHOLAR, url, self._s2_headers())
        return [parse_s2_paper(p) for p in (data.get("data") or []) if isinstance(p, dict)]

    @staticmethod
    def s2_ref(p: Paper) -> str:
        """The id Semantic Scholar accepts for ``p``, or "" when it has none."""
        if p.s2_id:
            return p.s2_id
        if p.doi:
            return f"DOI:{p.doi}"
        if p.arxiv_id:
            return f"ARXIV:{p.arxiv_id}"
        return ""

    def s2_neighbours(self, p: Paper, *, direction: str = "references", limit: int = 20) -> list[Paper]:
        """Papers ``p`` cites (references) or that cite it (citations)."""
        if direction not in ("references", "citations"):
            raise ValueError("direction must be references or citations")
        ref = self.s2_ref(p)
        if not ref:
            return []
        # The DOI's own slash stays as is: the Graph API reads /paper/DOI:10.x/y/references.
        url = f"{_S2_BASE}/paper/{urllib.parse.quote(ref, safe=':/')}/{direction}?" + urllib.parse.urlencode(
            {"limit": max(1, min(int(limit), 100)), "fields": _S2_FIELDS})
        data = self._json(SEMANTIC_SCHOLAR, url, self._s2_headers())
        key = "citedPaper" if direction == "references" else "citingPaper"
        return [parse_s2_paper(row[key]) for row in (data.get("data") or [])
                if isinstance(row, dict) and isinstance(row.get(key), dict) and row[key].get("title")]

    # -- arXiv

    def arxiv_search(self, query: str, *, max_results: int = 10) -> list[Paper]:
        """arXiv's search ANDs the terms, so only the first few content words of
        ``query`` are sent: a whole sentence would match nothing."""
        from .answer_critic import _STOPWORDS

        words = [w for w in re.split(r"[^\w\-]+", query.lower()) if len(w) > 1 and w not in _STOPWORDS]
        words = list(dict.fromkeys(words))
        if not words:
            return []
        expr = " AND ".join(f"all:{w}" for w in words[:ARXIV_MAX_TERMS])
        url = f"{_ARXIV_BASE}?" + urllib.parse.urlencode(
            {"search_query": expr, "start": 0, "max_results": max(1, min(int(max_results), 50))})
        body = self._get(ARXIV, url)
        try:
            return parse_arxiv_feed(body)
        except Exception as exc:  # noqa: BLE001 -- defusedxml raises several types for a bad body
            raise PaperSourceError(ARXIV, f"unreadable answer: {type(exc).__name__}: {exc}") from exc

    # -- Wikipedia (the context brief's encyclopedia pass)

    def wikipedia_article(self, topic: str, *, max_chars: int = 40_000) -> tuple[str, str, str] | None:
        """(title, url, plain text) of the best English Wikipedia match for
        ``topic``, or None when there is none."""
        topic = " ".join(topic.split())
        if not topic:
            return None
        base = "https://en.wikipedia.org/w/api.php?"
        found = self._json(WIKIPEDIA, base + urllib.parse.urlencode(
            {"action": "query", "list": "search", "srsearch": topic, "srlimit": 1, "format": "json"}))
        hits = ((found.get("query") or {}).get("search") or []) if isinstance(found, dict) else []
        if not hits or not hits[0].get("title"):
            return None
        title = str(hits[0]["title"])
        page = self._json(WIKIPEDIA, base + urllib.parse.urlencode(
            {"action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "titles": title,
             "format": "json"}))
        pages = ((page.get("query") or {}).get("pages") or {}) if isinstance(page, dict) else {}
        text = next((str(p.get("extract") or "") for p in pages.values() if isinstance(p, dict)), "")
        if not text.strip():
            return None
        url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        return title, url, text[:max_chars]

    # -- together

    def search(self, query: str, *, per_source: int = 10, sources: Iterable[str] = SOURCES) -> SearchResult:
        """Every requested service, merged and deduplicated. A failing service is
        recorded in ``errors`` and the others still answer."""
        result = SearchResult()
        found: list[Paper] = []
        calls: dict[str, Callable[[], list[Paper]]] = {
            OPENALEX: lambda: self.openalex_search(query, per_page=per_source),
            SEMANTIC_SCHOLAR: lambda: self.s2_search(query, limit=per_source),
            ARXIV: lambda: self.arxiv_search(query, max_results=per_source),
        }
        for s in sources:
            if s not in calls:
                raise ValueError(f"unknown paper source {s!r}")
            try:
                got = calls[s]()
            except PaperSourceError as exc:
                result.errors[s] = str(exc).split(": ", 1)[-1]
                continue
            result.counts[s] = len(got)
            found.extend(got)
        result.papers = merge_papers(found)
        return result

    def top_reviews(self, topic: str, *, n: int = 2) -> SearchResult:
        """The ``n`` most cited review papers whose title or abstract matches
        ``topic`` (OpenAlex works of type review, sorted by citations)."""
        result = SearchResult()
        try:
            got = self.openalex_search(topic, per_page=max(n, 5), reviews_only=True, sort="cited_by_count:desc")
        except PaperSourceError as exc:
            result.errors[OPENALEX] = str(exc).split(": ", 1)[-1]
            return result
        result.counts[OPENALEX] = len(got)
        result.papers = merge_papers(got)[:n]
        return result
