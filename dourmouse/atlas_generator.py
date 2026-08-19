"""ATLAS idea generator — the autonomous half of the v8.16 proposal system
(Phase 2, 2026-08-18).

Reads past proposals/runs (dourmouse.atlas_proposals) and periodically
writes ONE new idea — either genuinely novel or an explicit improvement on
a specific past strategy — through the SAME propose_from_idea entry point
chat ideas use, tagged source="generator". This module never approves or
runs anything: it only ever adds to the pending-review queue. Approval
stays a human decision either way (see atlas_proposals.py's module
docstring for why that gate is load-bearing, not decorative, for both
sources).

Backpressure (deliberate, not incidental): if there are already
_MAX_PENDING_GENERATED pending proposals from this generator, a cycle
skips rather than proposing more. The point of a review queue is that a
human actually reviews it — flooding it faster than that can happen
defeats the feature, it doesn't demonstrate productivity.

Same daemon-thread-with-sleep-interval shape as atlas_lab.py's own
_auto_sync_loop/start_auto_sync (deliberately mirrored, not reinvented):
silent on success, never dies on a single bad cycle, idempotent start.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from dourmouse import atlas_proposals as ap

_GENERATOR_INTERVAL_SECONDS = float(os.environ.get("ATLAS_GENERATOR_INTERVAL", "1800"))
_MAX_PENDING_GENERATED = int(os.environ.get("ATLAS_GENERATOR_MAX_PENDING", "5"))
_HISTORY_DEPTH = 12  # most-recent proposals considered for context, cap keeps the prompt bounded

_generator_started = False
_lock = threading.Lock()

_GENERATOR_SYSTEM_PROMPT = """You are a quantitative strategy researcher reviewing your own past work. You will be shown a summary of past trading-strategy proposals and, where available, their real backtest results.

Propose exactly ONE new trading idea. Either:
(a) a genuinely new angle not resembling anything already tried, or
(b) an explicit, named improvement on ONE specific past strategy — say which one and exactly what you are changing and why (a different lookback, a different asset, tightening or loosening a threshold, adding a filter condition, etc).

Rules:
- Output ONLY the idea itself: 1-3 plain-English sentences, written exactly like a person describing a trading idea to a colleague. No JSON, no code, no headers, no "Idea:" prefix.
- Do not claim it will work, or invent implied performance — you have not tested it yet, this is a hypothesis to be tested, not a result.
- Do not propose something already rejected or already tested with a similar-sounding result unless (b) applies and the variation is real and stated.
- If there is no history yet, propose one reasonable, simple, testable starting idea for a major FX pair.
"""


def _summarize_history() -> str:
    """Bounded plain-text summary of recent proposals + their runs, for the
    generator's own context — deliberately NOT the full code (that would
    bloat the prompt for no benefit; the generator reasons about IDEAS and
    RESULTS, not implementation detail)."""
    proposals = ap.list_proposals()[:_HISTORY_DEPTH]
    if not proposals:
        return "(no past proposals yet — this is the first idea)"

    lines = []
    for p in proposals:
        runs = ap.list_runs(proposal_id=p["id"])
        line = f"- [{p.get('source', '?')}] {p.get('strategy_name', '?')!r}: {p.get('status', '?')}"
        if p.get("status") in ("rejected", "rejected_unsafe") and p.get("reviewer_note"):
            line += f" (reviewer said: {p['reviewer_note']})"
        for r in runs:
            if r.get("status") == "done":
                line += f" -> {r.get('verdict', '?')}"
                if r.get("explanation"):
                    line += f": {r['explanation'][:200]}"
            elif r.get("status") == "failed":
                line += f" -> run failed: {(r.get('error') or '')[:150]}"
        lines.append(line)
    return "\n".join(lines)


def generate_one_idea() -> str:
    """One LLM call: past history in, one new idea prompt out. Raises
    RuntimeError on failure — same honest-failure convention as everything
    else in this feature, no fallback idea fabricated."""
    context = _summarize_history()
    idea = ap._llm_chat(
        f"Past proposals and results:\n{context}\n\nPropose your next idea.",
        system=_GENERATOR_SYSTEM_PROMPT,
    )
    idea = idea.strip()
    if not idea:
        raise RuntimeError("generator LLM returned an empty idea")
    return idea


def _pending_generated_count() -> int:
    return sum(
        1 for p in ap.list_proposals(status="pending") if p.get("source") == "generator"
    )


def generate_and_propose() -> dict[str, Any] | None:
    """One full cycle: skip if the queue already has enough of this
    source's unreviewed work, else generate + propose (same gated pipeline
    chat ideas use). Returns the new proposal dict, or None if skipped."""
    pending = _pending_generated_count()
    if pending >= _MAX_PENDING_GENERATED:
        return None
    idea = generate_one_idea()
    return ap.propose_from_idea(idea, source="generator")


def _generator_loop() -> None:
    while True:
        time.sleep(_GENERATOR_INTERVAL_SECONDS)
        try:
            generate_and_propose()
        except Exception:  # noqa: BLE001 -- a background cycle never kills the app
            pass


def start_idea_generator() -> None:
    """Start the background idea generator once (idempotent)."""
    global _generator_started
    with _lock:
        if _generator_started:
            return
        _generator_started = True
    threading.Thread(target=_generator_loop, daemon=True, name="atlas-idea-generator").start()
