"""One library store: a SQLite file of documents and passages with keyword search, a vector table and an embedding
queue (plan 2.1, finding 206).

The user wiki (``wiki.db``) and each project (``projects/<id>.db``) are two files with this one layout, so the code
that reads them is the same. A store never opens another store's file.

Layout: ``documents`` (one row per thing added: title, path or URL, folder, tier, date), ``passages`` (about 400 words
each, with page and heading), ``passages_fts`` (FTS5, BM25), ``vectors`` (one normalised float32 vector per passage and
the model that made it), ``embed_queue`` (passages waiting for a vector, or blocked with the reason), ``scan_state``
(size and modification time per scanned file, for the folder connector in plan 2.3) and ``meta``.

Search is hybrid: BM25 and cosine similarity are each cut to the top 50, joined by reciprocal rank fusion, with a small
recency boost. Without vectors (no key, Private mode, nothing embedded yet) it is keyword-only and says so.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .chunking import Chunk, chunk_text

TIERS = ("T0", "T1", "T2", "T3")
TEXT_MAX_CHARS = 5_000_000
PASSAGES_PER_DOC_MAX = 5_000
SEARCH_LIMIT_MAX = 50
CANDIDATES = 50
RRF_K = 60
RECENCY_WEIGHT = 0.15
RECENCY_HALF_LIFE_DAYS = 365.0
SCHEMA_VERSION = 1
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD = re.compile(r"[\w'-]+", re.UNICODE)
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         " ": " "})
MIN_QUOTE_WORDS = 3


def _now() -> float:
    return time.time()


def fts_words(raw: str) -> list[str]:
    return [w for w in _WORD.findall(raw or "") if w.strip("'-")][:12]


def fts_and(raw: str) -> str:
    """User words as a safe FTS5 query: every word quoted, all required. No operators reach the engine."""
    return " ".join('"' + w.replace('"', "") + '"' for w in fts_words(raw))


def fts_or(raw: str) -> str:
    return " OR ".join('"' + w.replace('"', "") + '"' for w in fts_words(raw))


def normalise_quote(text: str) -> str:
    """Whitespace collapsed and typographic quotes and dashes made plain, so a quote copied from a PDF still matches.
    Case and wording stay exact: a quote is verbatim or it is not a quote."""
    return " ".join((text or "").translate(_QUOTES).split())


def _clean(raw: Any, limit: int) -> str:
    return _CONTROL.sub("", str(raw if raw is not None else "")).strip()[:limit]


class Store:
    """One store file. Safe to share between threads; every call opens its own connection."""

    def __init__(self, path: Path, kind: str = "project") -> None:
        if kind not in ("wiki", "project"):
            raise ValueError("kind must be wiki or project")
        self.path = Path(path)
        self.kind = kind
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._matrix: tuple[Any, Any, str] | None = None  # (ids, matrix, model) cache of every vector
        self._matrix_stamp = -1
        with self._lock, self._connect() as db:
            self._migrate(db)

    # ---- connection and schema ------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}  # noqa: S608 - fixed names

    def _migrate(self, db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL,
              path TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
              folder TEXT NOT NULL DEFAULT '', namespace TEXT NOT NULL DEFAULT '', tier TEXT NOT NULL DEFAULT 'T1',
              external_id TEXT NOT NULL DEFAULT '', doc_date TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}',
              added_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(source, source_id));
            CREATE INDEX IF NOT EXISTS documents_folder ON documents(folder);
            CREATE INDEX IF NOT EXISTS documents_ns ON documents(namespace);
            CREATE TABLE IF NOT EXISTS passages (
              id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL,
              seq INTEGER NOT NULL, text TEXT NOT NULL, added_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS passages_src ON passages(source, source_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
              text, title, content='passages', content_rowid='id', tokenize='porter unicode61');
            CREATE TABLE IF NOT EXISTS vectors (
              passage_id INTEGER PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL,
              made_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS embed_queue (
              passage_id INTEGER PRIMARY KEY, queued_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              blocked TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS scan_state (
              path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL, doc_id INTEGER,
              seen_at REAL NOT NULL);
            """
        )
        have = self._columns(db, "passages")
        for col, ddl in (("doc_id", "INTEGER"), ("page", "INTEGER"), ("heading", "TEXT NOT NULL DEFAULT ''")):
            if col not in have:  # a store made before plan 2.1 (the projects PassageStore) gains the new columns
                db.execute(f"ALTER TABLE passages ADD COLUMN {col} {ddl}")  # noqa: S608 - fixed names
        db.execute("CREATE INDEX IF NOT EXISTS passages_doc ON passages(doc_id)")
        db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('kind', ?)", (self.kind,))
        db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        # a pre-2.1 passage has no vector row and no queue row: queue it so it is embedded once an embedder runs
        db.execute(
            "INSERT OR IGNORE INTO embed_queue(passage_id, queued_at) SELECT p.id, ? FROM passages p"
            " WHERE NOT EXISTS (SELECT 1 FROM vectors v WHERE v.passage_id = p.id)", (_now(),))

    def _dirty(self) -> None:
        self._matrix = None

    # ---- writing --------------------------------------------------------------------------------------------

    def add(self, source: str, text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Store ``text`` as a document of ``source`` and return ``{"doc_id", "source_id", "passage_ids"}``.

        ``meta``: ``title``, ``path``, ``url``, ``location``, ``date`` (ISO, the document's own date), ``tier``
        (T0 to T3, default T1), ``folder`` (the consented folder it came from), ``namespace`` (an agent's own shelf,
        for example ``agent:researcher``), ``external_id`` and ``source_id``. Adding the same ``(source, source_id)``
        again replaces the old passages; ``source_id`` defaults to ``external_id`` and then to a hash of the text."""
        meta = dict(meta or {})
        source = _clean(source, 40).lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", source):
            raise ValueError("source must be a short lower-case word such as file, memory, page or paper")
        text = _CONTROL.sub("", str(text or ""))
        if not text.strip():
            raise ValueError("there is no text to store")
        if len(text) > TEXT_MAX_CHARS:
            raise ValueError(f"the text is too long ({len(text)} characters, the limit is {TEXT_MAX_CHARS})")
        tier = _clean(meta.get("tier") or "T1", 2).upper()
        if tier not in TIERS:
            raise ValueError("tier must be T0, T1, T2 or T3")
        external_id = _clean(meta.get("external_id"), 300)
        source_id = _clean(meta.get("source_id") or external_id, 300) or hashlib.sha256(
            text.encode("utf-8", "replace")).hexdigest()[:32]
        title = _clean(meta.get("title"), 300) or _clean(Path(str(meta.get("path") or "")).name, 300) \
            or text.strip().splitlines()[0][:80]
        chunks = chunk_text(text)
        if len(chunks) > PASSAGES_PER_DOC_MAX:
            raise ValueError(f"the text makes {len(chunks)} passages, the limit is {PASSAGES_PER_DOC_MAX}")
        extra = {k: v for k, v in meta.items() if k not in _META_KEYS and _jsonable(v)}
        now = _now()
        with self._lock, self._connect() as db:
            self._drop(db, source, source_id)
            db.execute(
                "INSERT INTO documents(source, source_id, title, path, url, location, folder, namespace, tier,"
                " external_id, doc_date, meta, added_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source, source_id, title, _clean(meta.get("path"), 1000), _clean(meta.get("url"), 2000),
                 _clean(meta.get("location"), 300), _clean(meta.get("folder"), 1000),
                 _clean(meta.get("namespace"), 120), tier, external_id, _clean(meta.get("date"), 40),
                 json.dumps(extra, sort_keys=True)[:20_000], now, now))
            doc_id = int(db.execute("SELECT id FROM documents WHERE source=? AND source_id=?",
                                    (source, source_id)).fetchone()[0])
            ids = [self._insert_passage(db, doc_id, source, source_id, title, seq, ch, now)
                   for seq, ch in enumerate(chunks)]
        self._dirty()
        return {"doc_id": doc_id, "source_id": source_id, "passage_ids": ids}

    @staticmethod
    def _insert_passage(db: sqlite3.Connection, doc_id: int, source: str, source_id: str, title: str, seq: int,
                        ch: Chunk, now: float) -> int:
        cur = db.execute(
            "INSERT INTO passages(source, source_id, title, seq, text, added_at, doc_id, page, heading)"
            " VALUES (?,?,?,?,?,?,?,?,?)", (source, source_id, title, seq, ch.text, now, doc_id, ch.page, ch.heading))
        pid = int(cur.lastrowid or 0)
        db.execute("INSERT INTO passages_fts(rowid, text, title) VALUES (?,?,?)", (pid, ch.text, title))
        db.execute("INSERT OR REPLACE INTO embed_queue(passage_id, queued_at) VALUES (?,?)", (pid, now))
        return pid

    def replace(self, source: str, source_id: str, title: str, text: str) -> int:
        """Compatibility with the projects PassageStore: replace one source's passages. Returns how many."""
        if not (text or "").strip():
            self.remove(source, source_id)
            return 0
        return len(self.add(source, text, {"title": title, "source_id": source_id})["passage_ids"])

    def remove(self, source: str, source_id: str) -> None:
        with self._lock, self._connect() as db:
            self._drop(db, source, source_id)
        self._dirty()

    @staticmethod
    def _drop(db: sqlite3.Connection, source: str, source_id: str) -> None:
        for row in db.execute("SELECT id, text, title FROM passages WHERE source=? AND source_id=?",
                              (source, source_id)).fetchall():
            db.execute("INSERT INTO passages_fts(passages_fts, rowid, text, title) VALUES ('delete', ?,?,?)",
                       (row["id"], row["text"], row["title"]))
            db.execute("DELETE FROM vectors WHERE passage_id=?", (row["id"],))
            db.execute("DELETE FROM embed_queue WHERE passage_id=?", (row["id"],))
        db.execute("DELETE FROM passages WHERE source=? AND source_id=?", (source, source_id))
        db.execute("DELETE FROM documents WHERE source=? AND source_id=?", (source, source_id))

    # ---- reading --------------------------------------------------------------------------------------------

    def count(self) -> int:
        with self._lock, self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM passages").fetchone()[0])

    def sources(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT source, source_id, title, COUNT(*) AS passages FROM passages"
                " GROUP BY source, source_id ORDER BY title")]

    def documents(self, source: str = "", limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT d.*, (SELECT COUNT(*) FROM passages p WHERE p.doc_id = d.id) AS passages FROM documents d"
                " WHERE (? = '' OR d.source = ?) ORDER BY d.updated_at DESC LIMIT ?", (source, source, limit)).fetchall()
        return [self._doc_dict(r) for r in rows]

    @staticmethod
    def _doc_dict(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d.pop("meta", None)
        try:
            d["meta"] = json.loads(r["meta"] or "{}")
        except ValueError:
            d["meta"] = {}
        return d

    def cite(self, passage_id: int) -> dict[str, Any]:
        """The verbatim passage with where it came from. Raises ``LookupError`` for an id this store never made."""
        with self._lock, self._connect() as db:
            r = db.execute(
                "SELECT p.id, p.source, p.source_id, p.title, p.seq, p.text, p.page, p.heading, d.path, d.url,"
                " d.location, d.folder, d.namespace, d.tier, d.doc_date FROM passages p"
                " LEFT JOIN documents d ON d.id = p.doc_id WHERE p.id=?", (_int(passage_id),)).fetchone()
        if r is None:
            raise LookupError("no such passage")
        return self._hit(r)

    @staticmethod
    def _hit(r: sqlite3.Row) -> dict[str, Any]:
        page = r["page"]
        where = f"p. {page}" if page else f"passage {r['seq'] + 1}"
        return {
            "passage_id": r["id"], "source": r["source"], "source_id": r["source_id"], "title": r["title"],
            "passage": r["seq"] + 1, "page": page, "heading": r["heading"] or "", "text": r["text"],
            "path": r["path"] or "", "url": r["url"] or "", "location": r["location"] or "",
            "folder": r["folder"] or "", "namespace": r["namespace"] or "", "tier": r["tier"] or "T1",
            "date": r["doc_date"] or "", "citation": f"{r['title']}, {where}",
        }

    def verify_quote(self, passage_id: int, quote: str) -> dict[str, Any]:
        """Is ``quote`` word for word inside this passage? Whitespace and typographic quotes are ignored; case,
        wording and numbers are not. A quote of fewer than three words proves nothing and is refused."""
        passage = self.cite(passage_id)
        q = normalise_quote(quote)
        if len(q.split()) < MIN_QUOTE_WORDS:
            return {"ok": False, "reason": f"a quote needs at least {MIN_QUOTE_WORDS} words to check",
                    "passage_id": passage["passage_id"]}
        if q in normalise_quote(passage["text"]):
            return {"ok": True, "reason": "", "passage_id": passage["passage_id"], "citation": passage["citation"]}
        return {"ok": False, "reason": "the quote does not appear in this passage", "passage_id": passage["passage_id"]}

    # ---- search ---------------------------------------------------------------------------------------------

    def _filter_sql(self, filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        f = dict(filters or {})
        known = {"source", "namespace", "folder", "date_from", "date_to", "max_tier"}
        extra = set(f) - known
        if extra:
            raise ValueError(f"unknown search filter: {sorted(extra)[0]}")
        where, args = [], []
        if f.get("source"):
            srcs = f["source"] if isinstance(f["source"], (list, tuple)) else [f["source"]]
            where.append("p.source IN (" + ",".join("?" * len(srcs)) + ")")
            args += [str(s) for s in srcs]
        if f.get("namespace"):
            where.append("d.namespace = ?")
            args.append(str(f["namespace"]))
        if f.get("folder"):
            folder = str(f["folder"]).rstrip("/")
            where.append("(d.folder = ? OR d.folder LIKE ? ESCAPE '\\')")
            args += [folder, folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"]
        if f.get("date_from"):
            where.append("d.doc_date != '' AND d.doc_date >= ?")
            args.append(str(f["date_from"]))
        if f.get("date_to"):
            where.append("d.doc_date != '' AND d.doc_date <= ?")
            args.append(str(f["date_to"]))
        if f.get("max_tier"):
            tier = str(f["max_tier"]).upper()
            if tier not in TIERS:
                raise ValueError("max_tier must be T0, T1, T2 or T3")
            allowed = TIERS[: TIERS.index(tier) + 1]
            where.append("(d.tier IS NULL OR d.tier IN (" + ",".join("?" * len(allowed)) + "))")
            args += list(allowed)
        return (" AND " + " AND ".join(where)) if where else "", args

    def _fts_rows(self, db: sqlite3.Connection, match: str, where: str, args: list[Any], limit: int) -> list[sqlite3.Row]:
        if not match:
            return []
        return db.execute(
            "SELECT p.id AS pid, snippet(passages_fts, 0, '[', ']', ' ... ', 24) AS snippet, bm25(passages_fts) AS rank"  # noqa: S608 - the fragments are fixed text with ? placeholders
            " FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid LEFT JOIN documents d ON d.id = p.doc_id"
            " WHERE passages_fts MATCH ?" + where + " ORDER BY rank LIMIT ?", [match, *args, limit]).fetchall()

    def search(self, query: str, limit: int = 8, *, filters: dict[str, Any] | None = None,
               query_vector: list[float] | None = None, model: str = "") -> list[dict[str, Any]]:
        """Hybrid search. Keyword (BM25) always runs; with ``query_vector`` the cosine ranking is fused in.
        Every word is required first; if nothing matches, any word may match. Each hit carries ``passage_id``,
        ``citation``, ``snippet`` and ``score``; ``matched`` says ``keyword``, ``vector`` or both."""
        limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
        where, args = self._filter_sql(filters)
        with self._lock, self._connect() as db:
            kw = self._fts_rows(db, fts_and(query), where, args, CANDIDATES)
            if not kw:
                kw = self._fts_rows(db, fts_or(query), where, args, CANDIDATES)
            vec = self._vector_rank(db, query_vector, model, where, args) if query_vector else []
            scores: dict[int, float] = {}
            how: dict[int, set[str]] = {}
            snippets: dict[int, str] = {}
            for rank, r in enumerate(kw):
                scores[r["pid"]] = scores.get(r["pid"], 0.0) + 1.0 / (RRF_K + rank + 1)
                how.setdefault(r["pid"], set()).add("keyword")
                snippets[r["pid"]] = r["snippet"]
            for rank, pid in enumerate(vec):
                scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
                how.setdefault(pid, set()).add("vector")
            if not scores:
                return []
            top = sorted(scores, key=lambda pid: -scores[pid])[: max(limit * 3, limit)]
            rows = {r["id"]: r for r in db.execute(
                "SELECT p.id, p.source, p.source_id, p.title, p.seq, p.text, p.page, p.heading, p.added_at, d.path, d.url,"  # noqa: S608 - the fragments are fixed text with ? placeholders
                " d.location, d.folder, d.namespace, d.tier, d.doc_date FROM passages p"
                " LEFT JOIN documents d ON d.id = p.doc_id WHERE p.id IN (" + ",".join("?" * len(top)) + ")", top)}
        now = _now()
        out = []
        for pid in top:
            row = rows.get(pid)
            if row is None:
                continue
            r = row
            hit = self._hit(r)
            hit["snippet"] = snippets.get(pid) or hit["text"][:200]
            hit["matched"] = sorted(how[pid])
            hit["score"] = round(scores[pid] * (1 + RECENCY_WEIGHT * _recency(r["doc_date"], r["added_at"], now)), 6)
            out.append(hit)
        out.sort(key=lambda h: -h["score"])
        return out[:limit]

    def _vector_rank(self, db: sqlite3.Connection, query_vector: list[float], model: str, where: str,
                     args: list[Any]) -> list[int]:
        import numpy as np

        ids, matrix, have_model = self._vectors(db, model)
        if matrix is None or len(ids) == 0:
            return []
        q = np.asarray(query_vector, dtype=np.float32)
        if q.shape[0] != matrix.shape[1]:
            raise ValueError(f"the query vector has {q.shape[0]} numbers, this store's vectors have {matrix.shape[1]}")
        norm = float(np.linalg.norm(q))
        if norm == 0.0:
            return []
        sims = matrix @ (q / norm)
        if where:
            allowed = {r[0] for r in db.execute(
                "SELECT p.id FROM passages p LEFT JOIN documents d ON d.id = p.doc_id WHERE 1=1" + where, args)}  # noqa: S608 - the fragments are fixed text with ? placeholders
            mask = np.fromiter((int(i) in allowed for i in ids), dtype=bool, count=len(ids))
            sims = np.where(mask, sims, -2.0)
        order = np.argsort(-sims)[:CANDIDATES]
        return [int(ids[i]) for i in order if sims[i] > 0.0]

    def _vectors(self, db: sqlite3.Connection, model: str):
        import numpy as np

        if self._matrix is not None and (not model or self._matrix[2] == model):
            return self._matrix
        rows = db.execute("SELECT passage_id, model, dim, vec FROM vectors" + (" WHERE model=?" if model else ""),  # noqa: S608 - the fragments are fixed text with ? placeholders
                          [model] if model else []).fetchall()
        if not rows:
            return [], None, model
        dim = rows[0]["dim"]
        rows = [r for r in rows if r["dim"] == dim and r["model"] == rows[0]["model"]]
        ids = np.fromiter((r["passage_id"] for r in rows), dtype=np.int64, count=len(rows))
        matrix = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
        self._matrix = (ids, matrix, rows[0]["model"])
        return self._matrix

    # ---- embedding queue ------------------------------------------------------------------------------------

    def queue_stats(self) -> dict[str, int]:
        with self._lock, self._connect() as db:
            q = db.execute("SELECT COUNT(*) FROM embed_queue WHERE blocked = ''").fetchone()[0]
            b = db.execute("SELECT COUNT(*) FROM embed_queue WHERE blocked != ''").fetchone()[0]
            v = db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            p = db.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        return {"passages": int(p), "embedded": int(v), "waiting": int(q), "blocked": int(b)}

    def blocked_reasons(self) -> dict[str, int]:
        with self._lock, self._connect() as db:
            return {r["blocked"]: r["n"] for r in db.execute(
                "SELECT blocked, COUNT(*) AS n FROM embed_queue WHERE blocked != '' GROUP BY blocked")}

    def embed_pending(self, embedder: Any, *, batch: int = 32, max_batches: int | None = None,
                      privacy_store: Any = None) -> dict[str, Any]:
        """Embed queued passages in batches. Each passage goes through ``gate.may_embed`` first when the embedder
        is remote; a refused passage is marked blocked with the plain reason and is looked at again next run (the
        owner may have changed a consent). A remote batch writes one egress ledger row (size and hash, never text).
        An embedder error stops the run, leaves the passages queued and is returned in ``error``."""
        import numpy as np

        from . import gate

        batch = max(1, min(int(batch), 100))
        done = blocked = batches = 0
        error = ""
        dim_seen = int(getattr(embedder, "dim", 0) or 0)
        while max_batches is None or batches < max_batches:
            with self._lock, self._connect() as db:
                rows = db.execute(
                    "SELECT q.passage_id AS pid, p.text AS text, d.folder AS folder, d.tier AS tier FROM embed_queue q"
                    " JOIN passages p ON p.id = q.passage_id LEFT JOIN documents d ON d.id = p.doc_id"
                    " WHERE q.blocked = '' ORDER BY q.passage_id LIMIT ?", (batch,)).fetchall()
                if not rows:
                    # blocked rows are retried once per run, so a consent given since is noticed
                    retry = db.execute("SELECT COUNT(*) FROM embed_queue WHERE blocked != ''").fetchone()[0]
                    if retry and batches == 0:
                        db.execute("UPDATE embed_queue SET blocked = '' WHERE blocked != ''")
                        batches += 1
                        continue
                    break
            batches += 1
            ok_rows = []
            with self._lock, self._connect() as db:
                for r in rows:
                    allowed, why = gate.may_embed(
                        {"text": r["text"], "tier": r["tier"] or "T1", "folder": r["folder"] or ""},
                        remote=bool(getattr(embedder, "remote", True)), privacy_store=privacy_store)
                    if allowed:
                        ok_rows.append(r)
                    else:
                        db.execute("UPDATE embed_queue SET blocked=? WHERE passage_id=?", (why, r["pid"]))
                        blocked += 1
            if not ok_rows:
                continue
            texts = [r["text"] for r in ok_rows]
            try:
                if getattr(embedder, "remote", True):
                    from dourmouse import privacy

                    privacy.record_egress(backend="gemini", model=str(getattr(embedder, "model", "")),
                                          text="\n".join(texts), origin="library-embed",
                                          sources=["files"], size=sum(len(t.encode("utf-8")) for t in texts))
                vectors = embedder.embed_documents(texts)
                if len(vectors) != len(texts):
                    raise ValueError("the embedder returned a different number of vectors than passages")
            except Exception as exc:  # noqa: BLE001 - any embedder failure ends this run, the passages stay queued
                error = f"{type(exc).__name__}: {exc}"[:300]
                with self._lock, self._connect() as db:
                    db.executemany("UPDATE embed_queue SET attempts = attempts + 1, last_error=? WHERE passage_id=?",
                                   [(error, r["pid"]) for r in ok_rows])
                break
            now = _now()
            with self._lock, self._connect() as db:
                for r, v in zip(ok_rows, vectors, strict=True):
                    arr = np.asarray(v, dtype=np.float32)
                    n = float(np.linalg.norm(arr))
                    if arr.ndim != 1 or n == 0.0 or not math.isfinite(n):
                        db.execute("UPDATE embed_queue SET attempts = attempts + 1, last_error='unusable vector'"
                                   " WHERE passage_id=?", (r["pid"],))
                        continue
                    arr = arr / n
                    dim_seen = arr.shape[0]
                    db.execute("INSERT OR REPLACE INTO vectors(passage_id, model, dim, vec, made_at) VALUES (?,?,?,?,?)",
                               (r["pid"], str(getattr(embedder, "model", "")), dim_seen, arr.tobytes(), now))
                    db.execute("DELETE FROM embed_queue WHERE passage_id=?", (r["pid"],))
                    done += 1
            self._dirty()
        return {"embedded": done, "blocked": blocked, "error": error, "dim": dim_seen, "queue": self.queue_stats()}

    # ---- scan state (the folder connector in plan 2.3 keeps its place here) ---------------------------------

    def scan_get(self, path: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            r = db.execute("SELECT * FROM scan_state WHERE path=?", (path,)).fetchone()
        return dict(r) if r else None

    def scan_put(self, path: str, size: int, mtime: float, doc_id: int | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO scan_state(path, size, mtime, doc_id, seen_at) VALUES (?,?,?,?,?)",
                       (path, int(size), float(mtime), doc_id, _now()))

    def scan_paths(self, prefix: str = "") -> list[str]:
        with self._lock, self._connect() as db:
            return [r[0] for r in db.execute("SELECT path FROM scan_state WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                                             (prefix, prefix.rstrip("/").replace("\\", "\\\\").replace("%", "\\%")
                                              .replace("_", "\\_") + "/%"))]

    def scan_forget(self, path: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM scan_state WHERE path=?", (path,))

    # ---- whole-store operations -----------------------------------------------------------------------------

    def forget_folder(self, folder: str) -> int:
        """Remove every document that came from ``folder`` (or below it), with its passages and vectors. This is
        what revoking a folder's consent does to the store. Returns the number of documents removed."""
        folder = folder.rstrip("/")
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT source, source_id FROM documents WHERE folder = ? OR folder LIKE ? ESCAPE '\\'",
                (folder, folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")).fetchall()
            for r in rows:
                self._drop(db, r["source"], r["source_id"])
            db.execute("DELETE FROM scan_state WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                       (folder, folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"))
        self._dirty()
        return len(rows)

    def snapshot(self, dest: Path) -> Path:
        """A consistent copy of this store at ``dest`` (SQLite's online backup), for a local backup or, later, the
        encrypted backup in plan 2.7."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.unlink(missing_ok=True)
        with self._lock, self._connect() as src:
            out = sqlite3.connect(str(tmp))
            try:
                src.backup(out)
            finally:
                out.close()
        tmp.replace(dest)
        return dest


_META_KEYS = {"title", "path", "url", "location", "date", "tier", "folder", "namespace", "external_id", "source_id"}


def _jsonable(v: Any) -> bool:
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise LookupError("no such passage") from exc


def _recency(doc_date: str, added_at: float, now: float) -> float:
    """1.0 for something dated now, 0.5 a year ago, towards 0 for old things. A date that does not parse falls back
    to when the passage was added."""
    ts = added_at
    if doc_date:
        with contextlib.suppress(ValueError):
            ts = time.mktime(time.strptime(doc_date[:10], "%Y-%m-%d"))
    age_days = max(0.0, (now - ts) / 86400.0)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
