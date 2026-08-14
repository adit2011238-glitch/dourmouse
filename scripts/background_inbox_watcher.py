#!/usr/bin/env python3
"""Background inbox monitor: poll Gmail every N minutes, log new emails to memory.

Run hourly via cron/task scheduler. Logs new unread messages as facts in the
memory store with context (sender, subject, snippet) so the chat context
picks them up in daily digests and ad-hoc "what's new?" queries.

Usage:
  python scripts/background_inbox_watcher.py
  python scripts/background_inbox_watcher.py --interval 15  # check every 15 min
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import dourmouse.config  # noqa: F401  (loads .env)


def get_unread_emails(limit: int = 10) -> list[dict]:
    """Fetch recent unread emails from Gmail API."""
    from dourmouse.mail import gmail_client

    try:
        client = gmail_client()
        if not client:
            return []
        # service.users().messages().list(userId='me', q='is:unread')
        # Simplified: just return recent unread snippet.
        # Full impl would paginate + fetch message bodies.
        result = client.users().messages().list(
            userId="me", q="is:unread", maxResults=limit
        ).execute()
        msgs = result.get("messages", [])
        details = []
        for msg in msgs:
            full = client.users().messages().get(
                userId="me", id=msg["id"]
            ).execute()
            headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            details.append({
                "id": msg["id"],
                "from": headers.get("From", "unknown"),
                "subject": headers.get("Subject", "(no subject)"),
                "snippet": full.get("snippet", "")[:200],
                "timestamp": datetime.datetime.now().isoformat(),
            })
        return details
    except Exception as e:
        print(f"inbox watcher: gmail error: {e}", file=sys.stderr)
        return []


def log_to_memory(emails: list[dict]) -> None:
    """Add unread emails as memory facts so they surface in chat context."""
    try:
        from dourmouse.memory_store import open_default_store

        store = open_default_store()
        if not store:
            return

        for email in emails:
            fact_body = (
                f"Unread email from {email['from']}\n"
                f"Subject: {email['subject']}\n"
                f"Snippet: {email['snippet']}"
            )
            store.add_fact(
                title=f"inbox: {email['subject'][:50]}",
                body=fact_body,
                source="inbox_watcher",
                tags=["email", "inbox", "unread"],
            )
    except Exception as e:
        print(f"inbox watcher: memory error: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="check interval in minutes")
    ap.add_argument("--once", action="store_true", help="run once and exit (cron mode)")
    args = ap.parse_args()

    print(f"[inbox watcher] starting (interval={args.interval}min)")

    if args.once:
        emails = get_unread_emails(limit=5)
        if emails:
            print(f"[inbox watcher] found {len(emails)} unread")
            log_to_memory(emails)
        return

    try:
        while True:
            emails = get_unread_emails(limit=5)
            if emails:
                print(f"[{datetime.datetime.now()}] {len(emails)} unread")
                log_to_memory(emails)
            time.sleep(args.interval * 60)
    except KeyboardInterrupt:
        print("[inbox watcher] stopped")


if __name__ == "__main__":
    main()
