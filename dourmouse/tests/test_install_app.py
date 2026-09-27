"""scripts/install_app.sh builds ~/Applications/Dourmouse.app (finding #159).

The bundle is a re-branded clone of the checkout's own Electron runtime whose
payload runs the live electron/main.js. These tests build one in a temporary
folder (signing and registering are skipped) and read back what matters: the
name, the bundle id, the icon, the URL scheme and a payload that parses.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install_app.sh"
ELECTRON_APP = ROOT / "electron" / "node_modules" / "electron" / "dist" / "Electron.app"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not ELECTRON_APP.is_dir() or not (ROOT / ".venv" / "bin" / "python").exists(),
    reason="needs macOS, the checkout's Electron install and its .venv",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    apps = tmp_path_factory.mktemp("apps")
    env = {**os.environ, "DOURMOUSE_APP_DIR": str(apps), "DOURMOUSE_APP_NO_SIGN": "1"}
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=240, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return apps / "Dourmouse.app"


def _plist(app: Path) -> dict:
    out = subprocess.run(["plutil", "-convert", "json", "-o", "-", str(app / "Contents" / "Info.plist")], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class TestBundle:
    def test_the_app_is_called_dourmouse_and_has_its_own_identity(self, built):
        info = _plist(built)
        assert info["CFBundleName"] == "Dourmouse" and info["CFBundleDisplayName"] == "Dourmouse"
        assert info["CFBundleIdentifier"] == "com.dourmouse.app"
        assert info["CFBundleExecutable"] == "Electron", "the runtime binary keeps its name so Chromium finds its helpers"
        assert "ElectronAsarIntegrity" not in info

    def test_it_registers_the_dourmouse_url_scheme(self, built):
        schemes = [s for t in _plist(built)["CFBundleURLTypes"] for s in t["CFBundleURLSchemes"]]
        assert schemes == ["dourmouse"]

    def test_the_icon_is_the_dourmouse_icon_not_the_electron_one(self, built):
        icon = built / "Contents" / "Resources" / "electron.icns"
        assert icon.read_bytes() == (ROOT / "electron" / "resources" / "icon.icns").read_bytes()
        assert not os.access(icon, os.X_OK)

    def test_the_payload_runs_the_live_checkout_under_the_old_data_folder(self, built):
        pkg = json.loads((built / "Contents" / "Resources" / "app" / "package.json").read_text())
        assert pkg["name"] == "dourmouse-electron" and pkg["main"] == "main.js"
        main = built / "Contents" / "Resources" / "app" / "main.js"
        text = main.read_text()
        assert f'const REPO = "{ROOT}";' in text
        assert 'app.setName("Dourmouse")' in text and "dourmouse-electron" in text
        node = shutil.which("node")
        if node:
            subprocess.run([node, "--check", str(main)], check=True, capture_output=True)

    def test_the_script_only_ever_moves_an_old_bundle_to_the_trash(self):
        text = SCRIPT.read_text()
        assert '.Trash/Dourmouse-replaced-' in text
        assert "rm -rf" not in text and "rm -r " not in text


class TestPinnedAppFacts:
    def test_the_dock_icon_hook_uses_a_png_electron_can_load(self):
        main = (ROOT / "electron" / "main.js").read_text()
        assert 'app.dock.setIcon(path.join(__dirname, "resources", "icon.png"))' in main
        assert (ROOT / "electron" / "resources" / "icon.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_a_server_that_cannot_start_is_reported_in_a_dialog_not_a_silent_quit(self):
        main = (ROOT / "electron" / "main.js").read_text()
        assert 'dialog.showErrorBox("Dourmouse could not start"' in main
        assert "timeoutMs = 60000" in main
