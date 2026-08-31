#!/bin/bash
# =============================================================================
#  DOURMOUSE // build a double-clickable dourmouse.app (macOS)
#
#  Usage:  ./build_app.command [output_dir]
#  Output: dourmouse.app in the given dir (default: the project root).
#  build_dist.sh calls it with "$STAGE" so the staged copy ships with
#  dourmouse.app sitting right next to start.command.
#
#  Uses osacompile (ships with macOS — zero new dependencies) to wrap
#  start.command in a proper .app bundle you can double-click, drag to
#  /Applications, or pin to the Dock. The applet simply opens a Terminal
#  running start.command (which boots the native DOURMOUSE desktop window).
#
#  v13.5 real bug fixed here (live-reproduced): the applet used to find
#  the project root DYNAMICALLY via AppleScript's `path to me` — the
#  bundle's OWN current location. That only works if the .app is left
#  sitting right next to start.command forever. The moment a copy is
#  moved/dragged to ~/Applications (the whole point of "add the app to
#  my Applications folder" — a normal, expected thing to do with a .app),
#  `path to me` resolves to ~/Applications instead, `cd` lands there, and
#  `bash ./start.command` fails outright (no such file) — a real, exactly
#  this failure, confirmed live: `ls ~/Applications/start.command` ->
#  No such file or directory. Fixed by baking the REAL project root path
#  in at BUILD time (known-good, this script's own directory) instead of
#  resolving it at RUN time from wherever the bundle happens to be — the
#  built .app can now be freely copied/moved anywhere and still finds the
#  real project.
# =============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"  # the REAL project root — always this script's own directory, regardless of where the .app itself ends up
OUTPUT_DIR="${1:-$PROJECT_ROOT}"  # default: build straight into the project root, as before
APP="$OUTPUT_DIR/dourmouse.app"
# AppleScript string-literal escaping for the one character that would
# otherwise break out of the quoted string below (real paths essentially
# never contain a literal double-quote, but this is cheap insurance).
APPLESCRIPT_ROOT="${PROJECT_ROOT//\"/\\\"}"
# Deterministic temp path (mktemp chokes on a leftover file in some /tmp
# states); always cleared first, removed after compile.
TMP_SCRIPT="/tmp/dourmouse_applet.applescript"
rm -f "$TMP_SCRIPT"

cat > "$TMP_SCRIPT" <<APPLESCRIPT
on run
	set appRoot to "$APPLESCRIPT_ROOT"
	tell application "Terminal"
		activate
		do script "cd " & quoted form of appRoot & " && bash ./start.command"
	end tell
end run

-- v5.20: dourmouse:// deep links (atlas, world, portfolio, alerts, ...).
-- The OS delivers the URL here when the scheme is registered (Info.plist
-- CFBundleURLTypes below). The raw URL is ONLY ever passed as an env var
-- to start.command — the allow-list parser in dourmouse/deeplink.py decides
-- what it means; nothing here ever executes it.
on open location theURL
	set appRoot to "$APPLESCRIPT_ROOT"
	do shell script "cd " & quoted form of appRoot & " && DOURMOUSE_DEEP_LINK=" & quoted form of theURL & " nohup bash ./start.command >/dev/null 2>&1 &"
end open location
APPLESCRIPT

rm -rf "$APP"
osacompile -o "$APP" "$TMP_SCRIPT"
rm -f "$TMP_SCRIPT"

# --- v5.20: register the dourmouse:// URL scheme so deep links open the app
# (osacompile generates a generic Info.plist; add CFBundleURLTypes to it).
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Delete :CFBundleURLTypes" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0 dict" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLName string dourmouse" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string dourmouse" "$PLIST"

echo "✓ Built $APP"
echo "  Double-click dourmouse.app (or drag it to /Applications)."
echo "  dourmouse:// scheme registered — links open the app (see PROGRESS v5.20)."
