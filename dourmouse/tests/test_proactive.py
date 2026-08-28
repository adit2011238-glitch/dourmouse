"""Tests for dourmouse/proactive.py (Vision stage 7: proactive surfacing).

Mirrors dourmouse.desktop.DesktopNotifier's own (untested-elsewhere) shape:
a fake ``fetch`` stands in for the real /api/state HTTP call so the
dedupe/allowlist/threading logic runs for real against actual code paths,
with no network and no GUI. The popup WINDOW itself needs a real pywebview
backend to actually appear on screen — ``default_popup_factory`` is
exercised here only up to window construction, via a fake webview module
(same seam shape as dourmouse/tests/test_overlay.py uses for the overlay
window).
"""

from __future__ import annotations

import threading
import time

import pytest

from dourmouse import proactive


# --------------------------------------------------------------------------- #
# The allowlist itself
# --------------------------------------------------------------------------- #

class TestAllowlist:
    def test_allowlist_is_short_and_explicit(self):
        """Restraint is the point: this must stay small, not grow to match
        every kind the schema happens to validate."""
        assert set(proactive.ALLOWED_ALERT_KINDS) == {"system", "world", "atlas"}

    def test_market_is_deliberately_excluded(self):
        assert "market" not in proactive.ALLOWED_ALERT_KINDS

    def test_mail_has_no_entry_because_no_schema_kind_exists(self):
        assert "mail" not in proactive.ALLOWED_ALERT_KINDS
        assert "mail_unanswered" not in proactive.ALLOWED_ALERT_KINDS


# --------------------------------------------------------------------------- #
# refresh(): fetch -> allowlist filter -> dedupe -> popup_factory
# --------------------------------------------------------------------------- #

def _state(alerts):
    return {"alerts": alerts}


class TestRefresh:
    def test_first_refresh_only_primes_and_surfaces_nothing(self):
        """Matches DesktopNotifier's own launch behaviour: a fresh start
        must never replay a backlog of pre-existing alerts as if they were
        new."""
        popped = []
        surfacer = proactive.ProactiveSurfacer(
            "http://x", popped.append,
            fetch=lambda url: _state([{"id": 1, "kind": "system", "title": "t", "detail": "d"}]),
        )
        surfaced = surfacer.refresh()
        assert surfaced == []
        assert popped == []

    def test_genuinely_new_allowed_alert_pops_a_window(self):
        popped = []
        calls = {"n": 0}

        def fetch(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return _state([])
            return _state([{"id": 1, "kind": "system", "title": "hi", "detail": "d"}])

        surfacer = proactive.ProactiveSurfacer("http://x", popped.append, fetch=fetch)
        surfacer.refresh()  # primes
        surfaced = surfacer.refresh()
        assert len(surfaced) == 1
        assert popped == [{"id": 1, "kind": "system", "title": "hi", "detail": "d"}]

    def test_disallowed_kind_is_silently_skipped(self):
        popped = []
        calls = {"n": 0}

        def fetch(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return _state([])
            return _state([{"id": 1, "kind": "market", "title": "AAPL +2%", "detail": "d"}])

        surfacer = proactive.ProactiveSurfacer("http://x", popped.append, fetch=fetch)
        surfacer.refresh()
        surfaced = surfacer.refresh()
        assert surfaced == []
        assert popped == []

    def test_disallowed_kind_alert_id_is_still_marked_seen(self):
        """A market alert must not keep re-triggering a (skipped) check on
        every poll -- it's marked seen exactly like an allowed one."""
        calls = {"n": 0}

        def fetch(url):
            calls["n"] += 1
            return _state([{"id": 1, "kind": "market", "title": "x", "detail": "d"}])

        surfacer = proactive.ProactiveSurfacer("http://x", lambda item: None, fetch=fetch)
        surfacer.refresh()
        assert 1 in surfacer._seen

    def test_same_alert_never_surfaces_twice(self):
        popped = []

        def fetch(url):
            return _state([{"id": 1, "kind": "world", "title": "t", "detail": "d"}])

        surfacer = proactive.ProactiveSurfacer("http://x", popped.append, fetch=fetch)
        surfacer.refresh()  # primes on id 1
        surfacer.refresh()
        surfacer.refresh()
        assert popped == []  # id 1 was already seen on the priming pass

    def test_new_id_after_priming_surfaces_once(self):
        popped = []
        calls = {"n": 0}

        def fetch(url):
            calls["n"] += 1
            ids = [1] if calls["n"] == 1 else [1, 2]
            return _state([{"id": i, "kind": "atlas", "title": f"t{i}", "detail": "d"} for i in ids])

        surfacer = proactive.ProactiveSurfacer("http://x", popped.append, fetch=fetch)
        surfacer.refresh()  # primes on {1}
        surfacer.refresh()  # sees new id 2
        surfacer.refresh()  # id 2 now already seen
        assert [a["id"] for a in popped] == [2]

    def test_unreachable_server_returns_empty_list_not_none(self):
        surfacer = proactive.ProactiveSurfacer("http://x", lambda item: None, fetch=lambda url: None)
        assert surfacer.refresh() == []

    def test_malformed_alert_id_is_skipped_not_crashing(self):
        surfacer = proactive.ProactiveSurfacer(
            "http://x", lambda item: None,
            fetch=lambda url: _state([{"id": "not-an-int", "kind": "system"}]),
        )
        assert surfacer.refresh() == []  # must not raise

    def test_a_raising_popup_factory_never_crashes_refresh(self):
        def bad_factory(item):
            raise RuntimeError("boom")

        calls = {"n": 0}

        def fetch(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return _state([])
            return _state([{"id": 1, "kind": "system", "title": "t", "detail": "d"}])

        surfacer = proactive.ProactiveSurfacer("http://x", bad_factory, fetch=fetch)
        surfacer.refresh()
        surfaced = surfacer.refresh()  # must not raise even though the factory blows up
        assert len(surfaced) == 1

    def test_no_popup_factory_is_a_valid_dry_run_mode(self):
        calls = {"n": 0}

        def fetch(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return _state([])
            return _state([{"id": 1, "kind": "system", "title": "t", "detail": "d"}])

        surfacer = proactive.ProactiveSurfacer("http://x", None, fetch=fetch)
        surfacer.refresh()
        surfaced = surfacer.refresh()
        assert len(surfaced) == 1  # still reports what WOULD have surfaced

    def test_seen_set_is_bounded(self):
        surfacer = proactive.ProactiveSurfacer("http://x", lambda item: None, fetch=lambda url: _state([]))
        surfacer._seen = set(range(500))
        surfacer._primed = True
        surfacer.refresh()
        assert len(surfacer._seen) <= surfacer._MAX_SEEN

    def test_custom_allowed_kinds_override(self):
        popped = []
        calls = {"n": 0}

        def fetch(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return _state([])
            return _state([{"id": 1, "kind": "market", "title": "t", "detail": "d"}])

        surfacer = proactive.ProactiveSurfacer(
            "http://x", popped.append, fetch=fetch, allowed_kinds=("market",)
        )
        surfacer.refresh()
        surfaced = surfacer.refresh()
        assert len(surfaced) == 1


# --------------------------------------------------------------------------- #
# on_event: the SSE hub sink shape
# --------------------------------------------------------------------------- #

class TestOnEvent:
    def test_ignores_non_state_change_events(self):
        surfacer = proactive.ProactiveSurfacer("http://x", None, fetch=lambda url: _state([]))
        surfacer.on_event({"type": "ping"})
        time.sleep(0.05)
        assert surfacer._primed is False  # refresh() never ran

    def test_ignores_non_alerts_sections(self):
        surfacer = proactive.ProactiveSurfacer("http://x", None, fetch=lambda url: _state([]))
        surfacer.on_event({"type": "state_change", "section": "watchlist"})
        time.sleep(0.05)
        assert surfacer._primed is False

    def test_alerts_state_change_triggers_a_background_refresh(self):
        done = threading.Event()

        def fetch(url):
            done.set()
            return _state([])

        surfacer = proactive.ProactiveSurfacer("http://x", None, fetch=fetch)
        surfacer.on_event({"type": "state_change", "section": "alerts"})
        assert done.wait(timeout=2)

    def test_overlapping_events_do_not_spawn_concurrent_refreshes(self):
        calls = []
        gate = threading.Event()

        def slow_fetch(url):
            calls.append(1)
            gate.wait(timeout=2)
            return _state([])

        surfacer = proactive.ProactiveSurfacer("http://x", None, fetch=slow_fetch)
        surfacer.on_event({"type": "state_change", "section": "alerts"})
        time.sleep(0.05)
        surfacer.on_event({"type": "state_change", "section": "alerts"})  # dropped -- one already in flight
        gate.set()
        time.sleep(0.2)
        assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Popup construction (window-creation seam, no real GUI)
# --------------------------------------------------------------------------- #

class _FakeWindow:
    def __init__(self, title, **kwargs):
        self.title = title
        self.kwargs = kwargs
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


class _FakeWebviewModule:
    def __init__(self):
        self.windows = []

    def create_window(self, title, **kwargs):
        win = _FakeWindow(title, **kwargs)
        self.windows.append(win)
        return win


class TestPopupFactory:
    def test_builds_a_real_small_frameless_window(self):
        fake = _FakeWebviewModule()
        factory = proactive.default_popup_factory(fake)
        factory({"kind": "system", "title": "ATLAS run started", "detail": "managed run"})
        assert len(fake.windows) == 1
        win = fake.windows[0]
        assert win.kwargs["width"] == proactive._POPUP_WIDTH
        assert win.kwargs["frameless"] is True
        assert win.kwargs["resizable"] is False
        assert "ATLAS run started" in win.kwargs["html"]

    def test_only_control_exposed_is_dismiss(self):
        fake = _FakeWebviewModule()
        factory = proactive.default_popup_factory(fake)
        factory({"kind": "system", "title": "t", "detail": "d"})
        bridge = fake.windows[0].kwargs["js_api"]
        public = [m for m in dir(bridge) if not m.startswith("_")]
        assert public == ["attach", "dismiss"]  # attach is internal wiring, not JS-facing intent

    def test_dismiss_destroys_the_window(self):
        fake = _FakeWebviewModule()
        factory = proactive.default_popup_factory(fake)
        factory({"kind": "system", "title": "t", "detail": "d"})
        win = fake.windows[0]
        bridge = win.kwargs["js_api"]
        assert bridge.dismiss() is True
        assert win.destroyed is True

    def test_dismiss_before_attach_is_honest_false(self):
        bridge = proactive._PopupBridge()
        assert bridge.dismiss() is False

    def test_html_is_escaped(self):
        fake = _FakeWebviewModule()
        factory = proactive.default_popup_factory(fake)
        factory({"kind": "system", "title": "<script>evil()</script>", "detail": "d"})
        html = fake.windows[0].kwargs["html"]
        assert "<script>evil()" not in html
        assert "&lt;script&gt;" in html

    def test_successive_popups_stagger_vertically(self):
        fake = _FakeWebviewModule()
        factory = proactive.default_popup_factory(fake)
        factory({"kind": "system", "title": "a", "detail": "d"})
        factory({"kind": "system", "title": "b", "detail": "d"})
        ys = [w.kwargs["y"] for w in fake.windows]
        assert ys[1] > ys[0]


class TestCornerPlacement:
    def test_honest_none_without_appkit(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def blocked(name, *a, **k):
            if name == "AppKit":
                raise ImportError("no AppKit here")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", blocked)
        assert proactive._corner_x(320) is None
