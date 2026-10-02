"""Phase B1: source-level promises about electron/main.js that the running harness in
test_browser_tabs_shell.py cannot see (a fake electron has no real engine), plus the rules the
change must not have loosened. A source assertion is a weaker kind of test than a run; each one
here names the run or the live check that backs it."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
ELECTRON = _ROOT / "electron"
MAIN = (ELECTRON / "main.js").read_text(encoding="utf-8")
POLICY = (ELECTRON / "policy.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def code(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)(^|[^:'\"`])//[^\n]*", r"\1", js)


MAIN_CODE = code(MAIN)


def function_body(name: str) -> str:
    start = MAIN_CODE.index(f"function {name}(")
    depth, i = 0, MAIN_CODE.index(") {", start) + 2  # the body, not a destructured parameter
    j = i
    while True:
        depth += {"{": 1, "}": -1}.get(MAIN_CODE[j], 0)
        j += 1
        if depth == 0:
            return MAIN_CODE[i:j]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("name", ["main.js", "preload.js", "policy.js"])
def test_the_three_shell_files_parse(name):
    proc = subprocess.run([NODE, "--check", str(ELECTRON / name)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr


class TestEveryTabIsBuiltTheSameSafeWay:
    def test_there_is_one_shared_profile_and_every_view_uses_it(self):
        assert MAIN_CODE.count('"persist:dourmouse-browser"') == 1
        assert MAIN_CODE.count("new BrowserView(") == 2  # the first tab and every later one
        assert MAIN_CODE.count("new BrowserView({ webPreferences: TAB_WEB_PREFERENCES })") == 2

    def test_the_web_preferences_have_no_preload_and_keep_isolation(self):
        prefs = re.search(r"const TAB_WEB_PREFERENCES = (\{[^}]*\});", MAIN_CODE).group(1)
        assert "contextIsolation: true" in prefs and "partition: PANE_PARTITION" in prefs
        for forbidden in ("preload", "nodeIntegration", "sandbox: false", "webSecurity: false", "contextIsolation: false"):
            assert forbidden not in prefs

    def test_the_console_windows_keep_their_isolation_and_origin_lock(self):
        assert MAIN_CODE.count("contextIsolation: true") >= 4
        for target in ("win", "mainWindow", "mapWindow", "atlasWindow"):
            assert f"lockToAppOrigin({target})" in MAIN_CODE

    def test_the_chrome_user_agent_is_set_on_the_profile_and_on_each_tab(self):
        assert "session.setUserAgent(chromeUserAgent())" in MAIN_CODE
        assert "wc.setUserAgent(chromeUserAgent())" in MAIN_CODE

    def test_the_permission_policy_is_still_installed_on_the_pane_profile_and_not_widened(self):
        assert "installPermissionPolicy(paneView.webContents.session)" in MAIN_CODE
        body = function_body("installPermissionPolicy")
        # Phase B2 replaced "a tab is refused every permission" with per-site decisions
        # (test_browser_permissions_*.py). What must still hold: a tab goes to the per-site
        # handler, anything else in the pane's session is refused, and only a window outside
        # the pane falls back to the app-origin rule.
        assert "handlePaneRequest(tab, wc, permission, callback, details)" in body
        assert "handlePaneCheck(tab, wc, permission, requestingOrigin, details)" in body
        assert "paneSessions.has(ses)) return callback(false)" in body
        assert "policy.permissionAllowed(permission, origin, PORT)" in body

    def test_the_policy_files_navigation_rules_are_untouched(self):
        for name in ("isAppOrigin", "permissionAllowed", "navigationAllowed", "externalUrlAllowed"):
            assert f"function {name}(" in POLICY
        assert "function paneUrlAllowed(" in POLICY  # and the new one is the same http(s)-only rule


class TestPopupsAndNavigationInTabs:
    def test_window_open_never_creates_a_window_and_only_opens_web_addresses(self):
        body = function_body("wireTab")
        assert "setWindowOpenHandler" in body
        assert 'return { action: "deny" }' in body
        assert "policy.paneUrlAllowed(url)" in body and "tab.popupLimit.allow()" in body and "tabs.size < MAX_TABS" in body

    def test_every_route_that_loads_an_address_applies_the_same_rule(self):
        for name in ("openTab",):
            assert "policy.paneUrlAllowed(url)" in function_body(name)
        assert MAIN_CODE.count("policy.paneUrlAllowed(") >= 8
        assert "url must be a real http(s) URL" in MAIN_CODE

    def test_the_old_bridge_routes_are_still_there_for_the_browser_agent(self):
        for route in ('"/status"', '"/show"', '"/hide"', '"/navigate"', "/^\\/(back|forward|reload)$/"):
            assert route in MAIN_CODE, route
        assert "pane bridge" in MAIN_CODE and 'req.headers["sec-fetch-site"] !== undefined' in MAIN_CODE and "req.headers.origin !== undefined" in MAIN_CODE

    def test_the_new_bridge_routes_exist(self):
        for route in ('"/tabs"', '"/tabs/new"', '"/tabs/close"', '"/tabs/select"', '"/tabs/reopen"', '"/downloads"', '"/downloads/cancel"',
                      '"/history"', '"/history/add"', '"/history/remove"', '"/history/clear"', '"/bookmarks"', '"/bookmarks/add"', '"/bookmarks/remove"'):
            assert route in MAIN_CODE, route

    def test_the_agents_anchor_is_the_first_tab_at_about_blank_and_a_new_tab_is_not(self):
        assert 'registerTab(paneView, "about:blank")' in MAIN_CODE
        assert "NEW_TAB_URL" in function_body("openTab")
        assert MAIN_CODE.count('"about:blank"') <= 3


class TestDownloadsNeverOpenThemselves:
    def test_the_only_calls_that_open_or_reveal_a_file_are_inside_the_explicit_action(self):
        assert MAIN_CODE.count("shell.openPath(") == 1
        assert MAIN_CODE.count("shell.showItemInFolder(") == 1
        action = function_body("downloadAction")
        assert "shell.openPath(" in action and "shell.showItemInFolder(" in action
        assert "policy.isOpenableDownload(rec.filename)" in action
        handler = function_body("installDownloadHandler")
        assert "openPath" not in handler and "openExternal" not in handler and "showItemInFolder" not in handler

    def test_open_and_reveal_come_only_from_the_console_window_and_the_bridge_cannot_ask(self):
        assert 'if ((action === "open" || action === "reveal") && !fromConsole(evt))' in MAIN_CODE
        bridge = function_body("startPaneBridge")
        assert 'downloadAction(String(obj.id || ""), "cancel")' in bridge
        assert '"open"' not in bridge and '"reveal"' not in bridge

    def test_files_go_to_downloads_under_a_safe_unique_name_and_are_quarantined(self):
        handler = function_body("installDownloadHandler")
        assert "policy.safeFileName(item.getFilename())" in handler and "policy.uniqueFileName(" in handler
        assert ".crdownload" in handler
        assert "downloadsDir()" in handler
        assert "com.apple.quarantine" in function_body("markDownloaded")

    def test_the_download_handler_is_on_the_panes_profile(self):
        assert "installDownloadHandler(paneView.webContents.session)" in MAIN_CODE
        assert '"will-download"' in function_body("installDownloadHandler")


class TestRecordsStayInUserData:
    def test_history_bookmarks_zoom_and_downloads_are_json_files_in_the_user_data_folder(self):
        assert 'app.getPath("userData"), "browser"' in MAIN_CODE
        for name in ("history.json", "bookmarks.json", "zoom.json", "downloads.json"):
            assert f'"{name}"' in MAIN_CODE
        assert "renameSync(tmp, target())" in MAIN_CODE  # written atomically
        assert ".corrupt-" in MAIN_CODE  # a damaged file is kept aside, not overwritten

    def test_stores_are_flushed_on_quit(self):
        assert "flushBrowserStores();" in MAIN_CODE.split('app.on("before-quit"')[1]

    def test_no_new_npm_dependency(self):
        pkg = (ELECTRON / "package.json").read_text(encoding="utf-8")
        assert '"dependencies"' not in pkg or re.search(r'"dependencies"\s*:\s*\{\s*\}', pkg) or "electron-store" not in pkg


class TestIpcSurface:
    def test_every_ipc_channel_the_preload_calls_has_a_handler_and_the_reverse(self):
        pre = (ELECTRON / "preload.js").read_text(encoding="utf-8")
        called = set(re.findall(r'invoke\("([a-z:_-]+)"', pre))
        handled = set(re.findall(r'ipcMain\.handle\("([a-z:_-]+)"', MAIN))
        assert called <= handled, called - handled
        pane = {c for c in handled if c.startswith("pane:")}
        assert {c for c in called if c.startswith("pane:")} == pane

    def test_the_pane_channels_the_screen_pushes_to_are_listened_to_by_the_preload(self):
        pre = (ELECTRON / "preload.js").read_text(encoding="utf-8")
        for channel in ("pane:state", "pane:downloads", "pane:command"):
            assert f'ipcRenderer.on("{channel}"' in pre
            assert f'send("{channel}"' in MAIN
