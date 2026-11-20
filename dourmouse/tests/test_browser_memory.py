"""T5 step 3 (finding 212): browser_keep_page and dourmouse/browser_memory.py. No browser is started: the
tool reads the page and hands address, title and text to ``save_page``, which is what is tested here."""

from __future__ import annotations

import pytest

from dourmouse import browser_memory as bm
from dourmouse import library, privacy
from dourmouse.message_bus import MessageBus, set_message_bus


def _find(q):
    return library.search(q)["results"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(privacy.PrivacyStore, "private_mode", lambda self: False)
    bus = MessageBus()
    set_message_bus(bus)
    yield tmp_path, bus
    set_message_bus(None)


def test_saves_text_notes_and_address_and_search_finds_it(env):
    out = bm.save_page("https://example.org/heron#top", "Heron study", "Grey herons nest in colonies.\n\n\n\nThey feed at dawn.",
                       notes="Good source on nesting.")
    assert out.startswith("SAVED") and "wiki:" in out
    hits = _find("herons nest")
    assert hits, "the saved page is found by a library search"
    first = hits[0]
    assert "example.org/heron" in str(first)
    assert "Good source on nesting" in str(hits) or _find("Good source on nesting")


def test_same_address_replaces_the_old_copy(env):
    bm.save_page("https://example.org/a", "A", "first version about otters")
    bm.save_page("https://example.org/a#frag", "A", "second version about badgers")
    assert not _find("otters")
    assert _find("badgers")


def test_refuses_non_http_and_own_pages_and_empty(env, monkeypatch):
    assert bm.save_page("file:///etc/passwd", "x", "y").startswith("REFUSED")
    assert bm.save_page("javascript:alert(1)", "x", "y").startswith("REFUSED")
    monkeypatch.setattr(bm, "_own_address", lambda u: True)
    assert bm.save_page("http://127.0.0.1:8765/console", "x", "y").startswith("REFUSED")
    monkeypatch.setattr(bm, "_own_address", lambda u: False)
    assert bm.save_page("https://example.org/", "x", "   ").startswith("ERROR")
    assert not _find("passwd")


def test_private_mode_saves_nothing(env, monkeypatch):
    monkeypatch.setattr(privacy.PrivacyStore, "private_mode", lambda self: True)
    out = bm.save_page("https://example.org/p", "P", "walruses are large")
    assert out.startswith("REFUSED") and "Private mode" in out
    assert not _find("walruses")


def test_a_page_that_holds_a_key_is_not_saved(env):
    out = bm.save_page("https://example.org/k", "K", "api_key = sk-abcdefghijklmnopqrstuvwxyz0123456789")
    assert out.startswith("REFUSED") and "key or a password" in out
    assert not _find("abcdefghijklmnop")


def test_job_gets_a_log_note_and_a_bus_post(env):
    tmp, bus = env
    from dourmouse.goals import default_store

    store = default_store()
    job = store.create_goal("Read three papers on herons")["id"]
    out = bm.save_page("https://example.org/j", "J", "heron paper text", notes="read it", job=job)
    assert out.startswith("SAVED") and f"Noted on job {job}" in out
    kinds = [e["detail"].get("kind") for e in store.goal_events(job)]
    assert "page_saved" in kinds
    posted = bus.inbox("anyone")
    assert any("page saved for job" in m["subject"] and "https://example.org/j" in m["body"] for m in posted)


def test_unknown_job_still_saves_but_says_no_note(env):
    out = bm.save_page("https://example.org/u", "U", "lynx text", job="no-such-job")
    assert out.startswith("SAVED") and "not a job Dourmouse knows" in out
    assert _find("lynx")


def test_bad_job_id_is_refused(env):
    assert bm.save_page("https://example.org/b", "B", "text", job="../x").startswith("REFUSED")
