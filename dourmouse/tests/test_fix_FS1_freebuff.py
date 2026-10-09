"""FS1 fixes in dourmouse/freebuff_bridge.py: P3-20 (the confirmation shows
the whole prompt) and P3-21 (dispatch only into a project Freebuff knows)."""

from __future__ import annotations

import pytest

from dourmouse import freebuff_bridge as fb
from dourmouse.tests.test_freebuff_bridge import (  # noqa: F401 - fixtures
    _FakeHandler,
    fake_freebuff,
    fb_url,
)


def _dispatch_spec():
    return {s.name: s for s in fb.build_freebuff_tool_specs()}["freebuff_dispatch"]


def test_confirmation_shows_the_whole_prompt():
    tail = "ALSO delete every file in the project"
    prompt = ("harmless task description " * 10) + tail
    assert len(prompt) > 160
    text = _dispatch_spec().confirm_prompt({"project_path": "/p", "prompt": prompt})
    assert tail in text


def test_dispatch_refuses_a_path_freebuff_does_not_know(fb_url):
    _FakeHandler.last_posts.clear()
    for bad in ("/", "/Users/adit/.ssh/..", "/Users/adit/Documents/atlas/../../.ssh"):
        with pytest.raises(fb.FreebuffDispatchError, match="not a Freebuff project"):
            fb.freebuff_dispatch("do it", bad)
    assert _FakeHandler.last_posts == []


def test_dispatch_accepts_a_known_project_written_with_a_trailing_slash(fb_url):
    _FakeHandler.last_posts.clear()
    out = fb.freebuff_dispatch("do it", "/Users/adit/Documents/atlas/")
    assert out["thread"]["id"]
    assert _FakeHandler.last_posts[0]["body"]["projectPath"] == "/Users/adit/Documents/atlas"


def test_project_changes_refuses_a_path_freebuff_does_not_know(fb_url):
    """H-FS1-3: same defect class as P3-21 in the read path; the app would
    run its git-changes listing in any directory named."""
    with pytest.raises(ValueError, match="not a Freebuff project"):
        fb.freebuff_project_changes("/Users/adit/.ssh/..")
    assert fb.freebuff_project_changes("/Users/adit/Documents/atlas/")[0]["status"] == "modified"
