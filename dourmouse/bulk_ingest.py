"""dourmouse/bulk_ingest.py — bulk indexing into the shared RAG database
(dourmouse/memory_store.py's SQLite FTS5 store) from two real sources:
the local filesystem and the signed-in user's Google Drive.

Explicit user request, asked plainly and confirmed after being told the
real privacy tradeoff (2026-08-31): index EVERY file, no content-based
filtering — not scoped to "safe" folders, not excluding system files or
credentials-adjacent paths by guesswork. What IS skipped, and why that is
not a content exclusion: a file with no extractable text (a JPEG, an MP4,
a compiled binary) has nothing for a full-text index to search — skipping
those is a capability limit, not a privacy filter. Every skip is counted
and reported, never silent.

Real, honest scope decision made once and stated here rather than buried:
"every file on my laptop" starts the walk at the user's own home
directory (Path.home()), not macOS's own system partition (/System,
/Library, /usr, /bin, ...) — those are Apple's OS files, not the user's,
almost entirely unreadable without elevated privileges, and contain zero
personal content a RAG search would ever want back. This is a practical
boundary on WHERE "my laptop" starts, not a content filter on what gets
indexed once inside it — nothing under the home directory is skipped for
being sensitive, credential-adjacent, or belonging to another app.

Two entry points:
- ``ingest_local_tree(store, root, checkpoint_path)`` — walks a directory
  tree, extracts text per format, remembers each file under source
  "laptop_file". Checkpointed (a JSON file of already-processed absolute
  paths) so a killed/resumed run never re-does completed work.
- ``ingest_drive(store, token, checkpoint_path)`` — pages through the
  signed-in user's real Drive via files.list, reads each file's real
  content (reusing google_services.py's own per-file read logic, not
  reimplementing it), remembers each under source "gdrive_file".
  Checkpointed the same way, by Drive file id.

Both are real, deterministic, no-model-in-the-loop tools (Rule 2.8) with
honest progress reporting to a status JSON file (``_STATUS_SUFFIX``) any
caller can poll without touching the (potentially very long-running)
process itself — the same "persist and let a repeat read be cheap"
discipline project_bookkeeper.py already established for this codebase.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterator

from dourmouse.memory_store import is_duplicate_result
from dourmouse.rag_common import content_hash
from dourmouse.state_file import atomic_write_text, load_json_or_aside

# -- what actually has extractable text ------------------------------------ #

#: Read as plain UTF-8 text, verbatim.
_PLAIN_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".css", ".ini", ".cfg",
    ".toml", ".env", ".sh", ".bash", ".zsh", ".py", ".js", ".jsx", ".ts",
    ".tsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cc", ".go", ".rs",
    ".rb", ".php", ".sql", ".swift", ".kt", ".m", ".mm", ".pl", ".r",
    ".lua", ".vim", ".gradle", ".properties", ".gitignore", ".dockerfile",
    ".makefile", ".conf", ".rtf",
}
#: Needs a real extractor (reused, not reinvented — dourmouse/extract.py).
_PDF_EXTS = {".pdf"}

#: A real, measured cap so one pathological giant log file can't blow up a
#: single SQLite row / dominate the FTS5 index for one "document". Applied
#: per-file, never silently — the truncation is stated in the stored body.
_MAX_TEXT_CHARS = 200_000

_STATUS_SUFFIX = ".status.json"


def _write_status(checkpoint_path: Path, status: dict[str, Any]) -> None:
    status_path = checkpoint_path.with_name(checkpoint_path.stem + _STATUS_SUFFIX)
    status["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except OSError:
        pass  # status reporting is best-effort — never abort a real run over it


def _load_checkpoint(checkpoint_path: Path, log: Callable[[str], None] | None = None) -> set[str]:
    """The set of already-done keys. A checkpoint that cannot be read is kept aside (never overwritten by
    the next save) and the failure is reported through ``log``; the run then starts from nothing. A checkpoint
    that cannot be read at all right now raises ``state_file.StateUnreadable`` (an OSError) and the run does not
    start: it is left in place rather than replaced by a fresh one."""
    value, note = load_json_or_aside(checkpoint_path, list)
    if note:
        if log:
            log(f"WARNING checkpoint: {note} Starting this run without it.")
        else:
            print(f"dourmouse: bulk ingest checkpoint: {note}", file=sys.stderr)
    return {str(k) for k in value} if value else set()


def _save_checkpoint(checkpoint_path: Path, done: set[str]) -> None:
    try:
        atomic_write_text(checkpoint_path, json.dumps(sorted(done)))
    except OSError:
        pass


def _extract_local_text(path: Path) -> str | None:
    """Returns real extracted text, or None if this file has none to give
    (skip, not a filter) — never a fabricated placeholder."""
    ext = path.suffix.lower()
    if ext in _PLAIN_TEXT_EXTS or not ext:
        try:
            # A bounded prefix, never the whole file: an extension-less multi-GB log or binary must not be
            # allocated just to be cut to _MAX_TEXT_CHARS afterwards. 4 bytes per char is the UTF-8 maximum,
            # and one more char than the cap is kept so the caller can tell the file was longer.
            with path.open("rb") as fh:
                raw = fh.read(_MAX_TEXT_CHARS * 4 + 4)
            return raw.decode("utf-8", errors="replace")
        except OSError:
            return None
    if ext in _PDF_EXTS:
        from dourmouse.extract import extract_pdf_text

        text = extract_pdf_text(path)
        if text.startswith("ERROR") or text.startswith("PDF READ FAILED") or "no extractable text" in text:
            return None
        return text
    return None


def iter_local_files(root: Path) -> Iterator[Path]:
    """Every real file under root, deterministic order (sorted per
    directory) so a resumed/re-run walk visits files in the same sequence
    — checkpoint-friendly. No content-based skip here; iter_local_files
    yields every file, extraction decides what has text."""
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = sorted(cur.iterdir(), key=lambda p: p.name)
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue  # never follow symlinks — real cycle/loop risk, no unique content
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    yield entry
            except OSError:
                continue


def ingest_local_tree(
    store: Any,
    root: Path | str,
    checkpoint_path: Path | str,
    *,
    log: Callable[[str], None] | None = None,
    status_every: int = 200,
) -> dict[str, int]:
    """Walk ``root``, remember every file with extractable text into
    ``store`` under source "laptop_file". Resumable via ``checkpoint_path``
    (a JSON set of already-done absolute paths)."""
    root = Path(root).expanduser().resolve()
    checkpoint_path = Path(checkpoint_path)
    done = _load_checkpoint(checkpoint_path, log)
    stats = {
        "scanned": 0, "indexed": 0, "skipped_no_text": 0, "skipped_done": 0,
        "skipped_duplicate": 0, "errors": 0,
    }
    last_saved = 0
    # In-run content-hash dedup (rag_common.content_hash — the same
    # primitive store.remember() uses for its own persisted dedup). Two
    # different paths under this walk with byte-identical text (a copy, a
    # symlink farm, a build output checked into two places) are caught
    # here without a DB round trip; anything that survives this still goes
    # through store.remember()'s cross-run dedup below.
    seen_hashes: set[str] = set()
    for path in iter_local_files(root):
        key = str(path)
        if key in done:
            stats["skipped_done"] += 1
            continue
        stats["scanned"] += 1
        # Save from the top of the loop so the cadence does not depend on a file reaching the bottom (the
        # no-text, duplicate and error branches all `continue`).
        if stats["scanned"] - last_saved >= status_every:
            last_saved = stats["scanned"]
            _save_checkpoint(checkpoint_path, done)
            _write_status(checkpoint_path, {"root": str(root), "phase": "local", **stats})
            if log:
                log(f"local: {stats}")
        try:
            text = _extract_local_text(path)
        except Exception as exc:  # noqa: BLE001 — one bad file must never kill the run
            # Not added to `done`: a transient failure must be retried on the next run, not dropped for good.
            stats["errors"] += 1
            if log:
                log(f"ERROR reading {path}: {type(exc).__name__}: {exc}")
            continue
        if text is None or not text.strip():
            stats["skipped_no_text"] += 1
            done.add(key)
            continue
        body = text[:_MAX_TEXT_CHARS]
        if len(text) > _MAX_TEXT_CHARS:
            body += f"\n\n[TRUNCATED: the file is longer than the {_MAX_TEXT_CHARS:,} chars indexed here]"
        digest = content_hash(body)
        if digest in seen_hashes:
            # Same content already remembered earlier in THIS run (caught
            # before touching the store at all).
            stats["skipped_duplicate"] += 1
            done.add(key)
            continue
        try:
            result = store.remember("laptop_file", key, body)
            if is_duplicate_result(result):
                # Same content already indexed under a different path (a
                # copy/move, or a checkpoint-less re-run of a renamed file)
                # — real dedup, not an error, and not double-counted as
                # "indexed".
                stats["skipped_duplicate"] += 1
            else:
                stats["indexed"] += 1
                seen_hashes.add(digest)
        except Exception as exc:  # noqa: BLE001
            # Not added to `done`: a locked database must not drop this file from the index permanently.
            stats["errors"] += 1
            if log:
                log(f"ERROR storing {path}: {type(exc).__name__}: {exc}")
            continue
        done.add(key)
    _save_checkpoint(checkpoint_path, done)
    _write_status(checkpoint_path, {"root": str(root), "phase": "local", "done": True, **stats})
    return stats


# -- Google Drive ----------------------------------------------------------- #

_DRIVE_API = "https://www.googleapis.com/drive/v3"
_MAX_DRIVE_TEXT = _MAX_TEXT_CHARS
_MAX_DRIVE_BYTES = 8_000_000  # matches google_services.py's own real cap magnitude

_GOOGLE_NATIVE_MIMES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def list_all_drive_files(token: str, *, page_size: int = 1000) -> Iterator[dict[str, Any]]:
    """Every real file in the signed-in user's Drive (trashed excluded —
    a trashed file the user already threw away is not "my files"),
    paginated via the real files.list pageToken contract."""
    from dourmouse.google_services import _http_json

    page_token = None
    while True:
        params = {
            "q": "trashed = false",
            "pageSize": page_size,
            "fields": "nextPageToken, files(id,name,mimeType,modifiedTime,size)",
        }
        if page_token:
            params["pageToken"] = page_token
        data = _http_json("GET", f"{_DRIVE_API}/files?{urllib.parse.urlencode(params)}", token)
        for f in data.get("files") or []:
            yield f
        page_token = data.get("nextPageToken")
        if not page_token:
            return


#: Google 403 reason codes that mean "this file can never be read with this token": a Docs/Sheets export over
#: Google's size limit, or a file whose owner has disabled downloads. A 404 is also permanent. Rate-limit,
#: quota, auth and server failures are NOT in this set: they are retried on the next run.
_PERMANENT_403_REASONS = frozenset({
    "exportsizelimitexceeded", "cannotdownloadfile", "filenotdownloadable", "cannotdownloadabusivefile",
})


def _is_permanent_refusal(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    if status == 404:
        return True
    return status == 403 and str(getattr(exc, "reason", "")).lower() in _PERMANENT_403_REASONS


def _read_drive_file_text(token: str, file_meta: dict[str, Any]) -> str | None:
    from dourmouse.google_services import GoogleApiError, _http_raw

    fid = file_meta["id"]
    mime = str(file_meta.get("mimeType") or "")
    export_mime = _GOOGLE_NATIVE_MIMES.get(mime)
    try:
        if export_mime:
            body = _http_raw(
                "GET",
                f"{_DRIVE_API}/files/{fid}/export?mimeType={urllib.parse.quote(export_mime)}",
                token, max_bytes=_MAX_DRIVE_BYTES,
            ).decode("utf-8", errors="replace")
        elif mime.startswith("application/vnd.google-apps."):
            return None  # folders, forms, sites, ... — no exportable text form
        else:
            size_raw = file_meta.get("size")
            if size_raw and str(size_raw).isdigit() and int(size_raw) > _MAX_DRIVE_BYTES:
                return None
            raw = _http_raw(
                "GET", f"{_DRIVE_API}/files/{fid}?alt=media", token,
                max_bytes=_MAX_DRIVE_BYTES,
            )
            if mime == "application/pdf" or str(file_meta.get("name", "")).lower().endswith(".pdf"):
                import tempfile

                from dourmouse.extract import extract_pdf_text

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
                    tmp.write(raw)
                    tmp.flush()
                    text = extract_pdf_text(tmp.name)
                return None if ("no extractable text" in text or text.startswith("ERROR")) else text
            body = raw.decode("utf-8", errors="replace")
    except GoogleApiError as exc:
        # A file past the byte cap is a genuine "no text we can index" (a skip); anything else (401 after
        # token expiry, 403 rate limit, 5xx, network) is a failure that must reach the caller so it is
        # counted in `errors` and retried, not checkpointed. Classified by the typed status and reason
        # code, never by message text.
        if exc.over_cap:
            return None
        raise
    return body if body.strip() else None


def ingest_drive(
    store: Any,
    token: str,
    checkpoint_path: Path | str,
    *,
    log: Callable[[str], None] | None = None,
    status_every: int = 50,
) -> dict[str, int]:
    """Page through the signed-in user's real Drive, remember every file
    with extractable text into ``store`` under source "gdrive_file".
    Checkpointed by Drive file id (stable across runs, unlike a path)."""
    checkpoint_path = Path(checkpoint_path)
    done = _load_checkpoint(checkpoint_path, log)
    stats = {
        "scanned": 0, "indexed": 0, "skipped_no_text": 0, "skipped_done": 0,
        "skipped_duplicate": 0, "skipped_refused": 0, "errors": 0,
    }
    last_saved = 0
    seen_hashes: set[str] = set()  # see ingest_local_tree's in-run dedup note
    for f in list_all_drive_files(token):
        fid = f.get("id")
        if not fid:
            continue
        if fid in done:
            stats["skipped_done"] += 1
            continue
        stats["scanned"] += 1
        name = str(f.get("name") or fid)
        if stats["scanned"] - last_saved >= status_every:
            last_saved = stats["scanned"]
            _save_checkpoint(checkpoint_path, done)
            _write_status(checkpoint_path, {"phase": "gdrive", **stats})
            if log:
                log(f"gdrive: {stats}")
        try:
            text = _read_drive_file_text(token, f)
        except Exception as exc:  # noqa: BLE001
            if _is_permanent_refusal(exc):
                # Google will never give this file to this token: record it so every future run does not
                # ask again and count an error (W2R-7).
                stats["skipped_refused"] += 1
                done.add(fid)
                if log:
                    log(f"skipped drive file {name!r} ({fid}): Google refuses it permanently ({exc})")
                continue
            # Not added to `done`: an expired token, a rate limit or a 5xx is retried on the next run.
            stats["errors"] += 1
            if log:
                log(f"ERROR reading drive file {name!r} ({fid}): {type(exc).__name__}: {exc}")
            continue
        if text is None or not text.strip():
            stats["skipped_no_text"] += 1
            done.add(fid)
            continue
        body = text[:_MAX_DRIVE_TEXT]
        if len(text) > _MAX_DRIVE_TEXT:
            body += f"\n\n[TRUNCATED — {len(text):,} real chars, indexed first {_MAX_DRIVE_TEXT:,}]"
        title = f"{name} (drive id {fid})"
        digest = content_hash(body)
        if digest in seen_hashes:
            stats["skipped_duplicate"] += 1
            done.add(fid)
            continue
        try:
            result = store.remember("gdrive_file", title, body)
            if is_duplicate_result(result):
                # Same content already indexed under a different Drive file
                # (e.g. "Make a copy" duplicates, or the same file re-shared
                # with a new id) — real dedup, not an error.
                stats["skipped_duplicate"] += 1
            else:
                stats["indexed"] += 1
                seen_hashes.add(digest)
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            if log:
                log(f"ERROR storing drive file {name!r}: {type(exc).__name__}: {exc}")
            continue
        done.add(fid)
    _save_checkpoint(checkpoint_path, done)
    _write_status(checkpoint_path, {"phase": "gdrive", "done": True, **stats})
    return stats
