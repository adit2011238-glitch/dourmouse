"""FD2 fix P4-61: audit_tokens reports a missing or unparseable token instead of skipping it."""

from __future__ import annotations

from dourmouse import ui_contrast as uc

GROUND = "--ground: #06080F; --surface: #0B0F1A;"


def test_unparseable_text_token_is_a_failing_row():
    css = f":root {{ {GROUND} --text: #DCE9F7; --text-dim: hsl(210 10% 60%); }}"
    rows = uc.audit_tokens(css)
    bad = [r for r in rows if r["token"] == "--text-dim"]
    assert bad and all(r["passes"] is False for r in bad)
    assert bad[0].get("unparsed")


def test_missing_text_token_is_a_failing_row():
    css = f":root {{ {GROUND} --text: #DCE9F7; }}"  # --text-dim renamed away
    rows = uc.audit_tokens(css)
    assert any(r["token"] == "--text-dim" and r["passes"] is False for r in rows)


def test_missing_ground_token_is_a_failing_row():
    css = ":root { --ground: #06080F; --text: #DCE9F7; --text-dim: #9DB0C6; }"  # no --surface
    rows = uc.audit_tokens(css)
    assert any(r["token"] == "--surface" and r["passes"] is False for r in rows)


def test_all_tokens_present_and_legible_still_passes():
    css = f":root {{ {GROUND} --text: #DCE9F7; --text-dim: #9DB0C6; }}"
    rows = uc.audit_tokens(css)
    assert rows and all(r["passes"] for r in rows)
    assert not any("unparsed" in r for r in rows)


def test_percent_alpha_is_a_fraction():
    r, g, b, a = uc.parse_color("rgba(0,0,0,50%)")
    assert a == 0.5
    assert uc.parse_color("rgb(100% 0% 0% / 25%)") == (255.0, 0.0, 0.0, 0.25)
    assert uc.parse_color("rgba(0,0,0,0.5)")[3] == 0.5
