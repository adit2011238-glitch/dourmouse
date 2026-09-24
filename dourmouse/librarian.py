"""The file librarian (OS-5, finding #115): always on, never prompted.

The founding spec's first capability: an agent that "perpetually organizes
the user's files ... and handles data requests from other agents ... all
the time without any prompting". It is the standing-agent runtime's first
tenant (INFRA-1, #114), and it keeps to that runtime's leash:

- **It reads and proposes; it never moves a file on its own.** Each tick
  indexes a bounded batch of files under an explicit allowlist of folders
  (default ~/Documents, ~/Desktop, ~/Downloads; ``DOURMOUSE_LIBRARIAN_ROOTS``
  overrides; never the whole disk) and refreshes its organization proposals.
  Applying a proposal is a separate, approval-gated action, and every move
  is written to an undo record first. Nothing is ever deleted: duplicates and
  clutter go to an archive folder the owner can look through.
- **Incremental memory.** One row per file (path, size, mtime, kind, content
  hash when needed), first/last seen, missing flag; a file whose size and
  mtime did not change is not re-read.
- **Answers other agents.** A bus message "find <words>" (or any text) gets
  the best-matching files back; chat uses the same search.

Folders macOS protects (Desktop, Documents, Downloads) need the app to have
been allowed access; a folder it cannot read is reported as such.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir

ROOTS_ENV = "DOURMOUSE_LIBRARIAN_ROOTS"
SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", ".venv", "dist", "build", "target", "Library",
                       "Applications", "System", ".git", ".Trash"})
BATCH = 2000  # files stat'ed per tick: bounded work, the runtime's rule
HASH_BUDGET_BYTES = 200 * 1024 * 1024  # content hashed per tick, only for duplicate candidates
OLD_DOWNLOAD_DAYS = 30
INSTALLER_DAYS = 7

KINDS = {
    "document": {"pdf", "doc", "docx", "pages", "txt", "md", "rtf", "odt", "tex", "epub"},
    "spreadsheet": {"xls", "xlsx", "csv", "numbers", "ods", "tsv"},
    "presentation": {"ppt", "pptx", "key", "odp"},
    "image": {"png", "jpg", "jpeg", "gif", "heic", "webp", "svg", "bmp", "tiff", "raw"},
    "audio": {"mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "aiff"},
    "video": {"mp4", "mov", "m4v", "mkv", "avi", "webm", "wmv"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"},
    "installer": {"dmg", "pkg", "mpkg", "iso", "exe", "msi"},
    "code": {"py", "js", "ts", "tsx", "jsx", "java", "c", "cpp", "h", "rs", "go", "rb", "swift", "kt", "sh",
             "ipynb", "html", "css", "json", "yaml", "yml", "toml", "sql", "r"},
}
_KIND_OF = {ext: kind for kind, exts in KINDS.items() for ext in exts}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, root TEXT NOT NULL, name TEXT NOT NULL, ext TEXT NOT NULL, kind TEXT NOT NULL,
  size INTEGER NOT NULL, mtime REAL NOT NULL, sha256 TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  missing INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_name ON files(name);
CREATE INDEX IF NOT EXISTS files_size ON files(size);
CREATE TABLE IF NOT EXISTS moves (
  id INTEGER PRIMARY KEY AUTOINCREMENT, proposal TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
  at REAL NOT NULL, undone INTEGER NOT NULL DEFAULT 0
);
"""


def kind_of(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _KIND_OF.get(ext, "other")


def configured_roots() -> list[Path]:
    raw = os.environ.get(ROOTS_ENV, "").strip()
    if raw:
        parts = [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
    else:
        home = Path.home()
        parts = [home / "Documents", home / "Desktop", home / "Downloads"]
    return [p for p in parts if p.is_dir()]


_COPY_RE = re.compile(r"(^copy of |\bcopy\b|\(\d+\)|\s\d+$| - copy)", re.I)


def _looks_like_copy(path: str) -> bool:
    return bool(_COPY_RE.search(Path(path).stem))


def archive_root() -> Path:
    return Path.home() / "Documents" / "Dourmouse Archive"


class Librarian:
    name = "librarian"
    capabilities = frozenset({"read", "propose"})

    def __init__(self, db: Path | None = None, roots: list[Path] | None = None, interval_s: float = 120.0,
                 now: Any = time.time) -> None:
        self._db = db or workspace_dir() / "librarian" / "index.db"
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._roots = roots
        self.interval_s = interval_s
        self._now = now
        self._lock = threading.Lock()
        self._walk: Any = None  # the in-progress walk generator, resumed each tick
        self._walk_seen: set[str] = set()
        self._root_errors: dict[str, str] = {}
        self.full_passes = 0
        # Called with proposals that are new since the last full pass (the
        # server turns them into one notification, finding #119).
        self.on_new_proposals: Any = None
        self._notified: set[str] = set()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db, timeout=10)

    def roots(self) -> list[Path]:
        return self._roots if self._roots is not None else configured_roots()

    # ------------------------------------------------------------ indexing
    def _iter_files(self) -> Any:
        for root in self.roots():
            try:
                os.listdir(root)
            except OSError as exc:
                self._root_errors[str(root)] = (
                    "macOS has not allowed Dourmouse to read this folder (System Settings > Privacy & Security "
                    "> Files and Folders)" if isinstance(exc, PermissionError) else str(exc))
                continue
            self._root_errors.pop(str(root), None)
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                               and not d.endswith((".app", ".photoslibrary", ".bundle"))]
                for name in filenames:
                    if not name.startswith("."):
                        yield root, Path(dirpath) / name

    def index_batch(self, batch: int = BATCH) -> dict[str, int]:
        """Stat up to ``batch`` files and record them; at the end of a full
        pass, mark files not seen as missing. Returns counts."""
        now = self._now()
        if self._walk is None:
            self._walk, self._walk_seen = self._iter_files(), set()
        added = changed = 0
        done = False
        rows: list[tuple[Any, ...]] = []
        with self._lock, self._conn() as c:
            for _ in range(batch):
                try:
                    root, path = next(self._walk)
                except StopIteration:
                    done = True
                    break
                try:
                    st = path.stat()
                except OSError:
                    continue
                p = str(path)
                self._walk_seen.add(p)
                old = c.execute("SELECT size, mtime FROM files WHERE path=?", (p,)).fetchone()
                if old is None:
                    added += 1
                elif (old[0], old[1]) != (st.st_size, st.st_mtime):
                    changed += 1
                elif old is not None:
                    c.execute("UPDATE files SET last_seen=?, missing=0 WHERE path=?", (now, p))
                    continue
                rows.append((p, str(root), path.name, path.suffix.lstrip(".").lower(), kind_of(path.name),
                             st.st_size, st.st_mtime, now, now))
            c.executemany(
                "INSERT INTO files(path, root, name, ext, kind, size, mtime, sha256, first_seen, last_seen, missing) "
                "VALUES(?,?,?,?,?,?,?,NULL,?,?,0) ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                "mtime=excluded.mtime, sha256=NULL, last_seen=excluded.last_seen, missing=0, kind=excluded.kind",
                rows)
            missing = 0
            if done:
                roots = [str(r) for r in self.roots() if str(r) not in self._root_errors]
                for (p,) in c.execute("SELECT path FROM files WHERE missing=0").fetchall():
                    if p not in self._walk_seen and any(p.startswith(r + os.sep) for r in roots):
                        c.execute("UPDATE files SET missing=1 WHERE path=?", (p,))
                        missing += 1
                self._walk = None
                self.full_passes += 1
        return {"added": added, "changed": changed, "missing": missing, "pass_complete": int(done)}

    def hash_candidates(self, budget: int = HASH_BUDGET_BYTES) -> int:
        """Hash only files that share a size with another file (the only
        possible duplicates), within a byte budget per tick."""
        hashed = 0
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT path, size FROM files WHERE missing=0 AND sha256 IS NULL AND size > 0 AND size IN "
                "(SELECT size FROM files WHERE missing=0 GROUP BY size HAVING COUNT(*) > 1) ORDER BY size DESC"
            ).fetchall()
            for path, size in rows:
                if budget - size < 0 and hashed:
                    break
                h = hashlib.sha256()
                try:
                    with open(path, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                except OSError:
                    continue
                c.execute("UPDATE files SET sha256=? WHERE path=?", (h.hexdigest(), path))
                budget -= size
                hashed += 1
        return hashed

    # -------------------------------------------------------------- queries
    def find(self, query: str, limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
        if not words and not kind:
            return []
        sql = "SELECT path, name, kind, size, mtime FROM files WHERE missing=0"
        args: list[Any] = []
        for w in words:
            sql += " AND lower(path) LIKE ?"
            args.append(f"%{w}%")
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY mtime DESC LIMIT 400", args).fetchall()

        def score(r: tuple[Any, ...]) -> tuple[int, float]:
            name = r[1].lower()
            return (-sum(3 if w in name else 1 for w in words), -r[4])

        rows.sort(key=score)
        return [{"path": r[0], "name": r[1], "kind": r[2], "size": r[3], "modified": r[4]} for r in rows[:limit]]

    def stats(self) -> dict[str, Any]:
        with self._conn() as c:
            total, size = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE missing=0").fetchone()
            kinds = dict(c.execute("SELECT kind, COUNT(*) FROM files WHERE missing=0 GROUP BY kind").fetchall())
            missing = c.execute("SELECT COUNT(*) FROM files WHERE missing=1").fetchone()[0]
        return {"files": total, "bytes": size, "kinds": kinds, "missing": missing,
                "roots": [str(r) for r in self.roots()], "root_errors": dict(self._root_errors),
                "full_passes": self.full_passes}

    # ------------------------------------------------------------ proposals
    def proposals(self) -> list[dict[str, Any]]:
        """Suggestions only. Each lists the exact moves it would make."""
        now = self._now()
        out: list[dict[str, Any]] = []
        with self._conn() as c:
            dup_rows = c.execute(
                "SELECT sha256, path, size, mtime FROM files WHERE missing=0 AND sha256 IN (SELECT sha256 FROM files "
                "WHERE missing=0 AND sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*) > 1) ORDER BY size DESC"
            ).fetchall()
            downloads = str(Path.home() / "Downloads")
            in_downloads = "missing=0 AND path LIKE ? AND instr(substr(path, ?), ?) = 0"
            where = (downloads + os.sep + "%", len(downloads) + 2, os.sep)  # top level of Downloads only
            # in_downloads is a constant fragment; every value is a bound parameter.
            inst = c.execute(f"SELECT path, kind, mtime FROM files WHERE {in_downloads} AND kind='installer' "  # noqa: S608
                             "AND mtime < ? ORDER BY mtime", (*where, now - INSTALLER_DAYS * 86400)).fetchall()
            rest = c.execute(f"SELECT path, kind, mtime FROM files WHERE {in_downloads} AND kind!='installer' "  # noqa: S608
                             "AND mtime < ? ORDER BY mtime", (*where, now - OLD_DOWNLOAD_DAYS * 86400)).fetchall()
        groups: dict[str, list[tuple[str, int, float]]] = {}
        for sha, path, size, mtime in dup_rows:
            groups.setdefault(sha, []).append((path, size, mtime))
        dup_moves = []
        for sha, members in list(groups.items())[:200]:
            # Keep the original: not named like a copy, then the oldest, then the shortest path.
            ps = [m[0] for m in sorted(members, key=lambda m: (_looks_like_copy(m[0]), m[2], len(m[0]), m[0]))]
            size = members[0][1]
            for extra in ps[1:]:
                dup_moves.append({"src": extra, "dst": str(archive_root() / "Duplicates" / sha[:12] / Path(extra).name),
                                  "why": f"same content as {ps[0]}", "bytes": size})
        if dup_moves:
            out.append({"id": "duplicates", "title": f"Move {len(dup_moves)} duplicate file(s) to the archive",
                        "detail": "Each is byte-for-byte identical to a file that stays where it is.",
                        "bytes": sum(m["bytes"] for m in dup_moves), "moves": dup_moves})
        if inst:
            out.append({"id": "old-installers", "title": f"Archive {len(inst)} old installer(s) from Downloads",
                        "detail": f"Disk images and packages older than {INSTALLER_DAYS} days are usually already installed.",
                        "moves": [{"src": p, "dst": str(archive_root() / "Installers" / Path(p).name), "why": "old installer"}
                                  for p, _k, _m in inst]})
        if rest:
            out.append({"id": "old-downloads", "title": f"File {len(rest)} download(s) older than {OLD_DOWNLOAD_DAYS} days by type",
                        "detail": "Moves them into Dourmouse Archive/Downloads/<type>/<year-month>/ so Downloads holds only recent files.",
                        "moves": [{"src": p, "why": f"untouched since {time.strftime('%Y-%m-%d', time.localtime(m))}",
                                   "dst": str(archive_root() / "Downloads" / k / time.strftime("%Y-%m", time.localtime(m)) / Path(p).name)}
                                  for p, k, m in rest]})
        return out

    def apply(self, proposal_id: str) -> dict[str, Any]:
        """Carry out one proposal (the caller is the approval gate). Moves
        only; never overwrites; every move recorded for undo."""
        prop = next((p for p in self.proposals() if p["id"] == proposal_id), None)
        if prop is None:
            return {"ok": False, "error": f"no current proposal {proposal_id!r}"}
        moved, skipped = 0, []
        with self._lock, self._conn() as c:
            for m in prop["moves"]:
                src, dst = Path(m["src"]), Path(m["dst"])
                if not src.exists():
                    skipped.append(f"{src}: gone")
                    continue
                if dst.exists():
                    dst = dst.with_name(f"{dst.stem} ({int(time.time())}){dst.suffix}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                c.execute("INSERT INTO moves(proposal, src, dst, at) VALUES(?,?,?,?)", (proposal_id, str(src), str(dst), time.time()))
                c.commit()
                try:
                    shutil.move(str(src), str(dst))
                except OSError as exc:
                    c.execute("DELETE FROM moves WHERE id=(SELECT MAX(id) FROM moves)")
                    skipped.append(f"{src}: {exc}")
                    continue
                c.execute("UPDATE files SET missing=1 WHERE path=?", (str(src),))
                moved += 1
        return {"ok": True, "moved": moved, "skipped": skipped, "undo": f"librarian_undo {proposal_id}"}

    def undo(self, proposal_id: str) -> dict[str, Any]:
        restored, skipped = 0, []
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT id, src, dst FROM moves WHERE proposal=? AND undone=0 ORDER BY id DESC",
                             (proposal_id,)).fetchall()
            for mid, src, dst in rows:
                if Path(src).exists():
                    skipped.append(f"{src}: something new is there now")
                    continue
                if not Path(dst).exists():
                    skipped.append(f"{dst}: no longer in the archive")
                    continue
                Path(src).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(dst, src)
                c.execute("UPDATE moves SET undone=1 WHERE id=?", (mid,))
                c.execute("UPDATE files SET missing=0 WHERE path=?", (src,))
                restored += 1
        return {"ok": True, "restored": restored, "skipped": skipped}

    # ------------------------------------------------- standing-agent hooks
    def tick(self) -> str:
        r = self.index_batch()
        hashed = self.hash_candidates()
        if r["pass_complete"] and self.on_new_proposals is not None:
            props = self.proposals()
            ids = {p["id"] for p in props}
            new = [p for p in props if p["id"] not in self._notified]
            self._notified = ids
            if new:
                self.on_new_proposals(new)
        if not (r["added"] or r["changed"] or r["missing"] or hashed):
            return ""
        return (f"indexed +{r['added']} new, {r['changed']} changed, {r['missing']} gone"
                + (f", hashed {hashed} duplicate candidate(s)" if hashed else "")
                + ("; full pass complete" if r["pass_complete"] else ""))

    def handle_message(self, subject: str, body: str, sender: str) -> str | None:
        text = f"{subject} {body}".strip()
        query = re.sub(r"^(re:\s*)?(find|where is|where are|locate|search)\s*", "", text, flags=re.I)
        hits = self.find(query, limit=10)
        if not hits:
            return f"No files match {query!r} in {', '.join(str(r) for r in self.roots())}."
        return json.dumps([{k: h[k] for k in ("path", "kind", "size")} for h in hits])


def librarian_enabled() -> bool:
    return os.environ.get("DOURMOUSE_LIBRARIAN", "1").strip().lower() not in ("0", "false", "no", "off")


_shared: Librarian | None = None
_shared_lock = threading.Lock()


def get_librarian() -> Librarian:
    """One librarian per process: the standing loop and the chat tools share
    its index."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Librarian()
        return _shared


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def librarian_tools() -> list[Any]:
    """Chat tools, registered on admin_ops (file organization)."""
    from dourmouse.dispatch import Permission, ToolSpec

    def _find(a: dict[str, Any]) -> str:
        lib = get_librarian()
        if lib.stats()["files"] == 0:
            lib.index_batch(20000)
        hits = lib.find(str(a.get("query") or ""), limit=int(a.get("limit") or 15), kind=a.get("kind") or None)
        if not hits:
            return "No indexed file matches that."
        return "\n".join(f"{h['path']}  ({h['kind']}, {_fmt_size(h['size'])}, "
                         f"{time.strftime('%Y-%m-%d', time.localtime(h['modified']))})" for h in hits)

    def _status(_a: dict[str, Any]) -> str:
        st = get_librarian().stats()
        lines = [f"{st['files']} files ({_fmt_size(st['bytes'])}) indexed in: {', '.join(st['roots'])}; "
                 f"{st['full_passes']} full pass(es) this session."]
        lines.append("By kind: " + ", ".join(f"{k} {v}" for k, v in sorted(st["kinds"].items(), key=lambda kv: -kv[1])))
        lines += [f"Cannot read {r}: {e}" for r, e in st["root_errors"].items()]
        return "\n".join(lines)

    def _proposals(_a: dict[str, Any]) -> str:
        props = get_librarian().proposals()
        if not props:
            return "Nothing to organize right now."
        out = []
        for p in props:
            out.append(f"[{p['id']}] {p['title']}. {p['detail']}")
            out += [f"    {m['src']} -> {m['dst']}" for m in p["moves"][:8]]
            if len(p["moves"]) > 8:
                out.append(f"    ... and {len(p['moves']) - 8} more")
        return "\n".join(out)

    def _apply(a: dict[str, Any]) -> str:
        r = get_librarian().apply(str(a.get("proposal_id") or ""))
        if not r["ok"]:
            return "Not done: " + r["error"]
        return f"Moved {r['moved']} file(s)." + (" Skipped: " + "; ".join(r["skipped"][:10]) if r["skipped"] else "") \
            + f" Undo with librarian_undo {a.get('proposal_id')}."

    def _undo(a: dict[str, Any]) -> str:
        r = get_librarian().undo(str(a.get("proposal_id") or ""))
        return f"Put back {r['restored']} file(s)." + (" Skipped: " + "; ".join(r["skipped"][:10]) if r["skipped"] else "")

    pid = {"type": "object", "properties": {"proposal_id": {"type": "string"}}, "required": ["proposal_id"]}
    return [
        ToolSpec(name="librarian_find",
                 description="Find the user's files on this Mac by words in the name or folder path, "
                             "optionally by kind (document, spreadsheet, image, audio, video, archive, installer, code).",
                 parameters={"type": "object", "properties": {"query": {"type": "string"}, "kind": {"type": "string"},
                                                              "limit": {"type": "integer", "default": 15}}},
                 handler=_find),
        ToolSpec(name="librarian_status", description="What the file librarian has indexed, and any folder it cannot read.",
                 parameters={"type": "object", "properties": {}}, handler=_status),
        ToolSpec(name="librarian_proposals",
                 description="The librarian's suggestions for tidying files (duplicates, old installers, old "
                             "downloads), each with the exact moves. Suggestions only; nothing moves until approved.",
                 parameters={"type": "object", "properties": {}}, handler=_proposals),
        ToolSpec(name="librarian_apply", description="Carry out one librarian proposal (moves only, into the archive; undoable).",
                 parameters=pid, handler=_apply, permission=Permission.REQUIRES_CONFIRMATION,
                 confirm_prompt=lambda a: f"Carry out the librarian's '{a.get('proposal_id')}' proposal? Files move "
                                          "into the Dourmouse Archive folder; nothing is deleted and it can be undone."),
        ToolSpec(name="librarian_undo", description="Put back every file a librarian proposal moved.",
                 parameters=pid, handler=_undo, permission=Permission.REQUIRES_CONFIRMATION,
                 confirm_prompt=lambda a: f"Put back the files moved by '{a.get('proposal_id')}'?"),
    ]
