#!/bin/bash
# OPT-IN: switch this checkout's Electron to castLabs's "Electron for Content Security" (ECS)
# build, which adds the Widevine content decryption module that stock Electron does not have,
# so protected video can play in the BROWSER screen. Phase B3, finding in docs/ENGINEERING_AUDIT.md.
# The design, the risks, the rollback and the tests are in ~/Documents/DOURMOUSE/B3_DRM_PLAN.md.
# READ THAT FIRST.
#
# This script is NOT run by anything. Nothing in the repo calls it. It does nothing at all unless
# you pass --yes. Run with no arguments to see the plan.
#
#   bash scripts/install_drm_electron.sh                       # print the plan, change nothing
#   bash scripts/install_drm_electron.sh --yes                 # steps 1 to 4: back up, install ECS, check
#   bash scripts/install_drm_electron.sh --yes --save          # same, and write the version into electron/package.json
#   bash scripts/install_drm_electron.sh --yes --vmp           # also sign the runtime for the verified media path
#   bash scripts/install_drm_electron.sh --yes --reinstall-app # also rebuild ~/Applications/Dourmouse.app
#
# Environment:
#   CASTLABS_TAG   the release tag to install, for example v44.3.0+wvcus. When it is not set, the
#                  newest tag for the Electron major version already in use is looked up and shown.
#   EVS_VENV       where the castLabs signing tool is installed (default ~/.dourmouse/evs-venv).
#
# What this script never does: create an account, type or store a password, sign in for you, sign
# anything unless --vmp is given, delete anything, or touch the Dourmouse app data folder.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
EL="$REPO/electron"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$HOME/Documents/DOURMOUSE/backups/drm-$STAMP"
EVS_VENV="${EVS_VENV:-$HOME/.dourmouse/evs-venv}"
VENV_PY="$REPO/.venv/bin/python"
REPO_URL="https://github.com/castlabs/electron-releases"

YES=0; SAVE=0; VMP=0; REINSTALL=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    --save) SAVE=1 ;;
    --vmp) VMP=1 ;;
    --reinstall-app) REINSTALL=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

current_electron() {
  node -e 'try{console.log(require(process.argv[1]+"/node_modules/electron/package.json").version)}catch(e){console.log("")}' "$EL"
}

pick_tag() {
  local major="$1"
  if [ -n "${CASTLABS_TAG:-}" ]; then printf '%s\n' "$CASTLABS_TAG"; return; fi
  # tags look like v44.3.0+wvcus; the newest one for the major version already in use
  git ls-remote --tags "$REPO_URL" 2>/dev/null \
    | sed -n 's|.*refs/tags/\(v'"$major"'\.[0-9]*\.[0-9]*+wvcus\)$|\1|p' \
    | sort -V | tail -n 1
}

step "Plan"
say "1. Back up electron/package.json, electron/package-lock.json and a note of the current Electron version"
say "   to $BACKUP"
say "2. Install the castLabs ECS build of Electron in place of stock Electron (npm, from $REPO_URL)"
say "   (without --save, package.json is not edited; with --save it is, and you commit it yourself)"
say "3. Check that the new runtime has the 'components' module (the Widevine component manager)"
say "4. Tell you how to see it in the app: BROWSER, Site settings, the DRM line"
say "5. With --vmp: install castLabs's signing tool in its own virtualenv and sign the runtime"
say "   (needs a free castLabs EVS account that YOU create and sign in to; this script never does)"
say "6. With --reinstall-app: re-run scripts/install_app.sh so ~/Applications/Dourmouse.app uses it"
say ""
say "Rollback at any point: see the Rollback section of ~/Documents/DOURMOUSE/B3_DRM_PLAN.md"
say "(short version: restore the two backed-up files, then 'cd electron && npm ci')."

if [ "$YES" -ne 1 ]; then
  say ""
  say "Nothing was changed. Run again with --yes to do steps 1 to 4."
  exit 0
fi

command -v node >/dev/null || { echo "node is not installed" >&2; exit 1; }
command -v npm >/dev/null || { echo "npm is not installed" >&2; exit 1; }
[ -d "$EL" ] || { echo "no electron folder at $EL" >&2; exit 1; }

step "1. Back up"
CUR="$(current_electron)"
[ -n "$CUR" ] || { echo "Electron is not installed in $EL/node_modules: run 'cd $EL && npm install' first." >&2; exit 1; }
MAJOR="${CUR%%.*}"
mkdir -p "$BACKUP"
cp "$EL/package.json" "$BACKUP/package.json"
[ -f "$EL/package-lock.json" ] && cp "$EL/package-lock.json" "$BACKUP/package-lock.json"
printf 'stock electron version before: %s\n' "$CUR" > "$BACKUP/NOTE.txt"
say "backed up to $BACKUP (Electron $CUR)"

step "2. Install the castLabs ECS build"
TAG="$(pick_tag "$MAJOR" || true)"  # a failed lookup must reach the message below, not end the script silently under set -e
if [ -z "$TAG" ]; then
  echo "No castLabs tag was found for Electron $MAJOR (network down, or no such release)." >&2
  echo "List them yourself:  git ls-remote --tags $REPO_URL | grep wvcus | tail" >&2
  echo "then run again with CASTLABS_TAG=<tag>." >&2
  exit 1
fi
say "using tag: $TAG (stock Electron was $CUR)"
SAVE_FLAG="--no-save"; [ "$SAVE" -eq 1 ] && SAVE_FLAG="--save-dev"
( cd "$EL" && npm install $SAVE_FLAG "electron@github:castlabs/electron-releases#$TAG" )

step "3. Check the new runtime"
PROBE_DIR="$(mktemp -d)"
cat > "$PROBE_DIR/main.js" <<'JS'
const { app, components } = require("electron");
app.whenReady().then(async () => {
  const out = { electron: process.versions.electron, chrome: process.versions.chrome, components: typeof components };
  if (components && components.whenReady) {
    try { await components.whenReady(); out.status = components.status(); } catch (e) { out.error = String(e && e.message || e); }
  }
  console.log("DRM-PROBE " + JSON.stringify(out));
  app.quit();
});
JS
echo '{"name":"drm-probe","main":"main.js"}' > "$PROBE_DIR/package.json"
"$EL/node_modules/.bin/electron" "$PROBE_DIR" 2>&1 | grep "DRM-PROBE" || echo "the probe printed nothing: the runtime did not start (see the rollback section)" >&2
rm -rf "$PROBE_DIR"
say "expect:  components: \"object\" and a status entry for the Widevine component."
say "On the first run the module downloads over the network, so it can take a little while."

if [ "$VMP" -eq 1 ]; then
  step "5. Sign for the verified media path (VMP)"
  say "This needs a castLabs EVS account. Create it and sign in YOURSELF, in a terminal:"
  say "    $EVS_VENV/bin/python -m castlabs_evs.account signup     # once"
  say "    $EVS_VENV/bin/python -m castlabs_evs.account reauth     # when asked to sign in again"
  [ -x "$VENV_PY" ] || { echo "no Python environment at $REPO/.venv" >&2; exit 1; }
  if [ ! -x "$EVS_VENV/bin/python" ]; then
    "$VENV_PY" -m venv "$EVS_VENV"
    "$EVS_VENV/bin/python" -m pip install --upgrade pip castlabs-evs
  fi
  say "signing $EL/node_modules/electron/dist ..."
  "$EVS_VENV/bin/python" -m castlabs_evs.vmp sign-pkg "$EL/node_modules/electron/dist" \
    || { echo "signing failed. The usual cause is that you are not signed in: run the reauth command above and try again." >&2; exit 1; }
  say "signed. The runtime is now VMP-signed; scripts/install_app.sh signs the finished app ad hoc AFTER this, which is the order castLabs asks for."
fi

if [ "$REINSTALL" -eq 1 ]; then
  step "6. Rebuild the Dourmouse app"
  bash "$REPO/scripts/install_app.sh"
fi

step "4. Where to look"
say "Quit and reopen Dourmouse. BROWSER, the shield button (Site settings): the DRM line should say"
say "Widevine is available. Then run the tests in ~/Documents/DOURMOUSE/B3_DRM_PLAN.md."
say "Backup of what was replaced: $BACKUP"
