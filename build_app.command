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
# =============================================================================
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")" && pwd)}"  # default: THIS script's dir (the project root), not its parent
APP="$ROOT/dourmouse.app"
# Deterministic temp path (mktemp chokes on a leftover file in some /tmp
# states); always cleared first, removed after compile.
TMP_SCRIPT="/tmp/dourmouse_applet.applescript"
rm -f "$TMP_SCRIPT"

cat > "$TMP_SCRIPT" <<'APPLESCRIPT'
on run
	set bundlePath to POSIX path of (path to me)
	set appRoot to do shell script "dirname " & quoted form of bundlePath
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
	set appRoot to do shell script "dirname " & quoted form of (POSIX path of (path to me))
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
