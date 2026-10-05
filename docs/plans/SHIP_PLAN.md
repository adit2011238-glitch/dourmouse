# DOURMOUSE ship plan (phase I2 build preparation)

Written 2026-10-05. Nothing in this plan was run: no electron-builder, no npm install, no npx,
no pip install, no download. It is a review of what exists and a list of steps and decisions.
Where a statement says "read", it came from reading the file named; where it says "not tested",
it was not.

## 1. What exists today

| Piece | What it does | Where |
| --- | --- | --- |
| Live-checkout app | `~/Applications/Dourmouse.app`, a re-branded clone of the checkout's own Electron, ad-hoc signed, runs the live `electron/main.js` and the checkout's `.venv`. Needs `~/dourmouse-recon` to stay where it is. | `scripts/install_app.sh`, finding #159 |
| Self-contained build config | electron-builder: `files` (the shell's JS), `extraResources` (the Python package, the UI, a Python venv) laid out so `Contents/Resources` looks like the checkout root. dmg, arm64 and x64. | `electron/package.json` |
| Venv builder | Makes a fresh venv with the host Python and pip-installs `requirements.txt` and `requirements-desktop.txt` into `electron/build/venv-stage/.venv`. | `electron/scripts/prepare-venv.sh` |
| Notarization hook | `afterSign`: notarizes with a keychain profile (`DOURMOUSE_NOTARY_PROFILE`) or the `APPLE_ID` triple, skips honestly with neither. | `electron/scripts/notarize.js` |
| Entitlements | Hardened runtime plus the JIT and library-validation relaxations a bundled Python needs, Apple Events, camera, microphone. No App Sandbox. | `electron/resources/entitlements.mac.plist` |

The last electron-builder output (806 MB, 2026-09-13) was moved to the Trash in #159. No current self-contained build exists.

## 2. Review of the build config (what is right, what changed, what is still wrong)

### Changed in this phase (package.json `files` and `extraResources` lists only, no dependency change)

1. `resources/icon.png` added to `files`. `main.js` sets the Dock icon from `__dirname/resources/icon.png`; inside the packaged app that file was missing, so the call failed (it is caught and logged, so the app still started, with the bundle icon instead).
2. `!workspace/**` added to the `dourmouse` filter. A `dourmouse/workspace/` folder exists on the owner's disk (`atlas_lab` data). It is git-ignored, but electron-builder reads the disk, not git: a build would have shipped it.
3. `!**/.DS_Store` and `!**/*.orig` (any depth) added to the `dourmouse` and `ui` filters. The old `ui` filter only dropped `*.orig` at the top level.
4. A test (`test_crash_recovery_shell.py`, class `TestBuildConfigListsWhatTheShellLoads`) now fails if `main.js` requires a module the `files` list lacks, if a file `main.js` reads from its own folder is not packaged, or if a dependency changes.

Confirmed present and listed: `main.js`, `preload.js`, `policy.js`, `permissions.js`, `passwords.js`, `profiles.js`, `extensions.js`, `importers.js`, `drm.js` (the B2 and B3 modules), `content/frame-forms.js` (the C2 and form helper), and through `**/*` of the `dourmouse` resource: the whole Python package including `browser_scripts/element_ids.js` and `browser_scripts/media_control.js` (read by `browser_agent.py` next to itself), `os_api/`, `security/`, `app_driver/`; and through the `ui` resource: `ui/assets/os/**`, fonts, `console.html`, `shell.html`, `setup.html`, `login.html`.

### Still wrong. Each one blocks a self-contained build and needs the owner's or the main thread's decision (outside I2's file list)

A. **The packaged app would write into its own signed bundle.** `main.js` sets `PROJECT_ROOT = process.resourcesPath` when packaged, and the Python side defaults several write locations to `<package parent>/...`: `workspace/` (`config.workspace_dir`, `schedules`, `goals`, `sandbox`, `state_store`, `project_bookkeeper`), `logs/` (`obs.logs_dir`, the response cache `cache.db_path`), `data/images` (`image_gen`). Writing inside `Contents/Resources` breaks the code signature seal and fails on a read-only volume. Fix before a build: in packaged mode pass the server `DOURMOUSE_WORKSPACE`, `DOURMOUSE_LOG_DIR` and `DOURMOUSE_CACHE_DB` pointing into `~/Library/Application Support/Dourmouse/`, and give `image_gen`'s `_DATA_DIR` an override. `DOURMOUSE_CONFIG_DIR`-style overrides already exist for most of them; `image_gen` has none.
B. **The venv is not relocatable and depends on Homebrew's Python.** `.venv/pyvenv.cfg` says `home = /opt/homebrew/opt/python@3.14/bin`, and `bin/python` is a symlink to it. Copying that venv into `Contents/Resources/.venv` gives an app that only starts on a Mac that has the same Homebrew Python at the same path. A self-contained build needs its own Python runtime (section 3, step 3).
C. **Both architectures from one script cannot work.** The target list builds arm64 and x64 dmgs, but `prepare-venv.sh` installs wheels for the host architecture only. Build arm64 only (this is an Apple silicon Mac and so is anything the owner is likely to install it on), or run the whole pipeline once per architecture.
D. **Product name drift.** `productName` is `DourMouse`; the live app and every doc say `Dourmouse`. The `appId` is the same (`com.dourmouse.app`), so the two would collide in LaunchServices if both are installed. Change `productName` to `Dourmouse` (outside the two lists I may edit; one word).
E. **Size.** The venv is 1.1 GB on disk here, 26 MB for `ui`, 14 MB for the Python package without tests. A first build will be large (the last one was 806 MB). Trim before shipping: remove `pywebview`/`pystray` (the Electron shell does not use them, noted in `prepare-venv.sh` itself), strip `__pycache__`, `*.dist-info/RECORD` is fine, drop test folders inside site-packages.
F. **Native helpers inside the venv must be signed.** `imageio_ffmpeg` ships an `ffmpeg` binary, Playwright ships a Node driver (over 100 MB) and many wheels ship `.so`/`.dylib` files. With hardened runtime every one must be signed with the same identity (electron-builder signs the main bundle and its Electron frameworks; files under `Resources/.venv` need an explicit pass: `codesign --force --sign <identity> --options runtime` over every Mach-O file, found with `find ... -type f` plus `file`). The entitlements already include `disable-library-validation` for exactly this. Not tested.
G. **Playwright browsers are not bundled.** The app drives its own embedded pane through DevTools; the fallback that launches a separate Chrome (`browser_agent.launch`) needs Chrome installed on that Mac. Say so in the clean-account checklist; do not bundle Chromium (another 300 MB) unless that fallback must work on a bare Mac.

## 3. Self-contained build steps (for the owner to approve, then run)

Preconditions: items A and B above done and tested; an arm64 build host; Xcode command line tools; the owner's decision in section 4.

1. Branch check and clean tree (HARD_RULES rule 1). Full test suite green.
2. Bump `version` in `electron/package.json` (the dmg name is `${productName}-${version}-${arch}`). Record the commit hash with the build.
3. Python runtime. Use a relocatable CPython: the `python-build-standalone` arm64 `install_only` archive for the same minor version the project uses (3.14 today; a 3.12 or 3.13 runtime is also fine if every requirement has wheels). Unpack it to `electron/build/venv-stage/python`, then `python/bin/python3 -m pip install -r requirements.txt -r requirements-desktop.txt --target` or create a venv from that interpreter with `--copies`. The result must run with no Homebrew on the machine: verify with `otool -L` on `python3` (no `/opt/homebrew` paths) and by running it from a copied folder.
4. Change `prepare-venv.sh` to use that runtime instead of the first `python3.x` it finds, drop the unused packages (section 2E), precompile bytecode, run its existing leak check (it already scans for the owner's data).
5. `main.js` packaged branch: pass the write locations from section 2A. Add a test that no file under the bundle is written during a start (compare a hash listing of `Contents/Resources` before and after a run).
6. `cd electron && npm install` (only if `node_modules` is missing; this downloads packages, so ask first), then `npm run build:mac` with `DOURMOUSE_SIGN_IDENTITY` and the notarization variables set (section 4), or without them for an ad-hoc build.
7. Sign the venv's native files (2F) before electron-builder seals the bundle, or as an `afterPack` hook added to `package.json` `build` (a new script file under `electron/scripts/`).
8. Verify: `codesign --verify --deep --strict --verbose=2 Dourmouse.app`, `spctl -a -vv Dourmouse.app` (expect `accepted` only when notarized), `xcrun stapler validate`, then the clean-account checklist (section 6). Run `scripts/perf_check.py --app <the new .app>` and compare with `PERF_BUDGET.md`.
9. Keep the dmg, its SHA-256, the commit hash and the perf numbers in `~/Documents/DOURMOUSE/releases/<version>/` (section 7).

## 4. Code signing and notarization: the two paths (needs the owner's choice)

| | A. Ad-hoc, personal use | B. Developer ID |
| --- | --- | --- |
| Cost | none | Apple Developer Program, US$99 per year |
| What the owner does | nothing | enrol with the Apple ID, create a Developer ID Application certificate in Xcode or the developer site, store a notarytool keychain profile once (`xcrun notarytool store-credentials "dourmouse-notary" ...`, the name `notarize.js` already documents) |
| Signature | `codesign -s -` (what `install_app.sh` does) | `CSC_NAME`/`DOURMOUSE_SIGN_IDENTITY` set to the certificate; hardened runtime on |
| Gatekeeper on this Mac | opens (built here, no quarantine flag) | opens |
| Gatekeeper on another Mac | blocked on first open: right-click Open, or `xattr -dr com.apple.quarantine`; macOS 15 and later send the owner to System Settings, Privacy and Security, "Open Anyway" | opens normally once notarized and stapled |
| macOS privacy grants (Downloads, Documents, Desktop, microphone, camera, Accessibility, Automation) | tied to the code signature; an ad-hoc signature has no stable identity, so a rebuild can drop the grants and macOS asks again (the first launch of the #159 app already did this for the Electron-to-Dourmouse change) | the grant follows the certificate and bundle id, so a new version keeps them |
| Auto-update | cannot use Squirrel-based updating (needs a signed app) | can |
| Right for | the owner's own Macs while only the owner uses it | the moment anyone else installs it, or the owner wants updates without re-granting permissions |

Recommendation: path A until a second person installs Dourmouse; path B at that point. Both paths use the same build; only the environment variables change. The entitlements file was reasoned from the code, never checked against a real signed and notarized run; expect one round of "a missing entitlement" fixes on the first notarized build.

## 5. Auto-update options

**Option 1: electron-updater with a private feed.** Adds the `electron-updater` dependency and a `publish` block to `package.json` (a dependency change, so it needs the owner's go-ahead). The app checks `latest-mac.yml` on a feed, downloads the new zip, verifies it, replaces itself. Squirrel.Mac needs a signed app, so this means path B first. A private feed means a static HTTPS location (an object storage bucket or a small web server) with credentials; a GitHub private repository would need a token inside the app, which is a secret shipped to every install and should not be done. Also needs the `zip` target next to `dmg` (the updater uses the zip). Strongest experience, most moving parts, and a bad release reaches every install on the next check.

**Option 2: a simple version check that opens the download page.** The main process fetches one small JSON file (`{ "version": "0.2.0", "url": "https://..." }`) from a fixed HTTPS address on launch and once a day, compares the version with its own, and shows one toast "Version 0.2.0 is available" with a button that opens the download page in the browser. It downloads nothing and installs nothing. No new dependency, no signing requirement, works with ad-hoc builds, and a bad file can at worst show a wrong toast. The owner installs the new dmg by hand, which is also the rollback path (section 7).

Recommended: option 2 now. It is about 60 lines in `main.js`, uses the toast path I2 added for the restart notice, and its failure mode is harmless. Move to option 1 when path B exists and there are enough installs that updating by hand is a chore. Rules for option 2 when it is built: HTTPS only, a fixed host in the code (never taken from a page, a setting or the server), no redirects to another host, the link opened is the one from the file only if its host matches the fixed host, the check is skippable with a setting, nothing about the owner or the machine is sent (no identifier, no version of the OS), the check never blocks start-up.

## 6. Clean macOS account install checklist

Run on a second standard user account on the same Mac, or on another Mac, with nothing from the owner's account (no `~/dourmouse-recon`, no Homebrew Python on the second Mac if possible).

1. Create the standard user; log in; open nothing of the owner's.
2. Copy the dmg in (AirDrop or a shared folder; this is the point where the quarantine flag appears). Open it, drag Dourmouse to `/Applications`.
3. `codesign --verify --deep --strict --verbose=2 /Applications/Dourmouse.app`, `spctl -a -vv /Applications/Dourmouse.app`, and for path B `xcrun stapler validate`. Record the output.
4. First launch. Path A: right-click Open, then System Settings, Privacy and Security, Open Anyway. Expect the window within the first-screen budget in `PERF_BUDGET.md` (a first launch after a fresh copy is slower: macOS scans the bundle).
5. Setup wizard appears (no backend configured). Complete it with a test key or the local-model option. No reference to the owner's folders: `lsof -p <pid>` and `ps -axo command` show only `/Applications/Dourmouse.app/...` paths, no `dourmouse-recon`.
6. macOS prompts, each one seen and answered: Downloads, Documents, Desktop (only when a feature touches them), microphone, camera (only on use), Accessibility and Automation (app driving), Screen Recording if the vision features are on. Note which ones appear at launch and which only on use. A prompt at launch that nothing needs is a bug.
7. Data location: `~/Library/Application Support/Dourmouse/` holds the workspace, logs and state; `/Applications/Dourmouse.app` is byte-for-byte unchanged after a day of use (hash listing before and after).
8. Home, Browser, Media, Settings open; send one message; open one site in the Browser pane; play a video.
9. Crash recovery: end the server process by its PID (child of the app) with `kill -9 <pid>` (the app's own child only). Expect the toast "The server stopped and was restarted" within a few seconds and an alert in the Notification Centre. Do it four times quickly: after the fourth, expect one dialog with the log path.
10. Quit from the menu bar. No `dourmouse.webui` or Dourmouse helper process remains (`pgrep -fl dourmouse`).
11. Relaunch: works, the window is where it was left, the earlier chat is there.
12. Uninstall: drag the app to the Trash; the data folder stays (by design); document the one command to remove it.
13. Run `scripts/perf_check.py --app /Applications/Dourmouse.app` and keep the numbers.

## 7. Rollback

1. Before the first launch of any new version: copy `~/Library/Application Support/Dourmouse/` to `~/Documents/DOURMOUSE/backups/appdata_<date>/` (a normal `cp -R`, never move). The Python side migrates data forward only (state store, memory, config); an older version reading a newer schema is not tested.
2. Keep every released dmg with its SHA-256 and commit hash in `~/Documents/DOURMOUSE/releases/<version>/`. Rolling back is: quit, replace `/Applications/Dourmouse.app` with the previous one from its dmg, restore the data backup if the new version changed the data.
3. Keep `~/Applications/Dourmouse.app` (the live-checkout app from #159) installed and working until a self-contained build has passed the checklist on a clean account and has been used for a week. It is the fallback and it is already known to work.
4. With option 2 updating there is nothing to roll back automatically: the owner installed the new dmg by hand, so rolling back is step 2. With option 1, keep the previous `latest-mac.yml` and zip on the feed and republish them as the latest to roll everyone back.
5. A build that fails the checklist is never copied to `/Applications` on the owner's account.

## 8. Decisions needed from the owner

1. Path A or path B (section 4), and if B, the Apple ID to enrol with.
2. Whether the packaged app may default its write locations to `~/Library/Application Support/Dourmouse/` (section 2A); this changes `main.js`'s packaged branch and one Python module.
3. arm64 only, or both architectures (2C).
4. Option 1 or option 2 for updates (section 5). Option 1 changes dependencies.
5. Whether `productName` becomes `Dourmouse` (2D).
6. Whether the Playwright-launched Chrome fallback has to work on a Mac without Chrome (2G).
