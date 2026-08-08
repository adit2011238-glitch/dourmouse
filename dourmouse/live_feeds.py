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
_YAHOO_QUOTE = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
_YAHOO_SCREENER = (
    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    "?formatted=true&count={count}&scrIds={scr_id}"
)
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

def news_headlines(max_results: int = 10) -> list[dict[str, str]]:
    """Return real top headlines as {title, source, published} (newest first).

    Raises RuntimeError on any fetch/parse failure — never fabricates.
    """
    max_results = max(1, min(int(max_results), 25))
    raw = _http_get(_NEWS_FEED)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"could not parse news feed: {exc}") from exc
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
    if not items:
        raise RuntimeError("news feed returned no items")
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
    if not sym or not re.fullmatch(r"[A-Z0-9.\-^]{1,12}", sym):
        raise RuntimeError(f"invalid symbol {symbol!r}")
    data = _json(_YAHOO_QUOTE.format(sym=sym))
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
    data = _json(_YAHOO_SCREENER.format(count=count, scr_id=scr))
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

def read_inbox(max_items: int = 10) -> list[dict[str, str]]:
    """Read the most recent N messages from IMAP INBOX (read-only).

    Activated only when DOURMOUSE_IMAP_HOST / DOURMOUSE_IMAP_USER /
    DOURMOUSE_IMAP_PASS are set. Otherwise raises RuntimeError with a clear
    NOT CONFIGURED message. Returns {from_, subject, date, snippet} per
    message. Never sends or deletes anything (Rule 2.9: read-only).
    """
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
