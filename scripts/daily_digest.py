#!/usr/bin/env python3
"""Daily digest generator: morning briefing from memory + markets + mail.

Runs at 7am (via scheduled task), synthesizes a digest summary of:
  - unread emails (recent 5)
  - market changes (if APCA connected)
  - calendar events (if calendar agent available)
  - daily news (if worldmonitor connected)

Output: printed to stdout + optionally emailed or posted to a dashboard.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import dourmouse.config  # noqa: F401


def get_digest_sections() -> dict[str, str]:
    """Build digest sections from available data."""
    digest = {}

    # 1. Unread emails (last 5)
    try:
        from dourmouse.mail import gmail_client
        client = gmail_client()
        if client:
            result = client.users().messages().list(
                userId="me", q="is:unread", maxResults=5
            ).execute()
            msgs = result.get("messages", [])
            if msgs:
                subjects = []
                for msg in msgs[:5]:
                    full = client.users().messages().get(
                        userId="me", id=msg["id"]
                    ).execute()
                    headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
                    subjects.append(f"  - {headers.get('Subject', '(no subject)')}")
                digest["emails"] = "\n".join(subjects) if subjects else "(none)"
            else:
                digest["emails"] = "(none)"
    except Exception as e:
        digest["emails"] = f"(error: {e})"

    # 2. Memory highlights (facts tagged with today's date, or pinned facts)
    try:
        from dourmouse.memory_store import open_default_store
        store = open_default_store()
        if store:
            # Simplified: just count stored facts. Full impl would rank by recency/relevance.
            digest["memory"] = f"({store.count()} facts in memory)"
    except Exception as e:
        digest["memory"] = f"(error: {e})"

    # 3. Market status (if APCA configured)
    try:
        import os
        if os.environ.get("APCA_API_KEY_ID"):
            digest["markets"] = "(market data available; poll APCA for details)"
        else:
            digest["markets"] = "(not configured)"
    except Exception:
        digest["markets"] = "(error)"

    return digest


def format_digest(sections: dict[str, str]) -> str:
    """Format digest sections into readable output."""
    lines = [
        f"DOURMOUSE Daily Digest — {datetime.date.today()}",
        "",
    ]
    for label, content in sections.items():
        lines.append(f"{label.upper()}:")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


def main():
    print("Generating daily digest...")
    sections = get_digest_sections()
    digest = format_digest(sections)
    print(digest)
    # TODO: email to user, or post to dashboard


if __name__ == "__main__":
    main()
