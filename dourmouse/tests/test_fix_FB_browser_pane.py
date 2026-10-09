"""Fix agent FB: P5-11 (frame-ancestors 'self' is the SITE's own origin, not ours) and P5-12 (the
frame-bust rewrite has no word boundary and rewrites text outside scripts)."""

from __future__ import annotations

import email.message

import pytest

from dourmouse import browser_pane
from dourmouse.browser_pane import check_frameable


def _headers(*pairs):
    msg = email.message.Message()  # what urlopen really returns: case-insensitive, repeatable headers
    for k, v in pairs:
        msg[k] = v
    return msg


class _Resp:
    def __init__(self, headers):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _check(monkeypatch, *pairs, port="8765"):
    monkeypatch.setenv("DOURMOUSE_UI_PORT", port)
    monkeypatch.setattr(browser_pane, "guarded_urlopen", lambda *a, **k: _Resp(_headers(*pairs)))
    return check_frameable("https://site.example")


# --------------------------------------------------------------------------- #
# P5-11
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "policy",
    [
        "frame-ancestors 'self'",
        "frame-ancestors 'self' https://partner.example",
        "frame-ancestors https://*.site.example",
        "frame-ancestors https:",
        "frame-ancestors 'none'",
        "frame-ancestors http://127.0.0.1:9999",
        "default-src 'self'; frame-ancestors 'self'; img-src *",
    ],
)
def test_policies_that_do_not_let_us_in_are_blocked(monkeypatch, policy):
    out = _check(monkeypatch, ("Content-Security-Policy", policy))
    assert out["frameable"] is False, policy
    assert "frame-ancestors" in out["reason"]


@pytest.mark.parametrize(
    "policy",
    [
        "frame-ancestors *",
        "frame-ancestors 'self' *",
        "frame-ancestors http://localhost:*",
        "frame-ancestors http://127.0.0.1:8765",
        "frame-ancestors 127.0.0.1",
        "frame-ancestors http:",
        "default-src 'self'",
        "frame-ancestors http://localhost:8765 https://other.example",
    ],
)
def test_policies_that_do_let_us_in_are_frameable(monkeypatch, policy):
    out = _check(monkeypatch, ("Content-Security-Policy", policy))
    assert out["frameable"] is True, policy


def test_the_port_of_our_own_server_is_the_one_asked_for(monkeypatch):
    assert _check(monkeypatch, ("Content-Security-Policy", "frame-ancestors http://127.0.0.1:9100"), port="9100")["frameable"] is True
    assert _check(monkeypatch, ("Content-Security-Policy", "frame-ancestors http://127.0.0.1:9100"), port="8765")["frameable"] is False


def test_two_policy_headers_must_both_allow_us(monkeypatch):
    out = _check(monkeypatch, ("Content-Security-Policy", "frame-ancestors *"), ("Content-Security-Policy", "frame-ancestors 'self'"))
    assert out["frameable"] is False


def test_x_frame_options_still_blocks(monkeypatch):
    assert _check(monkeypatch, ("X-Frame-Options", "SAMEORIGIN"))["frameable"] is False
    assert _check(monkeypatch, ("X-Frame-Options", "deny"))["frameable"] is False
    assert _check(monkeypatch)["frameable"] is True


# --------------------------------------------------------------------------- #
# P5-12
# --------------------------------------------------------------------------- #

NB = browser_pane._neutralize_frame_busting


@pytest.mark.parametrize(
    "text",
    [
        "var a = desktop.location;",
        "laptop.location.href = x",
        "grandparent.location",
        "if (laptop != selfie) {}",
        "deskTop.location",
        "var mytop!=self;",
        "obj.top.location",
        "x.window.top",
    ],
)
def test_names_that_merely_end_in_top_or_parent_are_left_alone(text):
    html = f"<script>{text}</script>".encode()
    assert NB(html) == html


def test_text_outside_scripts_is_never_rewritten():
    html = b"<p>Use laptop.location, top.location or top != self in the example.</p><pre>top.location = self.location</pre>"
    assert NB(html) == html


@pytest.mark.parametrize(
    ("src", "must_have", "must_not_have"),
    [
        ("if (top !== self) top.location = self.location;", b"false", b"top.location"),
        ("if (top != self) { top.location = self.location }", b"false", b"top.location"),
        ("if (window.top !== window.self) { window.top.location = location }", b"false", b"window.top"),
        ("parent.location = 'x'", b"self.location", b"parent.location"),
        ("if (self !== top) {}", b"false", b"top"),
    ],
)
def test_real_frame_busting_inside_a_script_is_still_neutralised(src, must_have, must_not_have):
    out = NB(f"<html><head><script>{src}</script></head><body>x</body></html>".encode())
    assert must_have in out
    assert must_not_have not in out.split(b"</script>")[0].split(b"<script>")[1]


def test_scripts_with_attributes_and_several_scripts_are_handled():
    html = (
        b'<script type="text/javascript" nonce="x">if (top != self) top.location = self.location;</script>'
        b"<p>top.location stays</p>"
        b"<SCRIPT>parent.location = 1</SCRIPT>"
    )
    out = NB(html)
    assert b"<p>top.location stays</p>" in out
    assert out.count(b"self.location") >= 2 and b"parent.location" not in out
