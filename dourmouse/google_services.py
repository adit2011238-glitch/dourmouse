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

import imaplib
import json
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, parsedate_to_datetime
from pathlib import Path
from typing import Any


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

    ``query`` accepts real Gmail search syntax (``from:x``, ``newer_than:3d``,
    ``label:y``, ``has:attachment`` …) passed through X-GM-RAW, or plain words
    matched against subject/from/body. An empty query browses the most recent
    messages. Returns readable rows: date, from, subject.
    """
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
    """Fetch ONE message body by IMAP uid (returns text/plain body + headers)."""
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
    """Send an email via Gmail SMTP (must be confirmation-gated upstream)."""
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
    """Google Calendar read — honest NOT CONFIGURED for now.

    Calendar needs the Google Calendar API (google-api-python-client). It is
    intentionally not a hard dependency of Dourmouse; when the user wants it,
    ``pip install google-api-python-client google-auth-oauthlib`` and this
    tool is wired to the OAuth flow. Until then it reports the truth instead
    of fabricating events (Rule 2.2).
    """
    return (
        "NOT CONFIGURED: Google Calendar requires the optional "
        "'google-api-python-client' package + an OAuth client. Install it and "
        "run 'python -m dourmouse.google_services --setup-calendar' to link. "
        "No events were fetched."
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
