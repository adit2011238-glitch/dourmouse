"""UI-2 (finding #079): the type and spacing scales.

docs/UI_SOURCE_MAP.md section 2 recorded the real problem: colour is fully
tokenized ("every colour already routes through var()"), but SEVENTEEN
distinct font-size values ran from 7.5px to 22px in half-pixel increments
across console.html alone with no scale governing any of them, and nothing
governed section/panel spacing against ~720 raw px literals in the same style
block. docs/DESIGN_SYSTEM.md named this gap number 1 and number 2 and said to
close it BEFORE building new panels, so new components do not reintroduce the
same problem.

These read the REAL stylesheet on disk rather than a fixture, because the
thing under test is the shipped token set, not a copy of it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent.parent / "ui" / "assets" / "dourmouse-ui.css"

TEXT_STEPS = ["2xs", "xs", "sm", "base", "md", "lg", "xl", "2xl"]
SPACE_STEPS = [1, 2, 3, 4, 5, 6]


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


def _token(css_text: str, name: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(name)}:\s*([^;]+);", css_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _px(value: str) -> float:
    return float(value.removesuffix("px"))


class TestTypeScaleExists:
    @pytest.mark.parametrize("step", TEXT_STEPS)
    def test_every_step_is_defined(self, css, step):
        assert _token(css, f"--dm-text-{step}") is not None, step

    @pytest.mark.parametrize("step", TEXT_STEPS)
    def test_every_step_is_a_whole_pixel(self, css, step):
        # The half-pixel increments are the specific mess this replaces.
        value = _token(css, f"--dm-text-{step}")
        assert value.endswith("px"), value
        assert _px(value).is_integer(), f"{step} is {value}"

    def test_the_scale_ascends(self, css):
        sizes = [_px(_token(css, f"--dm-text-{s}")) for s in TEXT_STEPS]
        assert sizes == sorted(sizes)
        assert len(set(sizes)) == len(sizes), "a duplicated step is not a step"

    def test_it_spans_the_real_range_that_was_in_use(self, css):
        # The seventeen ad hoc values ran 7.5px to 22px. A scale that does not
        # reach the extremes cannot replace them.
        sizes = [_px(_token(css, f"--dm-text-{s}")) for s in TEXT_STEPS]
        assert min(sizes) <= 9
        assert max(sizes) >= 22

    def test_small_steps_stay_dense(self, css):
        # A conventional 1.25 major-third scale is wrong for this product: it
        # would collapse the five distinct metadata/label/body weights this
        # dense terminal UI genuinely distinguishes. Pin the intent so a
        # future "tidy up the scale" cannot silently undo it.
        small = [_px(_token(css, f"--dm-text-{s}")) for s in ["2xs", "xs", "sm", "base", "md"]]
        for a, b in zip(small, small[1:], strict=False):
            assert 1.0 < b / a <= 1.18, f"{a} -> {b} is too coarse for a dense UI"


class TestSpacingScaleExists:
    @pytest.mark.parametrize("step", SPACE_STEPS)
    def test_every_step_is_defined(self, css, step):
        assert _token(css, f"--dm-space-{step}") is not None, step

    def test_it_is_the_scale_the_design_doc_already_specified(self, css):
        # docs/DESIGN_SYSTEM.md gap 2 names 4/8/12/16/24/32 explicitly. This
        # implements that decision rather than inventing a second one.
        actual = [_px(_token(css, f"--dm-space-{s}")) for s in SPACE_STEPS]
        assert actual == [4, 8, 12, 16, 24, 32]

    def test_the_dense_row_tokens_still_exist_separately(self, css):
        # --dm-row-y and --dm-gap have precise, different jobs (padding inside
        # a dense row; the gap between adjacent controls). Folding them into a
        # generic spacing scale would lose that meaning, so this asserts the
        # new scale was added ALONGSIDE them, never as a replacement.
        for name in ("--dm-row-y", "--dm-row-y-lg", "--dm-gap", "--dm-gap-lg"):
            assert _token(css, name) is not None, name


class TestTokensAreDocumented:
    def test_the_design_doc_and_the_stylesheet_agree_on_the_spacing_values(self):
        # Two places that can drift is the exact bug class this codebase has
        # already been bitten by (console.html and workspace.html maintaining
        # separate copies of the same palette).
        doc = (CSS.parent.parent.parent / "docs" / "DESIGN_SYSTEM.md").read_text()
        assert "4/8/12/16/24/32" in doc

    def test_the_scales_carry_a_real_rationale_not_just_values(self, css):
        # A token block with no stated reasoning gets "tidied" by the next
        # person. Both scales explain why they are shaped the way they are.
        block = css[css.index("--dm-text-2xs") - 2000 : css.index("--dm-space-6")]
        assert "seventeen" in block.lower()
        assert "major third" in block.lower()  # the rejected alternative, named
