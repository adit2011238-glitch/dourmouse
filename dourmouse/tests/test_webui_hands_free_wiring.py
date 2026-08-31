"""dourmouse/webui.py's run_server() hands-free auto-start wiring
(v13.4) — the real "auto-start, no button press" half of the three
requested pieces (dourmouse/hands_free.py itself is covered separately
in test_hands_free.py; this file covers run_server() actually starting
it, honestly reporting when it can't, and never crashing server startup
either way).

Deliberately never lets a real HandsFreeController touch real hardware
here (dourmouse.hands_free.HandsFreeController is monkeypatched to a
fake BEFORE run_server() is called) -- opening a real microphone stream
from a test process is exactly the failure mode
dourmouse/tests/test_wakeword.py's own fake-stream discipline exists to
avoid, and this module's own wiring is what test_hands_free.py already
covers with real synthetic logic; this file only needs to prove
run_server() calls it correctly.
"""

from __future__ import annotations

import dourmouse.hands_free as hands_free_module
from dourmouse.tests.test_webui import _echo_registry
from dourmouse.webui import run_server


class _FakeController:
    """Records how run_server() constructed and drove it -- mirrors the
    real HandsFreeController's public surface (dispatch_fn, start,
    running) closely enough for this wiring-level test."""

    instances: list["_FakeController"] = []

    def __init__(self, *, dispatch_fn, **_kw):
        self.dispatch_fn = dispatch_fn
        self.start_calls = 0
        self.started = False
        type(self).instances.append(self)

    def start(self):
        self.start_calls += 1
        self.started = True
        return True, "listening"

    def stop(self):
        self.started = False

    @property
    def running(self):
        return self.started


def _run_test_server(monkeypatch, *, enabled: bool):
    _FakeController.instances = []
    monkeypatch.setattr(hands_free_module, "HandsFreeController", _FakeController)
    monkeypatch.setenv("DOURMOUSE_HANDS_FREE", "1" if enabled else "0")
    srv = run_server(_echo_registry(), port=0, client=None, config=None)
    return srv


class TestHandsFreeAutoStart:
    def test_disabled_by_default_status_is_honest(self, monkeypatch):
        srv = _run_test_server(monkeypatch, enabled=False)
        try:
            assert srv.hands_free is None
            assert srv.hands_free_status["enabled"] is False
            assert "off" in srv.hands_free_status["reason"].lower()
            assert _FakeController.instances == []  # never even constructed
        finally:
            srv.server_close()

    def test_enabled_starts_a_real_controller_instance(self, monkeypatch):
        srv = _run_test_server(monkeypatch, enabled=True)
        try:
            assert srv.hands_free is not None
            assert srv.hands_free.start_calls == 1
            assert srv.hands_free_status["enabled"] is True
            assert len(_FakeController.instances) == 1
        finally:
            srv.server_close()

    def test_controller_construction_failure_never_crashes_server_startup(self, monkeypatch):
        def boom(**_kw):
            raise RuntimeError("no audio backend on this machine")

        monkeypatch.setattr(hands_free_module, "HandsFreeController", boom)
        monkeypatch.setenv("DOURMOUSE_HANDS_FREE", "1")
        srv = run_server(_echo_registry(), port=0, client=None, config=None)
        try:
            assert srv.hands_free is None
            assert srv.hands_free_status["enabled"] is False
            assert "startup failed" in srv.hands_free_status["reason"]
            assert "no audio backend" in srv.hands_free_status["reason"]
        finally:
            srv.server_close()

    def test_dispatch_fn_calls_the_real_session_under_the_real_lock(self, monkeypatch):
        """The one real integration point: the closure run_server() builds
        must call server.session.ask() under server.session_lock -- the
        exact same real dispatch path a typed message goes through, not a
        second implementation. Verified by swapping in a fake session
        whose ask() records that it was called while the lock was held."""
        srv = _run_test_server(monkeypatch, enabled=True)
        try:
            calls = []

            class _FakeSession:
                confirmation_gate = None

                def ask(self, prompt, max_turns=8, voice=False, screen="HOME"):
                    calls.append({
                        "prompt": prompt, "voice": voice, "screen": screen,
                        "lock_held": srv.session_lock.locked(),
                    })
                    return {"final_text": "a real spoken reply"}

            srv.session = _FakeSession()
            reply = srv.hands_free.dispatch_fn("what's the weather")
            assert reply == "a real spoken reply"
            assert len(calls) == 1
            assert calls[0]["prompt"] == "what's the weather"
            assert calls[0]["voice"] is True  # shaped for speech, not markdown
            assert calls[0]["screen"] == "HANDS_FREE"
            assert calls[0]["lock_held"] is True
        finally:
            srv.server_close()

    def test_dispatch_fn_honest_fallback_when_no_final_text(self, monkeypatch):
        srv = _run_test_server(monkeypatch, enabled=True)
        try:
            class _FakeSession:
                confirmation_gate = None

                def ask(self, prompt, max_turns=8, voice=False, screen="HOME"):
                    return {"final_text": ""}

            srv.session = _FakeSession()
            reply = srv.hands_free.dispatch_fn("...")
            assert reply == "I didn't get a reply for that."
        finally:
            srv.server_close()
