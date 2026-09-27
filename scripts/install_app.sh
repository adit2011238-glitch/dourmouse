#!/bin/bash
# Installs the Dourmouse app: ~/Applications/Dourmouse.app (finding #159).
#
# The app is a re-branded clone of this checkout's own Electron runtime
# (electron/node_modules/electron/dist/Electron.app): named Dourmouse, with
# the Dourmouse icon and its own bundle id, and a tiny payload that runs the
# checkout's live electron/main.js. So the Dock shows ONE app called Dourmouse
# (a script launcher would show a second "Electron" tile), it always runs the
# current code of the checkout, and main.js starts the server exactly as it
# does for `electron electron/`.
#
# Re-run it after `npm install` changes Electron or after the checkout moves.
# It replaces an existing bundle by moving the old one to the Trash, never by
# deleting it.
#
#   scripts/install_app.sh                            # ~/Applications/Dourmouse.app
#   DOURMOUSE_APP_DIR=/some/folder scripts/install_app.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APPS="${DOURMOUSE_APP_DIR:-$HOME/Applications}"
APP="$APPS/Dourmouse.app"
ELECTRON_APP="$REPO/electron/node_modules/electron/dist/Electron.app"
ICON="$REPO/electron/resources/icon.icns"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/dourmouse/__init__.py")"
VERSION="${VERSION:-1.0.0}"
PB=/usr/libexec/PlistBuddy

case "$REPO" in *\"*|*\\*) echo "The checkout path contains a quote or backslash: $REPO" >&2; exit 1;; esac
[ -d "$ELECTRON_APP" ] || { echo "Electron is not installed: run 'cd $REPO/electron && npm install' first." >&2; exit 1; }
[ -x "$REPO/.venv/bin/python" ] || { echo "No Python environment at $REPO/.venv (create it first)." >&2; exit 1; }
[ -f "$ICON" ] || { echo "Missing icon: $ICON" >&2; exit 1; }

mkdir -p "$APPS"
if [ -e "$APP" ]; then
  mkdir -p "$HOME/.Trash"
  mv "$APP" "$HOME/.Trash/Dourmouse-replaced-$(date +%Y%m%d-%H%M%S).app"
fi

# APFS clone (instant, no extra disk); a normal copy if cloning is not possible
cp -cR "$ELECTRON_APP" "$APP" 2>/dev/null || cp -R "$ELECTRON_APP" "$APP"

PLIST="$APP/Contents/Info.plist"
set_key() { "$PB" -c "Set :$1 $2" "$PLIST" 2>/dev/null || "$PB" -c "Add :$1 string $2" "$PLIST"; }
set_key CFBundleName Dourmouse
set_key CFBundleDisplayName Dourmouse
set_key CFBundleIdentifier com.dourmouse.app
set_key CFBundleShortVersionString "$VERSION"
set_key CFBundleVersion "$VERSION"
set_key LSApplicationCategoryType public.app-category.productivity
"$PB" -c "Delete :ElectronAsarIntegrity" "$PLIST" 2>/dev/null || true
"$PB" -c "Delete :CFBundleURLTypes" "$PLIST" 2>/dev/null || true
"$PB" -c "Add :CFBundleURLTypes array" \
       -c "Add :CFBundleURLTypes:0 dict" \
       -c "Add :CFBundleURLTypes:0:CFBundleURLName string 'Dourmouse Deep Link'" \
       -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" \
       -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string dourmouse" "$PLIST"

cp "$ICON" "$APP/Contents/Resources/electron.icns"
chmod 644 "$APP/Contents/Resources/electron.icns"

# the payload: run the live checkout's main.js under the Dourmouse name, keeping
# the existing Electron data folder (window size, cookies, preferences)
mkdir -p "$APP/Contents/Resources/app"
cat > "$APP/Contents/Resources/app/package.json" <<JSON
{"name": "dourmouse-electron", "version": "$VERSION", "main": "main.js"}
JSON
cat > "$APP/Contents/Resources/app/main.js" <<JS
// Dourmouse.app payload (written by scripts/install_app.sh): runs the live
// checkout's electron/main.js under the Dourmouse name and data folder.
const fs = require("fs");
const os = require("os");
const path = require("path");
const { app, dialog } = require("electron");

const REPO = "$REPO";
const MAIN = path.join(REPO, "electron", "main.js");

app.setName("Dourmouse");
app.setPath("userData", path.join(os.homedir(), "Library", "Application Support", "dourmouse-electron"));

if (fs.existsSync(MAIN)) {
  require(MAIN);
} else {
  app.whenReady().then(() => {
    dialog.showErrorBox("Dourmouse", "The Dourmouse checkout is not reachable at:\\n" + REPO);
    app.quit();
  });
}
JS

# DOURMOUSE_APP_NO_SIGN=1 (tests only) skips signing and registering the bundle
if [ "${DOURMOUSE_APP_NO_SIGN:-0}" != "1" ]; then
  # the edits above invalidate the runtime's signature: sign the bundle again (ad hoc, local)
  /usr/bin/codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "warning: could not re-sign $APP; it may not start" >&2

  LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
  [ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true
fi
echo "Installed $APP (version $VERSION, runs the checkout at $REPO)"
