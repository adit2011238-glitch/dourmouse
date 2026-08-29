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
import re
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

from dourmouse import email_identity

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


def email_display_name() -> str:
    """The identity Dourmouse sends mail AS — DOURMOUSE_EMAIL_NAME env
    (v5.24) or the default brand name. The FROM address itself is the
    configured Gmail account (the App-Password user or the signed-in
    OAuth user)."""
    return os.environ.get("DOURMOUSE_EMAIL_NAME", "").strip() or "Dourmouse"


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
    method: str,
    url: str,
    token: str,
    timeout: float = 20.0,
    max_bytes: int | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> bytes:
    """One authed call returning RAW bytes (Drive export / alt=media content
    is text or binary, not JSON). Raises RuntimeError with the REAL Google
    message on any failure.

    ``max_bytes`` BOUNDS the read itself (reviewer-caught: Drive metadata's
    ``size`` field is not always present — a shortcut or an omitted field
    would otherwise let a huge file bypass the size check and be slurped
    entirely into memory). Reads in chunks and aborts past the cap.

    ``data`` + ``content_type`` (v5.27) support authed media uploads (the
    Drive create-doc content PATCH).
    """
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = content_type or "application/octet-stream"
    request = urllib.request.Request(url, headers=headers, method=method, data=data)
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


def _drive_create_oauth(token: str, title: str, content: str) -> str:
    """Create a Google Doc in the signed-in user's Drive (v5.27).

    Two calls: files.create (metadata) then a media PATCH to write the
    content. Requires Drive WRITE scope — drive.readonly 403s here and is
    surfaced with the exact fix, never masked.
    """
    name = (title or "").strip()[:120]
    if not name:
        return "ERROR: drive_create_doc requires a title."
    try:
        meta = _http_json(
            "POST",
            f"{_DRIVE_API}/files",
            token,
            {"name": name, "mimeType": "application/vnd.google-apps.document"},
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "403" in msg:
            raise RuntimeError(
                msg
                + " — Drive WRITE needs the full scopes (GOOGLE_OAUTH_FULL_SCOPES=1 "
                "in .env + a verified/testing-mode OAuth app). Nothing was created."
            ) from exc
        raise
    fid = str(meta.get("id") or "").strip()
    if not fid:
        return "ERROR: Drive did not return a file id — nothing was created."
    body = (content or "").strip()
    if body:
        try:
            _http_raw(
                "PATCH",
                f"https://www.googleapis.com/upload/drive/v3/files/{fid}?uploadType=media",
                token,
                data=body.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
        except RuntimeError as exc:
            msg = str(exc)
            if "403" in msg:
                raise RuntimeError(
                    msg
                    + " — Drive WRITE needs the full scopes (GOOGLE_OAUTH_FULL_SCOPES=1). "
                    f"The empty doc {name!r} was created but its content was not written."
                ) from exc
            raise
    return (
        f"DRIVE DOC CREATED: {name!r} (id {fid}) — "
        f"open at https://drive.google.com/open?id={fid} · "
        f"{len(body):,} chars written."
    )


def drive_create_doc(title: str, content: str = "") -> str:
    """Create a Google Doc in the SIGNED-IN user's Drive (v5.27, write).

    Real files.create + media upload with the logged-in Google user's token
    — the same per-user guarantee as drive_search/gmail_search. Honest
    NOT CONFIGURED when no OAuth user is signed in (Drive has no legacy
    shared write path), honest re-sign-in when the session is stale. Should
    be confirmation-gated upstream: it creates a real file.
    """
    token = _oauth_access_token()
    if token:
        try:
            return _drive_create_oauth(token, title, content)
        except RuntimeError as exc:
            return f"DRIVE DOC CREATE (reported honestly): {exc}"
    reauth = _oauth_user_needs_reauth("DRIVE WRITE")
    if reauth:
        return reauth
    return (
        "NOT CONFIGURED: creating a Drive document needs the signed-in Google "
        "user's OAuth session with Drive WRITE scope. No user is signed in — "
        "sign in at /login (with GOOGLE_OAUTH_FULL_SCOPES=1 in .env so the "
        "session grants Drive), then retry. Nothing was created."
    )


# -- v5.28: Google Slides (write, per-user OAuth) ------------------------ #

_SLIDES_API = "https://slides.googleapis.com/v1/presentations"


def _slides_create_oauth(
    token: str, title: str, slides: list[dict[str, str]] | None = None
) -> str:
    """Create a Google Slides deck in the signed-in user's Drive (v5.28).

    presentations.create (metadata) then one batchUpdate that deletes the
    default blank slide and inserts TITLE_AND_BODY layouts, then a second
    batchUpdate that draws real text boxes (createShape TEXT_BOX + insertText) on
    each slide. Deterministic objectIds throughout — the deck's content is
    real, never placeholder text. Requires the Google sign-in with Drive
    write scope; identity-only 403s are surfaced with the exact fix.
    """
    name = (title or "").strip()[:120]
    if not name:
        return "ERROR: slides_create requires a title."
    try:
        meta = _http_json(
            "POST",
            _SLIDES_API,
            token,
            {"title": name},
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "403" in msg:
            raise RuntimeError(
                msg
                + " — Slides WRITE needs the full scopes (GOOGLE_OAUTH_FULL_SCOPES=1 "
                "in .env + a verified/testing-mode OAuth app). Nothing was created."
            ) from exc
        raise
    pres_id = str(meta.get("presentationId") or "").strip()
    if not pres_id:
        return "ERROR: Slides did not return a presentation id — nothing was created."
    # The create response is a full Presentation resource; the default
    # blank slide is in ``slides[0].objectId`` (not a top-level pageId).
    default_slide = (meta.get("slides") or [{}])[0]
    page_id = str(default_slide.get("objectId") or "").strip()
    slides = slides or []
    requests: list[dict[str, Any]] = []
    if page_id:
        # The API ships a default blank slide; rebuild the deck from scratch
        # so it contains ONLY the requested slides.
        requests.append({"deleteObject": {"objectId": page_id}})
    for i, slide in enumerate(slides):
        sid = f"slide_{i + 1}"
        # createSlide, not insertLayout: the latter is not a Slides API
        # request type at all, so every deck this function ever built failed
        # with "Unknown name insertLayout" after the presentation had already
        # been created — leaving an empty deck behind.
        #
        # BLANK rather than TITLE_AND_BODY because the second batch draws its
        # own title/body text boxes; a placeholder layout would leave empty
        # "Click to add title" frames sitting under them.
        requests.append(
            {
                "createSlide": {
                    "objectId": sid,
                    "insertionIndex": i,
                    "slideLayoutReference": {"predefinedLayout": "BLANK"},
                }
            }
        )
    if not requests:
        return (
            f"SLIDES DECK CREATED: {name!r} (id {pres_id}) — no slides added. "
            f"open at https://docs.google.com/presentation/d/{pres_id}"
        )
    try:
        _http_json(
            "POST",
            f"{_SLIDES_API}/{pres_id}:batchUpdate",
            token,
            {"requests": requests},
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "403" in msg:
            raise RuntimeError(
                msg
                + " — Slides WRITE needs the full scopes (GOOGLE_OAUTH_FULL_SCOPES=1). "
                f"The empty deck {name!r} was created but its slides were not added."
            ) from exc
        raise
    # Second batch: draw explicit title/body text boxes on each slide.
    text_requests: list[dict[str, Any]] = []
    for i, slide in enumerate(slides):
        sid = f"slide_{i + 1}"
        title_text = (slide.get("title") or "").strip()[:200]
        body_text = (slide.get("body") or "").strip()[:1000]
        if title_text:
            tbox = f"{sid}_title_box"
            text_requests.append(
                {
                    "createShape": {
                        "objectId": tbox,
                        "shapeType": "TEXT_BOX",
                        "elementProperties": {
                            "pageObjectId": sid,
                            "size": {"width": {"magnitude": 685, "unit": "PT"},
                                    "height": {"magnitude": 60, "unit": "PT"}},
                            "transform": {
                                "scaleX": 1, "scaleY": 1, "shearX": 0,
                                "shearY": 0, "translateX": 55, "translateY": 45,
                                "unit": "PT",
                            },
                        },
                    }
                }
            )
            text_requests.append(
                {
                    "insertText": {
                        "objectId": tbox,
                        "insertionIndex": 0,
                        "text": title_text,
                    }
                }
            )
        if body_text:
            bbox = f"{sid}_body_box"
            text_requests.append(
                {
                    "createShape": {
                        "objectId": bbox,
                        "shapeType": "TEXT_BOX",
                        "elementProperties": {
                            "pageObjectId": sid,
                            "size": {"width": {"magnitude": 685, "unit": "PT"},
                                    "height": {"magnitude": 340, "unit": "PT"}},
                            "transform": {
                                "scaleX": 1, "scaleY": 1, "shearX": 0,
                                "shearY": 0, "translateX": 55, "translateY": 120,
                                "unit": "PT",
                            },
                        },
                    }
                }
            )
            text_requests.append(
                {
                    "insertText": {
                        "objectId": bbox,
                        "insertionIndex": 0,
                        "text": body_text[:800],
                    }
                }
            )
    if text_requests:
        try:
            _http_json(
                "POST",
                f"{_SLIDES_API}/{pres_id}:batchUpdate",
                token,
                {"requests": text_requests},
            )
        except RuntimeError as exc:
            msg = str(exc)
            if "403" in msg:
                raise RuntimeError(
                    msg
                    + " — Slides WRITE needs the full scopes (GOOGLE_OAUTH_FULL_SCOPES=1). "
                    f"The deck {name!r} was created but its text could not be written."
                ) from exc
            raise
    return (
        f"SLIDES DECK CREATED: {name!r} (id {pres_id}) — "
        f"{len(slides)} slide(s) · open at "
        f"https://docs.google.com/presentation/d/{pres_id}"
    )


def slides_create(
    title: str, slides: list[dict[str, str]] | None = None
) -> str:
    """Create a Google Slides presentation in the SIGNED-IN user's Drive
    (v5.28, write). Real presentations.create + batchUpdate with the logged-in
    Google user's token. Honest NOT CONFIGURED when no OAuth user is signed
    in; confirmation-gated upstream (creates a real deck).
    """
    token = _oauth_access_token()
    if token:
        try:
            return _slides_create_oauth(token, title, slides)
        except RuntimeError as exc:
            return f"SLIDES CREATE (reported honestly): {exc}"
    reauth = _oauth_user_needs_reauth("SLIDES WRITE")
    if reauth:
        return reauth
    return (
        "NOT CONFIGURED: creating a Slides presentation needs the signed-in "
        "Google user's OAuth session with Drive write scope. No user is "
        "signed in — sign in at /login (with GOOGLE_OAUTH_FULL_SCOPES=1 in "
        ".env so the session grants Drive), then retry. Nothing was created."
    )


def _drive_share_oauth(
    token: str, file_id: str, email: str, role: str, notify: bool
) -> str:
    """Grant `email` access to `file_id` via the Drive permissions API.

    Sharing is a real, outward-facing act: it puts the user's document in
    someone else's Drive and, when `notify` is set, sends them mail. So the
    caller upstream gates it, and this layer refuses anything it cannot do
    honestly rather than half-succeeding.

    drive.file scope covers files this app created; a doc the user made by
    hand in the browser is outside that scope and 404s here, which is
    surfaced as exactly that rather than as a generic failure.
    """
    fid = (file_id or "").strip()
    addr = (email or "").strip()
    if not fid:
        return "ERROR: drive_share requires a file_id."
    # Deliberately structural, not a full RFC check: a local part, one @, and
    # a dotted domain. "@nope" contains an @ and no spaces but is not an
    # address, and a share sent to a malformed target fails confusingly at
    # best. Reject here, before anything leaves.
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", addr):
        return f"ERROR: {addr!r} is not a valid email address. Nothing was shared."
    role = (role or "reader").strip().lower()
    if role not in ("reader", "commenter", "writer"):
        return (
            f"ERROR: role must be reader, commenter or writer (got {role!r}). "
            "Nothing was shared."
        )
    try:
        _http_json(
            "POST",
            f"{_DRIVE_API}/files/{fid}/permissions"
            f"?sendNotificationEmail={'true' if notify else 'false'}",
            token,
            {"type": "user", "role": role, "emailAddress": addr},
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "404" in msg:
            raise RuntimeError(
                msg
                + f" — file {fid} is not visible to this app. The drive.file scope "
                "only covers files DOURMOUSE created; a file made by hand in the "
                "browser cannot be shared this way. Nothing was shared."
            ) from exc
        if "403" in msg:
            raise RuntimeError(
                msg
                + " — sharing needs Drive WRITE (GOOGLE_OAUTH_FULL_SCOPES=1). "
                "Nothing was shared."
            ) from exc
        raise
    return (
        f"DRIVE SHARED: {addr} now has {role} access to {fid} "
        f"({'notification email sent' if notify else 'no notification sent'}) — "
        f"https://drive.google.com/open?id={fid}"
    )


def drive_share(
    file_id: str, email: str, role: str = "reader", notify: bool = True
) -> str:
    """Share a DOURMOUSE-created Drive file with someone (write, per-user OAuth).

    Honest NOT CONFIGURED when no OAuth user is signed in. Should be
    confirmation-gated upstream: it grants a real person real access to a
    real document, and optionally emails them about it.
    """
    token = _oauth_access_token()
    if token:
        try:
            return _drive_share_oauth(token, file_id, email, role, notify)
        except RuntimeError as exc:
            return f"DRIVE SHARE (reported honestly): {exc}"
    reauth = _oauth_user_needs_reauth("DRIVE SHARE")
    if reauth:
        return reauth
    return (
        "NOT CONFIGURED: sharing a Drive file needs the signed-in Google user's "
        "OAuth session with Drive write scope. No user is signed in — sign in at "
        "/login, then retry. Nothing was shared."
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
    # v13: real bug fixed here, live-caught through an actual directive
    # ("summarize my 5 most recent emails") — imaplib.IMAP4_SSL's own
    # default is timeout=None, meaning an unresponsive IMAP server (a
    # stalled TLS handshake, a network blip, Gmail rate-limiting) blocks
    # the underlying socket FOREVER. Live-observed: a read_inbox call sat
    # past 110 real seconds with zero result, holding the server's single
    # shared session_lock the whole time — every other request queued
    # behind it indefinitely, with no visible error anywhere. A timeout
    # here bounds every IMAP operation on the connection (login, select,
    # search, fetch all share the one socket's timeout), turning an
    # indefinite hang into an honest, real "IMAP timed out" error.
    conn = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)  # matches the SMTP send path's own timeout=30 below
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


_MSG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,80}$")


def _gmail_modify(token: str, msg_id: str, action: str, payload: Any = None) -> dict:
    """One Gmail message mutation. Raises RuntimeError with the real cause."""
    url = f"{_GMAIL_API}/messages/{msg_id}"
    url = url if action == "" else f"{url}/{action}"
    try:
        return _http_json("POST", url, token, payload if payload is not None else {})
    except RuntimeError as exc:
        msg = str(exc)
        if "403" in msg or "insufficientPermissions" in msg:
            raise RuntimeError(
                msg
                + " — this needs the gmail.modify scope. Sign in again at /login "
                "to grant it. Nothing was changed."
            ) from exc
        if "404" in msg:
            raise RuntimeError(
                msg + f" — no message with id {msg_id!r}. Nothing was changed."
            ) from exc
        raise


def _describe(token: str, msg_id: str) -> str:
    """Subject + sender for the audit line, so the result names what moved.

    Best-effort: a mutation that worked must not be reported as failed just
    because the follow-up description could not be fetched.
    """
    try:
        meta = _http_json(
            "GET",
            f"{_GMAIL_API}/messages/{msg_id}?format=metadata"
            "&metadataHeaders=Subject&metadataHeaders=From",
            token,
        )
    except RuntimeError:
        return msg_id
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in (meta.get("payload") or {}).get("headers", [])
    }
    subject = headers.get("subject", "(no subject)")[:80]
    sender = headers.get("from", "")[:60]
    return f"{subject!r}" + (f" from {sender}" if sender else "")


def gmail_archive(message_id: str) -> str:
    """Remove a message from the inbox, keeping it in All Mail (reversible).

    The gentlest of the three: nothing is deleted, the mail stays searchable
    forever, and re-adding the INBOX label puts it straight back. Should be
    confirmation-gated upstream — it still changes what the user sees.
    """
    mid = (message_id or "").strip()
    if not _MSG_ID_RE.match(mid):
        return f"ERROR: {message_id!r} is not a valid Gmail message id. Nothing was changed."
    token = _oauth_access_token()
    if not token:
        reauth = _oauth_user_needs_reauth("ARCHIVE")
        return reauth or (
            "NOT CONFIGURED: archiving needs the signed-in Google user's OAuth "
            "session with the gmail.modify scope. No user is signed in — sign in "
            "at /login, then retry. Nothing was changed."
        )
    what = _describe(token, mid)
    try:
        _gmail_modify(token, mid, "modify", {"removeLabelIds": ["INBOX"]})
    except RuntimeError as exc:
        return f"GMAIL ARCHIVE (reported honestly): {exc}"
    return (
        f"GMAIL ARCHIVED: {what} left the inbox (id {mid}). It is still in All "
        "Mail and fully searchable; nothing was deleted."
    )


def gmail_trash(message_id: str) -> str:
    """Move a message to Trash — recoverable for 30 days.

    Deliberately trash, not delete: Gmail's DELETE endpoint destroys a message
    immediately with no Trash stop and no recovery. An assistant acting on a
    misread instruction should not be able to do that, and trash covers the
    real need. Emptying the Trash stays a human job in Gmail's own UI.

    Must be confirmation-gated upstream.
    """
    mid = (message_id or "").strip()
    if not _MSG_ID_RE.match(mid):
        return f"ERROR: {message_id!r} is not a valid Gmail message id. Nothing was changed."
    token = _oauth_access_token()
    if not token:
        reauth = _oauth_user_needs_reauth("TRASH")
        return reauth or (
            "NOT CONFIGURED: moving mail to Trash needs the signed-in Google "
            "user's OAuth session with the gmail.modify scope. No user is signed "
            "in — sign in at /login, then retry. Nothing was changed."
        )
    what = _describe(token, mid)
    try:
        _gmail_modify(token, mid, "trash")
    except RuntimeError as exc:
        return f"GMAIL TRASH (reported honestly): {exc}"
    return (
        f"GMAIL TRASHED: {what} moved to Trash (id {mid}). Recoverable for 30 "
        "days — use gmail_untrash to put it back."
    )


def gmail_untrash(message_id: str) -> str:
    """Restore a message from Trash. The undo for gmail_trash."""
    mid = (message_id or "").strip()
    if not _MSG_ID_RE.match(mid):
        return f"ERROR: {message_id!r} is not a valid Gmail message id. Nothing was changed."
    token = _oauth_access_token()
    if not token:
        reauth = _oauth_user_needs_reauth("UNTRASH")
        return reauth or (
            "NOT CONFIGURED: restoring mail needs the signed-in Google user's "
            "OAuth session with the gmail.modify scope. Nothing was changed."
        )
    try:
        _gmail_modify(token, mid, "untrash")
    except RuntimeError as exc:
        return f"GMAIL UNTRASH (reported honestly): {exc}"
    return f"GMAIL RESTORED: message {mid} is back out of Trash."


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
    msg["From"] = formataddr((email_display_name(), _user()))
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


# --------------------------------------------------------------------------- #
# Google Sheets + Drive — link-shared access via stdlib urllib (no OAuth).
#
# The zero-setup small-business case: a sheet/file shared "Anyone with the
# link can view" is readable without any login. Private items honestly
# report the exact fix (share with the link) instead of fabricating data
# (Rule 2.2). Writes/private-Drive access need OAuth, which stays a future
# opt-in dependency — never a hard one.
# --------------------------------------------------------------------------- #

_UA = "Mozilla/5.0 (Dourmouse/1.0)"


def _http_get(url: str, timeout: int = 25) -> tuple[int, bytes, str]:
    """GET a URL with a browser-ish UA; returns (status, body, content_type)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), (resp.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as exc:
        return exc.code, b"", (exc.headers.get("Content-Type") or "")
    except Exception as exc:  # noqa: BLE001 - network failures, readable
        raise RuntimeError(f"NETWORK ERROR: {type(exc).__name__}: {exc}") from exc


def _gviz_to_json(payload: str) -> str:
    """Convert gviz's single-quoted/bare-key payload into strict JSON.

    Google's endpoint emits ``{version:'0.6',...}`` (bare keys, single-
    quoted string values, bare numbers) and ``Date(2024,1,1,0,0,0)`` for
    date cells. This stateful pass swaps the delimiters without touching
    apostrophes inside values, and turns Date(...) into a readable string.
    """
    import re

    out: list[str] = []
    i, n = 0, len(payload)
    while i < n:
        ch = payload[i]
        if ch == "'":
            prev = out[-1] if out else ""
            nxt = payload[i + 1] if i + 1 < n else ""
            if nxt == "'":  # escaped apostrophe inside a value
                out.append("'")
                i += 2
                continue
            if nxt in ",}]:":  # closing a quoted value
                out.append('"')
            elif prev in ":,[{":  # opening a quoted value
                out.append('"')
            else:
                out.append(ch)  # apostrophe inside a value — keep literal
        else:
            out.append(ch)
        i += 1
    text = "".join(out)
    # gviz object keys are bare (version:...) — wrap them for strict JSON.
    text = re.sub(r"([,{]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)", r'\1"\2"\3', text)
    return re.sub(r"Date\((\d{4}),(\d{1,2}),(\d{1,2})[^)]*\)", r'"\1-\2-\3"', text)


def _valid_google_id(value: str, what: str) -> bool:
    """A Google doc/file id is a plain token — reject path-ish input outright."""
    v = (value or "").strip()
    return bool(v) and "/" not in v and len(v) <= 200 and all(c.isalnum() or c in "-_" for c in v)


def sheets_read(
    spreadsheet_id: str, sheet: str = "Sheet1", max_rows: int = 50, max_cols: int = 20
) -> str:
    """Read a Google Sheet's values as aligned rows (link-shared, no login).

    Uses Google's public gviz JSON endpoint, so it works for any sheet
    shared "Anyone with the link can view" — the zero-setup case. Private
    sheets honestly report the exact fix (Rule 2.2).
    """
    sid = (spreadsheet_id or "").strip()
    if not _valid_google_id(sid, "spreadsheet"):
        return (
            "ERROR: sheets_read needs the spreadsheet ID (the long token in "
            "the URL between /d/ and /edit)."
        )
    name = (sheet or "Sheet1").strip()
    url = (
        "https://docs.google.com/spreadsheets/d/" + sid
        + "/gviz/tq?tqx=out:json&sheet=" + urllib.parse.quote(name)
    )
    status, body, _ctype = _http_get(url)
    text = body.decode("utf-8", errors="replace")
    if status != 200:
        return (
            f"SHEETS READ FAILED: HTTP {status}. The sheet must be shared "
            "'Anyone with the link can view' (Share -> General access -> "
            "Anyone with the link). No data was fabricated."
        )
    # gviz wraps the JSON in google.visualization.Query.setResponse({...});
    prefix = "google.visualization.Query.setResponse("
    if prefix not in text:
        return "SHEETS READ FAILED: unexpected response from Google (not gviz JSON)."
    payload = text[text.index(prefix) + len(prefix):]
    payload = payload[: payload.rfind(");")]
    try:
        data = json.loads(_gviz_to_json(payload))
    except json.JSONDecodeError as exc:
        return f"SHEETS READ FAILED: could not parse the sheet response ({exc})."
    if data.get("status") == "error":
        detail = " ".join(str(e) for e in data.get("errors") or [])
        return (
            f"SHEETS READ FAILED: Google said: {detail or 'access denied'}. "
            "The sheet must be shared 'Anyone with the link can view'. "
            "No data was fabricated."
        )
    table = data.get("table") or {}
    cols = [
        ((c or {}).get("label") or (c or {}).get("id") or "")
        for c in (table.get("cols") or [])[:max_cols]
    ]
    rows = []
    for r in (table.get("rows") or [])[:max_rows]:
        cells = [
            (c or {}).get("v") if isinstance(c, dict) else None
            for c in (r.get("c") or [])[:max_cols]
        ]
        rows.append(cells)
    if not cols and not rows:
        return "SHEETS READ: the sheet is empty."
    out = [f"SHEETS READ: {name}", ""]
    out.append(" | ".join(str(c) for c in cols))
    out.append("-" * 60)
    for cells in rows:
        out.append(" | ".join("" if v is None else str(v) for v in cells))
    out.append("")
    out.append(f"({len(rows)} rows x {len(cols)} cols shown)")
    return "\n".join(out)


def drive_download(file_id: str, dest: str = "") -> str:
    """Download a link-shared Google Drive file by its ID (stdlib urllib).

    Works for files shared "Anyone with the link" — the zero-setup case.
    Private files honestly report the exact fix. With no dest, saves into
    the workspace uploads sandbox (readable back via read_upload).
    """
    fid = (file_id or "").strip()
    if not _valid_google_id(fid, "drive file"):
        return (
            "ERROR: drive_download needs the file ID (the long token in the "
            "URL between /d/ and /view)."
        )
    if not dest:
        from dourmouse.system_access import _uploads_root

        dest = str(_uploads_root() / f"{fid}.bin")
    try:
        path = Path(dest).expanduser()
    except Exception:  # noqa: BLE001
        return "ERROR: drive_download needs a writable destination path."
    url = f"https://drive.google.com/uc?export=download&id={fid}"
    status, body, ctype = _http_get(url)
    if status != 200:
        return (
            f"DRIVE DOWNLOAD FAILED: HTTP {status}. The file must be shared "
            "'Anyone with the link' (Share -> General access -> Anyone with "
            "the link). Nothing was downloaded."
        )
    text = body.decode("utf-8", errors="replace")
    if ctype.startswith("text/html") and "googleusercontent.com" not in text:
        if "signin" in text.lower() or "Sign in" in text:
            return (
                "DRIVE DOWNLOAD FAILED: Google wants a login — the file is "
                "not link-shared. Share it 'Anyone with the link' (General "
                "access -> Anyone with the link) and retry. Nothing was "
                "downloaded."
            )
        if "confirm=" in text or "virus scan" in text.lower():
            return (
                "DRIVE DOWNLOAD: the file is large or behind Google's "
                "virus-scan confirmation. Download it manually in a browser "
                "or use a smaller file. Nothing was saved."
            )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(body)
    except OSError as exc:
        return f"DRIVE DOWNLOAD FAILED: could not write {path}: {exc}"
    return f"DRIVE DOWNLOAD OK: {path} ({len(body)} bytes, {ctype or 'unknown type'})"


def status() -> dict[str, Any]:
    """Honest capability report for the SETUP panel."""
    source = "env" if os.environ.get("GOOGLE_GMAIL_USER", "").strip() else "local_secrets.py"
    return {
        "configured": gmail_configured(),
        "detail": (
            f"{_user()} (via {source})" if gmail_configured() else "no Gmail login set"
        ),
        "hint": "env vars OR dourmouse/local_secrets.py; 2-Step Verification -> App passwords",
        "identity": f"{email_display_name()} <{_user()}>" if gmail_configured() else None,
        "own_address": (
            email_identity.own_address()
            if gmail_configured() and email_identity.own_address()
            else None
        ),
        "sheets": "link-shared sheets readable via the gviz endpoint (no login)",
        "drive": "link-shared files downloadable via uc?export=download (no login)",
    }


def _main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" in argv:
        s = status()
        print(f"GMAIL: {'CONFIGURED ' + s['detail'] if s['configured'] else 'NOT CONFIGURED — ' + s['hint']}")
        print(f"SHEETS: {s['sheets']}")
        print(f"DRIVE: {s['drive']}")
        print("SETUP: 1) enable 2-Step Verification  2) create an App password")
        print("       3) set GOOGLE_GMAIL_USER + GOOGLE_GMAIL_APP_PASSWORD in .env")
        print("          or fill GMAIL_USER/GMAIL_APP_PASSWORD in dourmouse/local_secrets.py")
        return 0 if s["configured"] else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
