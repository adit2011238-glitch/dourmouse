"""dourmouse/dispatch.py's _HarmonyDeltaFilter -- live-streaming counterpart
to _strip_harmony_markup (v13.8, real live-reproduced gap).

_strip_harmony_markup (see test_harmony_markup_sanitizer.py) only ever
cleans the FINAL, fully-assembled message. Live-reproduced during the
Google Workspace email-send test: the console UI streams assistant_delta
chunks straight to screen with no later "clean re-render" -- what streams
in is permanently what stays visible. A real turn ("...Next Step...
<|channel|>final<|message|>Email Sent...") showed the raw
"<|channel|>final<|message|>" marker literally on screen even though the
underlying gmail_send call and the FINAL persisted message (had it gone
through _OllamaMessage/.content) would have been clean.

_HarmonyDeltaFilter wraps on_delta so only "final"-channel body text (or,
for a backend that never emits Harmony markup, everything) reaches the
real callback, decided incrementally as each chunk arrives -- not just
once the whole message is done.
"""

from __future__ import annotations

from dourmouse.dispatch import _HarmonyDeltaFilter


def _run(chunks: list[str]) -> str:
    out: list[str] = []
    f = _HarmonyDeltaFilter(out.append)
    for c in chunks:
        f.feed(c)
    f.finish()
    return "".join(out)


class TestOrdinaryTextIsUnaffected:
    def test_plain_text_single_chunk_passes_through_unchanged(self):
        assert _run(["Bulgaria's capital is Sofia."]) == "Bulgaria's capital is Sofia."

    def test_plain_text_many_small_chunks_reassembles_exactly(self):
        text = "The quick brown fox jumps over the lazy dog."
        chunks = list(text)  # one character at a time -- the worst case
        assert _run(chunks) == text

    def test_real_content_with_pipes_and_angle_brackets_survives(self):
        text = "| a | b |\n|---|---|\n| 1 < 2 | x | y |"
        assert _run([text]) == text

    def test_a_lone_trailing_less_than_is_flushed_by_finish(self):
        # "a < b" split so the chunk boundary lands right after "<".
        assert _run(["a <", " b"]) == "a < b"

    def test_empty_feed_is_a_no_op(self):
        assert _run(["", "hello", ""]) == "hello"


class TestChannelMarkersNeverReachOnDelta:
    def test_final_channel_streams_live_analysis_is_dropped(self):
        chunks = [
            "<|channel|>analysis<|message|>",
            "private reasoning nobody should see",
            "<|end|><|start|>assistant<|channel|>final<|message|>",
            "Email Sent",
        ]
        assert _run(chunks) == "Email Sent"

    def test_marker_split_across_chunk_boundary_mid_token(self):
        # The exact shape live-reproduced: a header split right in the
        # middle of "<|channel|>".
        chunks = ["Next Step. <|chan", "nel|>final<|mess", "age|>Email Sent"]
        assert _run(chunks) == "Next Step. Email Sent"

    def test_commentary_channel_raw_tool_json_never_streams(self):
        chunks = [
            '<|channel|>commentary to=functions.gmail_send<|constrain|>json',
            '<|message|>{"to":"x@example.com"}<|call|>',
            "<|start|>assistant<|channel|>final<|message|>Sent.",
        ]
        assert _run(chunks) == "Sent."

    def test_text_before_the_first_marker_streams_immediately(self):
        chunks = ["Confirm, and I'll run gmail_send. ", "<|channel|>final<|message|>Email Sent"]
        assert _run(chunks) == "Confirm, and I'll run gmail_send. Email Sent"

    def test_multiple_final_segments_all_stream(self):
        chunks = [
            "<|channel|>final<|message|>Part one. ",
            "<|end|><|start|>assistant<|channel|>final<|message|>Part two.",
        ]
        assert _run(chunks) == "Part one. Part two."


class TestRealLiveReproducedLeak:
    """The exact live-observed shape from the console (v13.8 email test):
    ordinary narration, then a raw channel marker, then the real clean
    final text -- must come out with the marker gone and nothing dropped
    that was meant to be seen."""

    def test_exact_observed_shape(self):
        chunks = [
            "Confirm, and I'll run ", "gmail_send", ".The ", "gmail_send",
            " tool responded that sending the email requires explicit "
            "confirmation.\n\nNext Step\n\nPlease confirm you would like "
            "me to send this email. Once you do, I'll proceed.",
            "<|channel|>final<|message|>Email Sent\n\nThe test email has "
            "been sent to valerygordon200@gmail.com with the subject "
            '"Dourmouse Live Test 2." You should receive it shortly.',
        ]
        result = _run(chunks)
        assert "<|" not in result
        assert "channel" not in result or "final" not in result.split("channel", 1)[0]
        assert result.startswith("Confirm, and I'll run gmail_send.")
        assert result.endswith("You should receive it shortly.")
