"""Hermetic tests for the v5.19 desktop shell backend.

Uses the documented test seam: a fake ``webview_loader`` (no pywebview, no
GUI). Window-state memory is exercised against a real in-memory StateStore;
the notifier runs against a real hermetic ``run_server``; ``launch()`` runs
end-to-end with ``DOURMOUSE_LEARN=0`` / ``DOURMOUSE_LIVE=0`` and a tmp
workspace so nothing touches the working tree or the network.
"""

import threading

import pytest

from dourmouse import desktop
from dourmouse.artifacts import ArtifactStore
from dourmouse.dispatch import DispatchRegistry
from dourmouse.google_auth import AuthStore
from dourmouse.message_bus import MessageBus
from dourmouse.state_store import StateStore
from dourmouse.webui import run_server


class _FakeEvent:
    """Minimal pywebview-style event supporting += / -=."""

    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def fire(self):
        for handler in list(self._handlers):
            handler()


class _FakeWindow:
    def __init__(self, title, url, **kwargs):
        self.title = title
        self.url = url
        self.width = kwargs.get("width", 1000)
        self.height = kwargs.get("height", 700)
        self.x = kwargs.get("x", 0)
        self.y = kwargs.get("y", 0)
        self.events = type("Events", (), {"closed": _FakeEvent()})()
        self.js = []
        self.shown = False

    def show(self):
        self.shown = True

    def evaluate_js(self, js):
        self.js.append(js)
        return True


class _FakeWebview:
    def __init__(self):
        self.windows = []

    def create_window(self, title, url, **kwargs):
        win = _FakeWindow(title, url, **kwargs)
        self.windows.append(win)
        return win

    def start(self):
        return None


@pytest.fixture()
def bridge_and_store():
    store = StateStore()  # in-memory
    bridge = desktop.DesktopBridge(
        _FakeWindow("map", "http://x"), _FakeWebview(), "http://x",
        state=store,
    )
    return bridge, store


# -- typed window-state IPC ---------------------------------------------- #


def test_window_state_roundtrip(bridge_and_store):
    bridge, _store = bridge_and_store
    assert bridge.window_state() == {}  # nothing saved yet -> honest {}
    assert bridge.set_window_state(
        width=1440, height=900, x=10, y=20, maximized=True) is True
    state = bridge.window_state()
    assert state == {"width": 1440, "height": 900, "x": 10, "y": 20,
                     "maximized": True}


def test_window_state_rejects_garbage(bridge_and_store):
    bridge, _store = bridge_and_store
    # width below the sanity clamp and a non-int height -> nothing valid
    assert bridge.set_window_state(width=10, height="nope") is False
    assert bridge.window_state() == {}
    # mixed: valid width + negative x (multi-monitor) kept, junk dropped
    assert bridge.set_window_state(
        width=1200, height="nope", x=-40, maximized="yes") is True
    assert bridge.window_state() == {"width": 1200, "x": -40}


def test_navigate_accepts_only_validated_hash_routes(bridge_and_store):
    bridge, _store = bridge_and_store
    win = _FakeWindow("main", "http://x")
    bridge.attach_main_window(win)
    assert bridge.navigate("#/atlas") is True
    assert win.js == ['location.hash = "#/atlas"']
    assert bridge.navigate("#/atlas/research/example") is True
    for hostile in (None, "", "javascript:alert(1)", "#/atlas/../../etc",
                    "http://evil.example.com", "atlas"):
        assert bridge.navigate(hostile) is False, f"navigate accepted {hostile!r}"


def test_navigate_without_window_is_false(bridge_and_store):
    bridge, _store = bridge_and_store
    assert bridge.navigate("#/atlas") is False  # no main window attached yet


# -- native alert notifications ------------------------------------------ #


@pytest.fixture()
def state_server():
    srv = run_server(
        DispatchRegistry(),
        host="127.0.0.1",
        port=0,
        client=None,
        config=None,
        live_polling=False,
        memory=None,
        bus=MessageBus(),
        reporting=False,
        neuro=False,
        artifacts=ArtifactStore(),
        freebuff_events=False,
        state=StateStore(),  # in-memory
        auth=AuthStore(),  # in-memory
    )
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", srv.state
    srv.shutdown()
    srv.server_close()


def _wait_until(predicate, timeout=5.0):
    """Poll until predicate() is true (the notifier refreshes async)."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_notifier_primes_then_notifies_new_alerts(state_server):
    base, store = state_server
    calls = []
    notifier = desktop.DesktopNotifier(
        base, notifier=lambda title, body: calls.append((title, body)))
    store.add_alert("world", "old event", detail="pre-existing")
    notifier.refresh()  # first refresh seeds the seen set — no spam at launch
    assert calls == []
    store.add_alert("atlas", "new opportunity", detail="the headline")
    notifier.on_event({"type": "state_change", "section": "alerts"})
    # the refresh runs on a daemon thread (never blocking the SSE hub)
    assert _wait_until(lambda: len(calls) == 1)
    assert calls == [("new opportunity", "the headline")]
    # the same broadcast again must not re-notify (dedupe by alert id)
    notifier.on_event({"type": "state_change", "section": "alerts"})
    import time

    time.sleep(0.3)  # let the (deduped) background refresh finish
    assert calls == [("new opportunity", "the headline")]


def test_notifier_ignores_other_sections_and_events(state_server):
    base, _store = state_server
    calls = []
    notifier = desktop.DesktopNotifier(
        base, notifier=lambda title, body: calls.append((title, body)))
    notifier.on_event({"type": "state_change", "section": "watchlist"})
    notifier.on_event({"type": "freebuff_watch", "state": "online"})
    assert calls == []


# -- launch() end-to-end with the fake webview seam ---------------------- #


@pytest.fixture()
def hermetic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_LEARN", "0")
    monkeypatch.setenv("DOURMOUSE_LIVE", "0")
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("DOURMOUSE_DESKTOP_NOTIFICATIONS", "1")
    monkeypatch.setattr("dourmouse.webui._resolve_server_config", lambda _c: None)
    return tmp_path


def test_launch_restores_geometry_and_returns(hermetic_env, tmp_path):
    # launch() mounts the DEFAULT state store (<workspace>/state/dourmouse.db),
    # so the saved geometry must be written there before the window opens.
    store = StateStore(tmp_path / "state" / "dourmouse.db")
    store.set_pref("desktop.window",
                   {"width": 1280, "height": 800, "x": 5, "y": 6}, owner="*")
    store.close()
    fake = _FakeWebview()
    code = desktop.launch(
        registry=DispatchRegistry(),
        port=0,
        webview_loader=lambda: fake,
        live_polling=False,
        open_all_windows=False,
    )
    assert code == 0
    main = fake.windows[1]  # [0] is the hidden Agent Map window
    assert main.width == 1280  # restored from the prefs store
    assert main.height == 800
    assert main.url.startswith("http://127.0.0.1:")


def test_launch_deep_link_loads_validated_route(hermetic_env):
    fake = _FakeWebview()
    code = desktop.launch(
        registry=DispatchRegistry(),
        port=0,
        webview_loader=lambda: fake,
        live_polling=False,
        open_all_windows=False,
        deep_link="dourmouse://atlas/research",
    )
    assert code == 0
    main = fake.windows[1]
    assert main.url.endswith("#/atlas/research")


def test_launch_ignores_hostile_deep_link(hermetic_env):
    fake = _FakeWebview()
    code = desktop.launch(
        registry=DispatchRegistry(),
        port=0,
        webview_loader=lambda: fake,
        live_polling=False,
        open_all_windows=False,
        deep_link="dourmouse://shell/rm%20-rf",
    )
    assert code == 0
    main = fake.windows[1]
    assert "#" not in main.url  # the allow-list gate dropped it
