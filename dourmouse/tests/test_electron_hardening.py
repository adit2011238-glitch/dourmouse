"""Electron shell hardening (findings S34, S35, S36).

electron/policy.js holds the pure decisions and is exercised for real under
plain node. Electron itself cannot run in the test environment, so the wiring
in main.js is checked by source assertions plus ``node --check``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ELECTRON = Path(__file__).resolve().parents[2] / "electron"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = """
const p = require(%r);
const port = 8765;
const out = {
  own: [
    p.isAppOrigin("http://127.0.0.1:8765/workspace", port),
    p.isAppOrigin("http://localhost:8765", port),
  ],
  notOwn: [
    "https://127.0.0.1:8765/",
    "http://127.0.0.1:9999/",
    "http://127.0.0.1/",
    "http://evil.example:8765/",
    "http://127.0.0.1:8765@evil.example/",
    "http://127.0.0.1:8765.evil.example/",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "about:blank",
    "not a url",
    "",
  ].map((u) => p.isAppOrigin(u, port)),
  perms: {
    mediaOwn: p.permissionAllowed("media", "http://127.0.0.1:8765", port),
    clipOwn: p.permissionAllowed("clipboard-sanitized-write", "http://localhost:8765", port),
    mediaRemote: p.permissionAllowed("media", "https://example.com", port),
    clipRemote: p.permissionAllowed("clipboard-sanitized-write", "https://example.com", port),
    fullOwn: p.permissionAllowed("fullscreen", "http://127.0.0.1:8765", port),
    fullRemote: p.permissionAllowed("fullscreen", "https://example.com", port),
    geoOwn: p.permissionAllowed("geolocation", "http://127.0.0.1:8765", port),
    notifOwn: p.permissionAllowed("notifications", "http://127.0.0.1:8765", port),
    clipReadOwn: p.permissionAllowed("clipboard-read", "http://127.0.0.1:8765", port),
    unknownOwn: p.permissionAllowed("anything-else", "http://127.0.0.1:8765", port),
    noOrigin: p.permissionAllowed("media", "", port),
  },
  nav: [
    p.navigationAllowed("http://127.0.0.1:8765/agent/x", port),
    p.navigationAllowed("https://example.com/", port),
  ],
  external: {
    http: p.externalUrlAllowed("http://example.com"),
    https: p.externalUrlAllowed("https://example.com/a?b=1"),
    file: p.externalUrlAllowed("file:///etc/passwd"),
    js: p.externalUrlAllowed("javascript:alert(1)"),
    data: p.externalUrlAllowed("data:text/html,x"),
    custom: p.externalUrlAllowed("vscode://open"),
    junk: p.externalUrlAllowed("nope"),
  },
  envPort: (() => { process.env.DOURMOUSE_UI_PORT = "9100"; return [
    p.isAppOrigin("http://127.0.0.1:9100/"), p.isAppOrigin("http://127.0.0.1:8765/")]; })(),
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def decisions():
    proc = subprocess.run(
        [NODE, "-e", HARNESS % str(ELECTRON / "policy.js")],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_app_origin_only_is_recognised(decisions):
    assert decisions["own"] == [True, True]
    assert decisions["notOwn"] == [False] * 11


def test_app_origin_follows_configured_port(decisions):
    assert decisions["envPort"] == [True, False]


def test_permissions_deny_by_default(decisions):
    perms = decisions["perms"]
    assert perms["mediaOwn"] is True
    assert perms["clipOwn"] is True
    for key in ("mediaRemote", "clipRemote", "geoOwn", "notifOwn", "clipReadOwn", "unknownOwn", "noOrigin"):
        assert perms[key] is False, key


def test_navigation_restricted_to_app_origin(decisions):
    assert decisions["nav"] == [True, False]


def test_external_links_are_http_only(decisions):
    ext = decisions["external"]
    assert ext["http"] is True and ext["https"] is True
    for key in ("file", "js", "data", "custom", "junk"):
        assert ext[key] is False, key


@pytest.mark.parametrize("name", ["main.js", "preload.js", "policy.js"])
def test_node_syntax_check(name):
    proc = subprocess.run([NODE, "--check", str(ELECTRON / name)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr


def test_main_wires_the_policy():
    src = (ELECTRON / "main.js").read_text()
    assert 'require("./policy")' in src
    assert "setPermissionRequestHandler" in src and "setPermissionCheckHandler" in src
    assert "installPermissionPolicy(session.defaultSession)" in src
    assert "installPermissionPolicy(paneView.webContents.session)" in src
    # every console-privileged window is locked, including the task and re-created ones
    for target in ("win", "mainWindow", "mapWindow", "atlasWindow"):
        assert f"lockToAppOrigin({target})" in src
    assert '"will-navigate"' in src and '"will-redirect"' in src
    assert 'remote-debugging-address", "127.0.0.1"' in src


def test_pane_stays_isolated_and_bridge_check_intact():
    src = (ELECTRON / "main.js").read_text()
    pane = src.split("paneView = new BrowserView(", 1)[1].split(")", 1)[0]
    assert "preload" not in pane
    assert 'req.headers["sec-fetch-site"] !== undefined' in src
    assert "req.headers.origin !== undefined" in src


def test_policy_is_packaged():
    pkg = json.loads((ELECTRON / "package.json").read_text())
    assert "policy.js" in pkg["build"]["files"]


def test_the_media_players_fullscreen_works_for_the_app_and_never_for_a_remote_page(decisions):
    assert decisions["perms"]["fullOwn"] is True
    assert decisions["perms"]["fullRemote"] is False
