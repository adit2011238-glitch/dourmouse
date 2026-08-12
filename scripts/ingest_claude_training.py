#!/usr/bin/env python3
"""Ingest the labeled Claude export into dourmouse's MemoryStore so the app
can recall relevant conversations immediately (no training needed).

Usage:
  # Dry-run (show what would be ingested):
  python3 scripts/ingest_claude_training.py --dry-run

  # Ingest into the default store (DOURMOUSE_MEMORY_DB or <workspace>/memory/atlas_memory.db):
  python3 scripts/ingest_claude_training.py

  # Custom store location:
  python3 scripts/ingest_claude_training.py --db /path/to/atlas_memory.db

Pairs are stored as (source="claude_export", title="conversation_name") with
the full user+assistant body as the searchable text. Also tags each pair with
domain metadata so FTS5 searches match both the conversation and its domain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PAIRS = _PROJECT_ROOT / "training_data" / "instruction_pairs.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Claude training pairs into dourmouse MemoryStore")
    parser.add_argument("--pairs", default=str(_DEFAULT_PAIRS),
                        help=f"Path to instruction_pairs.jsonl (default: {_DEFAULT_PAIRS})")
    parser.add_argument("--db", default=None,
                        help="Path to atlas_memory.db (default: auto-detect from env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be ingested without touching the DB")
    args = parser.parse_args()

    pairs_path = Path(args.pairs)
    if not pairs_path.is_file():
        print(f"ERROR: {pairs_path} not found. Run scripts/label_claude_export.py first.", file=sys.stderr)
        return 1  # type: ignore[return-value]

    # Determine DB path.
    if args.db:
        db_path = Path(args.db)
    else:
        import os
        raw = os.environ.get("DOURMOUSE_MEMORY_DB")
        if raw:
            db_path = Path(raw)
        else:
            ws = os.environ.get("DOURMOUSE_WORKSPACE")
            if ws:
                db_path = Path(ws) / "memory" / "atlas_memory.db"
            else:
                db_path = _PROJECT_ROOT / "workspace" / "memory" / "atlas_memory.db"

    # Load pairs.
    pairs: list[dict] = []
    with pairs_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                pairs.append(json.loads(line))
    print(f"Loaded {len(pairs)} instruction pairs from {pairs_path}")

    if not pairs:
        print("No pairs to ingest.")
        return

    if args.dry_run:
        print(f"\nDRY RUN — would ingest {len(pairs)} pairs into {db_path}")
        sample = pairs[0]
        print(f"\nSample pair:")
        print(f"  conversation: {sample.get('conversation_name', '?')[:60]}")
        print(f"  domain:       {sample.get('domain', '?')}")
        print(f"  user (first 80): {sample.get('user', '')[:80]}")
        print(f"  assistant (first 80): {sample.get('assistant', '')[:80]}")
        return

    # Ingest.
    try:
        from dourmouse.memory_store import MemoryStore
        import sqlite3
    except ImportError as exc:
        print(f"ERROR: cannot import dourmouse modules: {exc}", file=sys.stderr)
        print("Run this script with the dourmouse .venv: .venv/bin/python scripts/ingest_claude_training.py")
        return 1  # type: ignore[return-value]

    # Open store. If FTS5 is unavailable, MemoryStore raises.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        store = MemoryStore(str(db_path))
    except RuntimeError as exc:
        print(f"ERROR: cannot open MemoryStore: {exc}", file=sys.stderr)
        print("Try: DOURMOUSE_MEMORY_DB=training_data/claude_memory.db")
        return 1  # type: ignore[return-value]

    counts = {"ingested": 0, "skipped": 0}
    for pair in pairs:
        uuid = pair.get("uuid", "?")
        conv_name = pair.get("conversation_name", "") or "Claude conversation"
        domain = pair.get("domain", "general")
        tags = pair.get("tags", []) or []
        user_text = (pair.get("user") or "").strip()
        assistant_text = (pair.get("assistant") or "").strip()

        if not user_text and not assistant_text:
            counts["skipped"] += 1
            continue

        title = f"[{domain}] {conv_name[:100]}"
        body = (
            f"DOMAIN: {domain}\n"
            f"TAGS: {', '.join(tags[:10])}\n\n"
            f"USER: {user_text}\n\n"
            f"ASSISTANT: {assistant_text}"
        )

        store.remember("claude_export", title, body)
        counts["ingested"] += 1

    print(f"\nIngested: {counts['ingested']} pairs into {db_path}")
    print(f"Skipped:  {counts['skipped']} pairs (empty)")
    print("dourmouse will now recall these conversations via FTS5 search.")


if __name__ == "__main__":
    main()