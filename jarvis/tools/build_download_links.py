"""Build the all-500-fields download link document from the download manifest.

Reads:  papers/MANIFEST.json   (written by download_exams.py)
Writes: docs/all-500-download-links.html  (clickable, one row per field)
        docs/all-500-download-links.txt   (plain URLs, wget -i friendly)
        docs/all-500-download-links.csv   (domain, field, urls)

Each field row lists:
  - TEST links  : every downloaded test PDF/HTML for the field (deduped)
  - KEY links   : published answer-key files where the archive ships them
  - ARCHIVE     : the landing page the field was mapped to
Fields whose source archive failed to fetch are marked FAILED with the URL
so they can be retried manually.
"""

from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path

# A file is a KEY only if its own URL says so. The manifest's stored "key" flag
# was sticky/wrong for some archives (Yale physics crawled as all-key), so we
# re-derive it from the URL — a URL that names a solution/answer/key is a key,
# everything else is a test.
_KEY_URL = re.compile(r"(solution|solutions|answer|answers|answer\s?key|keys|sol\.|_sol|sols|official\s?key)", re.I)

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "research_mesh" / "fields" / "exams" / "papers" / "MANIFEST.json"
DOCS = ROOT / "docs"

DOMAIN_ORDER = [
    "Mathematics", "Applied Mathematics", "Statistics & Probability",
    "Theoretical Computer Science", "Artificial Intelligence & Machine Learning",
    "Computer Vision & Graphics", "Databases & Data Systems", "Networking & Systems",
    "Cryptography & Security", "Quantum Computing & Information",
    "Computer Architecture & Hardware", "Software Engineering",
    "Physics", "Astrophysics & Cosmology", "Chemistry",
    "Condensed Matter & Materials (physics)", "Engineering",
    "Energy & Sustainability", "Materials Science & Engineering",
    "Mechanical Engineering", "Electrical & Computer Engineering",
    "Civil & Environmental Engineering", "Chemical Engineering",
    "Aerospace & Defense", "Biology", "Genomics & Systems Biology",
    "Molecular & Cellular Biology", "Evolutionary Biology", "Neuroscience",
    "Psychology", "Cognitive Science", "Medicine & Health",
    "Earth Science & Climate", "Environmental Science & Ecology",
    "Economics", "Finance & Quantitative Methods", "Business & Management",
    "Political Science", "Sociology", "Anthropology & Archaeology",
    "Linguistics", "Philosophy", "Law", "Education & Learning Science",
    "Human-Computer Interaction", "Library & Information Science",
    "Statistics & Data Science", "Operations Research",
]


def main() -> int:
    m = json.loads(MANIFEST.read_text())
    fields = {f["field"]: f for f in m["fields"]}

    # order fields: domain (registry order, fallback alpha) then field alpha
    domains = sorted({f["domain"] for f in m["fields"]},
                     key=lambda d: DOMAIN_ORDER.index(d) if d in DOMAIN_ORDER else 999)

    rows: list[tuple[str, str, list[str], list[str], str, str]] = []
    for dom in domains:
        for f in sorted(fields.values(), key=lambda x: x["field"]):
            if f["domain"] != dom:
                continue
            tests: list[str] = []
            keys: list[str] = []
            for t in f.get("tests", []) + f.get("keys", []):
                u = t["url"]
                if not u:
                    continue
                if _KEY_URL.search(u):
                    if u not in keys:
                        keys.append(u)
                elif u not in tests:
                    tests.append(u)
            archive = ""
            label = ""
            for ln in f.get("links", []):
                if ln.get("url"):
                    archive = ln["url"]
                    label = ln.get("label", "")
            status = "downloaded" if tests else (
                "failed" if f["status"] == "empty" else f["status"])
            rows.append((dom, f["field"], tests, keys, archive, status))

    # ---- TXT: one line per URL, grouped under DOMAIN > FIELD headers --------
    txt: list[str] = []
    for dom in domains:
        txt.append(f"### {dom}")
        for dom2, field, tests, keys, archive, status in rows:
            if dom2 != dom:
                continue
            txt.append(f"## {field}  [{status}]")
            if tests:
                txt.append("TEST:")
                for u in tests:
                    txt.append(f"  {u}")
            if keys:
                txt.append("KEY:")
                for u in keys:
                    txt.append(f"  {u}")
            if archive:
                txt.append(f"ARCHIVE: {archive}")
        txt.append("")
    (DOCS / "all-500-download-links.txt").write_text("\n".join(txt))

    # ---- CSV ----------------------------------------------------------------
    with (DOCS / "all-500-download-links.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["domain", "field", "status", "tests", "keys", "archive"])
        for dom, field, tests, keys, archive, status in rows:
            w.writerow([dom, field, status, " | ".join(tests), " | ".join(keys), archive])

    # ---- HTML ---------------------------------------------------------------
    esc = html.escape
    parts: list[str] = []
    parts.append("""<!doctype html><html><head><meta charset="utf-8">
<title>All 500 Fields — PhD Test Download Links</title>
<style>
  body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:2rem auto;max-width:1100px;padding:0 1rem;color:#1c1e21}
  h1{font-size:1.5rem} h2{font-size:1.15rem;border-bottom:2px solid #d4a017;padding-bottom:.25rem;margin-top:2rem}
  h3{font-size:1rem;margin:.8rem 0 .2rem}
  .status{font-size:.7rem;padding:.1rem .4rem;border-radius:3px;margin-left:.4rem}
  .downloaded{background:#e6f4ea;color:#1e7d34}
  .failed{background:#fdecea;color:#c5221f}
  .row{margin:.4rem 0 1rem;padding:.5rem .7rem;background:#fafafa;border-left:3px solid #ddd}
  .row.failed{border-left-color:#c5221f}
  a{color:#1a56db;text-decoration:none} a:hover{text-decoration:underline}
  .lbl{color:#666;font-size:.8rem;width:3.5rem;display:inline-block}
  ul{margin:.15rem 0;padding-left:1.4rem} li{font-size:.88rem;margin:.1rem 0}
  .archive{font-size:.82rem;color:#444}
  summary{cursor:pointer;font-weight:600}
</style></head><body>
<h1>All 500 Fields — PhD Test &amp; Answer-Key Download Links</h1>
<p>Sorted by domain. Each field lists its <b>TEST</b> files (direct PDF links where
possible, otherwise the archive landing page), its published <b>KEY</b> files
(only where the archive actually ships answer keys — 51 found), and the source
<b>ARCHIVE</b>. <span class="status failed">failed</span> = the source URL
timed out during the crawl; retry manually.</p>
""")

    for dom in domains:
        dom_rows = [r for r in rows if r[0] == dom]
        parts.append(f'<h2>{esc(dom)} <small>({len(dom_rows)} fields)</small></h2>')
        for _, field, tests, keys, archive, status in dom_rows:
            cls = "row" + (" failed" if status == "failed" else "")
            parts.append(f'<div class="{cls}">')
            parts.append(f'<h3>{esc(field)}<span class="status {esc(status)}">{esc(status)}</span></h3>')
            if tests:
                parts.append('<div><span class="lbl">TEST</span><ul>')
                for u in tests:
                    parts.append(f'<li><a href="{esc(u)}">{esc(u)}</a></li>')
                parts.append('</ul></div>')
            if keys:
                parts.append('<div><span class="lbl">KEY</span><ul>')
                for u in keys:
                    parts.append(f'<li><a href="{esc(u)}">{esc(u)}</a></li>')
                parts.append('</ul></div>')
            if archive:
                parts.append(f'<div class="archive"><span class="lbl">ARCHIVE</span>'
                             f'<a href="{esc(archive)}">{esc(archive)}</a></div>')
            if status == "failed":
                parts.append(f'<div class="archive"><span class="lbl">SOURCE</span>'
                             f'<a href="{esc(archive)}">{esc(archive)}</a> — '
                             f'unreachable during crawl, retry manually</div>')
            parts.append('</div>')
    parts.append("</body></html>")
    (DOCS / "all-500-download-links.html").write_text("\n".join(parts))

    n_test = sum(len(t) for *_, t, _, _, _ in rows)
    n_key = sum(len(k) for *_, _, k, _, _ in rows)
    n_fail = sum(1 for *_, s in [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows] if s == "failed")
    print(f"fields: {len(rows)}  test links: {n_test}  key links: {n_key}  failed: {n_fail}")
    for p in ("all-500-download-links.html", "all-500-download-links.txt", "all-500-download-links.csv"):
        f = DOCS / p
        print(f"  {f}  ({f.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
