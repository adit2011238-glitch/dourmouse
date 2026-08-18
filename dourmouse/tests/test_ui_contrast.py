"""Contrast regressions should fail CI, not reach users.

Also pins the compositing rule that a hand-audit got wrong: rgba at 6% alpha
over a near-black ground is near-black, not the bright hue in the literal.
"""

from __future__ import annotations

import pytest

from dourmouse import ui_contrast as uc

BLACKISH = (6, 8, 15)
WHITE = (255, 255, 255)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("#fff", (255, 255, 255, 1.0)),
        ("#FFFFFF", (255, 255, 255, 1.0)),
        ("#06080F", (6, 8, 15, 1.0)),
        ("rgb(79,195,247)", (79, 195, 247, 1.0)),
        ("rgba(79,195,247,0.06)", (79, 195, 247, 0.06)),
        ("  #DCE9F7  ", (220, 233, 247, 1.0)),
    ],
)
def test_parses_supported_colour_forms(text, expected):
    assert uc.parse_color(text) == expected


@pytest.mark.parametrize("text", ["var(--cyan)", "linear-gradient(90deg, red, blue)", "", "inherit", None])
def test_unparseable_values_return_none_rather_than_guessing(text):
    assert uc.parse_color(text) is None


# --------------------------------------------------------------------------- #
# the compositing rule the audit got wrong
# --------------------------------------------------------------------------- #

def test_low_alpha_accent_over_dark_ground_stays_dark():
    """rgba(cyan, .06) is NOT bright cyan — reading it as such reports a
    perfectly legible control as unreadable."""
    faint_cyan = uc.parse_color("rgba(79,195,247,0.06)")
    surface = uc.composite(faint_cyan, BLACKISH)

    assert all(c < 30 for c in surface), surface

    near_white = uc.parse_color("#DCE9F7")
    ratio = uc.contrast_ratio(near_white, surface)
    assert ratio > 12, f"expected a highly legible pairing, got {ratio}"


def test_ignoring_alpha_would_have_produced_the_false_finding():
    """Documents the bug: same colours, alpha dropped, verdict inverted."""
    near_white = uc.parse_color("#DCE9F7")
    solid_cyan = (79, 195, 247)          # the mistake: alpha thrown away
    composited = uc.composite(uc.parse_color("rgba(79,195,247,0.06)"), BLACKISH)

    wrong = uc.contrast_ratio(near_white, solid_cyan)
    right = uc.contrast_ratio(near_white, composited)

    assert wrong < uc.AA_NORMAL      # would be reported as failing
    assert right > uc.AA_NORMAL      # actually passes comfortably


def test_fully_opaque_colour_is_unchanged_by_compositing():
    assert uc.composite((10, 20, 30, 1.0), WHITE) == (10, 20, 30)


def test_fully_transparent_colour_becomes_the_ground():
    assert uc.composite((10, 20, 30, 0.0), WHITE) == WHITE


# --------------------------------------------------------------------------- #
# WCAG maths
# --------------------------------------------------------------------------- #

def test_known_reference_ratios():
    assert round(uc.contrast_ratio(WHITE, (0, 0, 0)), 1) == 21.0
    assert round(uc.contrast_ratio(WHITE, WHITE), 1) == 1.0


def test_ratio_is_symmetric():
    a, b = (17, 17, 17), (240, 240, 240)
    assert round(uc.contrast_ratio(a, b), 4) == round(uc.contrast_ratio(b, a), 4)


def test_luminance_ordering_is_sane():
    assert uc.relative_luminance(WHITE) > uc.relative_luminance((128, 128, 128))
    assert uc.relative_luminance((128, 128, 128)) > uc.relative_luminance((0, 0, 0))


# --------------------------------------------------------------------------- #
# token extraction
# --------------------------------------------------------------------------- #

def test_extracts_tokens_and_last_definition_wins():
    css = ":root { --text: #111; --text-dim: #777; } :root { --text-dim: #999; }"
    tokens = uc.extract_tokens(css)
    assert tokens["--text"] == "#111"
    assert tokens["--text-dim"] == "#999"   # the accessibility pass appends last


def test_audit_flags_a_failing_token():
    css = ":root { --ground: #06080F; --surface: #0B0F1A; --text: #DCE9F7; --text-dim: #14202E; }"
    rows = uc.audit_tokens(css)
    dim = [r for r in rows if r["token"] == "--text-dim"]
    assert dim and all(r["passes"] is False for r in dim)


def test_audit_passes_legible_tokens():
    css = ":root { --ground: #06080F; --surface: #0B0F1A; --text: #DCE9F7; --text-dim: #9DB0C6; }"
    rows = uc.audit_tokens(css)
    assert rows and all(r["passes"] for r in rows)


# --------------------------------------------------------------------------- #
# the live stylesheet — this is the regression guard
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def ui_source():
    path = uc.ui_index_path()
    if not path.exists():
        pytest.skip(f"UI not present at {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def test_shipping_text_tokens_meet_AA(ui_source):
    rows = uc.audit_tokens(ui_source)
    assert rows, "no text tokens found — has the token block moved?"
    failures = [r for r in rows if not r["passes"]]
    assert not failures, "text tokens below WCAG AA: " + "; ".join(
        f"{r['token']} on {r['ground']} = {r['ratio']}:1 (needs {r['threshold']})"
        for r in failures
    )


def test_text_dim_stays_distinguishable_from_text(ui_source):
    """The fix raised --text-dim; it must stay visibly secondary, not merge."""
    tokens = uc.extract_tokens(ui_source)
    text = uc.parse_color(tokens["--text"])
    dim = uc.parse_color(tokens["--text-dim"])
    assert text and dim
    assert uc.relative_luminance(text[:3]) > uc.relative_luminance(dim[:3])
