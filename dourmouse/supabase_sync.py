"""Local-first sync between the local SQLite memory store and Supabase.

The local store stays authoritative for reads. Dourmouse works with no
network, no account and no Supabase project configured at all; this module
only ever adds a background reconciliation on top of that. Nothing here is on
the read path of a chat turn.

Three things drove the design, and each one is a real hazard rather than a
hypothetical:

1. OFFLINE IS A NORMAL STATE, NOT AN ERROR.
   Writes go into a local outbox first and are drained when the network comes
   back. A laptop that is offline for a week queues a week of edits and
   reconciles in one pass. "Offline" is reported as an outcome, never as a
   failure, and never as an exception.

2. THIS MODULE MUST NEVER RAISE INTO A CHAT TURN.
   Every public method returns a ``SyncOutcome`` and catches ``Exception``.
   This is not defensive boilerplate -- it is the specific bug this codebase
   has already hit twice. ``MemoryStore`` never raises from ``remember`` /
   ``search`` / ``get``, so callers were written assuming that, and
   ``RemoteMemoryStore`` -- advertised in its own docstring as "a drop-in" --
   genuinely does raise (``RemoteMemoryStoreUnavailable`` on an unreachable
   host, ``NotImplementedError`` from ``count(source=...)``, and
   ``_unsupported()`` from seven more methods). One of those escaped into a
   live request and dropped the connection outright. A cloud-sync module is
   strictly more exposed than that one: DNS, TLS, 401s, 5xx, rate limits.
   So the boundary is drawn here, once, and it holds for whatever store it is
   handed -- ``self._store`` calls are individually guarded for exactly this
   reason.

3. THE CONFLICT RULE IS NOT OURS TO INVENT.
   ``public.sync_facts`` already fixes it, server-side, in one transaction:

       on conflict (user_id, source, title) do update
         set ... where excluded.updated_at > public.facts.updated_at

   Strictly newer wins; equal timestamps do NOT overwrite. ``pull()`` mirrors
   that exactly -- ``>``, not ``>=`` -- so a round trip is stable instead of
   flapping between two devices whose clocks agree to the second. Where a
   local read-compare-write could disagree with the server, the server wins,
   because its version is the atomic one.

THE TIMEZONE BUG THIS MODULE EXISTS TO PREVENT
----------------------------------------------
``MemoryStore.remember()`` stamps rows with
``datetime.now().isoformat(timespec="seconds")`` -- local wall time, NAIVE, no
offset. Postgres casts a naive string to ``timestamptz`` using the session
TimeZone, which is UTC on Supabase. So a fact written on a machine at UTC+4
(this developer's machine, verified: ``date +%z`` -> ``+0400``) arrives stamped
four hours in the FUTURE.

That is not cosmetic. The conflict rule is a ``>`` comparison on exactly this
column, so a future-dated row can never be overwritten by a correctly-stamped
one -- for four hours on a UTC+4 machine, and permanently against any device
further west. It silently wedges sync in the direction that looks like it is
working. Every timestamp crossing this boundary therefore goes through
``to_utc_iso()``, which interprets a naive stamp as local time (which is what
it is) and converts it to an explicit UTC offset.

WHY THERE IS A SYNC-STATE TABLE
-------------------------------
``MemoryStore.remember()`` always sets ``updated_at`` to *now* and offers no
way to preserve an incoming timestamp. So a row pulled from the cloud is
immediately stamped newer-than-the-cloud locally, and a naive implementation
would push it straight back, which would bump the remote timestamp, which the
other device would then pull... a permanent echo between two machines that
have identical data. The fix is to track what was last successfully synced
(remote timestamp + a hash of the body) and push only when the local body has
genuinely CHANGED -- content, not timestamp churn. State lives in this
module's own SQLite file; ``memory_store.py``'s schema is not touched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

__all__ = [
    "SyncOutcome",
    "SupabaseSync",
    "SyncOutbox",
    "to_utc_iso",
    "body_hash",
]

# A transport returns (status_code, body_bytes). Injectable so the tests are
# hermetic and never touch the network.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]

_DEFAULT_OUTBOX_NAME = "supabase_outbox.db"
_PUSH_BATCH = 200


class _Store(Protocol):
    """The slice of MemoryStore this module uses. Deliberately tiny: the
    fewer methods depended on, the fewer places a RemoteMemoryStore
    substitution can raise something a MemoryStore never would."""

    def remember(self, source: str, title: str, body: str) -> str: ...
    def get(self, source: str, title: str) -> dict[str, Any] | None: ...
    def all_facts(self) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #

def to_utc_iso(value: Any) -> str:
    """Normalise any timestamp this codebase produces to UTC ISO-8601.

    A NAIVE input is interpreted as LOCAL time, because that is what
    ``MemoryStore.remember()`` actually writes (``datetime.now()``). Treating
    it as UTC instead -- the tempting one-liner -- is precisely the bug
    described in this module's docstring: it shifts every local fact into the
    future by the machine's UTC offset and wedges the ``>`` conflict rule.

    Unparseable input falls back to *now*, never to the epoch. Epoch would
    mean "infinitely stale", so a fact with a slightly malformed timestamp
    would be silently ignored by the server forever; *now* at worst makes one
    write win a race it might have lost, which is recoverable and visible.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return _now_utc_iso()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return _now_utc_iso()
    if dt.tzinfo is None:
        # astimezone() on a naive datetime assumes it is local wall time --
        # which is exactly what it is.
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def body_hash(body: str) -> str:
    """Content identity for a fact body. Used to tell a genuine local edit
    apart from a re-stamped copy of what the cloud already has."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# outcome
# --------------------------------------------------------------------------- #

@dataclass
class SyncOutcome:
    """What a sync attempt did. Returned, never raised.

    ``ok=True`` with ``offline=True`` is the normal, healthy offline case:
    nothing was lost, the work is queued. Callers that surface this to a user
    should read ``summary()`` rather than inventing their own wording.
    """

    ok: bool = True
    pushed: int = 0
    skipped: int = 0
    pulled: int = 0
    queued: int = 0
    offline: bool = False
    configured: bool = True
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if not self.configured:
            return "SUPABASE SYNC NOT CONFIGURED — local memory is unaffected."
        if self.offline:
            return (
                f"OFFLINE — {self.queued} change(s) queued locally and will sync "
                f"when the network returns. Nothing was lost."
            )
        if not self.ok:
            return f"SYNC FAILED — {self.error}. Local memory is unaffected."
        return (
            f"synced: pushed {self.pushed}, pulled {self.pulled}, "
            f"skipped {self.skipped} (stale or already current), "
            f"{self.queued} still queued"
        )


# --------------------------------------------------------------------------- #
# the local outbox / sync-state database
# --------------------------------------------------------------------------- #

class SyncOutbox:
    """Durable local queue + last-synced bookkeeping.

    Its own SQLite file, NOT a new table inside ``atlas_memory.db``:
    ``memory_store.py`` is a separate module with its own schema and its own
    hard-won multi-process WAL story, and bolting cloud-sync state into it
    would couple two lifecycles that have no reason to be coupled (dropping
    this file must be a harmless "resync from scratch", not data loss).

    WAL + a busy timeout for the same reason ``memory_store.py`` documents:
    the webui process and a background sync can genuinely have this open at
    once.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                -- Keyed on (source, title), the same natural key the local
                -- UNIQUE(source, title) and the cloud
                -- UNIQUE(user_id, source, title) both use. Queueing is
                -- therefore COALESCING: editing one fact five times offline
                -- queues one row carrying the final body, not five rows
                -- replaying an edit history the server would just collapse.
                CREATE TABLE IF NOT EXISTS outbox (
                    source      TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    body        TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,   -- always UTC ISO-8601
                    queued_at   TEXT NOT NULL,
                    attempts    INTEGER NOT NULL DEFAULT 0,
                    last_error  TEXT,
                    PRIMARY KEY (source, title)
                );

                -- What the cloud last confirmed. `body_sha` is what makes a
                -- genuine local edit distinguishable from a row we pulled and
                -- MemoryStore.remember() then re-stamped with now(); without
                -- it, every pull would bounce straight back as a push.
                CREATE TABLE IF NOT EXISTS sync_state (
                    source              TEXT NOT NULL,
                    title               TEXT NOT NULL,
                    remote_updated_at   TEXT NOT NULL,
                    body_sha            TEXT NOT NULL,
                    PRIMARY KEY (source, title)
                );
                """
            )
            self._conn.commit()

    # -- queue ------------------------------------------------------------- #

    def enqueue(self, source: str, title: str, body: str, updated_at: str) -> None:
        """Queue one change. A newer version replaces a queued older one.

        The ``WHERE excluded.updated_at > outbox.updated_at`` guard is the
        same rule ``sync_facts`` applies server-side, applied here too so the
        queue cannot itself reorder a fact backwards while offline.
        """
        stamp = to_utc_iso(updated_at)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO outbox (source, title, body, updated_at, queued_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, title) DO UPDATE SET
                    body       = excluded.body,
                    updated_at = excluded.updated_at,
                    queued_at  = excluded.queued_at,
                    attempts   = 0,
                    last_error = NULL
                WHERE excluded.updated_at > outbox.updated_at
                """,
                (source, title, body, stamp, _now_utc_iso()),
            )
            self._conn.commit()

    def pending(self, limit: int = _PUSH_BATCH) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, title, body, updated_at FROM outbox "
                "ORDER BY queued_at LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def depth(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"])

    def clear(self, keys: list[tuple[str, str]]) -> None:
        """Drop rows the server has accepted."""
        if not keys:
            return
        with self._lock:
            self._conn.executemany(
                "DELETE FROM outbox WHERE source = ? AND title = ?", keys
            )
            self._conn.commit()

    def record_failure(self, keys: list[tuple[str, str]], error: str) -> None:
        """Leave the rows queued, but record why. Kept deliberately: a queue
        that silently retries forever with no visible reason is unfixable."""
        if not keys:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? "
                "WHERE source = ? AND title = ?",
                [(error[:500], s, t) for s, t in keys],
            )
            self._conn.commit()

    # -- sync state -------------------------------------------------------- #

    def mark_synced(self, source: str, title: str, remote_updated_at: str, body: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sync_state (source, title, remote_updated_at, body_sha)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, title) DO UPDATE SET
                    remote_updated_at = excluded.remote_updated_at,
                    body_sha          = excluded.body_sha
                """,
                (source, title, to_utc_iso(remote_updated_at), body_hash(body)),
            )
            self._conn.commit()

    def state(self, source: str, title: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT remote_updated_at, body_sha FROM sync_state "
                "WHERE source = ? AND title = ?",
                (source, title),
            ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #

def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        # The server DID answer. A 401/403/409 is a real, actionable answer and
        # must not be reported as "offline" -- queuing forever against a bad
        # token would look like a network problem and never get fixed. Caught
        # before URLError, its own superclass, exactly as RemoteMemoryStore
        # does for the same reason.
        return exc.code, exc.read()


class _Offline(Exception):
    """Internal: genuinely could not reach the server. Never escapes."""


# --------------------------------------------------------------------------- #
# the sync engine
# --------------------------------------------------------------------------- #

class SupabaseSync:
    """Local-first push/pull against ``public.facts``.

    Construction never raises for missing configuration -- an unconfigured
    instance reports ``configured=False`` from every method instead, so a
    caller can build one unconditionally at startup.
    """

    def __init__(
        self,
        store: _Store | None,
        *,
        url: str | None = None,
        anon_key: str | None = None,
        access_token: str | None = None,
        device_id: str | None = None,
        outbox_path: Path | str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._store = store
        self.url = (url or "").rstrip("/")
        self.anon_key = anon_key or ""
        # The USER's JWT. sync_facts starts with `if v_user_id is null then
        # raise` -- an anon-key-only call is rejected server-side, correctly.
        self.access_token = access_token or ""
        self.device_id = device_id or None
        self._transport = transport or _urllib_transport
        self.outbox = SyncOutbox(outbox_path or Path.cwd() / _DEFAULT_OUTBOX_NAME)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.anon_key and self.access_token and self._store)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _call(self, method: str, path: str, payload: Any = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            status, raw = self._transport(method, self.url + path, self._headers(), body)
        except Exception as exc:  # noqa: BLE001 -- DNS/TLS/socket/timeouts all land here
            raise _Offline(str(exc)) from exc
        if status >= 500:
            # The server is up but broken. Treat it like offline: keep the work
            # queued and retry, rather than discarding a change over a 502.
            raise _Offline(f"HTTP {status}")
        if status >= 400:
            raise RuntimeError(f"HTTP {status}: {raw.decode('utf-8', 'replace')[:300]}")
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"unparseable response: {exc}") from exc

    # -- enqueue ----------------------------------------------------------- #

    def queue(self, source: str, title: str, body: str, updated_at: str | None = None) -> SyncOutcome:
        """Record a local change for eventual upload. Safe to call on the
        write path: it is one local SQLite upsert and never touches the
        network."""
        try:
            self.outbox.enqueue(source, title, body, updated_at or _now_utc_iso())
            return SyncOutcome(queued=self.outbox.depth(), configured=self.configured)
        except Exception as exc:  # noqa: BLE001
            return SyncOutcome(ok=False, error=f"could not queue locally: {exc}")

    def queue_all_local(self) -> SyncOutcome:
        """Queue every local fact whose body differs from what the cloud last
        confirmed. This is the first-run / re-link path, and the reason it
        compares HASHES rather than timestamps is the echo loop described in
        the module docstring."""
        if self._store is None:
            return SyncOutcome(ok=False, configured=False, error="no local store")
        try:
            facts = self._store.all_facts()
        except Exception as exc:  # noqa: BLE001 -- RemoteMemoryStore.all_facts() genuinely raises
            return SyncOutcome(ok=False, error=f"local store cannot enumerate facts: {exc}")
        queued = 0
        for f in facts:
            source, title = f.get("source") or "", f.get("title") or ""
            body = f.get("body") or ""
            if not source or not title:
                continue
            st = self.outbox.state(source, title)
            if st and st["body_sha"] == body_hash(body):
                continue  # already up there, unchanged
            stamp = f.get("updated_at") or _now_utc_iso()
            try:
                self.outbox.enqueue(source, title, body, stamp)
                queued += 1
            except Exception:  # noqa: BLE001
                continue
        return SyncOutcome(queued=self.outbox.depth(), detail={"newly_queued": queued},
                           configured=self.configured)

    # -- push -------------------------------------------------------------- #

    def push(self) -> SyncOutcome:
        """Drain the outbox through ``public.sync_facts``.

        The RPC is one server-side transaction with the conflict rule baked
        in, so this is a single call rather than a read-compare-write loop two
        devices could interleave.
        """
        if not self.configured:
            return SyncOutcome(ok=True, configured=False, queued=self._safe_depth())
        batch = self.outbox.pending()
        if not batch:
            return SyncOutcome(queued=0)

        payload = [
            {
                "source": r["source"],
                "title": r["title"],
                "body": r["body"],
                "updated_at": to_utc_iso(r["updated_at"]),
            }
            for r in batch
        ]
        keys = [(r["source"], r["title"]) for r in batch]

        try:
            data = self._call(
                "POST",
                "/rest/v1/rpc/sync_facts",
                {"p_device_id": self.device_id, "p_facts": payload},
            )
        except _Offline as exc:
            self._safe_record_failure(keys, str(exc))
            return SyncOutcome(offline=True, queued=self._safe_depth(),
                               detail={"reason": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._safe_record_failure(keys, str(exc))
            return SyncOutcome(ok=False, error=str(exc), queued=self._safe_depth())

        row = (data or [{}])[0] if isinstance(data, list) else (data or {})
        synced = int(row.get("synced") or 0)
        skipped = int(row.get("skipped") or 0)

        # Clear the WHOLE batch, including the rows the server skipped.
        #
        # `skipped` does NOT mean "failed". sync_facts computes it as
        # `count(input) - synced`, so it covers both a row whose updated_at
        # was not strictly newer than what is already stored -- meaning the
        # cloud is already at least as current, so there is nothing left to
        # send -- and a row it dropped as malformed, which retrying cannot
        # fix. Keeping either queued would retry forever.
        self.outbox.clear(keys)
        for r in batch:
            self._safe_mark_synced(r["source"], r["title"], r["updated_at"], r["body"])

        return SyncOutcome(pushed=synced, skipped=skipped, queued=self._safe_depth())

    # -- pull -------------------------------------------------------------- #

    def pull(self) -> SyncOutcome:
        """Fetch the caller's cloud facts and apply the ones that are newer.

        RLS scopes the SELECT to this user; there is no user_id filter here
        and there must not be one.
        """
        if not self.configured:
            return SyncOutcome(ok=True, configured=False)
        try:
            rows = self._call(
                "GET",
                "/rest/v1/facts?select=source,title,body,updated_at&order=updated_at.asc",
            )
        except _Offline as exc:
            return SyncOutcome(offline=True, queued=self._safe_depth(),
                               detail={"reason": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return SyncOutcome(ok=False, error=str(exc), queued=self._safe_depth())

        if not isinstance(rows, list):
            return SyncOutcome(ok=False, error="facts endpoint did not return a list")

        applied = 0
        skipped = 0
        for row in rows:
            if not isinstance(row, dict):
                skipped += 1
                continue
            source = row.get("source") or ""
            title = row.get("title") or ""
            body = row.get("body") or ""
            if not source or not title or not body:
                skipped += 1
                continue
            remote_ts = to_utc_iso(row.get("updated_at"))

            try:
                local = self._store.get(source, title)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 -- RemoteMemoryStore.get() raises when unreachable
                skipped += 1
                continue

            if local is not None:
                local_ts = to_utc_iso(local.get("updated_at"))
                # THE SAME RULE AS sync_facts: strictly newer wins. `>`, not
                # `>=` -- with `>=`, two devices whose second-resolution
                # timestamps tie would each rewrite the other on every cycle.
                if not (remote_ts > local_ts):
                    # The cloud is not newer. If the bodies also match, this
                    # row is fully reconciled and the state row is refreshed so
                    # queue_all_local() will not re-queue it.
                    if body_hash(local.get("body") or "") == body_hash(body):
                        self._safe_mark_synced(source, title, remote_ts, body)
                    skipped += 1
                    continue
                if body_hash(local.get("body") or "") == body_hash(body):
                    # Newer stamp, identical content: nothing to write. Record
                    # the state and move on rather than churning the FTS index
                    # (MemoryStore.remember() re-indexes and drops the cached
                    # embedding on every call).
                    self._safe_mark_synced(source, title, remote_ts, body)
                    skipped += 1
                    continue

            try:
                self._store.remember(source, title, body)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 -- never escapes into a chat turn
                skipped += 1
                continue
            # remember() stamps this row with local now(), which is newer than
            # remote_ts. Recording the remote stamp + body hash here is what
            # stops the next push from bouncing it straight back.
            self._safe_mark_synced(source, title, remote_ts, body)
            applied += 1

        return SyncOutcome(pulled=applied, skipped=skipped, queued=self._safe_depth())

    # -- combined ---------------------------------------------------------- #

    def sync(self) -> SyncOutcome:
        """Push then pull. Push first so local work is never overwritten by a
        pull before it has had its chance at the conflict rule."""
        if not self.configured:
            return SyncOutcome(ok=True, configured=False, queued=self._safe_depth())
        pushed = self.push()
        pulled = self.pull()
        if pushed.offline or pulled.offline:
            return SyncOutcome(
                offline=True,
                pushed=pushed.pushed,
                pulled=pulled.pulled,
                queued=self._safe_depth(),
                detail={"push": pushed.detail, "pull": pulled.detail},
            )
        ok = pushed.ok and pulled.ok
        return SyncOutcome(
            ok=ok,
            pushed=pushed.pushed,
            pulled=pulled.pulled,
            skipped=pushed.skipped + pulled.skipped,
            queued=self._safe_depth(),
            error=pushed.error or pulled.error,
        )

    # -- internals --------------------------------------------------------- #

    def _safe_depth(self) -> int:
        try:
            return self.outbox.depth()
        except Exception:  # noqa: BLE001
            return 0

    def _safe_mark_synced(self, source: str, title: str, ts: str, body: str) -> None:
        try:
            self.outbox.mark_synced(source, title, ts, body)
        except Exception:  # noqa: BLE001
            pass

    def _safe_record_failure(self, keys: list[tuple[str, str]], err: str) -> None:
        try:
            self.outbox.record_failure(keys, err)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self.outbox.close()
        except Exception:  # noqa: BLE001
            pass
