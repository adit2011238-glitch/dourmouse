"""FS1 fixes in dourmouse/app_control_ax.py.

P5-1: the AX fast path must honour app_control's blocklist (by name, and by
the real bundle id of the running app so a localised name cannot dodge it).
P5-2: a menu path that goes through a plain leaf item is a clear
AXControlError, not an IndexError.
P5-3: keystrokes are only posted once the target is really frontmost, and
long text is sent in chunks the event can actually carry.
"""

from __future__ import annotations

import sys

import pytest

from dourmouse import app_control_ax as ax


class _App:
    def __init__(self, name: str, pid: int, bundle: str = "com.example.app") -> None:
        self._name, self._pid, self._bundle = name, pid, bundle
        self.activate_calls = 0
        self.terminate_calls = 0
        self.ws = None

    def localizedName(self):
        return self._name

    def processIdentifier(self):
        return self._pid

    def bundleIdentifier(self):
        return self._bundle

    def activateWithOptions_(self, _opts):
        self.activate_calls += 1
        if self.ws is not None and self.ws.follow_activation:
            self.ws.front = self
        return True

    def terminate(self):
        self.terminate_calls += 1
        return True


class _Workspace:
    def __init__(self) -> None:
        self.apps: list[_App] = []
        self.front: _App | None = None
        self.follow_activation = True

    def runningApplications(self):
        return self.apps

    def frontmostApplication(self):
        return self.front


class _AS:
    kAXErrorSuccess = 0
    kAXTitleAttribute = "AXTitle"
    kAXMenuBarAttribute = "AXMenuBar"
    kAXChildrenAttribute = "AXChildren"
    kAXPressAction = "AXPress"

    def __init__(self) -> None:
        self.performed: list[tuple[object, str]] = []
        self.menu_bar: dict | None = None

    def AXIsProcessTrusted(self):
        return True

    def AXUIElementCreateApplication(self, pid):
        return {"AXMenuBar": self.menu_bar}

    def AXUIElementCopyAttributeValue(self, element, attribute, _out):
        if attribute not in element:
            return -25204, None
        return 0, element[attribute]

    def AXUIElementPerformAction(self, element, action):
        self.performed.append((element, action))
        return 0


class _Quartz:
    kCGHIDEventTap = "hid"

    def __init__(self) -> None:
        self.posted: list[dict] = []

    def CGEventCreateKeyboardEvent(self, _src, code, down):
        return {"code": code, "down": down, "unicode": None, "len": None}

    def CGEventSetFlags(self, ev, flags):
        ev["flags"] = flags

    def CGEventKeyboardSetUnicodeString(self, ev, length, text):
        ev["unicode"], ev["len"] = text, length

    def CGEventPost(self, _tap, ev):
        self.posted.append(ev)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin", raising=False)
    ws = _Workspace()
    fake_as = _AS()
    quartz = _Quartz()

    class _WSClass:
        @staticmethod
        def sharedWorkspace():
            return ws

    monkeypatch.setattr(ax, "_AS", fake_as)
    monkeypatch.setattr(ax, "_Quartz", quartz)
    monkeypatch.setattr(ax, "NSWorkspace", _WSClass)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr(ax, "_FRONTMOST_WAIT_S", 0.05)

    def add(name, pid, bundle="com.example.app"):
        app = _App(name, pid, bundle)
        app.ws = ws
        ws.apps.append(app)
        return app

    return ws, fake_as, quartz, add


# -- P5-1 -------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["Terminal", "iTerm2", "Finder", "Passwords", "Keychain Access", "System Settings"])
def test_every_ax_action_refuses_a_blocklisted_app(env, name):
    _ws, _as, quartz, add = env
    app = add(name, 50)
    calls = [
        lambda: ax.activate_app_fast(name),
        lambda: ax.quit_app_fast(name),
        lambda: ax.send_keystrokes_ax(name, "rm -rf ~\n"),
        lambda: ax.press_key_ax(name, "return"),
        lambda: ax.click_menu_item_ax(name, ["File", "Close"]),
        lambda: ax.send_keystrokes_ax(name, "x", dry_run=True),
    ]
    for call in calls:
        with pytest.raises(ax.AXControlError, match="REFUSED"):
            call()
    assert app.activate_calls == 0
    assert app.terminate_calls == 0
    assert quartz.posted == []


def test_refusal_is_not_mistaken_for_not_configured(env):
    """general_roster falls back to AppleScript only on NOT CONFIGURED; a
    refusal must surface as a plain error instead."""
    _ws, _as, _q, add = env
    add("Terminal", 50, "com.apple.Terminal")
    with pytest.raises(ax.AXControlError) as exc:
        ax.quit_app_fast("Terminal")
    assert "NOT CONFIGURED" not in str(exc.value)


def test_blocked_by_bundle_id_even_under_a_localised_name(env):
    """On a German Mac 'System Settings' is 'Systemeinstellungen'; the name
    check alone would miss it, the bundle id does not."""
    _ws, _as, quartz, add = env
    app = add("Systemeinstellungen", 60, "com.apple.systempreferences")
    with pytest.raises(ax.AXControlError, match="REFUSED"):
        ax.activate_app_fast("Systemeinstellungen")
    with pytest.raises(ax.AXControlError, match="REFUSED"):
        ax.send_keystrokes_ax("Systemeinstellungen", "x")
    assert app.activate_calls == 0
    assert quartz.posted == []


def test_an_ordinary_app_is_still_driven(env):
    ws, _as, quartz, add = env
    app = add("TextEdit", 70, "com.apple.TextEdit")
    assert ax.activate_app_fast("TextEdit") == "ACTIVATED: TextEdit"
    assert app.activate_calls == 1
    assert ax.send_keystrokes_ax("TextEdit", "hi").startswith("TYPED into TextEdit")


# -- P5-2 -------------------------------------------------------------------- #

def test_menu_path_through_a_leaf_item_is_a_clear_not_found(env):
    _ws, fake_as, _q, add = env
    add("TextEdit", 70)
    close_item = {"AXTitle": "Close"}  # a plain item: no children at all
    file_menu = {"AXChildren": [close_item]}
    file_bar_item = {"AXTitle": "File", "AXChildren": [file_menu]}
    fake_as.menu_bar = {"AXChildren": [file_bar_item]}
    with pytest.raises(ax.AXControlError, match="MENU ITEM NOT FOUND"):
        ax.find_menu_item_ax("TextEdit", ["File", "Close", "Foo"])


# -- P5-3 -------------------------------------------------------------------- #

def test_keystrokes_are_not_posted_when_the_target_never_comes_forward(env):
    ws, _as, quartz, add = env
    term = add("Notes-ish other app", 10)
    add("Notes", 20)
    ws.follow_activation = False
    ws.front = term  # something else keeps focus
    with pytest.raises(ax.AXControlError, match="NOT FRONTMOST"):
        ax.send_keystrokes_ax("Notes", "secret text")
    assert quartz.posted == []
    with pytest.raises(ax.AXControlError, match="NOT FRONTMOST"):
        ax.press_key_ax("Notes", "return")
    assert quartz.posted == []


def test_long_text_is_typed_in_chunks_of_at_most_20_utf16_units(env):
    _ws, _as, quartz, add = env
    add("Notes", 20)
    text = "a" * 45 + "\U0001F600" + "b" * 10  # one emoji = 2 UTF-16 units
    result = ax.send_keystrokes_ax("Notes", text)
    downs = [ev for ev in quartz.posted if ev["down"]]
    assert "".join(ev["unicode"] for ev in downs) == text
    for ev in downs:
        assert ev["len"] == len(ev["unicode"].encode("utf-16-le")) // 2
        assert ev["len"] <= 20
    assert len(downs) >= 3
    assert result == f"TYPED into Notes: {len(text)} character(s)"


def test_focus_lost_mid_typing_stops_and_says_how_much_was_typed(env):
    ws, _as, quartz, add = env
    other = add("Other", 10)
    add("Notes", 20)
    real_post = quartz.CGEventPost

    def _post_then_steal(tap, ev):
        real_post(tap, ev)
        if not ev["down"]:
            ws.front = other  # the user clicked elsewhere after chunk one

    quartz.CGEventPost = _post_then_steal
    with pytest.raises(ax.AXControlError, match="20 of 45"):
        ax.send_keystrokes_ax("Notes", "c" * 45)
