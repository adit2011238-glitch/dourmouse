"""Keyboard-focus visibility regression guard for console.html, os.html,
login.html and setup.html.

Before this cycle: console.html had 2 real `:focus-visible` rules across
~500KB and 59 `<button>` elements; os.html had 2 across ~74KB and 16
`<button>` elements. Both already used plain `:focus` / `:focus-within` in
a few places (4 and 7 rules respectively) but not the modern focus-visible
form, and — worse than a low count — each file had at least one confirmed,
concrete gap where a keyboard user got NO visible indicator at all:

  - console.html: `#ta` (the main chat input) sets `outline:none` with no
    compensating style anywhere, defeating the file's one global
    `:focus-visible{outline:...}` rule outright. Separately, `.d3dNum`'s
    existing `:focus{border-color:var(--amber-line)}` changed the border
    to `--amber-line`, which is `#3F3F46` — the exact same hex as its own
    unfocused border-color (`--line-2:#3F3F46`) — so that "indicator" was
    a no-op even when it ran.
  - os.html: TWO bare, unscoped `:focus-visible{...}` rules existed at the
    same specificity; the later one (a `var(--cy)` accent ring meant for
    `.nav`/`.palrow`) silently overrode the file's real global default
    (`var(--t0)`) for every focusable element on the page. Separately,
    `.palbox input` (the command-palette search field) set `outline:none`
    with no compensating style, inside a `.palbox{overflow:hidden}`
    ancestor that would clip a real outline ring anyway.

A later cycle (the sign-in/onboarding audit) found the same class of bug on
both screens login.html/setup.html had never been checked before:

  - login.html: `#tok` (the access-token field) sets `outline:none`; its
    only compensating style was a `:focus` border-color change from
    `--line` to `--line-strong`, both close, low-contrast dark greys
    (1.34:1 / 1.91:1 against the field's own background) -- functionally
    invisible, same failure mode as `.d3dNum`'s identical-colour bug above,
    just non-identical this time instead of byte-identical.
  - setup.html: `input[type=text],input[type=password]` (the NVIDIA-key
    and node-URL fields) had the identical bug -- `outline:none` plus a
    plain `:focus` border shift from `--line` to `--line-2`, same two low
    hex values, same non-indicator.

Both now carry a real `:focus-visible` outline in `--amber` (each file's
own "active state" accent, >=3:1 against its background) instead.

This file checks the counts don't regress below the fixed baseline. It
intentionally does not try to re-verify browser layout (clipping,
z-index, computed specificity) — see dourmouse/ui_contrast.py's own scope
note for why that needs a real layout engine this project doesn't have in
CI. What IS checkable exactly: the rule text is present, comments aren't
inflating the count, and the two confirmed gaps stay closed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dourmouse import ui_contrast as uc

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# The baseline this cycle leaves behind. Not the bare pre-fix count (2/2) —
# the count after the real fixes below, so a later PR can't quietly delete
# one of these and still pass.
BASELINE_FOCUS_VISIBLE_COUNT = {
    "console": 5,
    "os": 4,
    "login": 1,
    "setup": 2,
}

_PATHS = {
    "console": uc.ui_console_path,
    "os": uc.ui_os_path,
    "login": uc.ui_login_path,
    "setup": uc.ui_setup_path,
}


def _source(screen: str) -> str:
    path = _PATHS[screen]()
    if not path.exists():
        pytest.skip(f"UI not present at {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _focus_visible_rule_count(source: str) -> int:
    """Count real `:focus-visible` CSS occurrences, comments excluded.

    A raw substring count over the whole file would also match this
    docstring-style file's own explanatory comments (both ui/console.html
    and ui/os.html now document their fixes inline using the literal
    string `:focus-visible`) — stripping comments first keeps the count
    honest about actual rules.
    """
    return _COMMENT.sub("", source).count(":focus-visible")


@pytest.mark.parametrize("screen", ["console", "os", "login", "setup"])
def test_focus_visible_count_does_not_regress(screen):
    count = _focus_visible_rule_count(_source(screen))
    baseline = BASELINE_FOCUS_VISIBLE_COUNT[screen]
    assert count >= baseline, (
        f"{screen}.html has {count} real :focus-visible rules, "
        f"below the {baseline} this cycle established — "
        "a keyboard-focus indicator was likely removed"
    )


def test_console_main_input_has_a_working_focus_indicator():
    """#ta sets outline:none; it must carry its own compensating
    :focus-visible rule rather than relying on (and silently losing) the
    file's global one."""
    source = _source("console")
    m = re.search(r"#ta\{[^}]*outline:none[^}]*\}", source)
    assert m, "expected #ta to still suppress the default outline"
    assert re.search(r"#ta:focus-visible\{[^}]*outline:", source), (
        "#ta removes the default outline but has no compensating "
        ":focus-visible rule of its own"
    )


def test_console_d3d_num_focus_indicator_actually_changes_the_border():
    """.d3dNum:focus(-visible) must change border-color to a value other
    than its own resting border — --amber-line was, at the time this test
    was written, byte-for-byte identical to --line-2, making the old
    'indicator' invisible."""
    source = _source("console")
    tokens = uc.extract_tokens(uc.default_root_block(source))
    resting = re.search(r"\.d3dNum\{[^}]*border:1px solid (var\(--[a-z0-9-]+\))", source)
    focused = re.search(r"\.d3dNum:focus-visible\{[^}]*border-color:(var\(--[a-z0-9-]+\))", source)
    assert resting and focused, ".d3dNum resting/focus border rules moved — update this test's regexes"
    resting_var = resting.group(1)[4:-1]  # "var(--line-2)" -> "--line-2"
    focused_var = focused.group(1)[4:-1]
    assert tokens[resting_var] != tokens[focused_var], (
        f"focus border-color ({focused_var}={tokens.get(focused_var)}) is the same colour as "
        f"the resting border ({resting_var}={tokens.get(resting_var)}) — no visible change on focus"
    )


def test_os_global_focus_default_is_not_shadowed_by_a_later_duplicate():
    """Two unscoped `:focus-visible{...}` rules at the same specificity
    used to exist; the later one always wins for every element, so the
    file's real global default must be the ONLY unscoped one left."""
    source = _COMMENT.sub("", _source("os"))
    unscoped = re.findall(r"""(?<![.\w#\[\]="'>:-])""" r":focus-visible\s*\{", source)
    assert len(unscoped) == 1, (
        f"expected exactly one unscoped `:focus-visible{{...}}` rule (the real global "
        f"default), found {len(unscoped)} — a later duplicate silently wins the cascade "
        "and the file's intended default never paints"
    )


def test_os_command_palette_input_has_a_working_focus_indicator():
    """.palbox input sets outline:none inside a .palbox{overflow:hidden}
    ancestor; it must carry its own non-outline (so it can't be clipped)
    compensating focus-visible style."""
    source = _source("os")
    assert re.search(r"\.palbox\{[^}]*overflow:hidden", source), (
        "expected .palbox to still clip overflow — this test's clipping "
        "rationale depends on it"
    )
    assert re.search(r"\.palbox input\{[^}]*outline:none", source)
    assert re.search(r"\.palbox input:focus-visible\{[^}]*box-shadow:", source), (
        ".palbox input removes the default outline but has no compensating "
        ":focus-visible rule of its own"
    )


def test_login_token_input_has_a_working_focus_indicator():
    """#tok (the access-token password field) sets outline:none. The only
    compensating style used to be a border-color change (--line ->
    --line-strong) that measured 1.34:1 -> 1.91:1 against the field's own
    background -- both nowhere near the 3:1 WCAG 2.1 SC 1.4.11 a focus
    indicator needs, so it was functionally invisible. It must now carry
    its own real (>=3:1) compensating :focus-visible outline."""
    source = _source("login")
    assert re.search(r'input\[type="password"\]\s*\{[^}]*outline:\s*none', source), (
        'expected input[type="password"] to still suppress the default outline'
    )
    m = re.search(r'input\[type="password"\]:focus-visible\s*\{([^}]*)\}', source)
    assert m and re.search(r"outline:\s*1px solid var\(--amber\)", m.group(1)), (
        'input[type="password"] removes the default outline but has no real '
        "compensating :focus-visible outline of its own"
    )
    tokens = uc.extract_tokens(uc.default_root_block(source))
    ground = uc.parse_color(tokens["--ground"])[:3]
    amber = uc.parse_color(tokens["--amber"])
    assert uc.contrast_ratio(amber, ground) >= 3.0


def test_setup_key_and_node_inputs_have_a_working_focus_indicator():
    """input[type=text]/[type=password] (the NVIDIA key and node-URL
    fields) set outline:none. The only compensating style used to be a
    plain :focus border-color change (--line -> --line-2) that measured
    1.34:1 -> 1.91:1 against the field's own background -- below the 3:1
    WCAG 2.1 SC 1.4.11 a focus indicator needs. It must now be
    :focus-visible (matching .opt's own convention) with a real (>=3:1)
    compensating outline."""
    source = _source("setup")
    assert re.search(r"input\[type=text\],input\[type=password\]\{[^}]*outline:none", source), (
        "expected input[type=text],input[type=password] to still suppress the default outline"
    )
    assert not re.search(r"(?<!:focus-visible)\binput:focus\{", source), (
        "input:focus should have been modernized to input:focus-visible"
    )
    m = re.search(r"input:focus-visible\{([^}]*)\}", source)
    assert m and re.search(r"outline:1px solid var\(--amber\)", m.group(1)), (
        "input:focus-visible has no real compensating outline of its own"
    )
    tokens = uc.extract_tokens(uc.default_root_block(source))
    bg = uc.parse_color(tokens["--bg"])[:3]
    amber = uc.parse_color(tokens["--amber"])
    assert uc.contrast_ratio(amber, bg) >= 3.0
