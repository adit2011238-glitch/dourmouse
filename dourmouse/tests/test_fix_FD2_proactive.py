"""FD2 fix P5-39: the alert surfacers are primed at start-up, so the first
alert after launch is not swallowed as 'backlog'."""

from __future__ import annotations

import inspect
import threading

from dourmouse import desktop, proactive


def _state(alerts):
    return {"alerts": alerts}


def test_alert_arriving_after_prime_is_surfaced_not_swallowed():
    popped = []
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        if calls["n"] == 1:  # the start-up priming fetch: one old backlog alert
            return _state([{"id": 1, "kind": "system", "title": "old", "detail": ""}])
        return _state([
            {"id": 1, "kind": "system", "title": "old", "detail": ""},
            {"id": 2, "kind": "system", "title": "first after launch", "detail": ""},
        ])

    s = proactive.ProactiveSurfacer("http://x", popped.append, fetch=fetch)
    s.prime()
    assert popped == []  # backlog never replayed
    done = threading.Event()
    s._popup_factory = lambda a: (popped.append(a), done.set())
    s.on_event({"type": "state_change", "section": "alerts"})
    assert done.wait(timeout=2)
    assert [a["id"] for a in popped] == [2]


def test_prime_is_idempotent_and_does_not_surface():
    popped = []
    s = proactive.ProactiveSurfacer(
        "http://x", popped.append,
        fetch=lambda url: _state([{"id": 1, "kind": "system", "title": "t", "detail": ""}]),
    )
    s.prime()
    s.prime()
    assert popped == []


def test_failed_prime_leaves_surfacer_unprimed():
    s = proactive.ProactiveSurfacer("http://x", None, fetch=lambda url: None)
    s.prime()
    assert s._primed is False


def test_desktop_notifier_has_prime():
    n = desktop.DesktopNotifier("http://127.0.0.1:1")
    assert callable(getattr(n, "prime", None))


def test_desktop_wiring_primes_both_consumers():
    src = inspect.getsource(desktop)
    assert "notifier.prime" in src
    assert "surfacer.prime" in src
