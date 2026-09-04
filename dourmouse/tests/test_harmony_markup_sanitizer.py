"""dispatch.py — _strip_harmony_markup / _OllamaMessage sanitization.

Live-reproduced real bug: asked to check "the RAG knowledge base" for
Bulgaria, gpt-oss:20b -- after a genuinely slow ~17s real tool call
(query_desktop_vault) it got confused about whether it had received a
result for -- emitted its own internal Harmony-format multi-channel
transcript literally as the visible answer: raw <|channel|>/<|message|>/
<|start|>/<|end|> markers, its own analysis reasoning, and the raw tool-
call JSON arguments, all concatenated ahead of the actual final answer.
The tool itself and its wiring were proven correct (a direct call and an
earlier Claude-CLI-path run both used it correctly); this is a real
output-quality defect regardless of whose "fault" the underlying model
confusion is -- a user should never see raw internal-format tokens.
"""

from __future__ import annotations

from dourmouse.dispatch import _OllamaMessage, _strip_harmony_markup

# The exact real shape observed live (paraphrased, not the literal
# session capture, but the same channel sequence and marker set).
_REAL_LEAK = (
    "assistantBelow are the top results from the shared memory that relate "
    "to Bulgaria:<|channel|>analysis<|message|>We attempted shared memory "
    "but it's not configured. Need alternative: query desktop vault. That "
    "might work. Let's call query_desktop_vault.<|end|><|start|>assistant"
    '<|channel|>commentary to=functions.query_desktop_vault<|constrain|>json'
    '<|message|>{"query":"Bulgaria","limit":5}<|call|><|start|>functions.'
    "query_desktop_vault<|channel|>analysis<|message|>The assistant doesn't "
    "have ability to check? It will return actual results if configured. "
    "Let's see.<|end|><|start|>assistant<|channel|>final<|message|>I'm "
    "unable to access the shared memory or desktop vault right now — no "
    "memory source is configured on this machine."
)


class TestNoOpOnCleanText:
    def test_plain_text_is_returned_unchanged(self):
        text = "Bulgaria's capital is Sofia."
        assert _strip_harmony_markup(text) is text or _strip_harmony_markup(text) == text

    def test_real_content_with_pipes_and_angle_brackets_survives(self):
        """The marker pattern must be narrow enough to never eat a
        legitimate table pipe or a < comparison in code/prose."""
        text = "| a | b |\n|---|---|\n| 1 < 2 | x | y |"
        assert _strip_harmony_markup(text) == text

    def test_empty_string_is_a_no_op(self):
        assert _strip_harmony_markup("") == ""


class TestExtractsOnlyTheFinalChannel:
    def test_the_real_leaked_transcript_yields_only_the_real_answer(self):
        result = _strip_harmony_markup(_REAL_LEAK)
        assert result == (
            "I'm unable to access the shared memory or desktop vault right "
            "now — no memory source is configured on this machine."
        )

    def test_no_marker_tokens_survive_in_the_output(self):
        result = _strip_harmony_markup(_REAL_LEAK)
        assert "<|" not in result
        assert "channel" not in result.lower()

    def test_the_leaked_analysis_and_tool_call_json_are_discarded(self):
        """analysis/commentary channels are private deliberation and raw
        tool-call bookkeeping -- never meant to be user-visible, so they
        must be gone entirely, not merely marker-stripped-but-present."""
        result = _strip_harmony_markup(_REAL_LEAK)
        assert "query_desktop_vault" not in result
        assert '"query":"Bulgaria"' not in result
        assert "Need alternative" not in result

    def test_multiple_final_segments_keeps_the_last_one(self):
        """A model could in principle emit more than one final-channel
        segment across retries -- the last one is the one actually sent."""
        text = (
            "<|channel|>final<|message|>draft one<|end|>"
            "<|start|>assistant<|channel|>final<|message|>the real answer"
        )
        assert _strip_harmony_markup(text) == "the real answer"


class TestFallbackWhenNoFinalChannelPresent:
    def test_bare_markers_with_no_channel_structure_are_stripped_not_left_raw(self):
        text = "some text <|end|> more text <|start|> tail"
        result = _strip_harmony_markup(text)
        assert "<|" not in result
        assert "some text" in result and "tail" in result


class TestWiredIntoOllamaMessage:
    def test_message_content_is_sanitized_on_construction(self):
        msg = _OllamaMessage(_REAL_LEAK, None)
        assert "<|" not in msg.content
        assert msg.content == (
            "I'm unable to access the shared memory or desktop vault right "
            "now — no memory source is configured on this machine."
        )

    def test_clean_content_passes_through_untouched(self):
        msg = _OllamaMessage("Sofia is the capital of Bulgaria.", None)
        assert msg.content == "Sofia is the capital of Bulgaria."

    def test_falsy_content_is_not_run_through_the_sanitizer_at_all(self):
        """An empty string or None must not raise -- _OllamaMessage is
        constructed on every real turn, including ones with no text
        content at all (a pure tool-call turn)."""
        msg = _OllamaMessage("", None)
        assert msg.content == ""
