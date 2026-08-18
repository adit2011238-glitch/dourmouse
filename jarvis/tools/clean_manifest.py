"""Drop clearly non-exam PDFs from per-field manifest associations.

The deep crawl swept in documents that are not exam papers and the rebuild
attached them to every field sharing a landing URL (e.g. GaTech reading lists
on ~30 AI/ML fields, UMD MEES theses on all Earth Science fields). This tool
removes only *strong-signal* non-exam documents from MANIFEST.json and the
per-field .meta.json files:

- URL/filename says thesis or dissertation          -> drop
- page 1 says "reading list" and the file is short  -> drop
- page 1 says curriculum/syllabus/handbook with no
  exam vocabulary at all                            -> drop

Files stay in _archives/ (source of truth); only the field associations are
removed. Anything ambiguous is kept. After running, re-run materialize_pdfs.py
to rebuild the per-field copies.

Usage: .venv/bin/python jarvis/tools/clean_manifest.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPERS = ROOT / "research_mesh" / "fields" / "exams" / "papers"
ARCHIVES = PAPERS / "_archives"
MANIFEST = PAPERS / "MANIFEST.json"

_EXAM_TERM = re.compile(
    r"(exam|qualif|prelim|comprehensive|candidacy|problem\s*\d|question\s*\d|"
    r"points?|answer\s*key|solutions|instructions|do\s*not\s*open|time\s*limit)",
    re.I,
)
_STRONG_NON_EXAM = re.compile(
    r"(thesis|dissertation|reading\s*list|curriculum|syllabus|handbook|"
    r"program\s*requirements|graduate\s*program\s*guide)",
    re.I,
)


def url_basename(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    return urllib.parse.unquote(path.rsplit("/", 1)[-1]).lower()


def reason_to_drop(rec: dict) -> str | None:
    """Return a human reason if this record is clearly not an exam paper."""
    url = rec.get("url", "")
    name = url_basename(url)
    # Strongest signal first: the file itself is a thesis/dissertation.
    if re.search(r"(thesis|dissertation)", name + " " + url, re.I):
        return "thesis/dissertation in URL"
    path = ARCHIVES / Path(rec["file"]).name
    if not path.exists():
        return None  # leave missing-file handling to the verifier
    try:
        import pypdf  # local import: only needed when probing

        r = pypdf.PdfReader(str(path))
        n = len(r.pages)
        if n == 0:
            return None
        text = (r.pages[0].extract_text() or "")[:2000].lower()
    except Exception:
        return None  # corrupt files are flagged separately, not by content
    if not text.strip():
        return None  # scanned: needs OCR, not cleanup
    m = _STRONG_NON_EXAM.search(text)
    if not m:
        return None
    hits = len(_EXAM_TERM.findall(text))
    word = m.group(0)
    if word == "reading list" or "reading list" in text:
        if n <= 5 and hits == 0:
            return f"'{word}' on p1, {n}p, no exam terms"
        return None
    if hits == 0:
        return f"'{word}' on p1, no exam terms"
    return None


def main() -> int:
    m = json.loads(MANIFEST.read_text())
    fields: list[dict] = m["fields"]

    dropped: list[tuple[str, str, str, str]] = []
    kept_fields = 0
    for f in fields:
        for side in ("tests", "keys"):
            kept = []
            for rec in f.get(side, []):
                reason = reason_to_drop(rec)
                if reason:
                    dropped.append((f["domain"], f["field"], rec["url"], reason))
                else:
                    kept.append(rec)
            f[side] = kept
        if f["tests"] or f["keys"]:
            kept_fields += 1

    m["fields"] = fields
    MANIFEST.write_text(json.dumps(m, indent=2))

    # Rewrite per-field meta files to match.
    for f in fields:
        meta = PAPERS / f["domain"] / f["field"] / ".meta.json"
        if meta.exists():
            meta.write_text(json.dumps(
                {"domain": f["domain"], "field": f["field"],
                 "tests": f["tests"], "keys": f["keys"], "links": f["links"]},
                indent=2))

    print(f"records dropped: {len(dropped)}")
    print(f"fields still with content: {kept_fields}")
    from collections import Counter
    for (reason), n in Counter(r[3] for r in dropped).most_common():
        print(f"  {n:4d}  {reason}")
    print("\nsample dropped:")
    for dom, field, url, reason in dropped[:12]:
        print(f"  {dom}/{field}: {url[:80]}  [{reason}]")
    print(f"\nfull list: {len(dropped)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
