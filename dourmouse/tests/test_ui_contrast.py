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


# --------------------------------------------------------------------------- #
# console.html and os.html — the two other primary live screens.
#
# Neither names its tokens --text/--ground: console.html paints with
# --blue/--blue-hi/--blue-dim on --bg/--panel/--panel2, os.html with
# --t0/--t1/--t2 on --v0..--v4. audit_tokens() takes those names directly
# rather than teaching it every screen's vocabulary.
#
# Both files also carry alternate themes as sibling selectors
# ([data-theme="x"], and in os.html a prefers-color-scheme block) that
# redeclare the same token names. extract_tokens' "last wins" doesn't know
# about selectors, so run on the whole file it would silently score
# whichever theme happens to sit last in the stylesheet (aurora, in
# console.html) instead of the one that actually renders by default.
# default_root_block() scopes each source to its base `:root { ... }`
# first so the regression guard checks what ships with no data-theme
# attribute set and no OS light-mode preference.
# --------------------------------------------------------------------------- #

CONSOLE_TEXT_TOKENS = {"--blue": uc.AA_NORMAL, "--blue-hi": uc.AA_NORMAL, "--blue-dim": uc.AA_NORMAL}
CONSOLE_GROUND_TOKENS = ("--bg", "--panel", "--panel2")

OS_TEXT_TOKENS = {"--t0": uc.AA_NORMAL, "--t1": uc.AA_NORMAL, "--t2": uc.AA_NORMAL}
OS_GROUND_TOKENS = ("--v0", "--v1", "--v2", "--v3", "--v4")

_SCREENS = {
    "console": (uc.ui_console_path, CONSOLE_TEXT_TOKENS, CONSOLE_GROUND_TOKENS),
    "os": (uc.ui_os_path, OS_TEXT_TOKENS, OS_GROUND_TOKENS),
}


def _screen_root_block(name: str) -> str:
    path_fn, _, _ = _SCREENS[name]
    path = path_fn()
    if not path.exists():
        pytest.skip(f"UI not present at {path}")
    return uc.default_root_block(path.read_text(encoding="utf-8", errors="replace"))


@pytest.mark.parametrize("screen", ["console", "os"])
def test_screen_shipping_text_tokens_meet_AA(screen):
    _, text_tokens, ground_tokens = _SCREENS[screen]
    rows = uc.audit_tokens(_screen_root_block(screen), text_tokens, ground_tokens)
    assert rows, f"no text tokens found for {screen}.html — has the token block moved?"
    failures = [r for r in rows if not r["passes"]]
    assert not failures, f"{screen}.html text tokens below WCAG AA: " + "; ".join(
        f"{r['token']} on {r['ground']} = {r['ratio']}:1 (needs {r['threshold']})"
        for r in failures
    )


@pytest.mark.parametrize("screen", ["console", "os"])
def test_screen_primary_text_stays_brightest(screen):
    """Whichever fix clears AA must not invert the hierarchy: the primary
    ('--blue-hi' / '--t0') token must stay the brightest of its file's
    audited text tokens."""
    _, text_tokens, _ = _SCREENS[screen]
    tokens = uc.extract_tokens(_screen_root_block(screen))
    primary = "--blue-hi" if screen == "console" else "--t0"
    primary_lum = uc.relative_luminance(uc.parse_color(tokens[primary])[:3])
    for name in text_tokens:
        if name == primary:
            continue
        other_lum = uc.relative_luminance(uc.parse_color(tokens[name])[:3])
        assert primary_lum >= other_lum, f"{primary} is no longer the brightest text token in {screen}.html"


def test_default_root_block_scopes_past_alternate_theme_selectors():
    """console.html's ARC CORE default (--blue-hi:#FAFAFA) must not be
    shadowed by a later [data-theme="..."] block's own --blue-hi."""
    source = uc.ui_console_path().read_text(encoding="utf-8", errors="replace")
    root = uc.default_root_block(source)
    tokens = uc.extract_tokens(root)
    assert tokens["--blue-hi"] == "#FAFAFA"
    # The full, unscoped file DOES contain other themes' conflicting
    # values for the same name (aurora, the theme block that happens to
    # sit last in the file) — this documents why scoping matters, not
    # just that it happens to work.
    assert "--blue-hi:#eef1f5" in source.replace(" ", "").lower()
    unscoped = uc.extract_tokens(source)
    assert unscoped["--blue-hi"] != tokens["--blue-hi"]


def test_extract_tokens_survives_a_colon_bearing_bullet_comment():
    """Regression for the real bug this cycle found: a revert-note comment
    written as `- --name: prose, no semicolon nearby` (this is os.html's
    actual documentation style) makes the naive `[^;}]+` value capture
    swallow every real declaration up to the next semicolon -- which can
    be a real, different token's whole declaration."""
    css = """
    :root {
      /* - --sans: this file's identity was "mono chrome", no semicolon
         for several lines, so a naive parser keeps reading right through
         the next real declaration. */
      --v0: #010101;
      --t0: #fefefe;
    }
    """
    tokens = uc.extract_tokens(css)
    assert tokens["--v0"] == "#010101"
    assert tokens["--t0"] == "#fefefe"


def test_os_default_root_block_excludes_light_theme_override():
    """os.html's dark default is the real regression surface; its
    prefers-color-scheme/[data-theme="light"] blocks re-declare --t2 with
    a different, already-passing value that must not mask the dark one."""
    source = uc.ui_os_path().read_text(encoding="utf-8", errors="replace")
    root = uc.default_root_block(source)
    tokens = uc.extract_tokens(root)
    # The light override sets --t0 to a near-black value; the dark
    # default's --t0 stays near-white. If the light block leaked in here,
    # this would flip.
    assert uc.relative_luminance(uc.parse_color(tokens["--t0"])[:3]) > 0.5
