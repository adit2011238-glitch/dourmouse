"""One attention feed for the whole app (plan 1.8, PL-03, finding 204).

Three kinds of item, kept apart on purpose:

* ``needs_you``: something is parked until the owner acts (an approval, a job waiting).
* ``finished``: something the owner asked for is done.
* ``heads_up``: something the owner should know about but nothing is waiting.

Every item names the screen it belongs to, an optional anchor on that screen and a few link arguments, so a toast, a
native notification and the feed itself all open the same place. The store is SQLite in the workspace, so the feed
survives a restart (the older in-memory ``AttentionQueue`` in ``webui.py`` is a different, per-chat observer and is left
alone).

Text rules (the plan's "notification text never carries bodies, file names from mail or secrets"):

* titles and details are cut short, control characters are removed and anything that looks like a key or token is
  replaced by ``[hidden]``;
* an item from a private source (mail, calendar, files, browser) is shown natively under a fixed sentence for its
  kind, never under its own title, because a native notification is readable on a locked screen.

Quiet hours and the per-kind switches decide whether an item is also shown as a toast or sent natively. Both are
decided here, once, and travel with the event, so the page and the Electron shell never disagree.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dourmouse.config import workspace_dir

KINDS = ("needs_you", "finished", "heads_up")
#: Sources whose own text must never leave the window in a native notification.
PRIVATE_SOURCES = frozenset({"mail", "calendar", "files", "browser", "memory"})
#: Only these sources may put their detail line in a native notification. A parked approval's detail is the
#: action it wants to take (a mail body, a file name), so every other source shows its title and nothing else.
PLAIN_DETAIL_SOURCES = frozenset({"test", "system", "security"})
GENERIC_NATIVE = {
    "needs_you": "Something needs you",
    "finished": "Something finished",
    "heads_up": "A heads up",
}

_SCREEN = re.compile(r"^[a-z0-9_-]{1,64}$")
_ANCHOR = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PARAM_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SOURCE = re.compile(r"^[a-z0-9_.-]{1,40}$")
_DEDUPE = re.compile(r"^[A-Za-z0-9_.:@-]{1,120}$")
_MAX_PARAMS = 6
_MAX_PARAM_VALUE = 120
_MAX_TITLE = 90
_MAX_DETAIL = 200
_MAX_ITEMS = 500
_KEEP_SECONDS = 30 * 24 * 3600
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Things that look like a secret. Conservative on purpose: a false hit only hides a few characters.
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|xox[abp]-[A-Za-z0-9-]{10,}"
    r"|Bearer\s+[A-Za-z0-9._~+/=-]{12,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*\S+|[A-Fa-f0-9]{32,}|[A-Za-z0-9+/_-]{40,}={0,2})",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

DEFAULT_PREFS: dict[str, Any] = {
    "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
    "kinds": {
        "needs_you": {"toast": True, "native": True},
        "finished": {"toast": True, "native": True},
        "heads_up": {"toast": True, "native": False},
    },
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    screen TEXT NOT NULL,
    anchor TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    read_at REAL,
    resolved_at REAL,
    dismissed_at REAL
);
CREATE INDEX IF NOT EXISTS items_open ON items (dismissed_at, resolved_at, created);
CREATE INDEX IF NOT EXISTS items_key ON items (dedupe_key);
CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def safe_text(value: Any, limit: int) -> str:
    """One line, no control characters, no key-like strings, cut to ``limit``."""
    text = _CONTROL.sub(" ", str(value or ""))
    text = _SECRET.sub("[hidden]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def clean_params(params: Any) -> dict[str, str]:
    """Link arguments: at most a few, simple keys, short values. Anything else is refused, not trimmed."""
    if params in (None, {}, ""):
        return {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if len(params) > _MAX_PARAMS:
        raise ValueError("too many link arguments")
    out: dict[str, str] = {}
    for key, value in params.items():
        if not isinstance(key, str) or not _PARAM_KEY.match(key) or key == "anchor":
            raise ValueError("a link argument has a name that is not allowed")
        text = str(value)
        if len(text) > _MAX_PARAM_VALUE or _CONTROL.search(text):
            raise ValueError("a link argument value is too long or has control characters")
        out[key] = text
    return out


def href_for(screen: str, params: dict[str, str] | None = None, anchor: str = "") -> str:
    """The hash route for an item: ``#/screen?key=value&anchor=name``. Only called with validated parts."""
    href = "#/" + screen
    pairs = [f"{k}={quote(v, safe='')}" for k, v in sorted((params or {}).items())]
    if anchor:
        pairs.append("anchor=" + anchor)
    if pairs:
        href += "?" + "&".join(pairs)
    return href


def _minutes(hhmm: str) -> int:
    m = _HHMM.match(hhmm)
    if not m:
        raise ValueError("time must be HH:MM")
    return int(m.group(1)) * 60 + int(m.group(2))


def in_quiet_hours(prefs: dict[str, Any], now: datetime) -> bool:
    quiet = prefs.get("quiet_hours") or {}
    if not quiet.get("enabled"):
        return False
    start, end = _minutes(quiet["start"]), _minutes(quiet["end"])
    here = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= here < end
    return here >= start or here < end  # overnight, for example 22:00 to 07:00


class AttentionStore:
    def __init__(self, root: Path | None = None, clock=time.time) -> None:
        base = Path(root) if root is not None else workspace_dir()
        self.base = base
        self.dir = base / "attention"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "attention.db"
        self._clock = clock
        self._lock = threading.RLock()
        self._hub: Any = None
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ---- delivery -------------------------------------------------------------------------------------------------

    def bind_hub(self, hub: Any) -> None:
        """The server's event hub. Without one, items are still stored; clients find them on their next read."""
        self._hub = hub

    def _emit(self, event: dict[str, Any]) -> None:
        hub = self._hub
        if hub is None:
            return
        try:
            hub.broadcast(event)
        except Exception:  # noqa: BLE001 -- a broken stream must never stop the thing that raised the item
            pass

    # ---- prefs ----------------------------------------------------------------------------------------------------

    def prefs(self) -> dict[str, Any]:
        merged = json.loads(json.dumps(DEFAULT_PREFS))
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value FROM prefs WHERE key='prefs'").fetchone()
        if row:
            try:
                saved = json.loads(row["value"])
            except ValueError:
                saved = {}
            if isinstance(saved.get("quiet_hours"), dict):
                merged["quiet_hours"].update({k: v for k, v in saved["quiet_hours"].items() if k in merged["quiet_hours"]})
            for kind, sw in (saved.get("kinds") or {}).items():
                if kind in merged["kinds"] and isinstance(sw, dict):
                    merged["kinds"][kind].update({k: bool(v) for k, v in sw.items() if k in ("toast", "native")})
        return merged

    def set_prefs(self, change: dict[str, Any]) -> dict[str, Any]:
        current = self.prefs()
        quiet = change.get("quiet_hours")
        if quiet is not None:
            if not isinstance(quiet, dict):
                raise ValueError("quiet_hours must be an object")
            if "enabled" in quiet:
                current["quiet_hours"]["enabled"] = bool(quiet["enabled"])
            for key in ("start", "end"):
                if key in quiet:
                    _minutes(str(quiet[key]))
                    current["quiet_hours"][key] = str(quiet[key])
        kinds = change.get("kinds")
        if kinds is not None:
            if not isinstance(kinds, dict):
                raise ValueError("kinds must be an object")
            for kind, sw in kinds.items():
                if kind not in KINDS or not isinstance(sw, dict):
                    raise ValueError("unknown kind of notification")
                for key in ("toast", "native"):
                    if key in sw:
                        current["kinds"][kind][key] = bool(sw[key])
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO prefs (key, value) VALUES ('prefs', ?)", (json.dumps(current),))
        return current

    def delivery(self, kind: str, now: datetime | None = None) -> dict[str, bool]:
        """Whether an item of this kind is shown as a toast and sent natively right now."""
        prefs = self.prefs()
        quiet = in_quiet_hours(prefs, now or datetime.now())
        sw = prefs["kinds"][kind]
        return {"toast": bool(sw["toast"]) and not quiet, "native": bool(sw["native"]) and not quiet, "quiet": quiet}

    # ---- items ----------------------------------------------------------------------------------------------------

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        params = json.loads(row["params"] or "{}")
        action = json.loads(row["action"]) if row["action"] else None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "detail": row["detail"],
            "screen": row["screen"],
            "anchor": row["anchor"],
            "params": params,
            "href": href_for(row["screen"], params, row["anchor"]),
            "source": row["source"],
            "action": action,
            "created": row["created"],
            "read": row["read_at"] is not None,
            "resolved": row["resolved_at"] is not None,
        }

    def native_text(self, item: dict[str, Any]) -> dict[str, str]:
        """What a native notification may say. Private sources get the fixed sentence for their kind."""
        if item["source"] in PRIVATE_SOURCES:
            return {"title": GENERIC_NATIVE[item["kind"]], "body": ""}
        body = item["detail"][:120] if item["source"] in PLAIN_DETAIL_SOURCES else ""
        return {"title": item["title"][:80], "body": body}

    def add(
        self,
        kind: str,
        title: str,
        *,
        detail: str = "",
        screen: str = "home",
        anchor: str = "",
        params: dict[str, Any] | None = None,
        source: str = "",
        dedupe_key: str = "",
        action: dict[str, str] | None = None,
        toast: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record one item. An open item with the same ``dedupe_key`` is returned instead of a second copy."""
        if kind not in KINDS:
            raise ValueError("kind must be needs_you, finished or heads_up")
        screen = str(screen or "").strip().lower()
        if not _SCREEN.match(screen):
            raise ValueError("screen is not a valid destination")
        anchor = str(anchor or "").strip()
        if anchor and not _ANCHOR.match(anchor):
            raise ValueError("anchor is not valid")
        source = str(source or "").strip().lower()
        if source and not _SOURCE.match(source):
            raise ValueError("source is not valid")
        dedupe_key = str(dedupe_key or "").strip()
        if dedupe_key and not _DEDUPE.match(dedupe_key):
            raise ValueError("dedupe_key is not valid")
        clean = clean_params(params)
        title = safe_text(title, _MAX_TITLE)
        if not title:
            raise ValueError("title is required")
        act = None
        if action:
            label = safe_text(action.get("label"), 24)
            if not label:
                raise ValueError("an action needs a label")
            act = {"label": label}
        with self._lock, self._connect() as db:
            if dedupe_key:
                row = db.execute(
                    "SELECT * FROM items WHERE dedupe_key=? AND dismissed_at IS NULL AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                    (dedupe_key,),
                ).fetchone()
                if row is not None:
                    return {**self._public(row), "new": False}
            cur = db.execute(
                "INSERT INTO items (kind, title, detail, screen, anchor, params, source, dedupe_key, action, created)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (kind, title, safe_text(detail, _MAX_DETAIL), screen, anchor, json.dumps(clean), source, dedupe_key,
                 json.dumps(act) if act else "", self._clock()),
            )
            row = db.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone()
            self._trim(db)
            item = self._public(row)
        delivery = self.delivery(kind, now)
        self._emit({
            "type": "attention", "action": "added", "item": item,
            "toast": delivery["toast"] and toast, "native": delivery["native"],
            "native_text": self.native_text(item), "counts": self.counts(),
        })
        return {**item, "new": True}

    def _trim(self, db: sqlite3.Connection) -> None:
        """Old finished business goes; open needs-you items are never dropped by age or count."""
        cutoff = self._clock() - _KEEP_SECONDS
        db.execute("DELETE FROM items WHERE created < ? AND (dismissed_at IS NOT NULL OR resolved_at IS NOT NULL)", (cutoff,))
        total = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if total > _MAX_ITEMS:
            db.execute(
                "DELETE FROM items WHERE id IN (SELECT id FROM items WHERE NOT (kind='needs_you' AND dismissed_at IS NULL AND resolved_at IS NULL)"
                " ORDER BY id LIMIT ?)",
                (total - _MAX_ITEMS,),
            )

    def items(self, *, include_done: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items"
        if not include_done:
            sql += " WHERE dismissed_at IS NULL AND resolved_at IS NULL"
        sql += " ORDER BY CASE kind WHEN 'needs_you' THEN 0 ELSE 1 END, id DESC LIMIT ?"
        with self._lock, self._connect() as db:
            return [self._public(r) for r in db.execute(sql, (max(1, min(int(limit), 300)),))]

    def counts(self) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT kind, screen, read_at IS NULL AS unread, COUNT(*) AS n FROM items"
                " WHERE dismissed_at IS NULL AND resolved_at IS NULL GROUP BY kind, screen, unread"
            ).fetchall()
        out: dict[str, Any] = {"needs_you": 0, "finished": 0, "heads_up": 0, "unread": 0, "open": 0, "screens": {}}
        for r in rows:
            out[r["kind"]] += r["n"]
            out["open"] += r["n"]
            if r["unread"]:
                out["unread"] += r["n"]
            if r["kind"] == "needs_you":
                out["screens"][r["screen"]] = out["screens"].get(r["screen"], 0) + r["n"]
        return out

    def _changed(self, action: str, ids: list[int]) -> None:
        self._emit({"type": "attention", "action": action, "ids": ids, "toast": False, "native": False, "counts": self.counts()})

    def mark_read(self, ids: list[int] | None = None) -> int:
        with self._lock, self._connect() as db:
            if ids is None:
                cur = db.execute("UPDATE items SET read_at=? WHERE read_at IS NULL", (self._clock(),))
            else:
                marks = ",".join("?" for _ in ids) or "NULL"
                cur = db.execute(f"UPDATE items SET read_at=? WHERE read_at IS NULL AND id IN ({marks})", (self._clock(), *ids))
            n = cur.rowcount
        if n:
            self._changed("read", ids or [])
        return n

    def dismiss(self, item_id: int) -> bool:
        with self._lock, self._connect() as db:
            cur = db.execute("UPDATE items SET dismissed_at=?, read_at=COALESCE(read_at, ?) WHERE id=? AND dismissed_at IS NULL",
                             (self._clock(), self._clock(), int(item_id)))
            ok = cur.rowcount > 0
        if ok:
            self._changed("dismissed", [int(item_id)])
        return ok

    def resolve(self, dedupe_key: str) -> int:
        """The thing the item was about is settled elsewhere (an approval answered, a job resumed): it leaves the feed."""
        if not dedupe_key:
            return 0
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT id FROM items WHERE dedupe_key=? AND dismissed_at IS NULL AND resolved_at IS NULL", (dedupe_key,)).fetchall()
            if not rows:
                return 0
            db.execute("UPDATE items SET resolved_at=? WHERE dedupe_key=? AND resolved_at IS NULL", (self._clock(), dedupe_key))
        self._changed("resolved", [r["id"] for r in rows])
        return len(rows)

    def clear_done(self) -> int:
        """Dismiss every open item that is not waiting on the owner. Needs-you items stay until they are settled."""
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT id FROM items WHERE kind != 'needs_you' AND dismissed_at IS NULL AND resolved_at IS NULL").fetchall()
            db.execute("UPDATE items SET dismissed_at=?, read_at=COALESCE(read_at, ?) WHERE kind != 'needs_you' AND dismissed_at IS NULL AND resolved_at IS NULL",
                       (self._clock(), self._clock()))
        if rows:
            self._changed("dismissed", [r["id"] for r in rows])
        return len(rows)


_store: AttentionStore | None = None
_pinned = False
_store_lock = threading.Lock()


def get_attention_store() -> AttentionStore:
    global _store
    with _store_lock:
        # The workspace can move (tests give each run its own): a store for another workspace is replaced, and the
        # event hub follows it.
        if _store is None or (not _pinned and _store.base != workspace_dir()):
            hub = _store._hub if _store is not None else None
            _store = AttentionStore()
            _store.bind_hub(hub)
        return _store


def set_attention_store(store: AttentionStore | None) -> None:
    global _store, _pinned
    with _store_lock:
        _store = store
        _pinned = store is not None


def notify(kind: str, title: str, **kwargs: Any) -> dict[str, Any] | None:
    """Add an item from code that must never fail because the feed did. Returns None when it could not be recorded."""
    try:
        return get_attention_store().add(kind, title, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- the caller is a runtime (a job, a gate); the feed is never worth stopping it
        logging.getLogger(__name__).warning("attention item not recorded: %s", exc)
        return None


def settle(dedupe_key: str) -> None:
    try:
        get_attention_store().resolve(dedupe_key)
    except Exception as exc:  # noqa: BLE001 -- same reason as notify
        logging.getLogger(__name__).warning("attention item not settled: %s", exc)


# ---- approvals (the confirmation gate in webui.py) --------------------------------------------------------------
# Fixed sentences only: the card's own text can carry a mail body or a file name, so it is never copied here.


def approval_requested(confirm_id: str) -> None:
    """A card is waiting. No toast: the card itself is in the chat; this puts it in the feed, the badge and, with the
    window hidden, a native notification."""
    notify("needs_you", "A step needs your approval", detail="Open the chat to review it.", screen="home",
           source="approvals", dedupe_key=f"approval:{confirm_id}", toast=False)


def approval_settled(confirm_id: str, answered: bool, window_gone: bool, approved: bool) -> None:
    """The card is over. Answered: it leaves the feed. Declined because nobody could see it: an item says so."""
    settle(f"approval:{confirm_id}")
    if answered:
        return
    if window_gone:
        notify("needs_you", "Declined because the window closed", detail="A step was not approved while you were away. Ask again to retry it.",
               screen="home", source="approvals", dedupe_key=f"declined:{confirm_id}")
    else:
        notify("heads_up", "A step timed out waiting for you", detail="It was declined after five minutes. Ask again to retry it.",
               screen="home", source="approvals", dedupe_key=f"declined:{confirm_id}")
