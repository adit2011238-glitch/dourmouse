"""Backend for the OS shell's NEWS screen (finding #146).

One route. The catch-up read (``GET /api/news``) and the live ``news_item``
events already exist; the only thing the screen needed that had no route was
TO RESEARCH.

* ``POST /api/os/news/research {url, title, channel?}``: starts a research
  record seeded with the headline's link as its first source. It goes through
  the same store and the same record type the evidence-pipeline tools use
  (``research_pipeline_tools._load_or_start`` and ``_save``), so a record made
  here is the record the RESEARCH screen and the ``research_*`` tools see.

  It does NOT call a model. The pipeline's own ``plan`` stage does (it spends
  the owner's cloud credits), so this route writes one plain sub-question
  built from the headline instead and leaves the model stages for the owner
  to run from RESEARCH. It writes one row in the local research database and
  nothing else: no fetch of the link, no mail, no file. The screen shows what
  it will do and asks first.
"""

from __future__ import annotations

import re
from typing import Any

from . import ApiError, Request, route

_MAX_URL = 2000
_MAX_TITLE = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f\s]")
_CHANNEL = re.compile(r"^[a-z0-9_ -]{0,40}$")


def clean_url(raw: Any) -> str:
    url = str(raw or "").strip()
    if not url:
        raise ApiError(400, "url is required")
    if len(url) > _MAX_URL:
        raise ApiError(400, f"url is longer than {_MAX_URL} characters")
    if _CONTROL.search(url):
        raise ApiError(400, "url must not contain spaces or control characters")
    if not re.match(r"^https?://[^/]", url, re.IGNORECASE):
        raise ApiError(400, "url must be a real http(s) URL")
    return url


def clean_title(raw: Any) -> str:
    title = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not title:
        raise ApiError(400, "title is required")
    return title[:_MAX_TITLE]


@route("POST", "/api/os/news/research")
def to_research(req: Request) -> tuple[int, dict[str, Any]]:
    body = req.body if isinstance(req.body, dict) else {}
    url = clean_url(body.get("url"))
    title = clean_title(body.get("title"))
    channel = str(body.get("channel") or "").strip().lower()
    if channel and not _CHANNEL.match(channel):
        raise ApiError(400, "channel has characters that are not allowed")

    from dourmouse.research_pipeline_tools import _load_or_start, _save

    record = _load_or_start(title)
    created = not record.plan and not record.sources
    if not record.plan:
        record.set_plan([f"What do reliable sources report about: {title}"])
    added = url not in record.sources
    if added:
        record.add_sources([url])
    _save(record)
    return 200, {
        "ok": True,
        "created": created,
        "source_added": added,
        "question": record.question,
        "stage": record.stage.name,
        "sources": list(record.sources),
        "channel": channel,
    }
