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
from typing import Any

_DEFAULT_DIR_NAME = "memory"
_DEFAULT_DB_NAME = "atlas_memory.db"


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

    def remember(self, source: str, title: str, body: str) -> str:
        """Upsert one fact/note; returns a plain confirmation."""
        source = (source or "agent").strip()[:200]
        title = (title or "").strip()[:500]
        body = (body or "").strip()
        if not title or not body:
            raise ValueError("remember requires a non-empty title and body")
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO facts(source, title, body, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, title) DO UPDATE SET
                    body = excluded.body,
                    updated_at = excluded.updated_at
                """,
                (source, title, body, now, now),
            )
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
        self, query: str, limit: int = 10, source: str | None = None
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
            SELECT f.source AS source,
                   f.title AS title,
                   snippet(facts_fts, 2, '[', ']', '…', 8) AS snippet,
                   bm25(facts_fts) AS score
            FROM facts_fts
            JOIN facts f ON f.id = facts_fts.rowid
            WHERE facts_fts MATCH ?
              AND (? IS NULL OR f.source = ?)
            ORDER BY score
            LIMIT ?
        """
        with self._lock:
            rows = self._conn.execute(
                sql, (match_expr, src, src, limit)
            ).fetchall()
        return [
            {
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

    def get_embeddings(self) -> dict[int, list[float]]:
        """fact_id -> vector for every cached embedding."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT fact_id, vector FROM fact_embeddings"
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

    # -- ingestion --------------------------------------------------------- #

    def ingest_session_file(self, path: Path | str) -> int:
        """Index every turn of a session JSONL as a fact. Returns ingested."""
        path = Path(path)
        added = 0
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return 0
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
            try:
                self.remember(f"session:{path.stem}", title, body)
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
    terms = [t for t in re.split(r"[^A-Za-z0-9_]+", query) if t]
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
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            # The remote server DID respond -- a real 400/409/500 with its
            # own honest {"ok": false, "error": "..."} JSON body (see
            # webui.py's _handle_memory_remote_search/_remember). Caught
            # SEPARATELY from URLError (its own superclass) and BEFORE it:
            # a genuine validation/config error from the remote server is
            # not the same thing as the remote being unreachable, and
            # must not be reported as one.
            try:
                return json.loads(exc.read().decode())
            except (ValueError, OSError):
                raise RemoteMemoryStoreUnavailable(
                    f"remote memory store at {self.base_url} returned "
                    f"HTTP {exc.code} with no parseable body"
                ) from exc
        except urllib.error.URLError as exc:
            # No response at all — genuinely unreachable (network down,
            # wrong host/port, remote process not running).
            raise RemoteMemoryStoreUnavailable(
                f"remote memory store unreachable at {self.base_url}: {exc}"
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

    def get_embeddings(self) -> list[dict[str, Any]]:
        raise self._unsupported("get_embeddings", "direct SQLite access")

    def save_embedding(self, fact_id: int, model: str, vector: Any) -> None:
        raise self._unsupported("save_embedding", "direct SQLite access")

    def ingest_session_file(self, path: Any) -> int:
        raise self._unsupported("ingest_session_file", "a bulk ingest endpoint")

    def ingest_vault(self, *args: Any, **kwargs: Any) -> int:
        raise self._unsupported("ingest_vault", "a bulk ingest endpoint")

    def close(self) -> None:
        pass  # stateless HTTP client — nothing to close
