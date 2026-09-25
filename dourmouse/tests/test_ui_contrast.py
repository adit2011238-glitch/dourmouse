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
# resolve_var_refs -- product.html/hub.html/graveyard.html name their text
# tokens as `rgba(var(--cyan-rgb), var(--a45))` rather than a literal hex or
# rgba like every other audited screen. parse_color can't see through a
# nested var(), so without this these tokens would silently read as
# unparseable and audit_tokens would skip them outright -- the same
# "dropped token" danger extract_tokens's own docstring warns about.
# --------------------------------------------------------------------------- #

def test_resolve_var_refs_substitutes_a_known_token():
    tokens = {"--cyan-rgb": "79,195,247", "--a85": "0.85"}
    assert uc.resolve_var_refs("rgba(var(--cyan-rgb), var(--a85))", tokens) == "rgba(79,195,247, 0.85)"


def test_resolve_var_refs_uses_the_fallback_when_unknown():
    assert uc.resolve_var_refs("var(--missing, 0.5)", {}) == "0.5"


def test_resolve_var_refs_leaves_truly_unresolvable_values_alone():
    assert uc.resolve_var_refs("var(--missing)", {}) == "var(--missing)"


def test_resolve_var_refs_is_a_noop_for_plain_hex():
    tokens = {"--text": "#FAFAFA"}
    assert uc.resolve_var_refs("#FAFAFA", tokens) == "#FAFAFA"


def test_audit_tokens_resolves_nested_var_refs_before_parsing():
    """The real shape this fixes: without resolution these rows either
    vanish (fg unparseable) or the ground is wrong -- both hide the actual
    regression (--text-dim at 0.45 alpha is nowhere near AA on this ground)."""
    css = """
    :root {
      --cyan-rgb: 79,195,247; --a45: 0.45; --a85: 0.85;
      --ground: #0B0E14;
      --text: rgba(var(--cyan-rgb), var(--a85));
      --text-dim: rgba(var(--cyan-rgb), var(--a45));
    }
    """
    rows = uc.audit_tokens(
        css,
        {"--text": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL},
        ("--ground",),
    )
    by_token = {r["token"]: r for r in rows}
    assert by_token["--text"]["passes"] is True
    assert by_token["--text-dim"]["passes"] is False
    assert by_token["--text-dim"]["ratio"] < 3.0


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


# login.html and setup.html -- the sign-in and onboarding screens, the two
# still furthest from the rest of the app's polish. Neither uses index.html's
# --text/--ground names either: login.html has its own --text/--text-dim/
# --text-body on --ground/--surface/--raised; setup.html reuses console.html's
# naming exactly (--blue/--blue-dim/--blue-hi/--blue-deep on --bg/--panel/
# --panel2) since its own header comment says it deliberately shares
# console's tokens. Both carry a single, unconditional `:root { ... }` with
# no sibling [data-theme] or prefers-color-scheme block, so default_root_block
# is a no-op safety net here rather than the load-bearing fix it is for
# console/os -- kept for consistency should a theme variant land later.
LOGIN_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL, "--text-body": uc.AA_NORMAL}
LOGIN_GROUND_TOKENS = ("--ground", "--surface", "--raised")

SETUP_TEXT_TOKENS = {
    "--blue": uc.AA_NORMAL,
    "--blue-dim": uc.AA_NORMAL,
    "--blue-hi": uc.AA_NORMAL,
    "--blue-deep": uc.AA_NORMAL,
}
SETUP_GROUND_TOKENS = ("--bg", "--panel", "--panel2")

# workspace.html and voice.html -- the "Vision" floating-window workbench
# and the hands-free voice lab, the two highest-value screens not yet
# covered (workspace.html is the flagship God's-Eye-View shell and the
# in-progress Tauri rewrite target; voice.html is the vision/hands-free
# surface). Both were retthemed to this app's shared zinc/amber system in
# an earlier pass and, like console.html/setup.html before them, name their
# tokens with that system's own vocabulary rather than --text/--ground:
# workspace.html reuses console.html's exact names (--blue/--blue-dim/
# --blue-hi on --bg/--panel/--panel2); voice.html has its own --text/
# --text-body/--dim on --bg/--panel/--raised. Neither carries an alternate
# theme or data-theme block, so default_root_block is a no-op safety net
# here, same as login.html/setup.html.
WORKSPACE_TEXT_TOKENS = {"--blue": uc.AA_NORMAL, "--blue-dim": uc.AA_NORMAL, "--blue-hi": uc.AA_NORMAL}
WORKSPACE_GROUND_TOKENS = ("--bg", "--panel", "--panel2")

VOICE_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-body": uc.AA_NORMAL, "--dim": uc.AA_NORMAL}
VOICE_GROUND_TOKENS = ("--bg", "--panel", "--raised")

# agent.html, all_hands.html, atlas_lab.html, map.html, mobile.html -- five
# screens confirmed via grep to have ZERO :focus-visible rules (see that
# fix at the bottom of this file). All five share index.html's own
# --text/--text-dim[/--text-body] vocabulary on --ground/--surface/(a third
# raised layer, named --raised except map.html's --layer-hi) -- unlike
# workspace.html/voice.html, these were retro-fitted from an older
# rgba(cyan)-on-dark palette to this shared zinc/amber system (see each
# file's own in-file revert-note comment), landing on the same token names
# as console.html/os.html's sibling screens. agent.html alone kept the
# OLDER two-tier shape (--text/--text-dim, no --text-body) rather than the
# newer three-tier one the other four already carry.
#
# All five had the identical WCAG 2.1 SC 1.4.3 failure already found and
# fixed on every other screen in this audit: --text-dim was #71717A
# (zinc-500), 3-4:1 against each file's own grounds, on real 8-11px labels/
# captions, not decorative glyphs. Raised to zinc-400 (#A1A1AA) in each --
# the same step every prior fix took.
AGENT_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL}
AGENT_GROUND_TOKENS = ("--ground", "--surface", "--raised")

ALL_HANDS_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL, "--text-body": uc.AA_NORMAL}
ALL_HANDS_GROUND_TOKENS = ("--ground", "--surface", "--raised")

ATLAS_LAB_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL, "--text-body": uc.AA_NORMAL}
ATLAS_LAB_GROUND_TOKENS = ("--ground", "--surface", "--raised")

MAP_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-body": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL}
MAP_GROUND_TOKENS = ("--ground", "--surface", "--layer-hi")

MOBILE_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL, "--text-body": uc.AA_NORMAL}
MOBILE_GROUND_TOKENS = ("--ground", "--surface", "--raised")

# product.html, hub.html, graveyard.html -- the second batch, also ZERO
# :focus-visible rules. Unlike every screen above, these three never got
# retro-fitted off the older palette: --text/--text-dim are still
# `rgba(var(--cyan-rgb), var(--a85|a45))` -- a translucent cyan composited
# over the ground, the exact nested-var() shape resolve_var_refs exists
# for. The real failure: --text-dim at 0.45 alpha measures 2.77-2.80:1
# against every one of this file's grounds -- not just below 4.5:1 AA, but
# below the 3:1 large-text/UI floor too. None of these files' alpha ladders
# define a step between 0.45 and 0.85 that clears 4.5:1, so each reuses
# --a85 directly (ties --text-dim's rendered value to --text's).
PRODUCT_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL}
PRODUCT_GROUND_TOKENS = ("--ground", "--surface", "--surface2")

HUB_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL}
HUB_GROUND_TOKENS = ("--ground", "--surface", "--surface2")

GRAVEYARD_TEXT_TOKENS = {"--text": uc.AA_NORMAL, "--text-dim": uc.AA_NORMAL}
GRAVEYARD_GROUND_TOKENS = ("--ground", "--surface", "--surface2")

_SCREENS = {
    "console": (uc.ui_console_path, CONSOLE_TEXT_TOKENS, CONSOLE_GROUND_TOKENS),
    "login": (uc.ui_login_path, LOGIN_TEXT_TOKENS, LOGIN_GROUND_TOKENS),
    "setup": (uc.ui_setup_path, SETUP_TEXT_TOKENS, SETUP_GROUND_TOKENS),
    "workspace": (uc.ui_workspace_path, WORKSPACE_TEXT_TOKENS, WORKSPACE_GROUND_TOKENS),
    "voice": (uc.ui_voice_path, VOICE_TEXT_TOKENS, VOICE_GROUND_TOKENS),
    "agent": (uc.ui_agent_path, AGENT_TEXT_TOKENS, AGENT_GROUND_TOKENS),
    "all_hands": (uc.ui_all_hands_path, ALL_HANDS_TEXT_TOKENS, ALL_HANDS_GROUND_TOKENS),
    "atlas_lab": (uc.ui_atlas_lab_path, ATLAS_LAB_TEXT_TOKENS, ATLAS_LAB_GROUND_TOKENS),
    "map": (uc.ui_map_path, MAP_TEXT_TOKENS, MAP_GROUND_TOKENS),
    "mobile": (uc.ui_mobile_path, MOBILE_TEXT_TOKENS, MOBILE_GROUND_TOKENS),
    "product": (uc.ui_product_path, PRODUCT_TEXT_TOKENS, PRODUCT_GROUND_TOKENS),
    "hub": (uc.ui_hub_path, HUB_TEXT_TOKENS, HUB_GROUND_TOKENS),
    "graveyard": (uc.ui_graveyard_path, GRAVEYARD_TEXT_TOKENS, GRAVEYARD_GROUND_TOKENS),
}

_PRIMARY_TOKEN = {
    "console": "--blue-hi", "login": "--text", "setup": "--blue-hi",
    "workspace": "--blue-hi", "voice": "--text",
    "agent": "--text", "all_hands": "--text", "atlas_lab": "--text", "map": "--text",
    "mobile": "--text", "product": "--text", "hub": "--text", "graveyard": "--text",
}

_ALL_SCREENS = list(_SCREENS.keys())


def _screen_root_block(name: str) -> str:
    path_fn, _, _ = _SCREENS[name]
    path = path_fn()
    if not path.exists():
        pytest.skip(f"UI not present at {path}")
    return uc.default_root_block(path.read_text(encoding="utf-8", errors="replace"))


@pytest.mark.parametrize("screen", _ALL_SCREENS)
def test_screen_shipping_text_tokens_meet_AA(screen):
    _, text_tokens, ground_tokens = _SCREENS[screen]
    rows = uc.audit_tokens(_screen_root_block(screen), text_tokens, ground_tokens)
    assert rows, f"no text tokens found for {screen}.html — has the token block moved?"
    failures = [r for r in rows if not r["passes"]]
    assert not failures, f"{screen}.html text tokens below WCAG AA: " + "; ".join(
        f"{r['token']} on {r['ground']} = {r['ratio']}:1 (needs {r['threshold']})"
        for r in failures
    )


@pytest.mark.parametrize("screen", _ALL_SCREENS)
def test_screen_primary_text_stays_brightest(screen):
    """Whichever fix clears AA must not invert the hierarchy: the primary
    ('--blue-hi' / '--t0' / '--text') token must stay the brightest of its
    file's audited text tokens.

    Values are resolved through resolve_var_refs before parsing -- a no-op
    for every screen here (all plain hex), but load-bearing for the
    product/hub/graveyard family below, whose tokens nest var(...)."""
    _, text_tokens, _ = _SCREENS[screen]
    tokens = uc.extract_tokens(_screen_root_block(screen))
    primary = _PRIMARY_TOKEN[screen]
    primary_lum = uc.relative_luminance(uc.parse_color(uc.resolve_var_refs(tokens[primary], tokens))[:3])
    for name in text_tokens:
        if name == primary:
            continue
        other_lum = uc.relative_luminance(uc.parse_color(uc.resolve_var_refs(tokens[name], tokens))[:3])
        assert primary_lum >= other_lum, f"{primary} is no longer the brightest text token in {screen}.html"


def test_default_root_block_scopes_past_alternate_theme_selectors():
    """console.html's ARC CORE default (--blue-hi:#FAFAFA) must not be
    shadowed by a later [data-theme="..."] block's own --blue-hi."""
    source = uc.ui_console_path().read_text(encoding="utf-8", errors="replace")
    root = uc.default_root_block(source)
    tokens = uc.extract_tokens(root)
    assert tokens["--blue-hi"] == "#FAFAFA"
    # The full, unscoped file DOES contain other themes' conflicting
    # values for the same name (studio-frost, the theme block that
    # happens to sit last in the file since Aurora was removed
    # 2026-09-14) — this documents why scoping matters, not just that it
    # happens to work.
    assert "--blue-hi:#1e2939" in source.replace(" ", "").lower()
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


# --------------------------------------------------------------------------- #
# login.html and setup.html -- the sign-in and onboarding screens.
#
# Both had a real WCAG 2.1 SC 1.4.3 failure: login.html's --text-dim and
# setup.html's --blue-deep were each #71717A (zinc-500) -- the identical
# value 506c078 already found failing in console.html/os.html, on real
# 9-13px labels/captions, not decorative glyphs. Both raised to #A1A1AA
# (zinc-400), the value each file's own token set already used elsewhere.
# --------------------------------------------------------------------------- #

def test_login_text_dim_was_the_known_failing_zinc_500_value():
    """Pins the regression this cycle fixed: --text-dim must no longer be
    the #71717A value that measured 3.08-4.12:1 against login.html's own
    grounds, below the 4.5:1 its real label/caption text needs."""
    tokens = uc.extract_tokens(uc.default_root_block(uc.ui_login_path().read_text(encoding="utf-8", errors="replace")))
    assert tokens["--text-dim"].upper() != "#71717A"


def test_setup_blue_deep_was_the_known_failing_zinc_500_value():
    """Same regression, setup.html's name for the same role: --blue-deep
    paints .sec/.note captions and must no longer be the failing #71717A."""
    tokens = uc.extract_tokens(uc.default_root_block(uc.ui_setup_path().read_text(encoding="utf-8", errors="replace")))
    assert tokens["--blue-deep"].upper() != "#71717A"


@pytest.mark.parametrize(
    "screen,dim_token",
    [
        ("login", "--text-dim"), ("setup", "--blue-deep"), ("workspace", "--blue-dim"), ("voice", "--dim"),
        ("agent", "--text-dim"), ("all_hands", "--text-dim"), ("atlas_lab", "--text-dim"),
        ("map", "--text-dim"), ("mobile", "--text-dim"),
        ("product", "--text-dim"), ("hub", "--text-dim"), ("graveyard", "--text-dim"),
    ],
)
def test_dim_token_stays_no_brighter_than_primary(screen, dim_token):
    """The AA fix must not invert the hierarchy: a secondary/tertiary token
    must not end up brighter than its screen's primary text token (it may
    now tie it, as login's --text-dim ties --text-body, and as product's/
    hub's/graveyard's --text-dim now ties --text exactly -- that collapses
    a visual distinction, not a contrast defect).

    Values are resolved through resolve_var_refs before comparing luminance
    -- a no-op for every plain-hex screen, load-bearing for product.html/
    hub.html/graveyard.html's nested rgba(var(...), var(...)) tokens."""
    tokens = uc.extract_tokens(_screen_root_block(screen))
    primary_lum = uc.relative_luminance(
        uc.parse_color(uc.resolve_var_refs(tokens[_PRIMARY_TOKEN[screen]], tokens))[:3]
    )
    dim_lum = uc.relative_luminance(uc.parse_color(uc.resolve_var_refs(tokens[dim_token], tokens))[:3])
    assert dim_lum <= primary_lum


# --------------------------------------------------------------------------- #
# hud.html -- the tactical HUD screen. (app.html, the old desktop shell, was
# retired into the console in finding #121; its checks went with it.)
#
# hud.html has no theme variants at all -- a single unconditional `:root`
# -- so default_root_block is the same no-op safety net there that it is
# for login.html/setup.html.
# --------------------------------------------------------------------------- #


HUD_TEXT_TOKENS = {"--txt": uc.AA_NORMAL, "--red": uc.AA_NORMAL, "--dim": uc.AA_NORMAL, "--dim-2": uc.AA_NORMAL}
HUD_GROUND_TOKENS = ("--bg", "--panel", "--raised")


@pytest.fixture(scope="module")
def hud_root():
    path = uc.ui_hud_path()
    if not path.exists():
        pytest.skip(f"UI not present at {path}")
    return uc.default_root_block(path.read_text(encoding="utf-8", errors="replace"))


def test_hud_shipping_text_tokens_meet_AA(hud_root):
    rows = uc.audit_tokens(hud_root, HUD_TEXT_TOKENS, HUD_GROUND_TOKENS)
    assert rows, "no text tokens found for hud.html -- has the token block moved?"
    failures = [r for r in rows if not r["passes"]]
    assert not failures, "hud.html text tokens below WCAG AA: " + "; ".join(
        f"{r['token']} on {r['ground']} = {r['ratio']}:1 (needs {r['threshold']})"
        for r in failures
    )


def test_hud_dim_tokens_were_the_known_failing_values(hud_root):
    """Pins the regression: --dim (was #71717A, 3.08-4.12:1 on
    bg/panel/raised) and --dim-2 (was #52525B, 1.93-2.57:1) must no longer
    be those failing values, on real 10.5-12px labels (.phead, .titlebox
    .sub, .hdrright, .logbox timestamps), not decorative glyphs."""
    tokens = uc.extract_tokens(hud_root)
    assert tokens["--dim"].upper() != "#71717A"
    assert tokens["--dim-2"].upper() != "#52525B"


def test_hud_dim_tokens_stay_no_more_prominent_than_txt(hud_root):
    """--dim/--dim-2 now equal --red exactly (same collapsed-distinction
    outcome console.html/setup.html already accepted for their own
    --blue-dim/--blue-deep) -- none of the three may end up more prominent
    than --txt, the brightest/primary token on this screen."""
    tokens = uc.extract_tokens(hud_root)
    bg = uc.parse_color(tokens["--bg"])[:3]
    txt_ratio = uc.contrast_ratio(uc.parse_color(tokens["--txt"]), bg)
    for name in ("--red", "--dim", "--dim-2"):
        assert uc.contrast_ratio(uc.parse_color(tokens[name]), bg) <= txt_ratio + 1e-9


def test_hud_taskadd_input_focus_visible_ring_is_legible():
    """.taskadd input set outline:none and, before this cycle, had no
    :focus-visible compensator at all -- every other control on the page
    relied on the browser default ring, which this red/amber terminal HUD
    never styled for, and this one input actively defeated. The new rule
    must paint a real, legible ring: --amber on --bg measures 9.26:1, well
    past the 3:1 SC 1.4.11 needs for a non-text UI-component indicator."""
    path = uc.ui_hud_path()
    if not path.exists():
        pytest.skip(f"UI not present at {path}")
    source = path.read_text(encoding="utf-8", errors="replace")
    assert ".taskadd input:focus-visible" in source
    assert "outline: 1px solid var(--amber)" in source
    tokens = uc.extract_tokens(uc.default_root_block(source))
    ratio = uc.contrast_ratio(uc.parse_color(tokens["--amber"]), uc.parse_color(tokens["--bg"])[:3])
    assert ratio >= uc.AA_LARGE


# --------------------------------------------------------------------------- #
# workspace.html and voice.html -- the floating-window workbench and the
# hands-free voice lab. Both had the identical WCAG 2.1 SC 1.4.3 failure
# already found in every prior screen (--blue-dim / --dim at #71717A, the
# same zinc-500 value), and, unlike any of the six screens above, NEITHER
# had a single :focus-visible or outline rule anywhere in the file -- every
# focusable control (dockbtn, #voxText/#voxGo, panel inputs, the mic/auth/
# queue buttons) relied entirely on the browser's native default ring. This
# is the same "ZERO focus-visible rules" shape hud.html was in before
# d7e4a32, not a defeated-rule shape -- fixed the same way: one global bare
# `:focus-visible` rule using each file's own accent name (--amber /
# --gold), per Apple HIG's requirement that focus be clearly and
# consistently indicated.
# --------------------------------------------------------------------------- #

def test_workspace_blue_dim_was_the_known_failing_zinc_500_value():
    """Pins the regression: --blue-dim must no longer be the #71717A value
    that measured 3.08-4.12:1 against workspace.html's own --bg/--panel/
    --panel2, below the 4.5:1 its real 9.5-13px labels/captions need
    (.brand small, .hfpill, .mailrow .d, .turn .meta, .brstatus, and more)."""
    tokens = uc.extract_tokens(_screen_root_block("workspace"))
    assert tokens["--blue-dim"].upper() != "#71717A"


def test_voice_dim_was_the_known_failing_zinc_500_value():
    """Pins the regression: --dim must no longer be the #71717A value that
    measured 3.08-4.12:1 against voice.html's own --bg/--panel/--raised,
    below the 4.5:1 its real 9.5-11px labels/captions need (.brand .sub,
    .chip, .miclabel, .statusline .kicker, .card .k/.row/.note)."""
    tokens = uc.extract_tokens(_screen_root_block("voice"))
    assert tokens["--dim"].upper() != "#71717A"


def test_workspace_focus_visible_ring_is_legible():
    """workspace.html had ZERO :focus-visible rules before this cycle -- the
    same shape hud.html was in pre-d7e4a32, not a defeated-rule shape. The
    new global rule must paint a real, legible ring: --amber on --bg
    measures 9.26:1, well past the 3:1 SC 1.4.11 needs for a non-text
    UI-component indicator."""
    path = uc.ui_workspace_path()
    if not path.exists():
        pytest.skip(f"UI not present at {path}")
    source = path.read_text(encoding="utf-8", errors="replace")
    assert ":focus-visible{outline:1px solid var(--amber);outline-offset:2px}" in source
    tokens = uc.extract_tokens(uc.default_root_block(source))
    ratio = uc.contrast_ratio(uc.parse_color(tokens["--amber"]), uc.parse_color(tokens["--bg"])[:3])
    assert ratio >= uc.AA_LARGE


def test_voice_focus_visible_ring_is_legible():
    """voice.html had ZERO :focus-visible rules before this cycle -- the mic,
    auth and queue buttons all relied on the browser default. The new global
    rule must paint a real, legible ring using this file's own accent name:
    --gold on --bg measures 9.26:1, well past the 3:1 SC 1.4.11 needs for a
    non-text UI-component indicator."""
    path = uc.ui_voice_path()
    if not path.exists():
        pytest.skip(f"UI not present at {path}")
    source = path.read_text(encoding="utf-8", errors="replace")
    assert ":focus-visible { outline: 1px solid var(--gold); outline-offset: 2px; }" in source
    tokens = uc.extract_tokens(uc.default_root_block(source))
    ratio = uc.contrast_ratio(uc.parse_color(tokens["--gold"]), uc.parse_color(tokens["--bg"])[:3])
    assert ratio >= uc.AA_LARGE


# --------------------------------------------------------------------------- #
# agent.html, all_hands.html, atlas_lab.html, map.html, mobile.html -- five
# more screens confirmed via grep to have ZERO :focus-visible rules, same
# shape as workspace.html/voice.html above (not a defeated-rule shape).
# Focus-visible presence/legibility is verified in test_ui_focus_visible.py
# alongside the other zero-rule screens from this same cycle; this file
# covers the token-value regression each of these five also carried.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("screen", ["agent", "all_hands", "atlas_lab", "map", "mobile"])
def test_zinc_family_text_dim_was_the_known_failing_zinc_500_value(screen):
    """Pins the regression: --text-dim must no longer be the #71717A value
    that measured 3-4:1 against each file's own grounds, below the 4.5:1
    its real 8-11px labels/captions need."""
    tokens = uc.extract_tokens(_screen_root_block(screen))
    assert tokens["--text-dim"].upper() != "#71717A"


# --------------------------------------------------------------------------- #
# product.html, hub.html, graveyard.html -- the second batch, also ZERO
# :focus-visible rules, and the one family in this whole audit that never
# got retro-fitted off the older rgba(cyan)-on-dark palette: --text-dim is
# `rgba(var(--cyan-rgb), var(--a45))`, not a literal hex. Pinning "not
# #71717A" doesn't apply here -- the regression to pin is the alpha
# reference itself.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("screen", ["product", "hub", "graveyard"])
def test_rgba_family_text_dim_no_longer_uses_the_failing_alpha(screen):
    """Pins the regression: --text-dim must no longer reference --a45 (0.45
    alpha measured 2.77-2.80:1 against every one of this file's grounds --
    below even the 3:1 large-text floor, let alone the 4.5:1 AA needs)."""
    tokens = uc.extract_tokens(_screen_root_block(screen))
    assert "var(--a45)" not in tokens["--text-dim"]


@pytest.mark.parametrize("screen", ["product", "hub", "graveyard"])
def test_rgba_family_text_dim_now_resolves_to_a_legible_composite(screen):
    """The alpha swap must actually land on a passing value once resolved
    against the real ground -- not just avoid the old literal."""
    tokens = uc.extract_tokens(_screen_root_block(screen))
    ground = uc.parse_color(uc.resolve_var_refs(tokens["--ground"], tokens))[:3]
    dim = uc.parse_color(uc.resolve_var_refs(tokens["--text-dim"], tokens))
    assert uc.contrast_ratio(dim, ground) >= uc.AA_NORMAL
