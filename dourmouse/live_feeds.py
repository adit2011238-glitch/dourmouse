"""Live-intelligence feeds (v2.3) — keyless, stdlib-only data sources.

Powers the preloaded ``news``, ``markets``, ``mail`` and ``tasks`` subagents
with REAL data over the wire — no fabricated numbers, no silent stubs
(Rules 2.1 / 2.2):

- News: Google News RSS (keyless, public).
- Markets: Yahoo Finance v8 chart API (quotes) + v1 screener API (day
  gainers / day losers) — keyless, needs a browser-ish User-Agent.
- Mail: read-only IMAP inbox via stdlib ``imaplib``, activated ONLY when
  ``DOURMOUSE_IMAP_HOST`` / ``DOURMOUSE_IMAP_USER`` / ``DOURMOUSE_IMAP_PASS`` are set;
  otherwise honestly NOT CONFIGURED. Never sends anything.
- Tasks: a deterministic local task list persisted to the workspace
  (``tasks.json``) — pure CRUD, no LLM in the data path (Rule 2.8).

Every network call carries a timeout and fails loudly with an honest error
string — a transient outage never masquerades as a data result.
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
_TIMEOUT = 15
_NEWS_FEED = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
_NEWS_SEARCH = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
# v8.9: Yahoo rate-limits per source IP, and it had blocked query1 outright
# from the compute host — every quote came back HTTP 429 there while the
# SAME url answered fine from another machine. query2 is a separate edge
# that was still serving, so both hosts are tried in order rather than
# hard-coding one. Verified live from the host: query1 429, query2 200.
_YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
_YAHOO_QUOTE_PATH = "/v8/finance/chart/{sym}?interval=1d&range=1d"
_YAHOO_SCREENER_PATH = (
    "/v1/finance/screener/predefined/saved"
    "?formatted=true&count={count}&scrIds={scr_id}"
)
#: Kept for callers/tests that reference the single-URL form.
_YAHOO_QUOTE = "https://" + _YAHOO_HOSTS[0] + _YAHOO_QUOTE_PATH
_YAHOO_SCREENER = "https://" + _YAHOO_HOSTS[0] + _YAHOO_SCREENER_PATH


def _yahoo_json(path: str) -> dict[str, Any]:
    """Fetch a Yahoo JSON path, trying each edge host before giving up.

    A 429 from one host does not mean the data is unavailable — it means
    that edge is throttling this IP. Falling through to the next host keeps
    the markets channel alive instead of reporting the whole source dead.
    The LAST error is raised so the message names a real failure.
    """
    last: Exception | None = None
    for host in _YAHOO_HOSTS:
        try:
            return _json("https://" + host + path)
        except Exception as exc:  # noqa: BLE001 - try the next edge
            last = exc
    raise RuntimeError(str(last) if last else "no Yahoo host reachable")
_SCR_IDS = {"gainers": "day_gainers", "losers": "day_losers"}

_IMAP_HOST = "DOURMOUSE_IMAP_HOST"
_IMAP_USER = "DOURMOUSE_IMAP_USER"
_IMAP_PASS = "DOURMOUSE_IMAP_PASS"
_TASKS_ENV = "DOURMOUSE_TASKS_FILE"


# --------------------------------------------------------------------------- #
# HTTP helper — honest errors only
# --------------------------------------------------------------------------- #

def _http_get(url: str, *, timeout: int = _TIMEOUT, headers: dict[str, str] | None = None) -> str:
    """GET a URL with a timeout and a browser-ish UA; raise on any failure."""
    hdrs = {"User-Agent": _UA, "Accept": "application/json, text/xml, */*"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error fetching {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"timeout fetching {url}") from exc
    except ssl.SSLError as exc:
        raise RuntimeError(f"TLS error fetching {url}: {exc}") from exc


def _json(url: str) -> dict[str, Any]:
    data = _http_get(url)
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-JSON response from {url}: {data[:120]!r}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"unexpected response shape from {url}")
    return parsed


# --------------------------------------------------------------------------- #
# News — Google News RSS (keyless)
# --------------------------------------------------------------------------- #

def _parse_rss_items(raw: str, max_results: int, *, what: str) -> list[dict[str, str]]:
    """Parse a Google News RSS body into {title, source, published} rows.

    Shared by the top-headlines feed and the topic search, which return the
    same document shape from different endpoints.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"could not parse {what}: {exc}") from exc
    items: list[dict[str, str]] = []
    for item in root.iter("item"):
        title = ""
        source = ""
        published = ""
        for child in item:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "title":
                title = (child.text or "").strip()
            elif tag == "source":
                source = (child.text or "").strip()
            elif tag == "pubDate":
                published = (child.text or "").strip()
        if title:
            items.append({"title": title, "source": source, "published": published})
        if len(items) >= max_results:
            break
    return items


def news_headlines(max_results: int = 10) -> list[dict[str, str]]:
    """Return real top headlines as {title, source, published} (newest first).

    Raises RuntimeError on any fetch/parse failure — never fabricates.
    """
    max_results = max(1, min(int(max_results), 25))
    raw = _http_get(_NEWS_FEED)
    items = _parse_rss_items(raw, max_results, what="news feed")
    if not items:
        raise RuntimeError("news feed returned no items")
    return items


def news_search(query: str, max_results: int = 10) -> list[dict[str, str]]:
    """Search current news for `query` — keyless, via Google News RSS.

    Exists because the roster had no tool for "what happened in X": live
    questions about sport, politics, or any unfolding event had nowhere
    correct to go, so the model routed them into stock_quote, which asked
    Yahoo Finance for a ticker named BANGLADESH and surfaced the 404. A
    dated, sourced headline is the honest answer to those questions.

    Raises RuntimeError on any fetch/parse failure — never fabricates.
    """
    query = (query or "").strip()
    if not query:
        raise RuntimeError("news_search requires a non-empty query")
    max_results = max(1, min(int(max_results), 25))
    url = _NEWS_SEARCH.format(q=urllib.parse.quote(query))
    raw = _http_get(url)
    items = _parse_rss_items(raw, max_results, what="news search feed")
    if not items:
        raise RuntimeError(f"no news results for {query!r}")
    return items


# --------------------------------------------------------------------------- #
# Markets — Yahoo Finance (keyless)
# --------------------------------------------------------------------------- #

def stock_quote(symbol: str) -> dict[str, Any]:
    """Real quote for one symbol via the Yahoo v8 chart API.

    Returns {symbol, price, currency, day_high, day_low, week52_high,
    week52_low, as_of}. Raises RuntimeError for unknown symbols or network
    failures — never a made-up price.
    """
    sym = (symbol or "").strip().upper()
    # v8.9: '=' added. Yahoo uses it for futures and FX pairs (CL=F crude,
    # GC=F gold, EURUSD=X), so the old pattern rejected the exact symbols
    # the world monitor asks for with "invalid symbol" — a validation bug
    # that looked like a data outage.
    if not sym or not re.fullmatch(r"[A-Z0-9.\-^=]{1,12}", sym):
        raise RuntimeError(f"invalid symbol {symbol!r}")
    data = _yahoo_json(_YAHOO_QUOTE_PATH.format(sym=urllib.parse.quote(sym)))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError(f"no quote for {sym} (symbol unknown?)")
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        raise RuntimeError(f"no price data for {sym}")
    return {
        "symbol": meta.get("symbol", sym),
        "price": float(price),
        "currency": meta.get("currency", "USD"),
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "week52_high": meta.get("fiftyTwoWeekHigh"),
        "week52_low": meta.get("fiftyTwoWeekLow"),
        "as_of": meta.get("regularMarketTime"),
    }


def market_movers(direction: str = "gainers", count: int = 10) -> list[dict[str, Any]]:
    """Real top day gainers/losers via the Yahoo screener API.

    Each row: {symbol, name, price, change, change_pct, currency}. Raises
    RuntimeError on failure — never fabricates a ranking.
    """
    direction = (direction or "gainers").lower()
    scr = _SCR_IDS.get(direction)
    if scr is None:
        raise RuntimeError(f"direction must be 'gainers' or 'losers', got {direction!r}")
    count = max(1, min(int(count), 25))
    data = _yahoo_json(_YAHOO_SCREENER_PATH.format(count=count, scr_id=scr))
    result = (data.get("finance") or {}).get("result") or []
    quotes = result[0].get("quotes") if result else None
    if not quotes:
        raise RuntimeError(f"no {direction} data returned by screener")
    rows = []
    for q in quotes:
        price = q.get("regularMarketPrice")
        if price is None:
            continue
        rows.append(
            {
                "symbol": q.get("symbol", ""),
                "name": q.get("longName") or q.get("shortName") or q.get("symbol", ""),
                "price": float(price),
                "change": q.get("regularMarketChange"),
                "change_pct": q.get("regularMarketChangePercent"),
                "currency": q.get("currency", "USD"),
            }
        )
    if not rows:
        raise RuntimeError(f"screener returned no usable {direction} rows")
    return rows


# --------------------------------------------------------------------------- #
# Mail — read-only IMAP, env-gated (never sends)
# --------------------------------------------------------------------------- #

def _read_inbox_oauth(token: str, max_items: int) -> list[dict[str, str]]:
    """Read recent messages as {from_, subject, date, snippet} via the Gmail
    API for the SIGNED-IN user's own mailbox (v5.16). One request per
    message with format=full (headers + snippet); never the shared inbox.
    """
    import urllib.parse

    from dourmouse.google_services import _GMAIL_API, _http_json

    params = urllib.parse.urlencode(
        {"maxResults": max(1, min(int(max_items), 50))}
    )
    listing = _http_json("GET", f"{_GMAIL_API}/messages?{params}", token)
    rows: list[dict[str, str]] = []
    for item in (listing.get("messages") or [])[:50]:
        mid = str(item.get("id") or "")
        meta = _http_json(
            "GET",
            f"{_GMAIL_API}/messages/{urllib.parse.quote(mid)}?format=full",
            token,
        )
        headers = {}
        for h in meta.get("payload", {}).get("headers", []):
            headers[str(h.get("name", "")).lower()] = str(h.get("value", ""))
        rows.append(
            {
                "from_": headers.get("from", "")[:120],
                "subject": headers.get("subject", "")[:160],
                "date": headers.get("date", "")[:40],
                "snippet": str(meta.get("snippet") or "")[:200],
            }
        )
    return rows


def read_inbox(max_items: int = 10) -> list[dict[str, str]]:
    """Read the most recent N messages from the INBOX (read-only).

    v5.16 per-user: a LOGGED-IN Google user reads THEIR own mailbox via the
    Gmail API (OAuth). The IMAP path (owner App-Password / DOURMOUSE_IMAP_*)
    applies ONLY when no user is signed in — a signed-in user whose token is
    missing/expired gets an honest re-sign-in error instead of the server
    owner's shared inbox (reviewer-caught cross-account leak).

    Returns {from_, subject, date, snippet} per message. Never sends or
    deletes anything (Rule 2.9: read-only).
    """
    from dourmouse.google_services import (
        _oauth_access_token,
        _oauth_user_needs_reauth,
    )

    token = _oauth_access_token()
    if token:
        return _read_inbox_oauth(token, max_items)
    reauth = _oauth_user_needs_reauth("INBOX")
    if reauth:
        raise RuntimeError(reauth)
    import email as email_mod
    import imaplib
    import os
    from email.header import decode_header

    host = os.environ.get(_IMAP_HOST, "").strip()
    user = os.environ.get(_IMAP_USER, "").strip()
    password = os.environ.get(_IMAP_PASS, "")
    # v5.2: fall back to the Gmail module's config (env vars or the
    # gitignored dourmouse/local_secrets.py) so one Google App Password
    # powers BOTH gmail_* tools and read_inbox — no duplicate setup.
    if not (host and user and password) and not host:
        # v5.2: fall back to the Gmail module's config ONLY when the IMAP
        # env vars are entirely absent — never mix a custom IMAP_HOST with
        # Gmail credentials (reviewer-caught: logging into a non-Gmail host
        # with a Gmail app password would fail confusingly).
        try:
            from dourmouse.google_services import _app_password, _user

            fallback_user = _user()
            fallback_pass = _app_password()
        except Exception:  # noqa: BLE001 - a broken gmail module must not crash feeds
            fallback_user, fallback_pass = "", ""
        if fallback_user and fallback_pass:
            host = "imap.gmail.com"
            user = fallback_user
            password = fallback_pass
    if not host or not user or not password:
        raise RuntimeError(
            "NOT CONFIGURED: set DOURMOUSE_IMAP_HOST / DOURMOUSE_IMAP_USER / "
            "DOURMOUSE_IMAP_PASS (or configure Gmail in local_secrets.py) to "
            "enable read-only inbox access."
        )
    max_items = max(1, min(int(max_items), 50))
    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
        _typ, data = conn.search(None, "ALL")
        ids = (data[0] or b"").split()
        recent = ids[-max_items:]  # newest last in IMAP; take the tail
        messages = []
        for msg_id in recent:
            _t, msg_data = conn.fetch(msg_id, "(RFC822)")
            if not msg_data or msg_data[0] is None:
                continue
            msg = email_mod.message_from_bytes(msg_data[0][1])

            def _dec(value: Any) -> str:
                if not value:
                    return ""
                parts = decode_header(value)
                out = []
                for text, charset in parts:
                    if isinstance(text, bytes):
                        try:
                            text = text.decode(charset or "utf-8", errors="replace")
                        except LookupError:
                            text = text.decode("utf-8", errors="replace")
                    out.append(str(text))
                return " ".join(out).strip()

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and not body:
                        body = part.get_payload(decode=True) or b""
                        body = body.decode("utf-8", errors="replace")
            else:
                body = (msg.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
            body = re.sub(r"\s+", " ", body).strip()
            messages.append(
                {
                    "from_": _dec(msg.get("From")),
                    "subject": _dec(msg.get("Subject")),
                    "date": (msg.get("Date") or "").strip(),
                    "snippet": body[:200],
                }
            )
        return messages
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001,S110 - logout must never mask results
            pass


# --------------------------------------------------------------------------- #
# Tasks — deterministic local task list
# --------------------------------------------------------------------------- #

def _tasks_path() -> Path:
    import os

    root = Path(os.environ.get("DOURMOUSE_WORKSPACE", "").strip() or "workspace")
    env = os.environ.get(_TASKS_ENV, "").strip()
    if env:
        return Path(env)
    return root / "tasks.json"


def _load_tasks(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, dict)]


def _save_tasks(path: Path, tasks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(tasks, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _tasks_file() -> Path:
    return _tasks_path().expanduser()


def list_tasks(include_done: bool = True) -> list[dict[str, Any]]:
    """Return the task list (id, title, created_at, done), oldest first."""
    tasks = _load_tasks(_tasks_file())
    tasks.sort(key=lambda t: t.get("created_at", ""))
    if not include_done:
        tasks = [t for t in tasks if not t.get("done")]
    return tasks


def add_task(title: str) -> dict[str, Any]:
    """Add one task; returns the created record. Deterministic local CRUD."""
    title = (title or "").strip()
    if not title:
        raise RuntimeError("add_task requires a non-empty title")
    path = _tasks_file()
    tasks = _load_tasks(path)
    task = {
        "id": f"task-{len(tasks) + 1}",
        "title": title[:300],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "done": False,
    }
    tasks.append(task)
    _save_tasks(path, tasks)
    return task


def complete_task(task_id: str) -> bool:
    """Mark a task done; returns whether it existed and was changed."""
    path = _tasks_file()
    tasks = _load_tasks(path)
    changed = False
    for t in tasks:
        if t.get("id") == task_id and not t.get("done"):
            t["done"] = True
            t["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            changed = True
    if changed:
        _save_tasks(path, tasks)
    return changed
