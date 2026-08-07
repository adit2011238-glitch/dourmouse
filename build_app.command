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

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
APP="$ROOT/dourmouse.app"
TMP_SCRIPT="$(mktemp /tmp/dourmouse_applet.XXXXXX.applescript)"

cat > "$TMP_SCRIPT" <<'APPLESCRIPT'
on run
	set bundlePath to POSIX path of (path to me)
	set appRoot to do shell script "dirname " & quoted form of bundlePath
	tell application "Terminal"
		activate
		do script "cd " & quoted form of appRoot & " && bash ./start.command"
	end tell
end run
APPLESCRIPT

rm -rf "$APP"
osacompile -o "$APP" "$TMP_SCRIPT"
rm -f "$TMP_SCRIPT"

echo "✓ Built $APP"
echo "  Double-click dourmouse.app (or drag it to /Applications)."
