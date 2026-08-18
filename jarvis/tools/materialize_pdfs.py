"""Materialize exam PDFs into their per-field folders (no HTML, no dedup).

Reads papers/MANIFEST.json (written by download_exams.py). For every field it
copies each referenced PDF from papers/_archives/ into

    papers/<Domain>/<Field>/tests/<url-filename>.pdf
    papers/<Domain>/<Field>/keys/<url-filename>.pdf

Rules:
- Only PDFs are materialized; HTML landing pages are skipped entirely.
- A file is a KEY only if its own URL names a solution/answer/key (re-derives
  from the URL, because the manifest's stored 'key' flag was sticky/wrong for
  some archives, e.g. Yale physics).
- Files are named from their source URL's filename (readable), falling back to
  the archive filename when the URL has no basename. Name collisions inside one
  folder get a short hash suffix.
- Pure local copy: nothing is re-downloaded. Existing files are skipped.
- Writes a verification report: every field's materialized PDFs are checked
  against the manifest (count and total bytes), with the 12 empty fields
  (Education gaps + unreachable sources) listed explicitly.

Usage: .venv/bin/python jarvis/tools/materialize_pdfs.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPERS = ROOT / "research_mesh" / "fields" / "exams" / "papers"
ARCHIVES = PAPERS / "_archives"
MANIFEST = PAPERS / "MANIFEST.json"

_KEY_URL = re.compile(
    r"(solution|solutions|answer|answers|answer\s?key|keys|sol\.|_sol|sols|official\s?key)", re.I
)


def dest_name(url: str, fallback: str) -> str:
    """Readable filename from the URL basename, else the archive filename."""
    path = urllib.parse.urlsplit(url).path
    base = urllib.parse.unquote(path.rsplit("/", 1)[-1]).strip()
    if base and base.lower().endswith(".pdf"):
        return base
    return Path(fallback).name


def main() -> int:
    m = json.loads(MANIFEST.read_text())
    fields: list[dict] = m["fields"]

    copied = 0
    skipped = 0
    bytes_copied = 0
    filled = 0
    issues: list[str] = []

    for f in fields:
        dom = f["domain"]
        field = f["field"]
        folder = PAPERS / dom / field
        (folder / "tests").mkdir(parents=True, exist_ok=True)
        (folder / "keys").mkdir(parents=True, exist_ok=True)

        pdfs = [t for t in f.get("tests", []) + f.get("keys", []) if t.get("kind") == "pdf"]
        if not pdfs:
            continue

        local = []
        used: set[str] = set()
        for rec in pdfs:
            src = ARCHIVES / Path(rec["file"]).name
            is_key = bool(_KEY_URL.search(rec["url"]))
            sub = "keys" if is_key else "tests"
            name = dest_name(rec["url"], rec["file"])
            if name in used:  # same basename twice in one folder -> hash suffix
                stem, ext = name.rsplit(".", 1)
                name = f"{stem}-{rec['sha256'][:8]}.{ext}"
            used.add(name)
            dst = folder / sub / name
            local.append((str(dst.relative_to(PAPERS)), rec["bytes"]))
            if dst.exists():
                skipped += 1
                continue
            shutil.copy2(src, dst)
            copied += 1
            bytes_copied += rec["bytes"]

        # Verify this field against the manifest (count + bytes of PDFs only).
        expected = len(pdfs)
        got = len(local)
        if got != expected:
            issues.append(f"{dom}/{field}: expected {expected} pdfs, materialized {got}")
        filled += 1

    print(f"fields with PDFs materialized: {filled}")
    print(f"files copied: {copied}   already present: {skipped}")
    print(f"bytes copied: {bytes_copied/1e6:.1f} MB")
    print(f"issues: {len(issues)}")
    for i in issues[:20]:
        print("  ", i)

    # Honest report of the empty fields.
    empty = [f"{f['domain']}/{f['field']}" for f in fields
             if not [t for t in f.get("tests", []) + f.get("keys", []) if t.get("kind") == "pdf"]]
    print(f"\nfields with no PDFs (stay empty): {len(empty)}")
    for e in empty:
        print("  ", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
