"""Fix agent FB: A-5, a seek to the position the media is already at reported a failure."""

from __future__ import annotations

import time

from dourmouse import browser_agent as ba
from dourmouse.tests.test_browser_element_ids import _eval, needs_chrome
from dourmouse.tests.test_browser_youtube import _ready, chrome, site  # noqa: F401 - fixtures


@needs_chrome
def test_seeking_to_where_it_already_is_is_not_a_failure(chrome, site):  # noqa: F811
    ba.browser_open({"url": site + "/watch.html"})
    _ready()
    assert ba.browser_media({"action": "seek", "to": 3}).startswith("SOUGHT to 0:03")
    started = time.monotonic()
    same = ba.browser_media({"action": "seek", "to": 3})
    took = time.monotonic() - started
    assert same.startswith("SOUGHT to 0:03"), same
    assert "did not finish" not in same and "PROBLEM" not in same, same
    assert took < 3.0, f"waited {took:.1f}s for a seeked event that never comes"
    by_zero = ba.browser_media({"action": "seek", "by": 0})
    assert "did not finish" not in by_zero and "PROBLEM" not in by_zero, by_zero
    assert abs(_eval("document.querySelector('video.html5-main-video').currentTime") - 3.0) < 0.1


@needs_chrome
def test_seeking_to_the_start_when_already_at_the_start(chrome, site):  # noqa: F811
    ba.browser_open({"url": site + "/watch.html"})
    _ready()
    out = ba.browser_media({"action": "seek", "to": 0})
    assert "did not finish" not in out and "PROBLEM" not in out, out


@needs_chrome
def test_a_real_seek_still_waits_for_the_seek_and_still_reports_a_refusal(chrome, site):  # noqa: F811
    ba.browser_open({"url": site + "/noseek.html"})
    _ready()
    out = ba.browser_media({"action": "seek", "to": 5})
    assert "PROBLEM: the player did not move there" in out
