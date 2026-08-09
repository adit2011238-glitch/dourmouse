"""Gmail + Google account integration (v5.0) — stdlib IMAP/SMTP, zero heavy deps.

Works on ANY device that can run Python: reads Gmail over IMAP (imap.gmail.com)
and sends over SMTP (smtp.gmail.com) using a Google **App Password** — the
same mechanism the mail agent's ``read_inbox`` already uses. No google-api
client, no OAuth dance, no token files to lose.

Setup (one time, in Google account security settings):
1. Turn on 2-Step Verification for the account.
2. Create an App Password (Security -> 2-Step Verification -> App passwords).
3. Configure the login either way:
   a. .env  (shared/multi-device): GOOGLE_GMAIL_USER + GOOGLE_GMAIL_APP_PASSWORD, or
   b. source-tree (single user, v5.1): fill in GMAIL_USER / GMAIL_APP_PASSWORD
      in ``dourmouse/local_secrets.py`` (GITIGNORED — same convenience as
      typing it into the code, but it can never land in git history or the
      dist zip). Env vars ALWAYS win when both are set.
4. ``python -m dourmouse.google_services --check`` shows the honest status.

Honesty contract (Rule 2.2): missing user/password reports NOT CONFIGURED with
the exact fix — never a fabricated inbox or send.

Send is confirmation-gated at the roster level (REQUIRES_CONFIRMATION) so a
human always approves the exact recipient/subject before anything leaves.
"""

from __future__ import annotations

import base64
import imaplib
import json
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, parsedate_to_datetime
from typing import Any

#: Swappable in tests (hermetic HTTP, no network).
urlopen = urllib.request.urlopen

#: Google REST bases for the per-user OAuth path (v5.15; Drive v5.18).
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
_DRIVE_API = "https://www.googleapis.com/drive/v3"

#: Google-native mime types read via the export endpoint (text/plain) — a
#: binary ``alt=media`` download of a Docs file would return export blobs.
_GOOGLE_NATIVE_MIMES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.knowledge": "text/plain",
    "application/vnd.google-apps.script": "text/plain",
}

#: drive_read refuses binary files larger than this (it is a TEXT-READ tool;
#: a 2 GB movie must never be slurped into the model's context).
_MAX_DRIVE_BYTES = 2_000_000
#: Cap on the text returned by drive_read (mirrors gmail_read's body cap).
_MAX_DRIVE_TEXT = 6000


def _local_secrets() -> dict[str, str]:
    """Single-user source-tree secrets (dourmouse/local_secrets.py).

    GITIGNORED by design — the credential lives next to the code the user
    edits directly, but can never be committed, pushed, or zipped into the
    dist. Returns {} when the file is absent/deleted so the caller falls
    back to the honest NOT CONFIGURED contract (Rule 2.2).

    Loaded via importlib so a machine WITHOUT the file (fresh checkout,
    any device install) never trips a static-import/mypy failure — the
    module is genuinely optional (reviewer-caught).
    """
    import importlib

    try:
        local_secrets = importlib.import_module("dourmouse.local_secrets")
    except Exception:  # noqa: BLE001 -- missing/broken file = no fallback
        return {}
    return {
        "user": str(getattr(local_secrets, "GMAIL_USER", "") or "").strip(),
        "password": str(getattr(local_secrets, "GMAIL_APP_PASSWORD", "") or "").strip(),
    }


def _user() -> str:
    env = os.environ.get("GOOGLE_GMAIL_USER", "").strip()
    if env:
        return env
    return _local_secrets().get("user", "")


def _app_password() -> str:
    env = os.environ.get("GOOGLE_GMAIL_APP_PASSWORD", "").strip()
    if env:
        return env
    return _local_secrets().get("password", "")


def gmail_configured() -> bool:
    """True when both the address and app password are set (deterministic)."""
    return bool(_user()) and bool(_app_password())


# -- v5.15: per-user OAuth path (Google sign-in) ------------------------- #


def _oauth_access_token() -> str | None:
    """The current request's logged-in Google user's access token, or None
    (no session user, or no token). The web server binds the user per
    /api/chat request; the agent tools then read THAT account — never a
    shared inbox."""
    from dourmouse import google_auth

    email = google_auth.current_user()
    if not email:
        return None
    store = google_auth.auth_store()
    if store is None:
        return None  # no mounted store -> honestly no tokens
    return store.access_token_for(email)


def _oauth_user_needs_reauth(action: str) -> str | None:
    """Honest per-user error when the LOGGED-IN user's token is unavailable.

    Returns None when NO user is signed in — the legacy single-owner
    App-Password fallback may then apply. When a user IS signed in but has
    no valid token (expired, unrefresheable, store missing), returns a
    message that sends them back to /login — the shared owner account must
    NEVER be silently used for a signed-in user (reviewer-caught cross-
    account leak: user A's expired session would otherwise read the owner's
    inbox).
    """
    from dourmouse import google_auth

    if google_auth.current_user() is None:
        return None
    return (
        f"GOOGLE {action}: your Google session is missing or expired — "
        "sign in again at /login. Nothing was fetched from any other account."
    )


def _http_json(method: str, url: str, token: str, body: Any = None) -> Any:
    """One authed REST call. Raises RuntimeError with the REAL Google message
    on any failure (Rule 2.2) — never a fabricated result."""
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"GOOGLE API {exc.code} on {url.split('?')[0]}: {raw[:300]}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface the real transport error
        raise RuntimeError(f"GOOGLE API: request failed: {exc}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _http_raw(
    method: str, url: str, token: str, timeout: float = 20.0, max_bytes: int | None = None
) -> bytes:
    """One authed call returning RAW bytes (Drive export / alt=media content
    is text or binary, not JSON). Raises RuntimeError with the REAL Google
    message on any failure.

    ``max_bytes`` BOUNDS the read itself (reviewer-caught: Drive metadata's
    ``size`` field is not always present — a shortcut or an omitted field
    would otherwise let a huge file bypass the size check and be slurped
    entirely into memory). Reads in chunks and aborts past the cap."""
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method=method
    )
    try:
        with urlopen(request, timeout=timeout) as resp:
            if max_bytes is None:
                return resp.read()
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(
                        f"GOOGLE API: response exceeds the {max_bytes:,} B cap "
                        f"on {url.split('?')[0]} — nothing was read."
                    )
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        raise RuntimeError(
            f"GOOGLE API {exc.code} on {url.split('?')[0]}: {raw[:300]!r}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface the real transport error
        raise RuntimeError(f"GOOGLE API: request failed: {exc}") from exc


def _gmail_search_oauth(token: str, query: str, max_results: int) -> str:
    """Gmail REST search (per-user token): list ids, then fetch metadata for
    each so the agent sees real rows, newest first."""
    params = urllib.parse.urlencode(
        {"q": query or "", "maxResults": max(1, min(int(max_results), 20))}
    )
    listing = _http_json("GET", f"{_GMAIL_API}/messages?{params}", token)
    messages = listing.get("messages") or []
    if not messages:
        return "GMAIL SEARCH: no messages matched."
    rows = []
    for item in messages[:10]:
        mid = str(item.get("id") or "")
        meta = _http_json(
            "GET",
            f"{_GMAIL_API}/messages/{urllib.parse.quote(mid)}?format=metadata"
            "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date",
            token,
        )
        headers = {}
        for h in meta.get("payload", {}).get("headers", []):
            headers[str(h.get("name", "")).lower()] = str(h.get("value", ""))
        rows.append(
            f"- [{headers.get('date', '')[:16]}] from {headers.get('from', '?')[:60]}"
            f" | {headers.get('subject', '(no subject)')[:80]} (id {mid})"
        )
    return "GMAIL SEARCH RESULTS (newest first):\n" + "\n".join(rows)


def _message_text(payload: Any) -> str:
    """Walk a Gmail message payload tree for the text/plain body."""
    if not isinstance(payload, dict):
        return ""
    mime = str(payload.get("mimeType", ""))
    if mime == "text/plain" and payload.get("body", {}).get("data"):
        try:
            raw = payload["body"]["data"]
            padding = "=" * (-len(raw) % 4)
            return base64.urlsafe_b64decode(raw + padding).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    for part in payload.get("parts", []) or []:
        found = _message_text(part)
        if found:
            return found
    return ""


def _gmail_read_oauth(token: str, message_id: str) -> str:
    mid = str(message_id or "").strip()
    if not mid:
        return "ERROR: gmail_read needs a message id."
    msg = _http_json(
        "GET", f"{_GMAIL_API}/messages/{urllib.parse.quote(mid)}?format=full", token
    )
    headers = {}
    for h in msg.get("payload", {}).get("headers", []):
        headers[str(h.get("name", "")).lower()] = str(h.get("value", ""))
    body = _message_text(msg.get("payload")) or "(no text body)"
    return (
        f"FROM: {headers.get('from', '?')}\n"
        f"SUBJECT: {headers.get('subject', '(no subject)')}\n"
        f"DATE: {headers.get('date', '?')}\nBODY:\n{body[:6000]}"
    )


def _gmail_send_oauth(token: str, to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["From"] = "me"  # Gmail fills the authenticated user's address
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content((body or "")[:50_000])
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    result = _http_json("POST", f"{_GMAIL_API}/messages/send", token, {"raw": raw})
    mid = str(result.get("id") or "")
    return f"GMAIL SEND OK: message delivered to {to} with subject {subject!r} (id {mid})."


def _calendar_events_oauth(token: str, max_results: int) -> str:
    params = urllib.parse.urlencode(
        {"maxResults": max(1, min(int(max_results), 25)), "singleEvents": "true"}
    )
    data = _http_json(
        "GET", f"{_CALENDAR_API}/calendars/primary/events?{params}", token
    )
    items = data.get("items") or []
    if not items:
        return "CALENDAR: no upcoming events."
    rows = []
    for event in items:
        start = (event.get("start") or {}).get("dateTime") or (
            (event.get("start") or {}).get("date") or "?"
        )
        rows.append(f"- {start[:16]} | {str(event.get('summary') or '(no title)')[:80]}")
    return "CALENDAR EVENTS (upcoming):\n" + "\n".join(rows)


# -- v5.18: Google Drive (read-only, per-user OAuth) ---------------------- #


def _drive_search_oauth(token: str, query: str, max_results: int) -> str:
    """Drive files.list (per-user token): name/fullText contains query,
    newest first, trashed excluded. Real rows — never fabricated files."""
    q = (query or "").strip()
    if q:
        # Drive q syntax: single quotes are escaped by doubling; ``and``
        # keeps trashed files out of the result.
        safe = q.replace("'", "''")
        clause = f"trashed = false and (name contains '{safe}' or fullText contains '{safe}')"
    else:
        clause = "trashed = false"
    params = urllib.parse.urlencode(
        {
            "q": clause,
            "pageSize": max(1, min(int(max_results), 25)),
            "fields": "files(id,name,mimeType,modifiedTime,size)",
            "orderBy": "modifiedTime desc",
        }
    )
    data = _http_json("GET", f"{_DRIVE_API}/files?{params}", token)
    files = data.get("files") or []
    if not files:
        return "DRIVE SEARCH: no files matched."
    rows = []
    for f in files:
        size = f.get("size")
        size_s = f"{int(size):,} B" if size else "—"
        rows.append(
            f"- {str(f.get('name') or '?')[:80]} ({str(f.get('mimeType') or '?')}, "
            f"{size_s}) — modified {str(f.get('modifiedTime') or '?')[:16]} | id {f.get('id')}"
        )
    return "DRIVE FILES (newest first):\n" + "\n".join(rows)


def _drive_read_oauth(token: str, file_id: str) -> str:
    """Read ONE Drive file's text content (per-user token). Google-native
    files (Docs/Sheets/Slides) go through the export endpoint; everything
    else through alt=media with a hard size cap. Honest refusal for
    oversized binaries — never a truncated lie or a fabricated read."""
    fid = urllib.parse.quote(str(file_id or "").strip())
    if not fid:
        return "ERROR: drive_read needs a file id."
    meta = _http_json(
        "GET",
        f"{_DRIVE_API}/files/{fid}?fields=id,name,mimeType,modifiedTime,size",
        token,
    )
    name = str(meta.get("name") or "?")
    mime = str(meta.get("mimeType") or "")
    size_raw = meta.get("size")
    export_mime = _GOOGLE_NATIVE_MIMES.get(mime)
    if export_mime:
        body = _http_raw(
            "GET",
            f"{_DRIVE_API}/files/{fid}/export?mimeType={urllib.parse.quote(export_mime)}",
            token,
            max_bytes=_MAX_DRIVE_BYTES,
        ).decode("utf-8", errors="replace")
    else:
        if size_raw and int(size_raw) > _MAX_DRIVE_BYTES:
            return (
                f"DRIVE READ: {name!r} is {int(size_raw):,} B — too large to "
                f"read as text (cap {_MAX_DRIVE_BYTES:,} B). Nothing was read."
            )
        # The bounded read is the real cap: metadata ``size`` may be absent
        # (shortcuts, omitted fields), so the read itself aborts past the cap
        # rather than trusting the metadata number.
        body = _http_raw(
            "GET", f"{_DRIVE_API}/files/{fid}?alt=media", token,
            max_bytes=_MAX_DRIVE_BYTES,
        ).decode("utf-8", errors="replace")
    if not body.strip():
        body = "(empty file)"
    truncated = len(body) > _MAX_DRIVE_TEXT
    return (
        f"DRIVE FILE: {name} ({mime}) — modified "
        f"{str(meta.get('modifiedTime') or '?')[:16]}\n"
        f"CONTENT ({_MAX_DRIVE_TEXT} char cap"
        f"{' — TRUNCATED' if truncated else ''}):\n{body[:_MAX_DRIVE_TEXT]}"
    )


def drive_search(query: str, max_results: int = 10) -> str:
    """Search the SIGNED-IN user's Google Drive (read-only, v5.18).

    Real files.list results for the logged-in Google user's own account —
    same per-user guarantee as gmail_search: a signed-in user without a
    valid token gets an honest re-sign-in message (never another account),
    and with no user signed in this reports NOT CONFIGURED (Drive has no
    legacy shared path to fall back to).
    """
    token = _oauth_access_token()
    if token:
        return _drive_search_oauth(token, query, max_results)
    reauth = _oauth_user_needs_reauth("DRIVE")
    if reauth:
        return reauth
    return (
        "NOT CONFIGURED: Google Drive needs the logged-in user's Google "
        "sign-in (drive.readonly scope). No OAuth session user found — "
        "no files were fetched."
    )


def drive_read(file_id: str) -> str:
    """Read ONE Drive file's text content for the SIGNED-IN user (v5.18).
    Same per-user guarantee as gmail_read — never another account."""
    token = _oauth_access_token()
    if token:
        return _drive_read_oauth(token, file_id)
    reauth = _oauth_user_needs_reauth("DRIVE")
    if reauth:
        return reauth
    return (
        "NOT CONFIGURED: Google Drive needs the logged-in user's Google "
        "sign-in (drive.readonly scope). No OAuth session user found — "
        "no file was fetched."
    )


def _imap() -> imaplib.IMAP4_SSL:
    if not gmail_configured():
        raise RuntimeError(
            "NOT CONFIGURED: set GOOGLE_GMAIL_USER + GOOGLE_GMAIL_APP_PASSWORD "
            "in .env, or fill in dourmouse/local_secrets.py (Google account "
            "-> 2-Step Verification -> App passwords). Nothing was fetched."
        )
    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    conn.login(_user(), _app_password())
    return conn


def _decode(b: bytes | None) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return str(b)


def gmail_search(query: str, max_results: int = 10) -> str:
    """Search the Gmail inbox (IMAP), newest first.

    ``query`` accepts real    Gmail search syntax (``from:x``, ``newer_than:3d``,
    ``label:y``, ``has:attachment`` …) passed through X-GM-RAW, or plain words
    matched against subject/from/body. An empty query browses the most recent
    messages. Returns readable rows: date, from, subject.

    v5.15: when the LOGGED-IN user has a Google OAuth session this searches
    THEIR mailbox via the Gmail API; the legacy App-Password IMAP path
    remains the fallback for the single-server-owner setup.
    """
    token = _oauth_access_token()
    if token:
        return _gmail_search_oauth(token, query, max_results)
    reauth = _oauth_user_needs_reauth("SEARCH")
    if reauth:
        return reauth
    max_results = max(1, min(int(max_results), 50))
    conn = _imap()
    try:
        conn.select("INBOX", readonly=True)
        # Restrict to the last ~200 messages so the search stays fast and
        # bounded; Gmail IMAP SEARCH can otherwise scan the whole mailbox.
        _typ, data = conn.search(None, "ALL")
        ids = (data[0] or b"").split()
        recent = ids[-200:]
        if not recent:
            return "GMAIL SEARCH: inbox is empty."
        # Embedded quotes are escaped so a malformed query can never break the
        # search syntax.
        safe = (query or "").strip().replace('"', "'")
        if not safe:
            # Empty query: browse the most recent messages instead of erroring
            # on a zero-length IMAP search (live-caught: Gmail rejects it).
            hit_ids = recent[-max_results:]
        elif _has_gmail_operators(safe):
            # Gmail search syntax (from:/newer_than:/label:/has:...) is passed
            # straight to Gmail's search engine via X-GM-RAW; a plain
            # SUBJECT/FROM/TEXT search cannot express it.
            try:
                status, search_data = conn.search(None, f'(X-GM-RAW "{safe}")')
            except Exception:  # noqa: BLE001 -- fall back to subject-only
                status, search_data = conn.search(None, f'(SUBJECT "{safe}")')
            if status != "OK":
                return f"GMAIL SEARCH: IMAP search failed ({status})."
            hit_ids = (search_data[0] or b"").split()
            hit_ids = [i for i in hit_ids if i in set(recent)][-max_results:]
            if not hit_ids:
                return f"GMAIL SEARCH: no messages matched {query!r} in the recent inbox."
        else:
            # Plain words: search subject/from/body; fall back to subject-only
            # if Gmail rejects the combined query.
            try:
                status, search_data = conn.search(
                    None, f'(OR (SUBJECT "{safe}") (FROM "{safe}") (TEXT "{safe}"))'
                )
            except Exception:  # noqa: BLE001 -- fall back to subject-only
                status, search_data = conn.search(None, f'(SUBJECT "{safe}")')
            if status != "OK":
                return f"GMAIL SEARCH: IMAP search failed ({status})."
            hit_ids = (search_data[0] or b"").split()
            hit_ids = [i for i in hit_ids if i in set(recent)][-max_results:]
            if not hit_ids:
                return f"GMAIL SEARCH: no messages matched {query!r} in the recent inbox."
        rows = []
        for mid in reversed(hit_ids):
            _st, msg_data = conn.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            header = b""
            for part in msg_data or []:
                if isinstance(part, tuple):
                    header += part[1]
            rows.append(_header_row(mid, header))
        return "GMAIL SEARCH RESULTS (newest first):\n" + "\n".join(rows)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001,S110 - logout must never mask results
            pass


_GMAIL_OPERATORS = (
    "from:", "to:", "cc:", "bcc:", "subject:", "label:", "in:", "has:",
    "is:", "newer_than:", "older_than:", "newer:", "older:", "larger:",
    "smaller:", "category:", "filename:", "after:", "before:", "list:",
    "deliveredto:",
)


def _has_gmail_operators(query: str) -> bool:
    """True when the query uses Gmail-specific search operators that a plain
    IMAP SUBJECT/FROM/TEXT search cannot express (requires X-GM-RAW)."""
    lowered = query.lower()
    return any(op in lowered for op in _GMAIL_OPERATORS)


def _header_row(mid: bytes, header: bytes) -> str:
    from email import policy
    from email.parser import BytesParser

    msg = BytesParser(policy=policy.default).parsebytes(header)
    subj = _decode(msg["Subject"]).strip() or "(no subject)"
    frm = _decode(msg["From"]).strip() or "(unknown sender)"
    when = ""
    if msg["Date"]:
        try:
            when = parsedate_to_datetime(_decode(msg["Date"]).strip()).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            when = _decode(msg["Date"]).strip()[:16]
    return f"- [{when}] from {frm[:60]} | {subj[:80]} (uid {_decode(mid)})"


def gmail_read(message_id: str) -> str:
    """Fetch ONE message body by IMAP uid (returns text/plain body + headers).

    v5.15: with a logged-in OAuth user this reads THEIR message by Gmail API
    id; the IMAP uid path is the App-Password fallback.
    """
    token = _oauth_access_token()
    if token:
        return _gmail_read_oauth(token, message_id)
    reauth = _oauth_user_needs_reauth("READ")
    if reauth:
        return reauth
    uid = (message_id or "").strip()
    if not uid or not uid.isdigit():
        return f"ERROR: gmail_read needs a numeric message uid, got {message_id!r}."
    conn = _imap()
    try:
        conn.select("INBOX", readonly=True)
        _st, msg_data = conn.fetch(uid.encode(), "(RFC822)")
        raw = b""
        for part in msg_data or []:
            if isinstance(part, tuple):
                raw += part[1]
        if not raw:
            return f"GMAIL READ: no message with uid {uid} (it may have been moved/deleted)."
        from email import policy
        from email.parser import BytesParser

        msg = BytesParser(policy=policy.default).parsebytes(raw)
        out = [
            f"FROM: {_decode(msg['From'])}",
            f"SUBJECT: {_decode(msg['Subject'])}",
            f"DATE: {_decode(msg['Date'])}",
        ]
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body = _decode(part.get_payload(decode=True))
                    except Exception:  # noqa: BLE001
                        body = ""
                    if body:
                        break
        else:
            body = _decode(msg.get_payload(decode=True))
        out.append("BODY:")
        out.append((body or "(no text body)")[:6000])
        return "\n".join(out)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001,S110 - logout must never mask a read
            pass


def gmail_send(to: str, subject: str, body: str) -> str:
    """Send an email via Gmail SMTP (must be confirmation-gated upstream).

    v5.15: with a logged-in OAuth user this sends from THEIR account via the
    Gmail API; SMTP App-Password is the fallback.
    """
    token = _oauth_access_token()
    if token:
        return _gmail_send_oauth(token, to, subject, body)
    reauth = _oauth_user_needs_reauth("SEND")
    if reauth:
        return reauth
    to = (to or "").strip()
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not to or "@" not in to:
        return "ERROR: gmail_send requires a valid recipient address."
    if not subject:
        return "ERROR: gmail_send requires a subject."
    if not gmail_configured():
        raise RuntimeError(
            "NOT CONFIGURED: set GOOGLE_GMAIL_USER + GOOGLE_GMAIL_APP_PASSWORD "
            "in .env, or fill in dourmouse/local_secrets.py (Google account "
            "-> 2-Step Verification -> App passwords). Nothing was sent."
        )
    msg = EmailMessage()
    msg["From"] = formataddr(("Dourmouse", _user()))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body[:50_000])
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(_user(), _app_password())
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            f"GMAIL SEND FAILED: authentication rejected ({exc.smtp_code}). "
            "Check GOOGLE_GMAIL_USER / GOOGLE_GMAIL_APP_PASSWORD. Nothing was sent."
        ) from exc
    except smtplib.SMTPException as exc:
        raise RuntimeError(f"GMAIL SEND FAILED: SMTP error: {exc}. Nothing was sent.") from exc
    return f"GMAIL SEND OK: message delivered to {to} with subject {subject!r}."


def calendar_events(max_results: int = 5) -> str:
    """Google Calendar read for the LOGGED-IN user.

    v5.15: real when the current user signed in with Google and granted the
    calendar.readonly scope — read via the Calendar REST API, stdlib only.
    Honest NOT CONFIGURED otherwise (no OAuth user, or the legacy App-Password
    setup has no calendar grant) — never fabricated events (Rule 2.2).
    """
    token = _oauth_access_token()
    if token:
        return _calendar_events_oauth(token, max_results)
    reauth = _oauth_user_needs_reauth("CALENDAR")
    if reauth:
        return reauth
    return (
        "NOT CONFIGURED: Google Calendar needs the logged-in user's Google "
        "sign-in (calendar.readonly scope). No OAuth session user found — "
        "no events were fetched."
    )


def status() -> dict[str, Any]:
    """Honest capability report for the SETUP panel."""
    source = "env" if os.environ.get("GOOGLE_GMAIL_USER", "").strip() else "local_secrets.py"
    return {
        "configured": gmail_configured(),
        "detail": (
            f"{_user()} (via {source})" if gmail_configured() else "no Gmail login set"
        ),
        "hint": "env vars OR dourmouse/local_secrets.py; 2-Step Verification -> App passwords",
    }


def _main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" in argv:
        s = status()
        print(f"GMAIL: {'CONFIGURED ' + s['detail'] if s['configured'] else 'NOT CONFIGURED — ' + s['hint']}")
        print("SETUP: 1) enable 2-Step Verification  2) create an App password")
        print("       3) set GOOGLE_GMAIL_USER + GOOGLE_GMAIL_APP_PASSWORD in .env")
        print("          or fill GMAIL_USER/GMAIL_APP_PASSWORD in dourmouse/local_secrets.py")
        return 0 if s["configured"] else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
