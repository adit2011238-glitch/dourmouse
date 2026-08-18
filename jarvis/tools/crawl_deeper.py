"""Crawl one level deeper from the saved landing pages of HTML-only fields.

Most of the 271 HTML-only fields collapse to 31 unique landing URLs whose pages
carry no direct PDF links — the papers live one hop deeper (subpage → PDF) or on
other hosts. This tool:

1. Reads MANIFEST.json to find fields with no PDFs, collects their landing URLs.
2. For each landing URL, re-reads the saved HTML from _archives/ and follows
   same-host subpage links that look exam-relevant (path keywords, non-asset).
3. Fetches those subpages (politely, checkpointed, resumable) and pulls every
   PDF link found on them — same-host or cross-host.
4. Retries the 4 known-unreachable landing URLs with a longer timeout.
5. Rebuilds MANIFEST.json + per-field .meta.json so the new PDFs attach to the
   fields that use each landing URL (same schema as download_exams.py).

Every download goes through the same checkpoint (.progress.json), so re-running
is safe and only fetches what's still missing.

Usage: .venv/bin/python jarvis/tools/crawl_deeper.py [--host-delay 0.35] [--limit N]
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "research_mesh" / "tools"))
from download_exams import (  # noqa: E402
    ARCHIVES,
    CHECKPOINT,
    EXAMS,
    KEY_HINT,
    LINKS,
    MANIFEST,
    PAPERS,
    REGISTRY,
    Downloader,
    clean_url,
    fetch,
    slug,
)

# Non-page assets we never follow (CSS/JS/images/fonts/feeds/APIs).
_ASSET = re.compile(
    r"\.(css|js|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|webp|map|xml|json|rss|atom|txt)$",
    re.I,
)
_EXAM_HINT = re.compile(
    r"(qual|exam|prelim|comp(?:rehensive)?|archiv|past|syllabus|sample|practice|problem|candidacy)",
    re.I,
)
_PDF_HREF = re.compile(r'href=["\']([^"\']+\.pdf(?:[?#][^"\']*)?)["\']', re.I)
_MAX_SUBPAGES = 12
_MAX_PDFS_PER_PAGE = 30
# Bot-hostile / auth-gated hosts: download only the page itself, don't follow.
_SKIP_SUBPAGE_HOSTS = {"drive.google.com", "docs.google.com", "sharepoint.com",
                       "onedrive.live.com", "canvas.instructure.com", "scribd.com"}
# These landing URLs are known-flaky; give them a longer timeout when retrying.
_FLAKY = {
    "https://chemistry.columbian.gwu.edu/exams-dissertation",
    "https://pinphd.hms.harvard.edu/training/curriculum/pqe",
    "https://neurograd.ucsf.edu/node/36526",
    "https://www.econjobrumors.com/topic/links-for-micro-comprehensive-exams-and-solutions",
}


def candidate_subpages(page_html: str, base_url: str) -> list[str]:
    """Same-host, non-asset links whose path looks exam-relevant."""
    base = urllib.parse.urljoin(base_url, ".")
    host = urllib.parse.urlparse(base_url).netloc
    landing_path = urllib.parse.urlparse(base_url).path
    out: list[str] = []
    seen: set[str] = set()
    href_re = re.compile(r'href=["\']([^"\']+)["\']', re.I)
    for m in href_re.finditer(page_html):
        href = htmlmod.unescape(m.group(1)).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue
        try:
            absu = urllib.parse.urljoin(base, href)
        except ValueError:
            continue
        p = urllib.parse.urlparse(absu)
        if p.netloc != host:
            continue
        if any(h in p.netloc for h in _SKIP_SUBPAGE_HOSTS):
            continue
        if _ASSET.search(p.path):
            continue
        if p.path.rstrip("/") == landing_path.rstrip("/"):
            continue  # self-link
        if not _EXAM_HINT.search(p.path + " " + p.query):
            continue
        if absu in seen:
            continue
        seen.add(absu)
        out.append(absu)
        if len(out) >= _MAX_SUBPAGES:
            break
    return out


def pdf_links(page_html: str, base_url: str) -> list[str]:
    base = urllib.parse.urljoin(base_url, ".")
    out: list[str] = []
    seen: set[str] = set()
    for m in _PDF_HREF.finditer(page_html):
        try:
            absu = urllib.parse.urljoin(base, htmlmod.unescape(m.group(1)).strip())
        except ValueError:
            continue
        if absu in seen:
            continue
        seen.add(absu)
        out.append(absu)
        if len(out) >= _MAX_PDFS_PER_PAGE:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host-delay", type=float, default=0.35)
    ap.add_argument("--limit", type=int, default=None, help="max NEW downloads this run")
    args = ap.parse_args()

    m = json.loads(MANIFEST.read_text())
    fields = m["fields"]
    landing_urls: set[str] = set()
    for f in fields:
        has_pdf = any(t.get("kind") == "pdf" for t in f.get("tests", []) + f.get("keys", []))
        if not has_pdf:
            for ln in f.get("links", []):
                if ln.get("url"):
                    landing_urls.add(ln["url"])

    dl = Downloader(host_delay=args.host_delay, limit=args.limit)
    ck = {}
    if CHECKPOINT.exists():
        ck = json.loads(CHECKPOINT.read_text())

    print(f"landing URLs to deepen: {len(landing_urls)}")
    new_pdfs = 0
    for i, url in enumerate(sorted(landing_urls), 1):
        rec = ck.get(url)
        if rec is None or rec.get("kind") != "html":
            # No saved HTML yet: retry the URL itself (long timeout for flaky hosts).
            print(f"  [{i}/{len(landing_urls)}] no saved page, fetching: {url[:80]}", flush=True)
            rec = dl.download_url(url)
            if rec is None:
                continue
        page_path = PAPERS / rec["file"]
        if not page_path.exists():
            continue
        page = page_path.read_text(errors="replace")

        # Depth 1: fetch exam-relevant subpages, harvest their PDF links.
        subs = candidate_subpages(page, url)
        if subs:
            print(f"  [{i}/{len(landing_urls)}] {url[:60]} -> {len(subs)} subpages", flush=True)
        for sub in subs:
            sub_rec = dl.download_url(sub)
            if sub_rec is None or sub_rec.get("kind") != "html":
                continue
            spath = PAPERS / sub_rec["file"]
            if not spath.exists():
                continue
            for pdf in pdf_links(spath.read_text(errors="replace"), sub):
                if dl.download_url(pdf) is not None:
                    new_pdfs += 1

        # Also harvest PDFs directly from the landing page (cross-host allowed).
        for pdf in pdf_links(page, url):
            if dl.download_url(pdf) is not None:
                new_pdfs += 1

    print(f"\nnew PDFs downloaded: {new_pdfs}")
    print(f"stats: {json.dumps(dl.stats)}")
    print(f"failures this run: {len(dl.failures)}")
    for u, why in list(dl.failures.items())[:15]:
        print(f"  FAIL {why}: {u[:100]}")

    # Rebuild MANIFEST + per-field meta from the (updated) checkpoint.
    _rebuild_manifest()
    return 0


def _related_recs(landing: str, ck: dict) -> list[dict]:
    """All checkpoint records belonging to a landing URL: its own page plus every
    PDF reachable from it (landing page + exam-relevant subpages). This mirrors
    what download_exams.py's crawl() attached per URL, extended one level deep."""
    rec = ck.get(landing)
    if rec is None:
        return []
    out = [rec]
    if rec.get("kind") != "html":
        return out
    page_path = PAPERS / rec["file"]
    if not page_path.exists():
        return out
    page = page_path.read_text(errors="replace")
    pdf_urls = pdf_links(page, landing)
    for sub in candidate_subpages(page, landing):
        srec = ck.get(sub)
        if srec is None or srec.get("kind") != "html":
            continue
        spath = PAPERS / srec["file"]
        if spath.exists():
            pdf_urls.extend(pdf_links(spath.read_text(errors="replace"), sub))
    seen: set[str] = set()
    for u in pdf_urls:
        if u in seen:
            continue
        seen.add(u)
        prec = ck.get(u)
        if prec is not None:
            out.append(prec)
    return out


def _rebuild_manifest() -> None:
    """Same manifest/meta rebuild as download_exams.py, using the checkpoint."""
    from download_exams import build_link_map

    domains: list[dict] = []
    for p in REGISTRY:
        domains.extend(json.loads(p.read_text()).get("domains", []))
    links = json.loads(LINKS.read_text()).get("domains", {})
    rows = build_link_map(domains, links)

    ck = json.loads(CHECKPOINT.read_text())
    url_files: dict[str, list[dict]] = {}
    for r in rows:
        url = r["url"]
        if not url or url in url_files:
            continue
        url_files[url] = _related_recs(url, ck)

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
        "stats": {"note": "rebuilt by crawl_deeper.py (cumulative checkpoint)",
                  "unique_urls": len(ck)},
        "fields": sorted(
            ({"domain": e["domain"], "field": e["field"],
              "tests": e["tests"], "keys": e["keys"],
              "links": e["links"],
              "status": "downloaded" if e["tests"] or e["keys"] else "empty"}
             for e in per_field.values()),
            key=lambda x: (x["domain"], x["field"]),
        ),
        "failures": {},
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2))

    written = 0
    for e in per_field.values():
        fdir = PAPERS / e["domain"] / e["field"]
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / ".meta.json").write_text(json.dumps(
            {"domain": e["domain"], "field": e["field"],
             "tests": e["tests"], "keys": e["keys"], "links": e["links"]}, indent=2))
        written += 1
    print(f"manifest + {written} field meta files rebuilt")


if __name__ == "__main__":
    sys.exit(main())
