"""Long-term memory store (Phase A1) — SQLite FTS5 full-text retrieval.

Upgrades the ``memory`` subagent from filesystem grep to a real retrieval
layer: a SQLite database with an FTS5 full-text index over remembered facts
and ingested knowledge (session ledgers + Obsidian notes). Deterministic,
stdlib-only (``sqlite3``), zero new dependencies.

- ``remember(source, title, body)`` — upsert a fact/note by (source, title).
- ``search(query, limit)`` — FTS5-ranked full-text search returning
  {source, title, snippet, score}.
- ``ingest_session_file(path)`` / ``ingest_vault(root)`` — bulk-index what
  the system already knows so agents can recall it.

Honest degradation (Rule 2.2): if FTS5 is unavailable on the running
Python's sqlite3 build, opening the store raises ``MemoryStoreUnavailable``
and the tools report NOT CONFIGURED — never a silent grep-fallback pretending
to be search.

Design: an external-content FTS5 table keeps ranking/search in SQLite while
a plain ``facts`` table owns the data (UNIQUE(source, title) upserts), with
the standard FTS5 rowid sync pattern. Thread-safe via a lock + per-thread
connection access guarded by ``check_same_thread=False``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dourmouse.rag_common import content_hash as _content_hash

_DEFAULT_DIR_NAME = "memory"
_DEFAULT_DB_NAME = "atlas_memory.db"

#: Prefix on remember()'s return string when content-hash dedup found an
#: existing fact with identical content under a DIFFERENT (source, title)
#: key and skipped the insert (Cycle 1 RAG storage audit — the confirmed
#: gap: zero content-dedup logic existed anywhere in the RAG stack before
#: this). Callers (bulk_ingest.py) check this via is_duplicate_result()
#: instead of re-deriving the string.
DUPLICATE_PREFIX = "MEMORY DUPLICATE"


def is_duplicate_result(result: str) -> bool:
    """True if a remember() call was skipped as a content-hash duplicate."""
    return isinstance(result, str) and result.startswith(DUPLICATE_PREFIX)


#: PL-06 (plan 2.4). Columns added to ``facts``. Facts the owner wrote or the model chose to keep are kind
#: ``fact`` (or ``preference``, ``person``, ``project``, ``instruction``, ``event``); the ledgers, vault notes,
#: repo index and ratings the store also holds are kind ``record``: searchable, never shown as "what it knows".
_PL06_COLUMNS = (
    ("kind", "TEXT"),
    ("origin", "TEXT"),
    ("valid_from", "TEXT"),
    ("valid_to", "TEXT"),
    ("superseded_by", "INTEGER"),
    ("pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("hidden", "INTEGER NOT NULL DEFAULT 0"),
    ("uses", "INTEGER NOT NULL DEFAULT 0"),
    ("last_used", "TEXT"),
    ("reviewed", "INTEGER NOT NULL DEFAULT 1"),
    ("project", "TEXT NOT NULL DEFAULT ''"),
)

KINDS = ("preference", "fact", "person", "project", "instruction", "event")
ORIGINS = ("you_said", "learned_from_chat", "from_file", "from_mail", "from_research")

#: Sources the store fills by itself. They are records, not things the owner was told to remember.
_RECORD_PREFIXES = ("session:", "repo", "vault", "feedback", "history", "device_wiki", "bulk", "import", "bus",
                    "chat_history", "claude_history", "codex_history", "manual_upload", "laptop_file", "gdrive_file")
PINNED_RECALL_CAP = 3


def classify_kind(source: str) -> str:
    """Deterministic, no model call: sources the store fills by itself are ``record``, everything else ``fact``."""
    src = (source or "").lower()
    return "record" if src.startswith(_RECORD_PREFIXES) else "fact"


def classify_origin(source: str) -> str:
    src = (source or "").lower()
    if src.startswith(("session:", "chat_history", "claude_history", "codex_history", "bus")):
        return "learned_from_chat"
    if src.startswith(("mail", "gmail", "email")):
        return "from_mail"
    if src.startswith(("research", "paper")):
        return "from_research"
    if src.startswith(("vault", "repo", "device_wiki", "bulk", "import", "history", "file", "manual_upload",
                       "laptop_file", "gdrive_file")):
        return "from_file"
    if src in ("you", "owner", "user", "ui"):
        return "you_said"
    return "learned_from_chat"


class MemoryStoreUnavailable(RuntimeError):
    """Raised when SQLite FTS5 is not available — honest NOT CONFIGURED."""


class MemoryStore:
    """SQLite + FTS5 long-term memory store (deterministic, stdlib-only)."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        # Real bug found live (2026-08-31): this store is now genuinely
        # opened by MULTIPLE SEPARATE PROCESSES at once — the live webui
        # server plus a bulk_ingest.py scan running for hours in the
        # background, both writing the same file. SQLite's default
        # rollback-journal mode holds an exclusive lock for the whole
        # duration of a write, and the plain sqlite3.connect() default
        # busy behavior is to raise "database is locked" immediately
        # rather than wait — reproduced live: a manual RAG upload arriving
        # mid-scan raised MemoryStoreUnavailable at the FTS5 schema PROBE
        # itself, not just on a write. WAL mode lets readers and one
        # writer proceed concurrently instead of blocking each other for
        # the transaction's whole duration; the connect()-level `timeout`
        # above (Python's busy_timeout equivalent) covers the remaining
        # writer-vs-writer window by retrying for up to 30s instead of
        # failing on the first collision. Both are real fixes for real
        # multi-process contention, not a workaround for a test flake.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass  # best-effort — an unsupported build still works, just without the concurrency headroom
        try:
            self._init_schema()
        except sqlite3.OperationalError as exc:
            self._conn.close()
            raise MemoryStoreUnavailable(
                f"SQLite FTS5 is not available on this build ({exc}) — "
                "long-term memory is NOT CONFIGURED. No search performed."
            ) from exc

    # -- schema ------------------------------------------------------------ #

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            # Probe FTS5 availability first so a missing build fails loudly.
            cur.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(probe)")
            cur.execute("DROP TABLE IF EXISTS _fts_probe")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source, title)
                )
                """
            )
            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                    source, title, body,
                    content='facts',
                    content_rowid='id'
                )
                """
            )
            # Content-hash dedup column (Cycle 1 RAG audit fix). ALTER TABLE
            # ADD COLUMN is additive and safe against an existing on-disk DB
            # from before this fix shipped — checked via table_info rather
            # than a bare ADD COLUMN so re-opening an already-migrated store
            # never raises "duplicate column name".
            cur.execute("PRAGMA table_info(facts)")
            existing_cols = {row[1] for row in cur.fetchall()}
            if "content_hash" not in existing_cols:
                cur.execute("ALTER TABLE facts ADD COLUMN content_hash TEXT")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_source_hash "
                "ON facts(source, content_hash)"
            )
            # Backfill: rows written before this fix (or the ALTER above on
            # first migration) have no content_hash yet. Compute it once so
            # dedup covers the whole table, not just facts written after
            # this deploy.
            stale = cur.execute(
                "SELECT id, body FROM facts WHERE content_hash IS NULL"
            ).fetchall()
            for row in stale:
                cur.execute(
                    "UPDATE facts SET content_hash = ? WHERE id = ?",
                    (_content_hash(row["body"]), row["id"]),
                )
            # PL-06 (plan 2.4): fact properties, additive like content_hash above. Rows that already exist keep
            # every value they had: they are facts the owner can see, already reviewed, still valid.
            for col, ddl in _PL06_COLUMNS:
                if col not in existing_cols:
                    cur.execute(f"ALTER TABLE facts ADD COLUMN {col} {ddl}")
            for row in cur.execute("SELECT id, source FROM facts WHERE kind IS NULL").fetchall():
                cur.execute("UPDATE facts SET kind = ?, origin = ? WHERE id = ?",
                            (classify_kind(row["source"]), classify_origin(row["source"]), row["id"]))
            cur.execute("CREATE INDEX IF NOT EXISTS idx_facts_kind ON facts(kind, hidden)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id INTEGER NOT NULL,
                    at TEXT NOT NULL,
                    change TEXT NOT NULL,
                    before TEXT NOT NULL DEFAULT '',
                    after TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fact_history_fact ON fact_history(fact_id)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_uses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id INTEGER NOT NULL,
                    session TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    at TEXT NOT NULL
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fact_uses_fact ON fact_uses(fact_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fact_uses_turn ON fact_uses(session, turn)")
            # v4.1 (P6): optional semantic-recall layer. Cached embeddings for
            # fact bodies (one row per fact, json vector). Empty by design —
            # the layer is populated lazily only when DOURMOUSE_EMBED is on.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_embeddings (
                    fact_id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    vector TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Keep the FTS index in sync with the facts table.
            cur.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO facts_fts(rowid, source, title, body)
                    VALUES (new.id, new.source, new.title, new.body);
                END;
                CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, source, title, body)
                    VALUES ('delete', old.id, old.source, old.title, old.body);
                END;
                CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, source, title, body)
                    VALUES ('delete', old.id, old.source, old.title, old.body);
                    INSERT INTO facts_fts(rowid, source, title, body)
                    VALUES (new.id, new.source, new.title, new.body);
                END;
                """
            )
            self._conn.commit()

    # -- write ------------------------------------------------------------- #

    def remember(self, source: str, title: str, body: str, *, kind: str | None = None, origin: str | None = None,
                 project: str = "") -> str:
        """Upsert one fact/note; returns a plain confirmation.

        ``kind`` and ``origin`` are guessed from the source when not given (``classify_kind``, ``classify_origin``).
        A new fact the owner did not say is put in the review queue. Changing the body of an existing fact keeps
        the earlier text in its history."""
        source = (source or "agent").strip()[:200]
        title = (title or "").strip()[:500]
        body = (body or "").strip()
        if not title or not body:
            raise ValueError("remember requires a non-empty title and body")
        now = datetime.now().isoformat(timespec="seconds")
        content_hash = _content_hash(body)
        with self._lock:
            cur = self._conn.cursor()
            # Unchanged: the same (source, title) already holds this exact content. Nothing to write, and
            # above all the cached embedding must survive: ChatSession re-ingests the whole session ledger
            # after every turn, which used to rewrite every old fact, rerun the FTS triggers and wipe every
            # session vector each time.
            same = cur.execute(
                "SELECT 1 FROM facts WHERE source = ? AND title = ? AND content_hash = ?",
                (source, title, content_hash),
            ).fetchone()
            if same is not None:
                return f"MEMORY STORED: [{source}] {title}"
            # Content-hash dedup (Cycle 1 RAG audit fix): the (source, title)
            # UNIQUE constraint below only catches a literal re-remember of
            # the SAME key (an upsert, handled by ON CONFLICT). It never
            # caught identical content arriving under a DIFFERENT title —
            # the real, confirmed gap (e.g. a resumed bulk_ingest checkpoint
            # re-scanning a moved/renamed file, or the same note ingested
            # from two paths). Scoped to the same source: two different
            # sources legitimately holding the same text (e.g. a vault note
            # quoted inside a session ledger) are not duplicates of each
            # other.
            dup = cur.execute(
                "SELECT source, title FROM facts "
                "WHERE source = ? AND content_hash = ? AND title != ?",
                (source, content_hash, title),
            ).fetchone()
            if dup is not None:
                return (
                    f"{DUPLICATE_PREFIX}: identical content already stored "
                    f"as [{dup['source']}] {dup['title']} — skipped re-insert"
                )
            kind = kind if kind in KINDS + ("record",) else classify_kind(source)
            origin = origin if origin in ORIGINS else classify_origin(source)
            reviewed = 0 if (kind != "record" and origin != "you_said") else 1
            before = cur.execute("SELECT id, body FROM facts WHERE source = ? AND title = ?", (source, title)).fetchone()
            cur.execute(
                """
                INSERT INTO facts(source, title, body, content_hash, created_at, updated_at,
                                  kind, origin, valid_from, reviewed, project)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, title) DO UPDATE SET
                    body = excluded.body,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (source, title, body, content_hash, now, now, kind, origin, now, reviewed, str(project or "")[:80]),
            )
            if before is not None and kind != "record":
                self._log(cur, before["id"], "edited", before["body"], body)
            # v4.1 (P6): an updated fact body invalidates its cached embedding
            # so semantic recall never scores against a stale vector (the
            # next semantic_search re-embeds it lazily).
            cur.execute(
                "DELETE FROM fact_embeddings WHERE fact_id = "
                "(SELECT id FROM facts WHERE source = ? AND title = ?)",
                (source, title),
            )
            self._conn.commit()
        return f"MEMORY STORED: [{source}] {title}"

    def delete(self, source: str, title: str) -> bool:
        """Delete one fact by (source, title); returns whether it existed."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM facts WHERE source = ? AND title = ?",
                (source, title),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # -- read -------------------------------------------------------------- #

    def search(
        self, query: str, limit: int = 10, source: str | None = None, project: str = ""
    ) -> list[dict[str, Any]]:
        """FTS5-ranked full-text search; returns real matches only.

        ``source`` restricts matches to EXACTLY one fact source (e.g. ``repo``
        or a scoped ``repo:myproj``) via an equality predicate on the facts
        table — NOT an FTS5 column filter. FTS5 column-filter matching is
        token-based, so a filter like ``source:"repo"`` also matches
        ``repo:myproj`` (both tokenize to contain the token "repo") and
        would leak every project into the default scope. Scoping must be
        exact, so it happens on the joined row, after FTS rank.
        """
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 50))
        match_expr = _fts_query(query)
        if not match_expr:
            return []
        src = str(source).strip() if source else None
        sql = """
            SELECT f.id AS id,
                   f.source AS source,
                   f.title AS title,
                   snippet(facts_fts, 2, '[', ']', '…', 8) AS snippet,
                   bm25(facts_fts) AS score
            FROM facts_fts
            JOIN facts f ON f.id = facts_fts.rowid
            WHERE facts_fts MATCH ?
              AND (? IS NULL OR f.source = ?)
              AND f.hidden = 0
              AND (f.valid_to IS NULL OR f.valid_to > ?)
              AND (f.project = '' OR f.project = ?)
            ORDER BY score
            LIMIT ?
        """
        with self._lock:
            rows = self._conn.execute(
                sql, (match_expr, src, src, _now_iso(), str(project or ""), limit)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "source": r["source"],
                "title": r["title"],
                "snippet": r["snippet"],
                "score": round(r["score"], 4),
            }
            for r in rows
        ]

    def count(self, source: str | None = None) -> int:
        """Total facts, or facts for one source (used by the repo index)."""
        with self._lock:
            if source is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM facts"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM facts WHERE source = ?", (source,)
                ).fetchone()
            return int(row["n"])

    def get(self, source: str, title: str) -> dict[str, Any] | None:
        """One fact by (source, title), or None. Used for idempotent scans."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, source, title, body, created_at, updated_at "
                "FROM facts WHERE source = ? AND title = ?",
                (source, title),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "source": row["source"],
            "title": row["title"],
            "body": row["body"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def all_facts(self) -> list[dict[str, Any]]:
        """Every fact (id/source/title/body) — the semantic layer's corpus."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, source, title, body FROM facts ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "source": r["source"],
                "title": r["title"],
                "body": r["body"],
            }
            for r in rows
        ]

    # -- semantic-layer embeddings (v4.1, P6) --------------------------- #

    def save_embedding(self, fact_id: int, model: str, vector: list[float]) -> None:
        """Cache one fact's embedding vector (upsert by fact_id)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO fact_embeddings(fact_id, model, vector, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(fact_id) DO UPDATE SET
                    model = excluded.model,
                    vector = excluded.vector,
                    updated_at = excluded.updated_at
                """,
                (fact_id, model, json.dumps(vector), datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def get_embeddings(self, model: str | None = None) -> dict[int, list[float]]:
        """fact_id -> vector for every cached embedding, or only those made by ``model`` when given
        (vectors from another embedding model are in a different space and must not be mixed in)."""
        with self._lock:
            if model is None:
                rows = self._conn.execute(
                    "SELECT fact_id, vector FROM fact_embeddings"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT fact_id, vector FROM fact_embeddings WHERE model = ?", (model,)
                ).fetchall()
        out: dict[int, list[float]] = {}
        for r in rows:
            try:
                vec = json.loads(r["vector"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(vec, list):
                out[int(r["fact_id"])] = [float(x) for x in vec]
        return out

    # -- PL-06: the owner's view of what is known (plan 2.4) ---------------- #

    _FACT_COLS = ("id, source, title, body, kind, origin, valid_from, valid_to, superseded_by, pinned, hidden, "
                  "uses, last_used, reviewed, project, created_at, updated_at")

    @staticmethod
    def _fact_dict(r: sqlite3.Row) -> dict[str, Any]:
        d = {k: r[k] for k in r.keys()}
        for flag in ("pinned", "hidden", "reviewed"):
            d[flag] = bool(d.get(flag))
        d["closed"] = bool(d.get("valid_to"))
        return d

    @staticmethod
    def _log(cur: sqlite3.Cursor, fact_id: int, change: str, before: str = "", after: str = "") -> None:
        cur.execute("INSERT INTO fact_history(fact_id, at, change, before, after) VALUES (?, ?, ?, ?, ?)",
                    (fact_id, _now_iso(), change, before[:4000], after[:4000]))

    def list_facts(self, *, query: str = "", kind: str = "", origin: str = "", pinned: bool | None = None,
                   expiring: bool = False, used_days: int = 0, closed: bool = False, review: bool = False,
                   records: bool = False, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """What the store knows, newest first, with the filters the Memory view offers. Records (ledgers, notes the
        store filled by itself) are left out unless ``records``. Hidden facts are listed: the owner can unhide."""
        where: list[str] = ["1=1"]
        args: list[Any] = []
        if not records:
            where.append("f.kind != 'record'")
        if kind:
            where.append("f.kind = ?")
            args.append(kind)
        if origin:
            where.append("f.origin = ?")
            args.append(origin)
        if pinned is not None:
            where.append("f.pinned = ?")
            args.append(1 if pinned else 0)
        where.append("f.valid_to IS NOT NULL" if closed else "f.valid_to IS NULL")
        if expiring:
            where.append("f.valid_to IS NOT NULL AND f.valid_to > ?")
            args.append(_now_iso())
        if used_days:
            from datetime import timedelta

            where.append("f.last_used >= ?")
            args.append((datetime.now() - timedelta(days=int(used_days))).isoformat(timespec="seconds"))
        if review:
            where.append("f.reviewed = 0")
        join = ""
        match = _fts_query(query) if (query or "").strip() else ""
        if (query or "").strip() and not match:
            return {"facts": [], "total": 0}
        if match:
            join = "JOIN facts_fts ON facts_fts.rowid = f.id"
            where.append("facts_fts MATCH ?")
            args.append(match)
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) AS n FROM facts f {join} WHERE {' AND '.join(where)}",
                                       args).fetchone()["n"]
            rows = self._conn.execute(
                f"SELECT {', '.join('f.' + c.strip() for c in self._FACT_COLS.split(','))} FROM facts f {join} "
                f"WHERE {' AND '.join(where)} ORDER BY f.pinned DESC, f.updated_at DESC, f.id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset]).fetchall()
        return {"facts": [self._fact_dict(r) for r in rows], "total": int(total)}

    def get_fact(self, fact_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {self._FACT_COLS} FROM facts WHERE id = ?", (int(fact_id),)).fetchone()
            if row is None:
                return None
            hist = self._conn.execute(
                "SELECT at, change, before, after FROM fact_history WHERE fact_id = ? ORDER BY id DESC LIMIT 50",
                (int(fact_id),)).fetchall()
            used = self._conn.execute(
                "SELECT session, turn, at FROM fact_uses WHERE fact_id = ? ORDER BY id DESC LIMIT 50",
                (int(fact_id),)).fetchall()
        out = self._fact_dict(row)
        out["history"] = [dict(h) for h in hist]
        out["used_in"] = [dict(u) for u in used]
        return out

    def counts(self) -> dict[str, int]:
        """Numbers for the Memory view's chips. Records are not counted as things it knows."""
        with self._lock:
            def one(sql: str, *a: Any) -> int:
                return int(self._conn.execute(sql, a).fetchone()[0])

            base = "FROM facts WHERE kind != 'record' AND valid_to IS NULL"
            return {"facts": one(f"SELECT COUNT(*) {base}"), "review": one(f"SELECT COUNT(*) {base} AND reviewed = 0"),
                    "pinned": one(f"SELECT COUNT(*) {base} AND pinned = 1"),
                    "hidden": one(f"SELECT COUNT(*) {base} AND hidden = 1"),
                    "closed": one("SELECT COUNT(*) FROM facts WHERE kind != 'record' AND valid_to IS NOT NULL"),
                    "records": one("SELECT COUNT(*) FROM facts WHERE kind = 'record'")}

    def add_by_hand(self, text: str, kind: str = "fact", project: str = "") -> dict[str, Any]:
        """A fact the owner typed. Origin ``you_said``, already reviewed."""
        text = (text or "").strip()
        if not text:
            raise ValueError("a fact needs some text")
        kind = kind if kind in KINDS else "fact"
        title = " ".join(text.split())[:80]
        title = f"{title} #{_content_hash(text)[:6]}"
        self.remember("you", title, text, kind=kind, origin="you_said", project=project)
        with self._lock:
            row = self._conn.execute("SELECT id FROM facts WHERE source = 'you' AND title = ?", (title,)).fetchone()
            self._log(self._conn.cursor(), row["id"], "added by hand", "", text)
            self._conn.commit()
        return self.get_fact(row["id"]) or {}

    def edit_fact(self, fact_id: int, *, body: str | None = None, kind: str | None = None) -> dict[str, Any]:
        """Change a fact's text or kind. The earlier text stays in its history. The cached vector is dropped."""
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute("SELECT body, kind FROM facts WHERE id = ?", (int(fact_id),)).fetchone()
            if row is None:
                raise LookupError("no such fact")
            if body is not None:
                body = body.strip()
                if not body:
                    raise ValueError("a fact needs some text")
                if body != row["body"]:
                    cur.execute("UPDATE facts SET body = ?, content_hash = ?, updated_at = ? WHERE id = ?",
                                (body, _content_hash(body), _now_iso(), int(fact_id)))
                    cur.execute("DELETE FROM fact_embeddings WHERE fact_id = ?", (int(fact_id),))
                    self._log(cur, int(fact_id), "edited", row["body"], body)
            if kind is not None and kind in KINDS and kind != row["kind"]:
                cur.execute("UPDATE facts SET kind = ? WHERE id = ?", (kind, int(fact_id)))
                self._log(cur, int(fact_id), "kind changed", row["kind"] or "", kind)
            cur.execute("UPDATE facts SET reviewed = 1 WHERE id = ?", (int(fact_id),))
            self._conn.commit()
        return self.get_fact(fact_id) or {}

    def set_flag(self, fact_id: int, flag: str, value: bool) -> dict[str, Any]:
        """Pin (always recalled, capped) or hide (never recalled, kept)."""
        if flag not in ("pinned", "hidden"):
            raise ValueError("flag must be pinned or hidden")
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"UPDATE facts SET {flag} = ?, reviewed = 1 WHERE id = ?", (1 if value else 0, int(fact_id)))
            if cur.rowcount == 0:
                raise LookupError("no such fact")
            self._log(cur, int(fact_id), flag if value else f"un{flag}")
            self._conn.commit()
        return self.get_fact(fact_id) or {}

    def mark_reviewed(self, fact_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE facts SET reviewed = 1 WHERE id = ?", (int(fact_id),))
            self._conn.commit()

    def supersede(self, old_id: int, new_text: str) -> dict[str, Any]:
        """Replace a fact without deleting it: the old one is closed (``valid_to``, ``superseded_by``) and stays in
        the history; the new one starts now."""
        new_text = (new_text or "").strip()
        if not new_text:
            raise ValueError("a fact needs some text")
        with self._lock:
            old = self._conn.execute("SELECT * FROM facts WHERE id = ?", (int(old_id),)).fetchone()
        if old is None:
            raise LookupError("no such fact")
        title = f"{' '.join(new_text.split())[:80]} #{_content_hash(new_text)[:6]}"
        self.remember("you", title, new_text, kind=old["kind"] if old["kind"] in KINDS else "fact",
                      origin="you_said", project=old["project"] or "")
        with self._lock:
            cur = self._conn.cursor()
            new_id = cur.execute("SELECT id FROM facts WHERE source = 'you' AND title = ?", (title,)).fetchone()["id"]
            if new_id != int(old_id):
                cur.execute("UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?",
                            (_now_iso(), new_id, int(old_id)))
                self._log(cur, int(old_id), "replaced", old["body"], new_text)
                self._log(cur, new_id, "replaces an earlier fact", old["body"], new_text)
            self._conn.commit()
        return self.get_fact(new_id) or {}

    def forget(self, fact_id: int) -> bool:
        """Hard delete: the row, its full-text entry (trigger), its vector, its history and its use log."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM facts WHERE id = ?", (int(fact_id),))
            gone = cur.rowcount > 0
            if gone:
                cur.execute("DELETE FROM fact_embeddings WHERE fact_id = ?", (int(fact_id),))
                cur.execute("DELETE FROM fact_history WHERE fact_id = ?", (int(fact_id),))
                cur.execute("DELETE FROM fact_uses WHERE fact_id = ?", (int(fact_id),))
                cur.execute("UPDATE facts SET superseded_by = NULL WHERE superseded_by = ?", (int(fact_id),))
            self._conn.commit()
        return gone

    def forget_where(self, *, source: str = "", since: str = "", until: str = "", dry_run: bool = False) -> int:
        """Forget every fact from one source, or created in a date range (ISO dates). Returns how many (or would)."""
        if not (source or since or until):
            raise ValueError("say a source or a date range")
        where, args = ["kind != 'record'"], []
        if source:
            where.append("source = ?")
            args.append(source)
        if since:
            where.append("created_at >= ?")
            args.append(since)
        if until:
            where.append("created_at <= ?")
            args.append(until if "T" in until else until + "T23:59:59")
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(f"SELECT id FROM facts WHERE {' AND '.join(where)}", args)]
        if dry_run:
            return len(ids)
        return sum(1 for i in ids if self.forget(i))

    def merge_suggestions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Pairs that look like the same fact: identical text, or most words shared. Deterministic."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, body, content_hash FROM facts WHERE kind != 'record' AND valid_to IS NULL ORDER BY id"
            ).fetchall()
        words = {r["id"]: set(re.findall(r"[a-z0-9]{3,}", r["body"].lower())) for r in rows}
        out: list[dict[str, Any]] = []
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                wa, wb = words[a["id"]], words[b["id"]]
                if not wa or not wb:
                    continue
                score = len(wa & wb) / len(wa | wb)
                if a["content_hash"] == b["content_hash"] or score >= 0.7:
                    out.append({"keep": a["id"], "drop": b["id"], "score": round(score, 2),
                                "keep_text": a["body"][:200], "drop_text": b["body"][:200]})
                    if len(out) >= limit:
                        return out
        return out

    def merge(self, keep_id: int, drop_id: int) -> dict[str, Any]:
        """The dropped fact is closed and points at the kept one; it keeps its history."""
        if int(keep_id) == int(drop_id):
            raise ValueError("pick two different facts")
        with self._lock:
            cur = self._conn.cursor()
            if cur.execute("SELECT 1 FROM facts WHERE id = ?", (int(keep_id),)).fetchone() is None:
                raise LookupError("no such fact")
            cur.execute("UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?",
                        (_now_iso(), int(keep_id), int(drop_id)))
            if cur.rowcount == 0:
                raise LookupError("no such fact")
            self._log(cur, int(drop_id), "merged into another fact")
            self._conn.commit()
        return self.get_fact(keep_id) or {}

    def export_all(self) -> dict[str, Any]:
        """Every fact the owner can see, closed ones included, as plain data (no vectors)."""
        facts = self.list_facts(limit=500)["facts"] + self.list_facts(closed=True, limit=500)["facts"]
        return {"exported_at": _now_iso(), "facts": facts}

    # -- recall bookkeeping --------------------------------------------------- #

    def recall_facts(self, query: str, limit: int = 5, project: str = "") -> list[dict[str, Any]]:
        """Search hits, plus the pinned facts (at most ``PINNED_RECALL_CAP``), never hidden or closed ones."""
        hits = self.search(query, limit=limit, project=project)
        seen = {h["id"] for h in hits}
        with self._lock:
            pins = self._conn.execute(
                "SELECT id, source, title, body FROM facts WHERE pinned = 1 AND hidden = 0 AND valid_to IS NULL "
                "AND (project = '' OR project = ?) ORDER BY updated_at DESC LIMIT ?",
                (str(project or ""), PINNED_RECALL_CAP)).fetchall()
        for r in pins:
            if r["id"] not in seen:
                hits.append({"id": r["id"], "source": r["source"], "title": r["title"], "snippet": r["body"][:300],
                             "score": 0.0})
        return hits

    def record_uses(self, fact_ids: list[int], session: str, turn: int) -> None:
        """The facts that were put in this turn's prompt (ids only)."""
        ids = sorted({int(i) for i in fact_ids})
        if not ids:
            return
        now = _now_iso()
        with self._lock:
            for i in ids:
                self._conn.execute("INSERT INTO fact_uses(fact_id, session, turn, at) VALUES (?, ?, ?, ?)",
                                   (i, str(session)[:200], int(turn), now))
                self._conn.execute("UPDATE facts SET uses = uses + 1, last_used = ? WHERE id = ?", (now, i))
            self._conn.commit()

    def used_in_turn(self, session: str, turn: int) -> list[dict[str, Any]]:
        """"Why did you say that": the facts injected into one turn. Facts forgotten since are simply absent."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join('f.' + c.strip() for c in self._FACT_COLS.split(','))} FROM fact_uses u "
                "JOIN facts f ON f.id = u.fact_id WHERE u.session = ? AND u.turn = ? GROUP BY f.id ORDER BY f.id",
                (str(session), int(turn))).fetchall()
        return [self._fact_dict(r) for r in rows]

    # -- ingestion --------------------------------------------------------- #

    def ingest_session_file(self, path: Path | str) -> int:
        """Index every turn of a session JSONL as a fact. Returns ingested."""
        path = Path(path)
        added = 0
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return 0
        source = f"session:{path.stem}"
        # What is already stored for this session, by title and content hash: a turn whose stored hash
        # matches is skipped without touching the database, so a long conversation costs one SELECT per
        # call instead of a write per old turn.
        with self._lock:
            stored = {
                r["title"]: r["content_hash"]
                for r in self._conn.execute("SELECT title, content_hash FROM facts WHERE source = ?", (source,))
            }
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            user = (rec.get("user") or "").strip()
            answer = (rec.get("final_text") or "").strip()
            if not user and not answer:
                continue
            body = f"USER: {user}\nANSWER: {answer}"
            title = f"turn {rec.get('turn', '?')}"
            if stored.get(title) == _content_hash(body.strip()):
                continue
            try:
                self.remember(source, title, body)
                added += 1
            except ValueError:
                continue
        return added

    def ingest_vault(self, root: Path | str) -> int:
        """Index every .md note in a vault. Returns ingested."""
        root = Path(root)
        added = 0
        if not root.is_dir():
            return 0
        for p in sorted(root.rglob("*.md")):
            try:
                body = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not body:
                continue
            try:
                self.remember("vault", str(p.relative_to(root)), body)
                added += 1
            except ValueError:
                continue
        return added

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression: OR of double-quoted terms.

    User input is never interpolated raw into FTS5 syntax (a bare MATCH
    string with special chars like `" OR "` would inject query syntax). Terms
    are tokenized on non-alphanumerics and each one double-quoted with
    embedded quotes doubled — the safe, standard form.

    OR (not AND) so recall degrades gracefully: FTS5 bm25 ranks rows that
    match more terms first, so a multi-term query still surfaces the best
    hits, while AND made recall all-or-nothing — every distilled term had
    to co-occur in ONE fact, so most natural-language recall queries
    ("what do you remember about X") returned zero hits.
    """
    # \w+ is the unicode word class, close to what the unicode61 tokenizer indexes: the old ASCII-only split
    # turned "café" into "caf" and a Japanese query into nothing.
    terms = re.findall(r"\w+", query, re.UNICODE)
    if not terms:
        return ""
    quoted = ["\"" + t.replace('"', '""') + "\"" for t in terms]
    return " OR ".join(quoted)


class RemoteMemoryStoreUnavailable(RuntimeError):
    """Raised when the remote machine's memory API can't be reached at
    all (network down, wrong URL, remote store not configured there
    either) — the same honest NOT CONFIGURED contract
    MemoryStoreUnavailable gives for the local case."""


class RemoteMemoryStore:
    """Same real public interface as MemoryStore (remember/search/count/
    close) — a drop-in for _open_memory_store()'s callers — but every call
    is a real HTTP request to another machine's own webui.py
    (GET /api/memory/search, POST /api/memory/remember), which runs the
    exact same MemoryStore against its OWN local disk.

    Explicit user request (2026-08-31): "move the actual rag to
    [the desktop], that machine has more storage". The real, honest
    architecture this implements: the SQLite file itself NEVER moves onto
    a network-mounted path — SQLite's file-locking is documented as
    unreliable over SMB/network filesystems, a real corruption risk, not
    a hypothetical one. Instead the desktop keeps owning its real file on
    its own local disk, and every machine that wants to read/write the
    shared RAG talks to it over a real HTTP API instead of opening the
    file directly. Selected by general_roster._open_memory_store() when
    DOURMOUSE_MEMORY_REMOTE_URL is set — a machine with that env var
    unset keeps using the local MemoryStore exactly as before, zero
    behavior change for a single-machine setup.

    Honest scope: semantic/embedding recall (memory_embed.semantic_search)
    reads a MemoryStore's SQLite connection directly and is NOT proxied
    here — remote mode covers the default FTS5 remember/search/recall
    path (Rule 2.2: no attempt to fake vector search over HTTP that isn't
    real); semantic_recall on a remote-configured machine reports its own
    honest NOT CONFIGURED rather than silently degrading.
    """

    def __init__(self, base_url: str, timeout: float = 15.0, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Real gap found before this ever ran cross-machine (2026-08-31):
        # webui.py's own _authorized() requires a Bearer token for any
        # NON-loopback client the moment DOURMOUSE_ACCESS_TOKEN is set on
        # the remote server — which the desktop deployment already has
        # set (binds 0.0.0.0). An unauthenticated remote call would 401,
        # not silently succeed against the wrong data — but it should
        # authenticate correctly in the first place, not rely on hitting
        # and parsing that 401.
        self.token = token

    def _send(self, req: Any) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            if not isinstance(data, dict):
                raise ValueError(f"expected a JSON object, got {type(data).__name__}")
            return data
        except urllib.error.HTTPError as exc:
            # The remote server DID respond -- a real 400/409/500 with its
            # own honest {"ok": false, "error": "..."} JSON body (see
            # webui.py's _handle_memory_remote_search/_remember). Caught
            # SEPARATELY from URLError (its own superclass) and BEFORE it:
            # a genuine validation/config error from the remote server is
            # not the same thing as the remote being unreachable, and
            # must not be reported as one.
            try:
                body = json.loads(exc.read().decode())
            except (ValueError, OSError):
                raise RemoteMemoryStoreUnavailable(
                    f"remote memory store at {self.base_url} returned "
                    f"HTTP {exc.code} with no parseable body"
                ) from exc
            if not (isinstance(body, dict) and "ok" in body):
                # An error status whose body is not the store's own {"ok": ...} answer (a 401 from a wrong
                # token, a proxy's 500 page): not a result. count() used to turn it into 0.
                raise RemoteMemoryStoreUnavailable(
                    f"remote memory store at {self.base_url} returned HTTP {exc.code}: {str(body)[:200]}"
                ) from exc
            return body
        except urllib.error.URLError as exc:
            # No response at all — genuinely unreachable (network down,
            # wrong host/port, remote process not running).
            raise RemoteMemoryStoreUnavailable(
                f"remote memory store unreachable at {self.base_url}: {exc}"
            ) from exc
        except (OSError, ValueError) as exc:
            # A read timeout or reset after the connection was made (TimeoutError, ConnectionResetError are
            # OSError, not URLError), or a 200 whose body is not JSON: the remote did not give a usable
            # answer, so LocalFallbackMemoryStore must be able to fall back.
            raise RemoteMemoryStoreUnavailable(
                f"remote memory store at {self.base_url} gave no usable answer: {type(exc).__name__}: {exc}"
            ) from exc

    def _get(self, path: str) -> dict[str, Any]:
        import urllib.request

        return self._send(urllib.request.Request(self.base_url + path))

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import urllib.request

        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._send(req)

    def remember(self, source: str, title: str, body: str) -> str:
        result = self._post(
            "/api/memory/remember", {"source": source, "title": title, "body": body}
        )
        if not result.get("ok"):
            raise ValueError(result.get("error") or "remote remember failed")
        return str(result.get("result") or "")

    def search(
        self, query: str, limit: int = 10, source: str | None = None
    ) -> list[dict[str, Any]]:
        import urllib.parse as _up

        params = {"q": query, "limit": str(limit)}
        if source:
            params["source"] = source
        result = self._get("/api/memory/search?" + _up.urlencode(params))
        if not result.get("ok"):
            raise RemoteMemoryStoreUnavailable(result.get("error") or "remote search failed")
        return result.get("hits") or []

    def count(self, source: str | None = None) -> int:
        # The remote server's own /api/memory already reports a total
        # count; a per-source count isn't exposed remotely (not needed by
        # any current remote caller) — honestly unsupported rather than
        # silently wrong.
        if source is not None:
            raise NotImplementedError("RemoteMemoryStore.count(source=...) is not supported remotely")
        result = self._get("/api/memory")
        return int(result.get("count") or 0)

    def get(self, source: str, title: str) -> dict[str, Any] | None:
        """Fetch one fact by its exact (source, title) key.

        Implemented against the remote FTS search endpoint, because the
        remote API exposes no by-key read: search for the title, then
        keep only a genuinely exact (source, title) match. The exactness
        filter is what makes this a real `get` rather than a fuzzy lookup
        that happens to usually work.

        HONEST CAVEAT, and it is a real one: FTS5 tokenises. A title made
        entirely of characters the remote tokeniser drops could fail to
        come back from search even though the row exists, and this would
        then return None -- a false negative, never a false positive. The
        local MemoryStore.get() does a direct keyed SELECT and has no such
        gap. Callers that must not tolerate a false negative should not
        use a remote store for that job.
        """
        try:
            hits = self.search(title, limit=25, source=source)
        except RemoteMemoryStoreUnavailable:
            raise
        for hit in hits:
            if hit.get("source") == source and hit.get("title") == title:
                return hit
        return None

    # ---- operations that are genuinely NOT proxyable over this API ---- #
    #
    # These exist so that calling one produces an honest, typed, readable
    # failure naming the operation, instead of an AttributeError.
    #
    # This was a real live bug, not a hypothetical: GET /api/profile died
    # with "'RemoteMemoryStore' object has no attribute 'get'" and dropped
    # the connection outright, because this class advertised itself in its
    # own docstring as "a drop-in for _open_memory_store()'s callers"
    # while implementing 4 of MemoryStore's 12 public methods. The other
    # seven were reached by personality_profile, semantic_graph,
    # repo_index, memory_embed and chat -- every one of them crashing or
    # silently doing nothing the moment DOURMOUSE_MEMORY_REMOTE_URL was
    # set, which is this machine's real configuration.
    #
    # Why these specific ones stay unsupported rather than being faked:
    # each needs either a bulk dump or direct SQLite access that the
    # remote HTTP API deliberately does not expose (see this class's
    # docstring on why the SQLite file itself must never be shared over a
    # network filesystem). Returning an empty list instead would be worse
    # than failing -- a caller would treat "no facts" as truth and, in
    # repo_index's case, delete real rows on the strength of it.

    def _unsupported(self, op: str, needs: str) -> "RemoteMemoryStoreUnavailable":
        return RemoteMemoryStoreUnavailable(
            f"RemoteMemoryStore.{op}() is not available against a remote "
            f"memory store at {self.base_url}: it needs {needs}, which the "
            f"remote HTTP API does not expose. Unset DOURMOUSE_MEMORY_REMOTE_URL "
            f"to use the local store for this operation."
        )

    def all_facts(self, source: str | None = None) -> list[dict[str, Any]]:
        raise self._unsupported("all_facts", "a full table dump")

    def delete(self, source: str, title: str) -> bool:
        raise self._unsupported("delete", "a remote delete endpoint")

    def get_embeddings(self, model: str | None = None) -> dict[int, list[float]]:
        raise self._unsupported("get_embeddings", "direct SQLite access")

    def save_embedding(self, fact_id: int, model: str, vector: Any) -> None:
        raise self._unsupported("save_embedding", "direct SQLite access")

    def ingest_session_file(self, path: Any) -> int:
        raise self._unsupported("ingest_session_file", "a bulk ingest endpoint")

    def ingest_vault(self, *args: Any, **kwargs: Any) -> int:
        raise self._unsupported("ingest_vault", "a bulk ingest endpoint")

    def close(self) -> None:
        pass  # stateless HTTP client — nothing to close


def _pl06_unsupported(op: str):
    def method(self: "RemoteMemoryStore", *args: Any, **kwargs: Any) -> Any:
        raise self._unsupported(op, "the owner's Memory view, which works on the local store")

    method.__name__ = op
    return method


#: The Memory view edits the local store. A remote store gets an honest refusal for each of these, not an AttributeError.
for _op in ("list_facts", "get_fact", "counts", "add_by_hand", "edit_fact", "set_flag", "mark_reviewed", "supersede",
            "forget", "forget_where", "merge_suggestions", "merge", "export_all", "recall_facts", "record_uses",
            "used_in_turn"):
    setattr(RemoteMemoryStore, _op, _pl06_unsupported(_op))


class LocalFallbackMemoryStore:
    """A RemoteMemoryStore that keeps working when the remote is down.

    v14 (user-directed, 2026-09-08): RemoteMemoryStore's own design is
    correct for the common case (2026-08-31's "move the actual rag to
    [the desktop]") but has a real, live-observed gap: DOURMOUSE_MEMORY_
    REMOTE_URL being set says "the shared store is on that other
    machine" -- it says nothing about whether that machine is actually
    reachable RIGHT NOW. When it isn't (asleep, off the LAN, network
    down), every memory operation on THIS machine raised
    RemoteMemoryStoreUnavailable and the whole memory subagent went
    NOT CONFIGURED, even though this machine has a perfectly good local
    MemoryStore it could fall back to -- exactly the local behavior it
    already had before DOURMOUSE_MEMORY_REMOTE_URL was ever set.

    Real design choices, not incidental:
    - The local store is opened LAZILY (via ``local_factory``, not an
      already-open MemoryStore) -- a healthy remote never touches disk
      here at all, so a single-machine setup with the remote reachable
      pays zero extra cost.
    - A write made during a fallback is tagged with a "[PENDING SYNC] "
      title prefix -- an honest, visible signal (not a silent success)
      that this fact lives ONLY on this machine until something
      reconciles it with the remote; nothing here invents a real sync
      mechanism that does not exist.
    - ``last_used_local_fallback`` / ``last_remote_error`` are read by
      webui.py's memory API payload so the UI can say "LOCAL FALLBACK"
      truthfully instead of silently presenting local results as if
      they were the shared remote store's own.
    """

    def __init__(
        self,
        remote: "RemoteMemoryStore",
        local_factory: Callable[[], "MemoryStore"],
    ) -> None:
        self._remote = remote
        self._local_factory = local_factory
        self._local: MemoryStore | None = None
        self.last_used_local_fallback = False
        self.last_remote_error: str | None = None
        self._probe_at = 0.0
        self._probe_local = False

    #: How long a reachability answer is reused (seconds), so a loop of deletes does not probe the remote
    #: once per fact.
    _PROBE_TTL = 3.0

    def _writes_go_local(self) -> bool:
        """True when ``remember`` would land in the LOCAL store right now, because the remote cannot be
        reached. ``all_facts`` and ``delete`` must act on the store that ``remember`` writes to (W2R / P4-50):
        a reachable remote holds the facts and cannot list or delete them over its HTTP API."""
        import time

        now = time.monotonic()
        if now - self._probe_at < self._PROBE_TTL:
            return self._probe_local
        try:
            self._remote.count()
            self._note_remote_ok()
            self._probe_local = False
        except RemoteMemoryStoreUnavailable as exc:
            self._note_fallback(exc)
            self._probe_local = True
        self._probe_at = now
        return self._probe_local

    def _local_store(self) -> "MemoryStore":
        if self._local is None:
            self._local = self._local_factory()
        return self._local

    def _note_fallback(self, exc: Exception) -> None:
        self.last_used_local_fallback = True
        self.last_remote_error = str(exc)

    def _note_remote_ok(self) -> None:
        self.last_used_local_fallback = False
        self.last_remote_error = None

    def remember(self, source: str, title: str, body: str) -> str:
        try:
            result = self._remote.remember(source, title, body)
            self._note_remote_ok()
            return result
        except RemoteMemoryStoreUnavailable as exc:
            self._note_fallback(exc)
            fallback_title = title if title.startswith("[PENDING SYNC] ") else f"[PENDING SYNC] {title}"
            return self._local_store().remember(source, fallback_title, body)

    def search(
        self, query: str, limit: int = 10, source: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            result = self._remote.search(query, limit=limit, source=source)
            self._note_remote_ok()
            return result
        except RemoteMemoryStoreUnavailable as exc:
            self._note_fallback(exc)
            return self._local_store().search(query, limit=limit, source=source)

    def count(self, source: str | None = None) -> int:
        try:
            result = self._remote.count(source=source)
            self._note_remote_ok()
            return result
        except (RemoteMemoryStoreUnavailable, NotImplementedError) as exc:
            self._note_fallback(exc)
            return self._local_store().count(source=source)

    def get(self, source: str, title: str) -> dict[str, Any] | None:
        try:
            result = self._remote.get(source, title)
            self._note_remote_ok()
            return result
        except RemoteMemoryStoreUnavailable as exc:
            self._note_fallback(exc)
            return self._local_store().get(source, title)

    def all_facts(self) -> list[dict[str, Any]]:
        # RemoteMemoryStore.all_facts() always raises unsupported (a full table dump is deliberately not
        # exposed over the remote HTTP API). While the remote is reachable, remember() writes there, so a
        # list from the local store would be a different set of facts than the one just written: the typed
        # error is raised instead (callers already handle it). While it is down, remember() writes locally
        # and this lists the same local store. Matches MemoryStore.all_facts()'s signature (no `source`).
        if not self._writes_go_local():
            self._remote.all_facts()  # raises the remote's own typed "not available over the remote API"
        return self._local_store().all_facts()

    # The operations the remote API does not expose (RemoteMemoryStore raises its typed "unsupported" error
    # for each). They are served by the local store, the same way all_facts() is, so the post-turn session
    # ingest, vault ingest, deletes and the embedding cache work on a machine with a remote URL configured
    # instead of raising AttributeError. What they write lives only on this machine.

    def ingest_session_file(self, path: Any) -> int:
        return self._local_store().ingest_session_file(path)

    def ingest_vault(self, root: Any) -> int:
        return self._local_store().ingest_vault(root)

    def delete(self, source: str, title: str) -> bool:
        # Same rule as all_facts(): act on the store remember() writes to. A reachable remote cannot delete
        # over its HTTP API, so the typed error is raised and the local store is left alone.
        if not self._writes_go_local():
            self._remote.delete(source, title)  # raises the remote's own typed error
        return self._local_store().delete(source, title)

    def save_embedding(self, fact_id: int, model: str, vector: list[float]) -> None:
        self._local_store().save_embedding(fact_id, model, vector)

    def get_embeddings(self, model: str | None = None) -> dict[int, list[float]]:
        return self._local_store().get_embeddings(model)

    def close(self) -> None:
        self._remote.close()
        if self._local is not None:
            self._local.close()


def _pl06_local(op: str):
    def method(self: "LocalFallbackMemoryStore", *args: Any, **kwargs: Any) -> Any:
        return getattr(self._local_store(), op)(*args, **kwargs)

    method.__name__ = op
    return method


#: The owner's Memory view and the per-turn recall bookkeeping work on this machine's own file, remote or not.
for _op in ("list_facts", "get_fact", "counts", "add_by_hand", "edit_fact", "set_flag", "mark_reviewed", "supersede",
            "forget", "forget_where", "merge_suggestions", "merge", "export_all", "recall_facts", "record_uses",
            "used_in_turn"):
    setattr(LocalFallbackMemoryStore, _op, _pl06_local(_op))
