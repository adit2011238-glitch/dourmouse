"""Document acquisition for research (R0-6 raw cache, R0-4 decoding, R0-5
provenance; finding #089).

Before this module a fetched page was stripped and cut to 8000 characters
the moment it arrived, and only that text was kept. ``document_hash``
therefore fingerprinted text that no longer matched anything on disk, and a
cited passage could never be re-read against the page it came from. Four
more defects corrupted evidence silently: a fixed byte read taken before any
parsing (a big ``<head>`` could leave no body at all), a hardcoded UTF-8
decode that turned Latin-1 or Shift-JIS pages into mojibake, no
content-type check (a PDF was regex-stripped as if it were HTML), and the
requested URL recorded even when a redirect served the content from
elsewhere.

Here every fetch goes through ``net_guard`` (the SSRF-safe opener), the raw
bytes are stored content-addressed by their SHA-256 with the fetch metadata
beside them, and the text is derived from those stored bytes, so the same
text can be re-derived and re-verified from the cache at any later time.
"""

from __future__ import annotations

import codecs
import contextlib
import hashlib
import html
import json
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dourmouse import net_guard
from dourmouse.config import workspace_dir

from .extract_html import ExtractedDocument, extract_main

#: Bytes read from one response. Past this the document is kept but marked
#: truncated: never silently presented as whole.
MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
_USER_AGENT = "dourmouse-research/1.0"

_HTML_TYPES = ("text/html", "application/xhtml+xml")
_TEXT_TYPES = ("text/plain", "text/markdown", "text/csv", "application/json", "application/xml", "text/xml")
_PDF_TYPES = ("application/pdf",)

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.IGNORECASE,
)
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


class UnsupportedContent(ValueError):
    """The URL answered with something that is not a readable document
    (an image, an archive, a PDF when PDF support is not installed)."""


def default_cache_root() -> Path:
    """Resolved per call (finding #084), never at import."""
    return workspace_dir() / "research_pipeline" / "raw"


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    status: int
    content_type: str
    charset: str
    charset_source: str  # "header" | "bom" | "meta" | "default" | "binary"
    fetched_at: float
    raw_sha256: str
    raw_bytes: int
    truncated: bool
    kind: str  # "html" | "text" | "pdf"
    text: str = field(repr=False)
    # R0-2 (finding #092): True when this is the DOM a headless render
    # produced for a JavaScript-only page; `rendered_from` is the sha of the
    # static bytes the server actually sent. `render_note` says why a page
    # that looked empty was NOT rendered (switched off, Chrome missing, ...).
    rendered: bool = False
    rendered_from: str = ""
    render_note: str = ""
    # Headings and blocks of an HTML document (finding #091), derived from
    # the raw bytes like `text`; never stored in the metadata.
    structure: ExtractedDocument | None = field(default=None, repr=False, compare=False)

    def meta(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k not in ("text", "structure")}
        d["redirect_chain"] = list(self.redirect_chain)
        return d


def detect_charset(raw: bytes, header_charset: str | None) -> tuple[str, str]:
    """(codec name, where it came from). Order: the HTTP header, a BOM, a
    <meta charset> in the first 4 KB, then UTF-8. An unknown codec name is
    skipped rather than trusted."""
    for name, source in ((header_charset, "header"),):
        if name and _known_codec(name):
            return _known_codec(name), source  # type: ignore[return-value]
    for bom, name in _BOMS:
        if raw.startswith(bom):
            return name, "bom"
    m = _META_CHARSET_RE.search(raw[:4096])
    if m:
        name = m.group(1).decode("ascii", "replace")
        known = _known_codec(name)
        if known:
            return known, "meta"
    return "utf-8", "default"


def _known_codec(name: str) -> str | None:
    try:
        return codecs.lookup(name.strip()).name
    except LookupError:
        return None


def classify(content_type: str, raw: bytes) -> str:
    ct = content_type.split(";", 1)[0].strip().lower()
    if ct in _PDF_TYPES or raw.startswith(b"%PDF-"):
        return "pdf"
    if ct in _HTML_TYPES:
        return "html"
    if ct in _TEXT_TYPES or ct.startswith("text/"):
        return "text"
    if not ct:
        head = raw[:1024].lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")):
            return "html"
    raise UnsupportedContent(f"not a readable document (Content-Type: {ct or 'unknown'})")


def html_to_text(markup: str) -> str:
    """Main-content text of an HTML page (finding #091), falling back to a
    plain tag strip when extraction finds nothing to keep."""
    extracted = extract_main(markup).text
    return extracted or _strip_tags(markup)


def _strip_tags(markup: str) -> str:
    """Script/style/noscript removed, tags removed, ALL entities decoded
    (the old path decoded four), whitespace collapsed with paragraph breaks
    kept at block boundaries."""
    markup = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?s)<!--.*?-->", " ", markup)
    markup = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr|/section|/article)[^>]*>", "\n", markup)
    markup = re.sub(r"(?s)<[^>]+>", " ", markup)
    text = html.unescape(markup)
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def _pdf_to_text(raw_path: Path) -> str:
    from dourmouse.extract import extract_pdf_text

    try:
        text = extract_pdf_text(raw_path)
    except RuntimeError as exc:  # pypdf not installed: honest, not garbage
        raise UnsupportedContent(str(exc)) from exc
    # extract_pdf_text reports failure (and a PDF with no text layer) as a
    # message string; stored as evidence text it would read as content.
    if text.startswith(("ERROR:", "PDF READ FAILED", "PDF READ:")):
        raise UnsupportedContent(text)
    return text


class DocumentCache:
    """Content-addressed raw store: ``raw/<ab>/<sha>.bin`` plus
    ``<sha>.json`` (the latest fetch metadata for those bytes), and a small
    per-URL index so a repeated source is not re-fetched."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_cache_root()

    def _blob(self, sha: str) -> Path:
        return self.root / sha[:2] / f"{sha}.bin"

    def _meta(self, sha: str) -> Path:
        return self.root / sha[:2] / f"{sha}.json"

    def _index(self, url: str) -> Path:
        return self.root / "by_url" / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:32]}.json"

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def put(self, raw: bytes, meta: dict[str, Any]) -> None:
        sha = meta["raw_sha256"]
        if not self._blob(sha).exists():
            self._atomic_write(self._blob(sha), raw)
        body = json.dumps(meta, indent=2, sort_keys=True).encode("utf-8")
        self._atomic_write(self._meta(sha), body)
        for url in {meta["requested_url"], meta["final_url"]}:
            entry = {"url": url, "raw_sha256": sha, "fetched_at": meta["fetched_at"]}
            self._atomic_write(self._index(url), json.dumps(entry).encode("utf-8"))

    def raw_path(self, sha: str) -> Path:
        return self._blob(sha)

    def read_raw(self, sha: str) -> bytes:
        """The stored bytes, verified: a blob whose hash no longer matches
        its name is reported, never served as evidence."""
        data = self._blob(sha).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha:
            raise ValueError(f"cached document {sha} is corrupt (hashes to {actual})")
        return data

    def lookup(self, url: str) -> dict[str, Any] | None:
        idx = self._index(url)
        if not idx.exists():
            return None
        entry = json.loads(idx.read_text(encoding="utf-8"))
        meta_path = self._meta(entry["raw_sha256"])
        if not meta_path.exists() or not self._blob(entry["raw_sha256"]).exists():
            return None
        meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta


def _decode(raw: bytes, kind: str, charset: str, raw_path: Path) -> tuple[str, ExtractedDocument | None]:
    if kind == "pdf":
        return _pdf_to_text(raw_path), None
    decoded = raw.decode(charset, errors="replace")
    if kind != "html":
        return decoded.strip(), None
    structure = extract_main(decoded)
    text = structure.text
    if not text:
        return _strip_tags(decoded), None
    return text, structure


def load_cached(url: str, cache: DocumentCache | None = None) -> FetchedDocument | None:
    cache = cache or DocumentCache()
    meta = cache.lookup(url)
    if meta is None:
        return None
    raw = cache.read_raw(meta["raw_sha256"])
    text, structure = _decode(raw, meta["kind"], meta["charset"], cache.raw_path(meta["raw_sha256"]))
    return FetchedDocument(
        **{**meta, "redirect_chain": tuple(meta.get("redirect_chain") or ())},
        text=text, structure=structure,
    )


def fetch_document(
    url: str,
    *,
    cache: DocumentCache | None = None,
    use_cache: bool = True,
    render: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
) -> FetchedDocument:
    """Fetch ``url`` through the SSRF guard, store the raw bytes, return the
    document. Raises net_guard.FetchRefused, UnsupportedContent, or the
    ordinary urllib/OSError network errors; never returns a hollow success."""
    cache = cache or DocumentCache()
    if use_cache:
        cached = load_cached(url, cache)
        if cached is not None:
            return cached

    hops: list[str] = []
    # S310: the scheme is enforced by net_guard.guarded_urlopen (http/https only).
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
    with net_guard.guarded_urlopen(req, timeout=timeout, hops=hops) as resp:
        raw = resp.read(max_bytes + 1)
        status = int(getattr(resp, "status", 200) or 200)
        final_url = resp.geturl() or url
        content_type = resp.headers.get("Content-Type", "") or ""
        header_charset = resp.headers.get_content_charset()
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]

    kind = classify(content_type, raw)
    if kind == "pdf":
        charset, charset_source = "", "binary"
    else:
        charset, charset_source = detect_charset(raw, header_charset)
    sha = hashlib.sha256(raw).hexdigest()
    meta = {
        "requested_url": url,
        "final_url": final_url,
        "redirect_chain": hops,
        "status": status,
        "content_type": content_type,
        "charset": charset,
        "charset_source": charset_source,
        "fetched_at": time.time(),
        "raw_sha256": sha,
        "raw_bytes": len(raw),
        "truncated": truncated,
        "kind": kind,
    }
    text, structure = _decode(raw, kind, charset, cache.raw_path(sha)) if kind != "pdf" else ("", None)
    if kind == "html" and render:
        rendered = _maybe_render(raw, text, meta, cache)
        if rendered is not None:
            return rendered
    cache.put(raw, meta)
    if kind == "pdf":
        text, structure = _decode(raw, kind, charset, cache.raw_path(sha))
    return FetchedDocument(**{**meta, "redirect_chain": tuple(hops)}, text=text, structure=structure)  # type: ignore[arg-type]


def _maybe_render(raw: bytes, text: str, meta: dict[str, Any], cache: DocumentCache) -> FetchedDocument | None:
    """For a page that is an empty shell until its JavaScript runs, render
    it and store the rendered DOM as its own document (it is not what the
    server sent, so it never overwrites or impersonates the static bytes).
    Returns None when the page does not need rendering. When it does but
    rendering is unavailable, the reason is recorded on the static meta."""
    from .render import RenderUnavailable, looks_like_an_empty_shell, render_page

    if not looks_like_an_empty_shell(raw, text):
        return None
    try:
        result = render_page(meta["final_url"])
    except RenderUnavailable as exc:
        meta["render_note"] = f"looked empty without JavaScript; not rendered: {exc}"
        return None
    except Exception as exc:  # noqa: BLE001 -- a render must never lose the static fetch
        meta["render_note"] = f"looked empty without JavaScript; render failed: {type(exc).__name__}: {exc}"
        return None
    cache.put(raw, meta)  # keep the server's own bytes too, under their own hash
    body = result.html.encode("utf-8")
    rsha = hashlib.sha256(body).hexdigest()
    rmeta = {
        **meta,
        "final_url": result.final_url or meta["final_url"],
        "content_type": "text/html; charset=utf-8",
        "charset": "utf-8",
        "charset_source": "rendered",
        "fetched_at": time.time(),
        "raw_sha256": rsha,
        "raw_bytes": len(body),
        "truncated": False,
        "rendered": True,
        "rendered_from": meta["raw_sha256"],
        "render_note": (
            f"rendered in headless Chrome; {result.requests_served} requests served through the "
            f"SSRF guard, {len(result.requests_refused)} refused"
        ),
    }
    cache.put(body, rmeta)
    rtext, rstructure = _decode(body, "html", "utf-8", cache.raw_path(rsha))
    return FetchedDocument(**{**rmeta, "redirect_chain": tuple(rmeta["redirect_chain"])}, text=rtext, structure=rstructure)  # type: ignore[arg-type]


def cut_at_word(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut to at most ``limit`` chars at a word boundary (never
    mid-word), and whether anything was cut."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ", int(limit * 0.8))
    newline = cut.rfind("\n", int(limit * 0.8))
    edge = max(space, newline)
    return (cut[:edge] if edge > 0 else cut).rstrip(), True
