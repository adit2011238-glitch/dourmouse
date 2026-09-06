"""Static contrast checker for the UI's CSS custom properties.

Phase 4 of the interface audit: stop contrast regressions reaching users by
checking them the way the test suite checks everything else.

Scope is deliberately narrow. A faithful check needs a real layout engine —
translucent layers composite, gradients have two ends, ancestors carry
opacity — and this project has no browser in CI. So rather than approximate
badly, this verifies the *token layer*: the semantic colours in `:root` that
the stylesheet actually paints text with. Those are the values that caused
the real failures, and they are checkable exactly.

The compositing detail matters and is easy to get wrong: a colour written as
`rgba(79,195,247,0.06)` is not bright cyan. It is 6% cyan over whatever sits
beneath, which on this page's near-black ground is near-black. Reading the
literal without compositing reports a legible control as unreadable — the
mistake that produced a wrong audit finding before this module existed.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "relative_luminance",
    "contrast_ratio",
    "composite",
    "parse_color",
    "extract_tokens",
    "audit_tokens",
    "AA_NORMAL",
    "AA_LARGE",
]

AA_NORMAL = 4.5
AA_LARGE = 3.0

_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_FUNC = re.compile(r"^rgba?\(([^)]+)\)$", re.IGNORECASE)


def parse_color(value: str) -> tuple[float, float, float, float] | None:
    """Parse `#rgb`, `#rrggbb`, `rgb(...)` or `rgba(...)` to (r, g, b, alpha).

    Returns None for anything else — `var(...)`, gradients, keywords — so the
    caller skips it rather than guessing.
    """
    v = (value or "").strip()
    m = _HEX.match(v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = _FUNC.match(v)
    if m:
        parts = [p.strip() for p in m.group(1).replace("/", " ").split(",")]
        if len(parts) == 1:
            parts = m.group(1).split()
        try:
            nums = [float(p.rstrip("%")) for p in parts[:4]]
        except ValueError:
            return None
        if len(nums) < 3:
            return None
        alpha = nums[3] if len(nums) >= 4 else 1.0
        return (nums[0], nums[1], nums[2], alpha)
    return None


def composite(
    fg: tuple[float, float, float, float],
    bg: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Source-over composite of a translucent colour onto an opaque one."""
    a = max(0.0, min(1.0, fg[3]))
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3))  # type: ignore[return-value]


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG 2.1 relative luminance."""
    chan = []
    for c in rgb:
        s = c / 255.0
        chan.append(s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]


def contrast_ratio(
    fg: tuple[float, float, float, float] | tuple[float, float, float],
    bg: tuple[float, float, float],
) -> float:
    """Contrast ratio, compositing `fg` over `bg` when it carries alpha."""
    painted = composite(fg, bg) if len(fg) == 4 else fg  # type: ignore[arg-type]
    l1, l2 = relative_luminance(painted), relative_luminance(bg)  # type: ignore[arg-type]
    hi, lo = (l1, l2) if l1 > l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


_DECL = re.compile(r"(--[A-Za-z0-9-]+)\s*:\s*([^;}]+)[;}]")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def extract_tokens(css_or_html: str) -> dict[str, str]:
    """Collect `--name: value` declarations, last definition winning.

    Comments are stripped first. Without this, a documentation comment
    written as `- --sans: this file's identity was ...` (a real style in
    this codebase's revert notes) reads as a token declaration whose
    "value" is the entire greedy `[^;}]+` run up to the next semicolon —
    which can be lines away, swallowing a real declaration (`--v0:#...;`)
    in between so it never gets its own match. That dropped token then
    silently fails to appear in the result rather than raising, which is
    the dangerous failure mode: a ground or text token goes missing and
    `audit_tokens` just skips it instead of reporting the gap.
    """
    stripped = _COMMENT.sub("", css_or_html)
    out: dict[str, str] = {}
    for name, value in _DECL.findall(stripped):
        out[name] = value.strip()
    return out


# Text tokens that must stay legible on the page's own ground, with the
# threshold each is actually held to. Decorative tokens are not listed: a
# glow or a hairline is not text and does not owe 4.5:1.
TEXT_TOKENS: dict[str, float] = {
    "--text": AA_NORMAL,
    "--text-dim": AA_NORMAL,
}

GROUND_TOKENS = ("--ground", "--surface")


def audit_tokens(
    source: str,
    text_tokens: dict[str, float] | None = None,
    ground_tokens: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    """Check each text token against each ground. Returns one row per pair.

    `text_tokens` / `ground_tokens` default to index.html's own vocabulary
    (`--text`/`--text-dim` on `--ground`/`--surface`). console.html and
    os.html name the same four roles differently (`--blue`/`--blue-dim` on
    `--bg`/`--panel`; `--t0`/`--t1`/`--t2` on `--v0`/`--v1`) — pass the
    file's own names rather than teaching this function every screen's
    palette.
    """
    tokens = extract_tokens(source)
    grounds: list[tuple[str, tuple[float, float, float]]] = []
    for g in (ground_tokens if ground_tokens is not None else GROUND_TOKENS):
        parsed = parse_color(tokens.get(g, ""))
        if parsed:
            grounds.append((g, parsed[:3]))
    rows: list[dict[str, object]] = []
    for token, threshold in (text_tokens if text_tokens is not None else TEXT_TOKENS).items():
        fg = parse_color(tokens.get(token, ""))
        if not fg:
            continue
        for gname, gcolor in grounds:
            ratio = contrast_ratio(fg, gcolor)
            rows.append(
                {
                    "token": token,
                    "ground": gname,
                    "ratio": round(ratio, 2),
                    "threshold": threshold,
                    "passes": ratio >= threshold,
                }
            )
    return rows


def ui_index_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "index.html"


def ui_console_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "console.html"


def ui_os_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "os.html"


def ui_login_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "login.html"


def ui_setup_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "setup.html"


def ui_app_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "app.html"


def ui_hud_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "hud.html"


def ui_workspace_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "workspace.html"


def ui_voice_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "voice.html"


_ROOT_BLOCK = re.compile(r":root\s*\{([^{}]*)\}")


def default_root_block(source: str) -> str:
    """Return the file's base, unconditional `:root { ... }` block.

    console.html and os.html both carry alternate themes as sibling
    `[data-theme="x"]` rules (and, in os.html, an
    `@media (prefers-color-scheme: light)` block) that redeclare the same
    token names. `extract_tokens`'s "last definition wins" rule is a
    faithful reading of a plain cascade, but it knows nothing about
    selectors — fed the whole stylesheet, "last" is just whichever theme
    block happens to sit at the bottom of the file (alphabetically or
    otherwise), not the one that actually renders with no `data-theme`
    attribute set and no OS light-mode preference. Scoping to the first
    bare `:root { ... }` gets the real default back.
    """
    m = _ROOT_BLOCK.search(source)
    return m.group(0) if m else source
