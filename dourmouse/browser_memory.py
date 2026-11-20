"""Save a browser page and the agent's notes to the library (T5 step 3, finding 212).

``save_page`` is the part of ``browser_keep_page`` that does not need a browser: the tool reads the current page
(address, title, readable text) and hands it here. The page goes to the user wiki as source ``page`` through
``library.wiki().add``, so it shows up in Library search with its address and title. Saving the same address again
replaces the earlier copy. When the save names a job, the job's log gets a ``note`` event and the message bus gets a
short post (the Agent Room replaces the bus once it exists).

Nothing here opens a page or reads one: a page that is Dourmouse's own, or that is not a plain http(s) address, is
refused, and so is every save while Private mode is on.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

#: Readable text kept per page. The library's own limit is far larger; this keeps one page from filling a store.
PAGE_TEXT_MAX = 200_000
NOTES_MAX = 4_000
_JOB_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _own_address(url: str) -> bool:
    from dourmouse.browser_agent import _is_own_server_url

    return _is_own_server_url(url)


def _job_known(job: str) -> bool:
    from dourmouse.goals import default_store

    try:
        return default_store().get_goal(job) is not None
    except Exception:  # noqa: BLE001 - a goal store that cannot be read is "no such job", said in the result
        return False


def _tell_job(job: str, title: str, url: str, ref: str, notes: str) -> str:
    """Post the short note. Returns "" or a plain sentence about what could not be posted."""
    problems: list[str] = []
    line = f"Saved page: {title or url} ({url}). Library entry {ref}."
    if notes:
        line += " Notes: " + notes[:300]
    try:
        from dourmouse.goals import default_store

        default_store().log_event(job, "note", {"kind": "page_saved", "url": url, "title": title, "library": ref})
    except Exception as exc:  # noqa: BLE001 - the page is already saved; say what did not follow
        problems.append(f"the job log did not take the note ({type(exc).__name__})")
    try:
        from dourmouse.message_bus import BROADCAST, get_message_bus

        get_message_bus().post("browser", BROADCAST, f"page saved for job {job}", line)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"the message bus did not take the note ({type(exc).__name__})")
    return "; ".join(problems)


def save_page(url: str, title: str, text: str, notes: str = "", job: str = "") -> str:
    """Store one page. Returns a plain sentence for the model, starting ``SAVED`` or ``REFUSED``/``ERROR``."""
    from dourmouse import library, privacy

    url = str(url or "").strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"REFUSED: only a page with an http or https address can be saved, not {url[:80]!r}."
    if _own_address(url):
        return "REFUSED: that page is Dourmouse's own, and a page of the app is never saved."
    if privacy.PrivacyStore().private_mode():
        return "REFUSED: Private mode is on, so nothing from a page is saved to the library."
    job = str(job or "").strip()
    if job and not _JOB_ID.match(job):
        return "REFUSED: the job id has characters a job id never has."
    body = re.sub(r"\n{3,}", "\n\n", str(text or "")).strip()[:PAGE_TEXT_MAX]
    notes = str(notes or "").strip()[:NOTES_MAX]
    if not body and not notes:
        return "ERROR: the page has no readable text and there are no notes, so there is nothing to save."
    parts_out = []
    if notes:
        parts_out.append("Notes by the agent:\n" + notes)
    if body:
        parts_out.append(body)
    full = "\n\n".join(parts_out)
    if library.holds_secret(full):
        return "REFUSED: the page or the notes look like they hold a key or a password, so they were not saved."
    clean = url.split("#", 1)[0]
    source_id = hashlib.sha256(clean.encode("utf-8", "replace")).hexdigest()[:32]
    meta: dict[str, Any] = {
        "title": str(title or "").strip()[:300] or parts.hostname,
        "url": url,
        "source_id": source_id,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "tier": "T1",
    }
    if job:
        meta["job"] = job
    try:
        added = library.wiki().add("page", full, meta)
    except ValueError as exc:
        return f"ERROR: the library did not take the page: {exc}"
    ref = f"wiki:{added['passage_ids'][0]}" if added.get("passage_ids") else "wiki"
    out = f"SAVED {meta['title']!r} ({url}) to the library as {ref}, {len(added['passage_ids'])} passage(s)."
    if job:
        if not _job_known(job):
            return out + f" Job {job} is not a job Dourmouse knows, so no note was posted."
        problem = _tell_job(job, meta["title"], url, ref, notes)
        out += f" Noted on job {job}." if not problem else f" Saved, but {problem}."
    return out
