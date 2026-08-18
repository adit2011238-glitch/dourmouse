"""Download the real PhD exams (and published answer keys) into one sorted folder.

Layout (all under jarvis/research_mesh/fields/exams/papers/):

    papers/
      _archives/<slug>/...                 # one physical copy per unique URL
      <Domain>/<Field>/.meta.json          # per-field: its test files + keys + links
      MANIFEST.json                        # full sorted index of all 500 fields

Behavior:
- Reads real_exams_registry_{a,b}.json (archives) and field_test_links.json
  (per-field link mapping).
- One physical download per unique URL (deduped); per-field meta files reference
  the same files, so 500 fields never mean 500 copies of the same archive.
- Landing pages (HTML) are parsed for same-site PDF links (capped, polite rate
  limit); anything with 'solution|answer|key' in its label/name goes to a keys/ slot.
- Answer keys are recorded only when the archive actually publishes them
  (e.g., Yale physics 2023-24 solutions, MIT physREFS solutions, UC Davis keys).
  No key is invented.
- Resumable: files already present (same size) are skipped; re-run to continue.
- Writes MANIFEST.json with per-field status, file list, sizes, and any failures.

Usage: .venv/bin/python jarvis/research_mesh/tools/download_exams.py [--limit N] [--host-delay 0.6]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
EXAMS = ROOT / "research_mesh" / "fields" / "exams"
PAPERS = EXAMS / "papers"
ARCHIVES = PAPERS / "_archives"
REGISTRY = [EXAMS / "real_exams_registry_a.json", EXAMS / "real_exams_registry_b.json"]
LINKS = EXAMS / "field_test_links.json"
MANIFEST = PAPERS / "MANIFEST.json"

USER_AGENT = "Dourmouse-ResearchMesh/0.1 (local-first academic archive crawler; polite rate limit)"
MAX_PDFS_PER_PAGE = 20
MAX_PAGE_BYTES = 4_000_000  # don't slurp huge HTML
FETCH_TIMEOUT = 12.0
CHECKPOINT = PAPERS / ".progress.json"
_PDF_HREF = re.compile(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', re.I)
_ALT_PDF = re.compile(r'(?:src|data-href)=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', re.I)
KEY_HINT = re.compile(r"(solution|answer|answer\s?key|solutions|key|sol\.)", re.I)


def slug(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:maxlen] or "untitled"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_url(url: str) -> str:
    """Quote the path/query so a page can never hand us an unparseable URL
    (spaces or control characters break urllib). Scheme/host stay intact."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=?_~-.")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def fetch(url: str, timeout: float = 25.0) -> tuple[bytes, str] | None:
    """Return (body, final_content_type) or None on failure."""
    url = clean_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - public academic archives
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read(MAX_PAGE_BYTES + 1_000_000)
            return body, ctype.lower()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, http.client.InvalidURL) as exc:
        return None  # caller records failure honestly


def looks_like_pdf(url: str, ctype: str) -> bool:
    return url.lower().split("?")[0].endswith(".pdf") or "pdf" in ctype


class Downloader:
    def __init__(self, host_delay: float = 0.6, limit: int | None = None) -> None:
        self.host_delay = host_delay
        self.limit = limit
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0, "keys": 0}
        self.failures: dict[str, str] = {}
        self._last_hit: dict[str, float] = {}
        self._progress: dict[str, dict] = {}
        if CHECKPOINT.exists():
            try:
                self._progress = json.loads(CHECKPOINT.read_text())
            except (json.JSONDecodeError, OSError):
                self._progress = {}

    def _checkpoint(self, url: str, rec: dict) -> None:
        self._progress[url] = rec
        try:
            CHECKPOINT.write_text(json.dumps(self._progress))
        except OSError:
            pass

    def _throttle(self, host: str) -> None:
        prev = self._last_hit.get(host, 0.0)
        wait = self.host_delay - (time.monotonic() - prev)
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _save(self, url: str, body: bytes) -> Path:
        digest = sha256_of(body)
        ext = ".pdf" if body[:4] == b"%PDF" else ".html"
        fname = f"{slug(url)[:40]}-{digest[:10]}{ext}"
        path = ARCHIVES / fname
        if path.exists():
            self.stats["skipped"] += 1
        else:
            path.write_bytes(body)
            self.stats["downloaded"] += 1
        self.stats["bytes"] += len(body)
        return path

    def download_url(self, url: str, is_key: bool = False) -> dict | None:
        if not url:
            return None
        if url in self._progress:
            self.stats["skipped"] += 1
            return dict(self._progress[url])
        if self.limit is not None and self.stats["downloaded"] >= self.limit:
            return None
        host = urllib.parse.urlparse(url).netloc
        self._throttle(host)
        fetched = fetch(url, timeout=FETCH_TIMEOUT)
        if fetched is None:
            self.stats["failed"] += 1
            self.failures[url] = "unreachable/timeout"
            return None
        body, ctype = fetched
        path = self._save(url, body)
        if is_key:
            self.stats["keys"] += 1
        rec = {"url": url, "file": str(path.relative_to(PAPERS)), "bytes": len(body),
               "sha256": sha256_of(body), "key": bool(is_key),
               "kind": "pdf" if body[:4] == b"%PDF" else "html"}
        self._checkpoint(url, rec)
        return rec

    def crawl(self, url: str, is_key: bool = False) -> list[dict]:
        """Download a URL; if it's an HTML landing page, also pull its same-site PDFs."""
        out: list[dict] = []
        rec = self.download_url(url, is_key=is_key)
        if rec is None:
            return out
        out.append(rec)
        if rec["kind"] != "html":
            return out
        text = rec["file"]
        page_path = PAPERS / text
        if page_path.stat().st_size > MAX_PAGE_BYTES:
            return out
        try:
            page = page_path.read_text(errors="replace")
        except OSError:
            return out
        base = urllib.parse.urljoin(url, ".")
        seen: set[str] = set()
        hrefs: list[str] = []
        for m in list(_PDF_HREF.finditer(page)) + list(_ALT_PDF.finditer(page)):
            hrefs.append(html.unescape(m.group(1)))
        pdfs = 0
        for href in hrefs:
            abs_url = urllib.parse.urljoin(base, href)
            if abs_url in seen:
                continue
            seen.add(abs_url)
            if urllib.parse.urlparse(abs_url).netloc != urllib.parse.urlparse(url).netloc:
                continue  # stay on the archive's own host
            pdfs += 1
            if pdfs > MAX_PDFS_PER_PAGE:
                break
            sub = self.download_url(abs_url, is_key=is_key or bool(KEY_HINT.search(abs_url)))
            if sub is not None:
                out.append(sub)
        return out


def build_link_map(domains: list[dict], links: dict) -> list[dict]:
    """One entry per field: domain, field, url, label, key_hint."""
    rows: list[dict] = []
    for d in domains:
        dl = links.get(d["name"], {})
        primary = dl.get("primary", {})
        for f in d.get("fields", []):
            override = dl.get("per_field", {}).get(f)
            if override:
                url, label = override.get("url", ""), override.get("label", "")
            else:
                url, label = primary.get("url", ""), primary.get("label", "")
            rows.append({
                "domain": d["name"], "field": f, "url": url, "label": label,
                "key_hint": bool(KEY_HINT.search(label or "")),
            })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="max NEW downloads this run")
    ap.add_argument("--host-delay", type=float, default=0.6)
    args = ap.parse_args()

    domains: list[dict] = []
    for p in REGISTRY:
        domains.extend(json.loads(p.read_text()).get("domains", []))
    links = json.loads(LINKS.read_text()).get("domains", {})
    rows = build_link_map(domains, links)

    ARCHIVES.mkdir(parents=True, exist_ok=True)
    dl = Downloader(host_delay=args.host_delay, limit=args.limit)

    # Dedupe URLs before downloading: one physical copy per unique URL.
    unique_urls: dict[str, dict] = {}
    for r in rows:
        if r["url"] and r["url"] not in unique_urls:
            unique_urls[r["url"]] = {"label": r["label"], "key_hint": r["key_hint"]}
    print(f"unique URLs to fetch: {len(unique_urls)}")

    url_files: dict[str, list[dict]] = {}
    done = 0
    for url, meta in unique_urls.items():
        recs = dl.crawl(url, is_key=meta["key_hint"])
        url_files[url] = recs
        done += 1
        if done % 5 == 0 or done == len(unique_urls):
            print(f"  [{done}/{len(unique_urls)}] {url[:70]} -> {len(recs)} files", flush=True)

    # Build per-field meta files (sorted folder tree: Domain/Field/.meta.json).
    per_field: dict[tuple, dict] = {}
    for r in rows:
        key = (r["domain"], r["field"])
        if key not in per_field:
            per_field[key] = {"domain": r["domain"], "field": r["field"],
                              "links": [], "tests": [], "keys": []}
        entry = per_field[key]
        entry["links"].append({"url": r["url"], "label": r["label"]})
        for rec in url_files.get(r["url"], []):
            if rec.get("key"):
                entry["keys"].append(rec)
            else:
                entry["tests"].append(rec)

    manifest = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stats": dl.stats,
        "fields": sorted(
            (
                {"domain": e["domain"], "field": e["field"],
                 "tests": e["tests"], "keys": e["keys"],
                 "links": e["links"], "status": "downloaded" if e["tests"] else "empty"}
                for e in per_field.values()
            ),
            key=lambda x: (x["domain"], x["field"]),
        ),
        "failures": dl.failures,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2))

    # Also write .meta.json per field for a browsable sorted tree.
    written = 0
    for e in per_field.values():
        fdir = PAPERS / e["domain"] / e["field"]
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / ".meta.json").write_text(json.dumps(
            {"domain": e["domain"], "field": e["field"],
             "tests": e["tests"], "keys": e["keys"], "links": e["links"]}, indent=2))
        written += 1

    print(f"fields with meta files: {written}")
    print(f"stats: {json.dumps(dl.stats)}")
    print(f"failures: {len(dl.failures)}")
    for u, why in list(dl.failures.items())[:15]:
        print(f"  FAIL {why}: {u}")
    print(f"manifest: {MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
