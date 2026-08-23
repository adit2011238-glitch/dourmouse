"""World Brief — deterministic "what happened while I was away" summaries.

Takes a ``world_pulse_snapshot()``-shaped dict (see ``dourmouse/world_pulse.py``)
and composes it into a short, human-readable morning brief: a few plain-text
paragraphs a person can read in ten seconds instead of scrolling a wall of
raw feed items.

Why template-based, not LLM-based, right now
----------------------------------------------
This codebase's LLM-dispatch path (``dourmouse/dispatch.py``) is a complex
multi-agent orchestration loop, not a simple one-shot "summarize this text"
completion helper. Bolting a shortcut call into it here would be guesswork —
either subtly wrong (wrong context, wrong tool budget, wrong agent identity)
or fragile against that module's own evolution. A correct, honest,
deterministic summary is the real deliverable for this pass: it composes
REAL facts already present in the snapshot (the pulse score/label, item
counts, titles, severities, source health) into readable prose using fixed
templates and ranking rules — it never asks a model to invent phrasing, and
therefore it can never fabricate a headline, a number, or an event that
isn't actually in the input (Rule 2.2 — the single most important
constraint on this module: when in doubt, say less rather than invent
something plausible-sounding).

Determinism follows directly from that design: the same snapshot dict always
produces the exact same text, because every sentence is built by walking
the input in a fixed order (a stable channel order, then each channel's own
severity-then-position ranking) and formatting real fields — no randomness,
no LLM sampling, no wall-clock-dependent phrasing beyond the timestamps that
are themselves already present in the input.

What a future "llm" mode would need
-------------------------------------
``generate_brief()`` already returns a ``mode`` field so a future elaborated
mode can be added without a breaking API change — today it is always the
literal string ``"template"``. A future ``"llm"`` mode would need: (1) a
supported one-shot completion entry point into ``dispatch.py`` (or a
narrower helper built specifically for short summarization calls, not the
full agent-orchestration loop), (2) a decision on how to keep the same
never-fabricate guarantee when the model is free to choose its own words —
e.g. constraining it to rephrase-only over the same fact list this module
already extracts, rather than free-form generation over the raw snapshot,
and (3) a fallback to this deterministic path when the LLM call fails or
times out, so a brief is still always produced. None of that is needed for
this module to be correct today.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

#: Channels in the fixed order the brief walks them. Order is part of
#: determinism: two runs over the same snapshot must produce identical text,
#: so channel order can never depend on dict iteration order alone (which is
#: insertion order in Python, but the snapshot is assembled by a thread pool
#: in world_pulse.py — see world_pulse_snapshot — so insertion order is not
#: guaranteed to be stable run-to-run even though CPython dicts preserve it).
_CHANNEL_ORDER = (
    "disasters",
    "conflict",
    "cyber",
    "quakes",
    "markets",
    "news",
    "macro",
    "flights",
)

#: Human labels for prose, matching world_pulse.py's own channel labels.
_CHANNEL_LABEL = {
    "disasters": "Disasters",
    "conflict": "Conflict/humanitarian",
    "cyber": "Cyber",
    "quakes": "Earthquakes",
    "markets": "Markets",
    "news": "News",
    "macro": "Macro",
    "flights": "Flights",
}

#: Severity rank for picking the most-severe real items within a channel.
#: Lower number = surfaced first. Severities not listed here (e.g.
#: "quote", "flat", "humanitarian") fall through to the default rank and
#: are effectively ordered by their original position in the item list.
_SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "watch": 2,
    "advisory": 3,
    "warn": 4,
    "down": 4,
    "up": 4,
    "info": 5,
}
_DEFAULT_SEVERITY_RANK = 6

#: How many of a channel's real items get named in the brief.
_MAX_NAMED_ITEMS = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rank_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable-sort real items most-severe-first, ties keep original order.

    ``sorted`` is stable, so items with equal (or unranked) severity retain
    the order the source already put them in — for most channels that's
    already newest/strongest first (see world_pulse.py's per-source
    fetchers), so no fabricated recency claim is being made here.
    """
    return sorted(
        items,
        key=lambda it: _SEVERITY_RANK.get((it or {}).get("severity"), _DEFAULT_SEVERITY_RANK),
    )


def _clean_title(item: dict[str, Any]) -> str:
    title = (item or {}).get("title")
    if not isinstance(title, str):
        return ""
    return title.strip()


def _join_titles(titles: list[str]) -> str:
    titles = [t for t in titles if t]
    if not titles:
        return ""
    if len(titles) == 1:
        return titles[0]
    if len(titles) == 2:
        return f"{titles[0]} and {titles[1]}"
    return ", ".join(titles[:-1]) + f", and {titles[-1]}"


def _channel_sentence(chan: str, items: list[dict[str, Any]], count: int) -> str:
    """One short sentence naming the most severe/newest real items.

    ``count`` is the source's own reported item count (``sources[chan]
    ["count"]``), used as the headline number instead of ``len(items)`` so
    the sentence stays honest even if ``items`` was truncated somewhere
    upstream of this module — the count in the sentence always traces back
    to a real field in the snapshot, never to a re-derived value that could
    drift from it.
    """
    label = _CHANNEL_LABEL.get(chan, chan.capitalize())
    ranked = _rank_items(items)
    top_titles = [_clean_title(it) for it in ranked[:_MAX_NAMED_ITEMS]]
    top_titles = [t for t in top_titles if t]
    noun = "item" if count == 1 else "items"

    if not top_titles:
        # Count says there's data but no usable titles came through — say
        # exactly that rather than inventing a description.
        return f"{label}: {count} {noun} reported, but no usable headlines were present."

    lead_sev = (ranked[0] or {}).get("severity") or ""
    if lead_sev in ("critical", "high"):
        sev_word = "critical-severity" if lead_sev == "critical" else "high-severity"
        return (
            f"{label}: {count} {noun}, including {sev_word} reports — "
            f"{_join_titles(top_titles)}."
        )
    return f"{label}: {count} {noun}, led by {_join_titles(top_titles)}."


def _offline_sentence(chan: str, source: dict[str, Any]) -> str:
    label = _CHANNEL_LABEL.get(chan, chan.capitalize())
    err = source.get("error")
    err_text = str(err).strip()[:140] if err else "no error detail reported"
    return f"{label} feed was unreachable ({err_text})."


def _empty_sentence(chan: str) -> str:
    label = _CHANNEL_LABEL.get(chan, chan.capitalize())
    return f"No {label.lower()} items this cycle."


def _pulse_read(score: Any, label: Any) -> str:
    """One-line overall read. Only uses the real score/label — no invented
    editorializing beyond the fixed wording tied to each label bucket
    (matching world_pulse.py's own label thresholds), plus the exact score.
    """
    if not isinstance(label, str) or not label:
        return "Pulse status is unavailable this cycle."
    reads = {
        "STABLE": "Conditions read stable overall.",
        "ELEVATED": "Conditions are somewhat elevated — worth a skim.",
        "HEIGHTENED": "Conditions are heightened — a few things need attention.",
        "CRITICAL": "Conditions are critical — this cycle needs a closer look.",
    }
    read = reads.get(label, f"Pulse label is {label}.")
    if isinstance(score, int):
        return f"Pulse is {label} at {score}. {read}"
    return f"Pulse is {label}. {read}"


def generate_brief(snapshot: dict) -> dict:
    """Turn a world_pulse_snapshot()-shaped dict into a short written brief.

    Never raises. A malformed or empty snapshot (missing keys, wrong types)
    produces a short, honest brief saying the data was incomplete rather
    than crashing or fabricating a narrative — every code path below
    degrades gracefully instead of trusting the input's shape.

    See the module docstring for why this is deterministic and
    template-based, and what a future "llm" mode would need.
    """
    generated_at = _now_iso()

    if not isinstance(snapshot, dict) or not snapshot:
        text = (
            "Overnight brief unavailable: no snapshot data was provided. "
            "This is an honest gap, not a fabricated summary — check that "
            "the world monitor is running and has produced a snapshot."
        )
        return {
            "text": text,
            "mode": "template",
            "generated_at": generated_at,
            "window_note": "reflects the current cached snapshot as of unknown",
        }

    snap_at = snapshot.get("generated_at")
    snap_at_display = snap_at if isinstance(snap_at, str) and snap_at else "unknown"
    window_note = f"reflects the current cached snapshot as of {snap_at_display}"

    score = snapshot.get("pulse_score")
    label = snapshot.get("pulse_label")
    sources = snapshot.get("sources")
    items = snapshot.get("items")
    if not isinstance(sources, dict):
        sources = {}
    if not isinstance(items, dict):
        items = {}

    paragraphs: list[str] = []

    # --- Opening: pulse score/label + one-line overall read ---------------
    paragraphs.append(_pulse_read(score, label))

    if not sources and not items:
        # Nothing at all to report per-channel — say so honestly instead of
        # silently producing a one-line brief that looks complete.
        paragraphs.append(
            "No channel data was present in this snapshot, so no per-source "
            "detail can be reported for this cycle."
        )
        text = " ".join(paragraphs)
        return {
            "text": text,
            "mode": "template",
            "generated_at": generated_at,
            "window_note": window_note,
        }

    # --- Per-channel lines --------------------------------------------------
    channel_lines: list[str] = []
    empty_but_ok: list[str] = []

    # Walk the fixed order first, then any channel present in the snapshot
    # that isn't in the known order (forward-compatible with a future
    # channel this module doesn't yet know the label for), in sorted order
    # for determinism.
    known = [c for c in _CHANNEL_ORDER if c in sources or c in items]
    extra = sorted((set(sources) | set(items)) - set(_CHANNEL_ORDER))
    all_chans = known + [c for c in extra if c not in known]

    for chan in all_chans:
        src = sources.get(chan)
        chan_items = items.get(chan)
        if not isinstance(chan_items, list):
            chan_items = []
        real_items = [it for it in chan_items if isinstance(it, dict)]

        if not isinstance(src, dict):
            # No health record for this channel at all — nothing honest to
            # say beyond acknowledging it's unaccounted for.
            if real_items:
                count = len(real_items)
                channel_lines.append(_channel_sentence(chan, real_items, count))
            continue

        if src.get("ok") is False:
            channel_lines.append(_offline_sentence(chan, src))
            continue

        count = src.get("count")
        if not isinstance(count, int):
            count = len(real_items)

        if count > 0 and real_items:
            channel_lines.append(_channel_sentence(chan, real_items, count))
        else:
            # ok but zero items — honest note, not silent omission.
            empty_but_ok.append(_empty_sentence(chan))

    if channel_lines:
        paragraphs.append(" ".join(channel_lines))

    # --- Closing: zero-item-but-ok channels --------------------------------
    if empty_but_ok:
        paragraphs.append(" ".join(empty_but_ok))

    text = "\n\n".join(paragraphs)
    return {
        "text": text,
        "mode": "template",
        "generated_at": generated_at,
        "window_note": window_note,
    }
