"""The rules the Dourmouse lockdown browser extension enforces (extension/lockdown).

Whole website names are blocked system-wide through /etc/hosts. A URL path
("reddit.com/r/all") cannot be, so those entries go to a Chrome/Chromium
Manifest V3 extension as declarativeNetRequest dynamic rules. The extension
polls GET /api/security/lockdown/rules and installs whatever this module
builds: `rules_payload(blocklist)` is the whole answer, `build_rules` the pure
core. Rules exist only while a lockdown is active.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .lockdown import Blocklist

#: Chrome allows 30,000 dynamic rules; the blocklist itself is capped at
#: lockdown.MAX_URLS, so the rule count is far below any limit. Pages and
#: their frames only: images and scripts of other sites stay untouched.
RESOURCE_TYPES = ["main_frame", "sub_frame"]


def build_rules(bl: Blocklist) -> list[dict[str, Any]]:
    """Chrome declarativeNetRequest dynamic rules for the blocklist's URL
    entries: [] unless lockdown is active. Ids are dense integers from 1, in
    sorted URL order, so the same blocklist always yields the same rules.
    `||host/path` anchors at the host (and its subdomains) and blocks any URL
    whose path starts with the entry."""
    if not bl.active:
        return []
    urls = sorted({u["url"] for u in bl.urls})
    return [
        {"id": i, "priority": 1, "action": {"type": "block"},
         "condition": {"urlFilter": "||" + url, "resourceTypes": list(RESOURCE_TYPES)}}
        for i, url in enumerate(urls, start=1)
    ]


def rules_version(rules: list[dict[str, Any]]) -> str:
    """A short hash of the rules, so the extension can skip an unchanged sync."""
    blob = json.dumps(rules, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def rules_payload(bl: Blocklist) -> dict[str, Any]:
    """The JSON the rules endpoint returns: {"active", "version", "rules"}."""
    rules = build_rules(bl)
    return {"active": bl.active, "version": rules_version(rules), "rules": rules}
