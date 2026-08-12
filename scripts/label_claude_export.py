#!/usr/bin/env python3
"""Parse the Claude export, classify conversations by domain, produce a
labeled dataset for training dourmouse + a summary report.

Outputs:
  training_data/labeled_conversations.jsonl — one JSON object per conversation
    with uuid, name, summary, domain, tags, turn_count, created_at, date
  training_data/instruction_pairs.jsonl    — (system, user, assistant) pairs
  training_data/summary.json               — domain counts, total turns, etc.
  training_data/domains/{domain}.txt       — concatenated for per-domain ingest

Usage: python3 scripts/label_claude_export.py [--export PATH] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Domain classifiers (keyword heuristics, deterministic) ────────────── #

_DOMAIN_HEURISTICS: list[tuple[str, list[str], list[str]]] = [
    ("finance", [
        "market", "stock", "trade", "portfolio", "sharpe", "backtest",
        "eurusd", "gbpusd", "forex", "fx", "equity", "bond", "etf",
        "dividend", "volatility", "risk", "return", "asset", "option",
        "future", "commodity", "p&l", "gain", "loss", "hedge",
        "mean reversion", "momentum", "trend follow", "algorithmic trading",
    ], [
        "financial", "rate", "price", "chart", "candlestick",
        "indicator", "moving average", "rsi", "macd", "bollinger",
    ]),
    ("coding", [
        "python", "function", "class", "import", "def ", "return ",
        "git ", "github", "docker", "api", "endpoint", "request",
        "response", "json", "async", "await", "threading", "lambda",
        "pytest", "unittest", "debug", "compile", "syntax",
        "refactor", "type hint", "constructor", "decorator",
    ], [
        "code", "program", "script", "app", "software", "web",
        "database", "sql", "query", "server", "client", "config",
    ]),
    ("atlas", [
        "atlas", "strategy lab", "valerygordon", "backtest engine",
        "factor model", "cross-sectional", "universe", "security master",
        "phase", "trial registry", "deflated sharpe", "feature sort",
    ], [
        "quant", "algorithm", "signal", "screener", "screen",
    ]),
    ("research", [
        "research", "paper", "study", "analysis", "investigate",
        "literature", "survey", "review", "findings", "conclusion",
        "hypothesis", "methodology", "experiment", "result",
    ], [
        "reference", "source", "cite", "citation", "academic",
    ]),
    ("planning", [
        "plan", "roadmap", "timeline", "schedule", "gantt",
        "slide", "presentation", "ppt", "flowchart", "diagram",
        "project", "milestone", "phase", "sprint", "task",
    ], [
        "organize", "structure", "outline", "agenda", "overview",
    ]),
    ("writing", [
        "write", "draft", "essay", "article", "blog", "post",
        "content", "copy", "edit", "rewrite", "proofread",
        "grammar", "style", "tone", "narrative", "story",
    ], [
        "document", "report", "summary", "describe", "explain",
    ]),
    ("general", [], []),
]

# Everything NOT matched to another domain gets "general".


def classify_conversation(conv: dict[str, Any]) -> tuple[str, list[str]]:
    """Classify a single Claude conversation by domain. Returns (domain, tags).

    Works on: name, summary, and the text of every (user + assistant) message.
    """
    name = conv.get("name", "") or ""
    summary = conv.get("summary", "") or ""
    messages = conv.get("chat_messages", []) or []

    # Build the full text body for analysis.
    body_parts = [name, summary]
    for msg in messages:
        text = msg.get("text", "") or ""
        body_parts.append(text)
        content = msg.get("content", []) or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                body_parts.append(block.get("text", ""))

    full_text = " ".join(body_parts).lower()

    tags: list[str] = []
    primary_domain = "general"

    for domain, primary_kw, secondary_kw in _DOMAIN_HEURISTICS:
        if domain == "general":
            continue
        for kw in primary_kw:
            if kw in full_text:
                if primary_domain == "general":
                    primary_domain = domain
                break
        for kw in secondary_kw:
            if kw in full_text:
                tags.append(kw)
                if primary_domain == "general":
                    primary_domain = domain

    tags = list(set(tags))

    # Extract additional domain-specific tags.
    for msg in messages:
        sender = msg.get("sender", "")
        text = (msg.get("text") or "").lower()
        if sender == "human":
            if text.startswith("write ") or text.startswith("create "):
                tags.append("request_create")
            if text.startswith("/"):
                tags.append("command_like")
            if "?" in text:
                tags.append("question")
            if len(text) < 80 and not text.endswith("?"):
                tags.append("brief_request")

    return primary_domain, tags


def extract_instruction_pairs(conv: dict[str, Any]) -> list[dict[str, str]]:
    """Pair (user msg → assistant answer) from a conversation.

    The assistant's first reply after each user message is the pair's output.
    Consecutive assistant messages without an intervening user message are
    collapsed into one (the assistant talking to itself / tool calls).
    """
    messages = conv.get("chat_messages", []) or []
    pairs: list[dict[str, str]] = []
    current_input: str | None = None
    current_output: str | None = None

    for msg in messages:
        sender = msg.get("sender", "")
        text = msg.get("text", "") or ""
        content = msg.get("content", []) or []
        # Prefer content blocks for full text.
        full_text = text
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if t:
                    full_text = t
        if not full_text.strip():
            continue

        if sender == "human":
            # If we were accumulating an assistant reply, flush it.
            if current_input is not None and current_output:
                pairs.append({
                    "uuid": conv.get("uuid", ""),
                    "conversation_name": conv.get("name", ""),
                    "system_summary": conv.get("summary", ""),
                    "user": current_input.strip()[:2000],
                    "assistant": current_output.strip()[:4000],
                })
            current_input = full_text.strip()[:2000]
            current_output = None
        elif sender == "assistant":
            if current_input is not None:
                current_output = (current_output or "") + "\n" + full_text.strip()
                current_output = current_output.strip()[:4000]

    # Flush the last pair.
    if current_input is not None and current_output:
        pairs.append({
            "uuid": conv.get("uuid", ""),
            "conversation_name": conv.get("name", ""),
            "system_summary": conv.get("summary", ""),
            "user": current_input.strip()[:2000],
            "assistant": current_output.strip()[:4000],
        })

    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description="Label a Claude export for dourmouse training")
    parser.add_argument("--export", default="/tmp/claude_export/conversations.json",
                        help="Path to conversations.json (default: /tmp/claude_export/conversations.json)")
    parser.add_argument("--out", default="training_data",
                        help="Output directory (default: training_data)")
    args = parser.parse_args()

    export_path = Path(args.export)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    domains_dir = out_dir / "domains"
    domains_dir.mkdir(parents=True, exist_ok=True)

    # Load.
    data = json.loads(export_path.read_text(encoding="utf-8"))
    conversations = data if isinstance(data, list) else data.get("conversations", [])
    print(f"Loaded {len(conversations)} conversations from {export_path}")

    # Process.
    labeled_path = out_dir / "labeled_conversations.jsonl"
    pairs_path = out_dir / "instruction_pairs.jsonl"
    domain_files: dict[str, list[str]] = {}
    stats: dict[str, Any] = {
        "total_conversations": len(conversations),
        "total_turns": 0,
        "total_pairs": 0,
        "domains": {},
        "date_range": [None, None],
    }

    conv_written = 0
    for conv in conversations:
        domain, tags = classify_conversation(conv)
        n_messages = len(conv.get("chat_messages") or [])
        created = conv.get("created_at", "")
        stats["total_turns"] += n_messages

        record = {
            "uuid": conv.get("uuid", ""),
            "name": conv.get("name", ""),
            "summary": (conv.get("summary") or "")[:300],
            "domain": domain,
            "tags": tags,
            "turn_count": n_messages,
            "created_at": created,
            "date": created[:10] if created else "",
        }
        with labeled_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        conv_written += 1

        # Per-domain collection.
        domain_files.setdefault(domain, []).append(
            f"=== {conv.get('name', '?')} ({conv.get('uuid', '?')[:8]}) ===\n"
            f"Summary: {(conv.get('summary') or '')[:300]}\n"
            f"Messages: {n_messages}\n"
        )

        # Domain stats.
        if domain not in stats["domains"]:
            stats["domains"][domain] = {"count": 0, "turns": 0}
        stats["domains"][domain]["count"] += 1
        stats["domains"][domain]["turns"] += n_messages

        # Date range.
        if created:
            d = created[:10]
            if stats["date_range"][0] is None or d < stats["date_range"][0]:
                stats["date_range"][0] = d
            if stats["date_range"][1] is None or d > stats["date_range"][1]:
                stats["date_range"][1] = d

        # Extract instruction pairs.
        pairs = extract_instruction_pairs(conv)
        stats["total_pairs"] += len(pairs)
        for pair in pairs:
            pair["domain"] = domain
            pair["tags"] = tags
            with pairs_path.open("a") as f:
                f.write(json.dumps(pair) + "\n")

    # Write per-domain summary files.
    for domain, lines in domain_files.items():
        df = domains_dir / f"{domain}.txt"
        with df.open("a") as f:
            f.write(f"{domain.upper()} — {len(lines)} conversations\n")
            f.write(f"{'=' * 50}\n\n")
            for l in lines:
                f.write(l + "\n")

    stats["conversations_written"] = conv_written
    stats["total_conversations"] = len(conversations)

    # Write summary.
    (out_dir / "summary.json").write_text(json.dumps(stats, indent=2) + "\n")

    print(f"  Labeled conversations: {labeled_path}")
    print(f"  Instruction pairs:     {pairs_path}")
    print(f"  Summary:               {out_dir / 'summary.json'}")
    print(f"  Domain breakdown:")
    for d, s in sorted(stats["domains"].items(), key=lambda x: -x[1]["count"]):
        print(f"    {d:>12s}: {s['count']:2d} conversations, {s['turns']:3d} turns")
    if stats.get("date_range") and stats["date_range"][0]:
        print(f"  Date range: {stats['date_range'][0]} → {stats['date_range'][1]}")
    print(f"  Total instruction pairs: {stats['total_pairs']:,}")


if __name__ == "__main__":
    main()