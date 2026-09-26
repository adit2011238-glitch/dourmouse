"""Backend for the OS shell's COMMS screen (finding #149).

* ``GET  /api/os/comms/inbox?q=&limit=&fresh=1``: the mailbox as structured rows
  (sender, subject, real Gmail timestamp, unread, starred, snippet when the
  signed-in Google account can supply them). An empty ``q`` is served from a
  short cache and says how old it is; ``fresh=1`` forces a live read. This
  exists because ``/api/gmail/search`` parses only ``(uid N)`` rows and so
  returns no rows at all for a signed-in Google user, whose rows end in
  ``(id N)``.
* ``GET  /api/os/comms/message?id=``: one message read, split into headers and
  body.
* ``POST /api/os/comms/archive|trash|flag|unflag {id}``: the owner's own click
  on a row. The screen shows a confirmation card that names the message and
  what will happen before it calls these. Trash is Gmail's Trash (recoverable
  for 30 days); nothing here can delete permanently, and sending mail is not
  here at all: it stays on the model's approval gate.

Every id is validated, every size is bounded, and a refusal or a Google error is
returned in the words the underlying function used.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any

from . import ApiError, Request, route

_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_QUERY = 200
MAX_LIMIT = 50
DEFAULT_LIMIT = 25
MAX_LINE = 600
MAX_RAW = 200_000
#: A Google or IMAP failure message that means "not connected", not "broken".
_UNAVAILABLE = re.compile(r"^(NOT CONFIGURED|GOOGLE [A-Z ]+:)")
_ROW = re.compile(r"^- \[([^\]]{0,60})\] from ([^|]{0,300}) \| (.{0,400}?) \((?:uid|id) ([A-Za-z0-9_-]{1,80})\)$")

_cache_lock = threading.Lock()
_cache: dict[tuple[str, int], dict[str, Any]] = {}
CACHE_KEYS = 8


def _ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("DOURMOUSE_GMAIL_INBOX_TTL", "180")))
    except ValueError:
        return 180.0


@contextmanager
def _as_user(req: Request):
    """Bind the signed-in Google user to this thread, as /api/chat does, so the
    Gmail functions act on that account. Cleared again on the way out."""
    from dourmouse import google_auth

    google_auth.set_current_user(req.user)
    try:
        yield
    finally:
        google_auth.set_current_user(None)


def _mode() -> str:
    """'oauth' (signed-in Google account), 'imap' (App Password) or 'none'."""
    from dourmouse import google_services as gs

    try:
        if gs._oauth_access_token():
            return "oauth"
    except Exception as exc:  # noqa: BLE001 -- a broken token store is "not signed in", said honestly by the read below
        logging.getLogger(__name__).debug("gmail token check failed: %s", exc)
    return "imap" if gs.gmail_configured() else "none"


def parse_rows(raw: str) -> list[dict[str, Any]]:
    """Rows from gmail_search's text: '- [date] from X | subject (uid N)' or
    '(id N)'. Bounded: the text and each line are capped before matching."""
    rows: list[dict[str, Any]] = []
    for line in (raw or "")[:MAX_RAW].splitlines():
        if len(line) > MAX_LINE:
            continue
        m = _ROW.match(line)
        if m:
            rows.append({
                "id": m.group(4), "from": m.group(2), "subject": m.group(3), "date": m.group(1),
                "ts": 0, "unread": None, "flagged": None, "snippet": "",
            })
    return rows


def parse_message(text: str) -> dict[str, str]:
    """Split gmail_read's 'FROM: ..\\nSUBJECT: ..\\nDATE: ..\\nBODY:\\n..' text."""
    head, _, body = (text or "").partition("BODY:\n")
    fields = {"from": "", "subject": "", "date": ""}
    for line in head.splitlines()[:6]:
        key, _, value = line.partition(": ")
        if key.lower() in fields:
            fields[key.lower()] = value.strip()[:500]
    return {**fields, "body": body}


def _limit(req: Request) -> int:
    try:
        n = int(req.arg("limit", str(DEFAULT_LIMIT)) or DEFAULT_LIMIT)
    except ValueError:
        raise ApiError(400, "limit must be a whole number") from None
    return max(1, min(n, MAX_LIMIT))


def _read(q: str, limit: int) -> dict[str, Any]:
    """One live read of the mailbox as {state, rows, note}."""
    from dourmouse import google_services as gs

    try:
        rows = gs.gmail_inbox_rows(q, limit)
        raw = ""
        if rows is None:
            raw = gs.gmail_search(q, limit)
            rows = parse_rows(raw)
    except RuntimeError as exc:
        msg = str(exc)
        if _UNAVAILABLE.match(msg):
            return {"state": "unavailable", "rows": [], "note": msg}
        raise ApiError(502, msg) from exc
    except Exception as exc:  # noqa: BLE001 -- IMAP and socket failures, reported in their own words
        raise ApiError(502, f"{type(exc).__name__}: {exc}") from exc
    if rows:
        return {"state": "populated", "rows": rows, "note": ""}
    if raw and _UNAVAILABLE.match(raw):
        return {"state": "unavailable", "rows": [], "note": raw}
    return {"state": "empty", "rows": [], "note": raw or "No messages."}


@route("GET", "/api/os/comms/inbox")
def inbox(req: Request):
    q = req.arg("q").strip()
    if len(q) > MAX_QUERY:
        raise ApiError(400, f"the search is longer than {MAX_QUERY} characters")
    limit = _limit(req)
    fresh = req.arg("fresh") == "1"
    key = (req.user or "", limit)
    now = time.time()
    with _as_user(req):
        mode = _mode()
        if not q and not fresh:
            with _cache_lock:
                hit = _cache.get(key)
            if hit and now - hit["at"] < _ttl() and hit["mode"] == mode:
                return 200, {"ok": True, **hit["payload"], "mode": mode, "cached": True, "cached_at": hit["at"], "ttl": _ttl()}
        payload = _read(q, limit)
    payload["query"] = q
    if not q:
        with _cache_lock:
            if len(_cache) >= CACHE_KEYS and key not in _cache:
                _cache.pop(next(iter(_cache)))
            _cache[key] = {"at": now, "mode": mode, "payload": payload}
    return 200, {"ok": True, **payload, "mode": mode, "cached": False, "cached_at": now if not q else None, "ttl": _ttl()}


@route("GET", "/api/os/comms/message")
def message(req: Request):
    mid = req.need("id")
    if not _ID.match(mid):
        raise ApiError(400, "id is not a valid message id")
    from dourmouse import google_services as gs

    with _as_user(req):
        try:
            text = gs.gmail_read(mid)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(502, f"{type(exc).__name__}: {exc}") from exc
    if not text.startswith("FROM: "):
        raise ApiError(502, text[:600] or "the message could not be read")
    return 200, {"ok": True, "id": mid, **parse_message(text)}


#: action -> (callable name, flag argument, prefix of the success text)
_ACTIONS = {
    "archive": ("gmail_archive", None, "GMAIL ARCHIVED:"),
    "trash": ("gmail_trash", None, "GMAIL TRASHED:"),
    "flag": ("gmail_flag", True, "GMAIL FLAGGED:"),
    "unflag": ("gmail_flag", False, "GMAIL UNFLAGGED:"),
}


def _make_action(name: str):
    func_name, flag, success = _ACTIONS[name]

    def handler(req: Request):
        mid = str(req.body.get("id") or "").strip()
        if not mid:
            raise ApiError(400, "id is required")
        if not _ID.match(mid):
            raise ApiError(400, "id is not a valid message id")
        from dourmouse import google_services as gs

        func = getattr(gs, func_name)
        with _as_user(req):
            try:
                text = func(mid) if flag is None else func(mid, flag)
            except Exception as exc:  # noqa: BLE001
                raise ApiError(502, f"{type(exc).__name__}: {exc}") from exc
        if not text.startswith(success):
            raise ApiError(502, text[:800] or f"{name} did not report success")
        with _cache_lock:
            _cache.clear()
        return 200, {"ok": True, "action": name, "id": mid, "message": text}

    return handler


for _name in _ACTIONS:
    route("POST", f"/api/os/comms/{_name}")(_make_action(_name))
