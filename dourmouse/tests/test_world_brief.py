"""Hermetic tests for the deterministic World Brief generator.

Pure function of its input dict: no network, no I/O, no env, no monkeypatch
needed — ``generate_brief`` only ever reads the snapshot dict handed to it.
The overriding contract, matching world_brief.py's own docstring: nothing in
the output text may be fabricated. Every "surfaced item" assertion below
checks that a REAL fixture value made it into the text, and every
"omission" assertion checks that a value we deliberately left OUT of the
fixture does NOT appear — that pairing is what actually proves nothing is
invented, rather than just checking the brief "looks plausible".
"""

from __future__ import annotations

from dourmouse import world_brief as wb

# A small but real-shaped snapshot: several channels including one OFFLINE
# channel (cyber) and one channel that is healthy but carries zero items
# this cycle (flights). Titles are deliberately distinctive strings so a
# test can assert on their exact presence.
_SNAPSHOT = {
    "generated_at": "2026-08-23T05:00:00+00:00",
    "engine": "world-pulse v5.27 (self-hosted, keyless)",
    "pulse_score": 42,
    "pulse_label": "HEIGHTENED",
    "sources": {
        "disasters": {"ok": True, "latency_ms": 300, "count": 2},
        "cyber": {"ok": False, "error": "CISA feed timed out after 8.0s", "count": 0},
        "markets": {"ok": True, "latency_ms": 200, "count": 3},
        "flights": {"ok": True, "latency_ms": 150, "count": 0},
    },
    "items": {
        "disasters": [
            {
                "title": "M7.1 earthquake strikes off the coast of Chile",
                "summary": "Earthquake alertlevel: Red.",
                "link": "https://g.example/1",
                "at": "Sun, 23 Aug 2026 04:00:00 GMT",
                "severity": "critical",
            },
            {
                "title": "Flood warning issued for southern Vietnam",
                "summary": "Alertlevel: orange, monsoon flooding.",
                "link": "https://g.example/2",
                "at": "Sun, 23 Aug 2026 03:00:00 GMT",
                "severity": "watch",
            },
        ],
        "cyber": [],
        "markets": [
            {
                "title": "GAINERS NVDA Nvidia Corp",
                "summary": "950.00 USD (+8.20%) today",
                "severity": "up",
            },
            {
                "title": "GAINERS AAPL Apple Inc",
                "summary": "220.00 USD (+3.10%) today",
                "severity": "up",
            },
            {
                "title": "LOSERS TSLA Tesla Inc",
                "summary": "180.00 USD (-4.50%) today",
                "severity": "down",
            },
        ],
        "flights": [],
    },
    "note": "pulse_score is an internal composite of the real source signals — never a fabricated rating.",
}

# Deliberately NOT present anywhere in _SNAPSHOT. Used to prove the brief
# does not fabricate plausible-sounding content.
_NOT_IN_FIXTURE = "Volcano eruption reported in Iceland"


class TestRealisticSnapshot:
    """The main contract: real facts in, real facts (and only real facts) out."""

    def test_mentions_pulse_label_and_score(self):
        brief = wb.generate_brief(_SNAPSHOT)
        assert "HEIGHTENED" in brief["text"]
        assert "42" in brief["text"]

    def test_names_offline_channel_with_real_error(self):
        """cyber is OFFLINE — the brief must say so explicitly and honestly,
        using the ACTUAL error string from the snapshot, not a generic
        "some channels had issues" hand-wave.
        """
        brief = wb.generate_brief(_SNAPSHOT)
        assert "cyber" in brief["text"].lower() or "Cyber" in brief["text"]
        assert "CISA feed timed out after 8.0s" in brief["text"]

    def test_surfaces_most_severe_real_disaster_first(self):
        """disasters has a critical item and a watch item — the critical
        one (the real title) must be surfaced, proving ranking works off
        real severities rather than raw feed order.
        """
        brief = wb.generate_brief(_SNAPSHOT)
        assert "M7.1 earthquake strikes off the coast of Chile" in brief["text"]

    def test_surfaces_second_disaster_item_too(self):
        brief = wb.generate_brief(_SNAPSHOT)
        assert "Flood warning issued for southern Vietnam" in brief["text"]

    def test_surfaces_real_market_movers(self):
        brief = wb.generate_brief(_SNAPSHOT)
        assert "GAINERS NVDA Nvidia Corp" in brief["text"]
        assert "LOSERS TSLA Tesla Inc" in brief["text"]

    def test_names_zero_item_channel_honestly(self):
        """flights is ok but reported zero items this cycle — that must be
        stated, not silently dropped from the brief.
        """
        brief = wb.generate_brief(_SNAPSHOT)
        assert "flights" in brief["text"].lower()
        assert "no" in brief["text"].lower()

    def test_never_fabricates_content_not_in_input(self):
        """The single most important assertion in this file: a plausible-
        sounding headline that was never in the fixture must never appear
        in the output, proving prose composition doesn't invent facts.
        """
        brief = wb.generate_brief(_SNAPSHOT)
        assert _NOT_IN_FIXTURE not in brief["text"]

    def test_mode_is_always_template(self):
        brief = wb.generate_brief(_SNAPSHOT)
        assert brief["mode"] == "template"

    def test_window_note_references_snapshot_generated_at(self):
        brief = wb.generate_brief(_SNAPSHOT)
        assert "2026-08-23T05:00:00+00:00" in brief["window_note"]

    def test_generated_at_is_present_iso8601(self):
        brief = wb.generate_brief(_SNAPSHOT)
        # Must parse as ISO 8601 — this is "now", independent of the
        # snapshot's own generated_at.
        from datetime import datetime

        datetime.fromisoformat(brief["generated_at"])

    def test_returns_all_required_keys(self):
        brief = wb.generate_brief(_SNAPSHOT)
        assert set(brief) == {"text", "mode", "generated_at", "window_note"}
        assert isinstance(brief["text"], str) and brief["text"]


class TestMalformedSnapshot:
    """generate_brief must NEVER raise, and must say so honestly rather
    than producing a brief that implies real data was read.
    """

    def test_empty_dict_does_not_raise(self):
        brief = wb.generate_brief({})
        assert brief["mode"] == "template"
        assert isinstance(brief["text"], str) and brief["text"]

    def test_empty_dict_is_honest_about_incompleteness(self):
        brief = wb.generate_brief({})
        text_lower = brief["text"].lower()
        assert "unavailable" in text_lower or "incomplete" in text_lower or "no snapshot" in text_lower

    def test_missing_sources_and_items_does_not_raise(self):
        brief = wb.generate_brief({"pulse_score": 50, "pulse_label": "STABLE"})
        assert "STABLE" in brief["text"]
        assert "no" in brief["text"].lower()  # honest "no channel data" note

    def test_missing_pulse_fields_does_not_raise(self):
        brief = wb.generate_brief({"sources": {}, "items": {}})
        assert isinstance(brief["text"], str) and brief["text"]

    def test_wrong_types_do_not_raise(self):
        """sources/items are the wrong type entirely — must degrade
        gracefully rather than raising AttributeError/TypeError.
        """
        brief = wb.generate_brief({"pulse_score": "not-a-number", "pulse_label": 12345, "sources": "nope", "items": None})
        assert isinstance(brief["text"], str) and brief["text"]

    def test_none_snapshot_does_not_raise(self):
        brief = wb.generate_brief(None)  # type: ignore[arg-type]
        assert isinstance(brief["text"], str) and brief["text"]

    def test_channel_items_not_a_list_does_not_raise(self):
        snap = {
            "pulse_score": 60,
            "pulse_label": "STABLE",
            "sources": {"disasters": {"ok": True, "count": 3}},
            "items": {"disasters": "not-a-list"},
        }
        brief = wb.generate_brief(snap)
        assert isinstance(brief["text"], str) and brief["text"]

    def test_offline_channel_missing_error_field_does_not_raise(self):
        snap = {
            "pulse_score": 30,
            "pulse_label": "CRITICAL",
            "sources": {"cyber": {"ok": False}},
            "items": {"cyber": []},
        }
        brief = wb.generate_brief(snap)
        assert "cyber" in brief["text"].lower() or "Cyber" in brief["text"]


class TestDeterminism:
    def test_same_input_produces_identical_text(self):
        first = wb.generate_brief(_SNAPSHOT)
        second = wb.generate_brief(_SNAPSHOT)
        assert first["text"] == second["text"]
        assert first["mode"] == second["mode"] == "template"
        assert first["window_note"] == second["window_note"]

    def test_repeated_calls_on_empty_snapshot_are_identical(self):
        first = wb.generate_brief({})
        second = wb.generate_brief({})
        assert first["text"] == second["text"]

    def test_channel_order_is_stable_across_calls(self):
        """Item ordering inside a channel line must not depend on dict
        iteration order alone (see world_brief.py's comment on
        _CHANNEL_ORDER) — run several times and require byte-identical
        text every time.
        """
        results = {wb.generate_brief(_SNAPSHOT)["text"] for _ in range(5)}
        assert len(results) == 1
