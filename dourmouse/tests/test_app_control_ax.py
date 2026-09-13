"""Tests for dourmouse/app_control_ax.py — real macOS Accessibility-API
app control (Phase 2, 2026-09-13). Two kinds of coverage, deliberately
kept separate:

- Mocked-boundary tests: fake stand-ins for the pyobjc AS/Quartz/
  NSWorkspace calls, so the module's own logic (menu-tree walking,
  error mapping, argument validation, the dry-run-surfaces-real-
  blockers consistency) is exercised deterministically, on any machine,
  regardless of whether THIS process happens to have Accessibility
  trust when the suite runs.
- Real, live, non-mocked tests (TestRealLiveIntegration): the handful of
  things genuinely safe to exercise for real on any Mac running this
  suite — ax_trusted() answering honestly, and activate_app_fast
  actually working against Finder (live-verified this session to need
  NO Accessibility permission at all, unlike everything else here).
"""

from __future__ import annotations

import sys

import pytest

from dourmouse import app_control_ax as ax


class _FakeRunningApp:
    def __init__(self, name: str, pid: int) -> None:
        self._name = name
        self._pid = pid
        self.activate_calls: list[int] = []
        self.activate_result = True

    def localizedName(self):
        return self._name

    def processIdentifier(self):
        return self._pid

    def activateWithOptions_(self, options):
        self.activate_calls.append(options)
        return self.activate_result


class _FakeWorkspace:
    def __init__(self, apps: list[_FakeRunningApp]) -> None:
        self._apps = apps

    def runningApplications(self):
        return self._apps


class _FakeAXElement:
    """A minimal fake AXUIElement: a dict of attribute -> value, real
    enough for the module's own _copy_attr/_menu_children/_element_title
    helpers to walk exactly like a real one."""

    def __init__(self, attrs: dict[str, object] | None = None) -> None:
        self.attrs = attrs or {}


class _FakeAS:
    """Fake ApplicationServices module surface — only the names
    app_control_ax.py actually touches."""

    kAXErrorSuccess = 0
    kAXWindowsAttribute = "AXWindows"
    kAXTitleAttribute = "AXTitle"
    kAXMenuBarAttribute = "AXMenuBar"
    kAXChildrenAttribute = "AXChildren"
    kAXPressAction = "AXPress"
    kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"

    def __init__(self, trusted: bool = False) -> None:
        self._trusted = trusted
        self.performed_actions: list[tuple[object, str]] = []
        self.perform_result = 0  # kAXErrorSuccess

    def AXIsProcessTrusted(self):
        return self._trusted

    def AXIsProcessTrustedWithOptions(self, options):
        return self._trusted

    def AXUIElementCreateApplication(self, pid):
        return _FakeAXElement({"__pid__": pid})

    def AXUIElementCopyAttributeValue(self, element, attribute, _out):
        if not self._trusted:
            return -25211, None  # kAXErrorAPIDisabled
        if attribute not in element.attrs:
            return -25204, None  # kAXErrorAttributeUnsupported
        return 0, element.attrs[attribute]

    def AXUIElementPerformAction(self, element, action):
        self.performed_actions.append((element, action))
        return self.perform_result


class _FakeQuartz:
    kCGHIDEventTap = "hid"

    def __init__(self) -> None:
        self.posted: list[tuple] = []
        self.created: list[tuple] = []

    def CGEventCreateKeyboardEvent(self, source, keycode, keydown):
        ev = {"keycode": keycode, "keydown": keydown, "flags": None, "unicode": None}
        self.created.append(ev)
        return ev

    def CGEventSetFlags(self, event, flags):
        event["flags"] = flags

    def CGEventKeyboardSetUnicodeString(self, event, length, text):
        event["unicode"] = text

    def CGEventPost(self, tap, event):
        self.posted.append((tap, event))


@pytest.fixture
def fake_darwin(monkeypatch):
    monkeypatch.setattr(ax, "sys", sys)
    monkeypatch.setattr(sys, "platform", "darwin", raising=False)


@pytest.fixture
def fake_as(monkeypatch, fake_darwin):
    """Wires a fresh fake ApplicationServices module in as ax._AS,
    untrusted by default (matching this real dev machine's actual
    current state, and the safer default to test against)."""
    fake = _FakeAS(trusted=False)
    monkeypatch.setattr(ax, "_AS", fake)
    return fake


@pytest.fixture
def fake_quartz(monkeypatch):
    fake = _FakeQuartz()
    monkeypatch.setattr(ax, "_Quartz", fake)
    return fake


@pytest.fixture
def fake_workspace(monkeypatch):
    """Wires a fake NSWorkspace with a controllable running-app list.
    Returns the list so tests can mutate it, and the workspace class for
    monkeypatching sharedWorkspace()."""
    apps: list[_FakeRunningApp] = []
    workspace = _FakeWorkspace(apps)

    class _FakeWorkspaceClass:
        @staticmethod
        def sharedWorkspace():
            return workspace

    monkeypatch.setattr(ax, "NSWorkspace", _FakeWorkspaceClass)
    return apps


class TestPlatformAndImportGates:
    def test_refuses_honestly_on_non_macos(self, monkeypatch):
        monkeypatch.setattr(ax.sys, "platform", "linux")
        with pytest.raises(ax.AXControlError, match="NOT CONFIGURED"):
            ax.activate_app_fast("Finder")

    def test_ax_trusted_is_false_without_raising_on_non_macos(self, monkeypatch):
        """A pure probe must never raise, even on a platform where this
        feature plainly doesn't apply."""
        monkeypatch.setattr(ax.sys, "platform", "linux")
        assert ax.ax_trusted() is False

    def test_refuses_honestly_when_pyobjc_is_not_importable(self, monkeypatch, fake_darwin):
        monkeypatch.setattr(ax, "_AS", None)
        monkeypatch.setattr(ax, "_ax_import_error", ImportError("no module named ApplicationServices"))
        with pytest.raises(ax.AXControlError, match="NOT CONFIGURED"):
            ax.activate_app_fast("Finder")


class TestPidResolution:
    def test_finds_running_app_case_insensitively(self, fake_as, fake_workspace):
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        fake_workspace.append(_FakeRunningApp("Safari", 222))
        assert ax._pid_for_app("finder") == 111
        assert ax._pid_for_app("SAFARI") == 222

    def test_honest_not_running_error_lists_what_actually_is(self, fake_as, fake_workspace):
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        fake_workspace.append(_FakeRunningApp("Safari", 222))
        with pytest.raises(ax.AXControlError, match="NOT RUNNING") as exc:
            ax._pid_for_app("Microsoft Word")
        assert "Finder" in str(exc.value)
        assert "Safari" in str(exc.value)

    def test_empty_app_name_is_a_clear_error(self, fake_as, fake_workspace):
        with pytest.raises(ax.AXControlError, match="app_name is required"):
            ax._pid_for_app("")


class TestActivateAppFast:
    def test_activates_without_needing_ax_trust(self, fake_as, fake_workspace):
        """The whole point of this function: works even though fake_as
        defaults to untrusted -- live-verified this session that the
        real NSRunningApplication API needs no Accessibility permission
        at all, unlike every other function in this module."""
        app = _FakeRunningApp("Finder", 111)
        fake_workspace.append(app)
        result = ax.activate_app_fast("Finder")
        assert result == "ACTIVATED: Finder"
        assert app.activate_calls == [0]

    def test_dry_run_does_not_actually_activate(self, fake_as, fake_workspace):
        app = _FakeRunningApp("Finder", 111)
        fake_workspace.append(app)
        result = ax.activate_app_fast("Finder", dry_run=True)
        assert "DRY RUN" in result
        assert app.activate_calls == []

    def test_dry_run_still_surfaces_a_real_not_running_error(self, fake_as, fake_workspace):
        """Consistency this module deliberately keeps throughout: dry_run
        skips only the FINAL action, never a real environmental check
        that would have refused it anyway (mirrors app_control.py's own
        stated dry-run contract)."""
        with pytest.raises(ax.AXControlError, match="NOT RUNNING"):
            ax.activate_app_fast("Nonexistent App", dry_run=True)

    def test_macos_refusal_is_a_real_honest_error_not_a_silent_false(self, fake_as, fake_workspace):
        app = _FakeRunningApp("Finder", 111)
        app.activate_result = False
        fake_workspace.append(app)
        with pytest.raises(ax.AXControlError, match="refused to activate"):
            ax.activate_app_fast("Finder")


class TestListWindowsAx:
    def test_reads_real_window_titles_via_ax_attributes(self, fake_as, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        win1 = _FakeAXElement({"AXTitle": "Downloads"})
        win2 = _FakeAXElement({"AXTitle": "Documents"})
        # AXUIElementCreateApplication returns a fresh element each call in
        # the real API; wire this app's own element to report the windows.
        app_element = _FakeAXElement({"AXWindows": [win1, win2]})
        fake_as.AXUIElementCreateApplication = lambda pid: app_element
        assert ax.list_windows_ax("Finder") == ["Downloads", "Documents"]

    def test_untrusted_process_gets_the_real_permission_error(self, fake_as, fake_workspace):
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        with pytest.raises(ax.AXControlError, match="Accessibility permission"):
            ax.list_windows_ax("Finder")

    def test_a_window_that_refuses_its_own_title_still_counts_as_a_window(self, fake_as, fake_workspace):
        """A window existing but not answering AXTitle is still a real
        window -- reported as an empty title, not silently dropped from
        the list (which would make the count lie)."""
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        untitled = _FakeAXElement({})  # no AXTitle key at all
        app_element = _FakeAXElement({"AXWindows": [untitled]})
        fake_as.AXUIElementCreateApplication = lambda pid: app_element
        assert ax.list_windows_ax("Finder") == [""]


class TestFindAndClickMenuItemAx:
    def _build_menu_tree(self, fake_as):
        """File > Export > PDF..., a real nested-submenu shape, built the
        same way find_menu_item_ax expects to walk it: a menu-bar-item's
        AXChildren is [the pulldown menu], whose AXChildren are the real
        items."""
        pdf_item = _FakeAXElement({"AXTitle": "PDF..."})
        export_menu = _FakeAXElement({"AXChildren": [pdf_item]})
        export_item = _FakeAXElement({"AXTitle": "Export", "AXChildren": [export_menu]})
        new_window_item = _FakeAXElement({"AXTitle": "New Window"})
        file_pulldown = _FakeAXElement({"AXChildren": [new_window_item, export_item]})
        file_menu_bar_item = _FakeAXElement({"AXTitle": "File", "AXChildren": [file_pulldown]})
        menu_bar = _FakeAXElement({"AXChildren": [file_menu_bar_item]})
        app_element = _FakeAXElement({"AXMenuBar": menu_bar})
        fake_as.AXUIElementCreateApplication = lambda pid: app_element
        return pdf_item, new_window_item

    def test_finds_a_top_level_item(self, fake_as, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        _pdf, new_window_item = self._build_menu_tree(fake_as)
        found = ax.find_menu_item_ax("Finder", ["File", "New Window"])
        assert found is new_window_item

    def test_finds_a_nested_submenu_item(self, fake_as, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        pdf_item, _new = self._build_menu_tree(fake_as)
        found = ax.find_menu_item_ax("Finder", ["File", "Export", "PDF..."])
        assert found is pdf_item

    def test_wrong_item_name_lists_what_is_really_there(self, fake_as, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        self._build_menu_tree(fake_as)
        with pytest.raises(ax.AXControlError, match="MENU ITEM NOT FOUND") as exc:
            ax.find_menu_item_ax("Finder", ["File", "Close Window"])
        assert "New Window" in str(exc.value)
        assert "Export" in str(exc.value)

    def test_menu_path_too_short_is_a_clear_error(self, fake_as, fake_workspace):
        with pytest.raises(ax.AXControlError, match="menu_path needs at least"):
            ax.find_menu_item_ax("Finder", ["File"])

    def test_click_performs_the_real_ax_press_action(self, fake_as, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        _pdf, new_window_item = self._build_menu_tree(fake_as)
        result = ax.click_menu_item_ax("Finder", ["File", "New Window"])
        assert result == "CLICKED File > New Window in Finder"
        assert fake_as.performed_actions == [(new_window_item, "AXPress")]

    def test_click_dry_run_confirms_existence_but_never_presses(self, fake_as, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        self._build_menu_tree(fake_as)
        result = ax.click_menu_item_ax("Finder", ["File", "New Window"], dry_run=True)
        assert "DRY RUN" in result
        assert fake_as.performed_actions == []

    def test_click_dry_run_still_surfaces_a_real_not_found_error(self, fake_as, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        self._build_menu_tree(fake_as)
        with pytest.raises(ax.AXControlError, match="MENU ITEM NOT FOUND"):
            ax.click_menu_item_ax("Finder", ["File", "Nope"], dry_run=True)

    def test_a_real_press_failure_is_reported_honestly_not_as_success(self, fake_as, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        self._build_menu_tree(fake_as)
        fake_as.perform_result = -25202  # kAXErrorCannotComplete
        with pytest.raises(ax.AXControlError, match="could not click"):
            ax.click_menu_item_ax("Finder", ["File", "New Window"])


class TestSyntheticEventsNeedExplicitTrustCheck:
    """The real, live-caught finding this session: CGEventPost has no
    error return at all, and a real empirical test (a genuine empty
    TextEdit document, a real CGEventPost call while untrusted, then
    reading the document back) confirmed macOS silently drops the event
    rather than erroring -- so these two functions check ax_trusted()
    explicitly instead of trusting the API's own (nonexistent) signal."""

    def test_press_key_refuses_honestly_when_untrusted(self, fake_as, fake_quartz, fake_workspace):
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        with pytest.raises(ax.AXControlError, match="Accessibility permission"):
            ax.press_key_ax("Finder", "escape")
        assert fake_quartz.posted == []

    def test_send_keystrokes_refuses_honestly_when_untrusted(self, fake_as, fake_quartz, fake_workspace):
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        with pytest.raises(ax.AXControlError, match="Accessibility permission"):
            ax.send_keystrokes_ax("Finder", "hello")
        assert fake_quartz.posted == []

    def test_press_key_dry_run_still_surfaces_the_real_blocker(self, fake_as, fake_quartz, fake_workspace):
        """Consistency, not a loophole: a dry run must not claim things
        look fine when the real call would fail for an environmental
        reason (matches activate_app_fast's own convention)."""
        with pytest.raises(ax.AXControlError, match="Accessibility permission"):
            ax.press_key_ax("Finder", "escape", dry_run=True)

    def test_send_keystrokes_dry_run_still_surfaces_the_real_blocker(self, fake_as, fake_quartz, fake_workspace):
        with pytest.raises(ax.AXControlError, match="Accessibility permission"):
            ax.send_keystrokes_ax("Finder", "hello", dry_run=True)

    def test_press_key_actually_posts_real_events_once_trusted(self, fake_as, fake_quartz, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        result = ax.press_key_ax("Finder", "escape")
        assert result == "PRESSED escape in Finder"
        assert len(fake_quartz.posted) == 2  # keydown + keyup

    def test_press_key_with_modifiers_sets_real_cgevent_flags(self, fake_as, fake_quartz, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        ax.press_key_ax("Finder", "tab", modifiers=["command", "shift"])
        posted_events = [ev for _tap, ev in fake_quartz.posted]
        expected_flags = ax._MODIFIER_FLAGS["command"] | ax._MODIFIER_FLAGS["shift"]
        assert all(ev["flags"] == expected_flags for ev in posted_events)

    def test_unknown_key_is_a_clear_error(self, fake_as, fake_quartz, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        with pytest.raises(ax.AXControlError, match="unknown key"):
            ax.press_key_ax("Finder", "not_a_real_key")

    def test_unknown_modifier_is_a_clear_error(self, fake_as, fake_quartz, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        with pytest.raises(ax.AXControlError, match="unknown modifier"):
            ax.press_key_ax("Finder", "tab", modifiers=["not_a_real_modifier"])

    def test_send_keystrokes_actually_posts_the_real_text(self, fake_as, fake_quartz, fake_workspace):
        fake_as._trusted = True
        fake_workspace.append(_FakeRunningApp("Finder", 111))
        result = ax.send_keystrokes_ax("Finder", "hello world")
        assert result == "TYPED into Finder: 11 character(s)"
        assert any(ev["unicode"] == "hello world" for _tap, ev in fake_quartz.posted)


class TestRequestAxTrust:
    def test_wraps_the_real_prompting_api_and_returns_current_state(self, fake_as):
        fake_as._trusted = False
        assert ax.request_ax_trust() is False
        fake_as._trusted = True
        assert ax.request_ax_trust() is True


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only feature")
class TestRealLiveIntegration:
    """Genuinely real, non-mocked calls against the actual machine
    running this suite — only the handful of things safe to exercise for
    real regardless of this process's own Accessibility trust state."""

    def test_ax_trusted_answers_honestly_without_raising(self):
        assert isinstance(ax.ax_trusted(), bool)

    def test_activate_app_fast_really_works_against_finder(self):
        """Live-verified this session: needs no Accessibility permission
        at all. Finder is always running on any real Mac, so this is
        safe to exercise for real in CI on a real Mac runner too."""
        result = ax.activate_app_fast("Finder")
        assert result == "ACTIVATED: Finder"

    def test_activate_app_fast_dry_run_is_side_effect_free(self):
        result = ax.activate_app_fast("Finder", dry_run=True)
        assert "DRY RUN" in result

    def test_honest_not_running_error_for_a_definitely_fake_app_name(self):
        with pytest.raises(ax.AXControlError, match="NOT RUNNING"):
            ax.activate_app_fast("Definitely Not A Real App Name XYZ123")
