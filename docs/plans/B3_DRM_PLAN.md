# B3: Widevine DRM for the BROWSER screen (design, opt-in install, risks, rollback, tests)

Written 2026-10-03 as part of phase B3. **Nothing in this plan has been run.** No package was
downloaded, `electron/package.json` was not changed, no account was created and no signing was
done. What exists today is (a) the opt-in script `scripts/install_drm_electron.sh` (not executed),
(b) a feature check in `electron/main.js` that never throws when Widevine is absent, and (c) a
"DRM status" line in BROWSER, Site settings that reports what the engine really says. Every
command below is from castLabs's published procedure as I know it and has to be checked against
their current documentation before the first run (these tools change).

## 1. The situation today

* The BROWSER pane runs on stock Electron 44.3 (`electron/node_modules/electron`). Stock Electron
  ships **no Widevine content decryption module (CDM)**, so a page that asks the browser for the
  `com.widevine.alpha` key system is refused. Protected video on Netflix, Disney+, Spotify's web
  player and similar does not play.
* Stock Electron also has no `components` module (`require("electron").components` is
  `undefined`). That is how the code tells the two builds apart, with no guessing.
* The status line (BROWSER, shield button, Site settings) calls the standard `navigator.
  requestMediaKeySystemAccess("com.widevine.alpha", ...)` from a secure page and shows the
  answer. On stock Electron it says plainly that DRM is not available. It says "available" only
  when the engine itself says so.

## 2. The chosen route: castLabs "Electron for Content Security" (ECS)

castLabs publishes a fork of Electron that is the same release (same Chromium, same version
number with a `+wvcus` suffix, for example `v44.3.0+wvcus`) plus: the Widevine CDM downloaded and
registered at first start through a `components` module, and a way to **VMP-sign** the runtime
(see section 3). The repository is `https://github.com/castlabs/electron-releases`. It installs
into the same `node_modules/electron`, so `electron/main.js` and `scripts/install_app.sh` need
no other change.

Routes considered and rejected:

* Copying the CDM out of an installed Chrome: that breaks Google's terms for the CDM and the
  version will not match. Not done.
* Embedding Chromium through CEF or another browser engine: a new engine under a working app,
  and it contradicts "extend the pattern that exists" (HARD_RULES 9).
* Opening protected sites in the owner's real Chrome: possible today with the "open in browser"
  action, and honest, but it is not the BROWSER screen.

## 3. What each kind of site needs

Widevine has a software level (L3) and hardware levels (L1, L2). Desktop Chrome on a Mac gets L3;
that is also what ECS provides. Separately, some services refuse to give a licence unless the
host application proves it has not been tampered with, using the **Verified Media Path (VMP)**
signature. castLabs signs a runtime for VMP through its EVS service.

| Needs | Examples (from the brief, not verified one by one by me) | What it takes |
| --- | --- | --- |
| Nothing (no DRM) | most of the web, most of YouTube | stock Electron is enough |
| Plain Widevine L3, no VMP | YouTube Premium and DRM titles, Spotify Web, many others | the ECS build, no signing |
| Widevine L3 plus a VMP-signed host | Netflix, Disney+ and similar | the ECS build **and** VMP signing of the runtime |

Be honest about what is unknown: which side of the table a given service falls on can change
without notice. Step 6 below is how the owner finds out for their own sites. Hardware levels
(L1, 4K, HDR) are not available to Electron on macOS; expect standard definition to 720p on
services that cap software-only players.

## 4. Steps, with the exact commands

All paths are in `~/dourmouse-recon` (HARD_RULES 1). The script does steps 1 to 4 (and 5 and 7
with flags) and prints the plan when run with no arguments.

```bash
cd ~/dourmouse-recon
bash scripts/install_drm_electron.sh                  # prints the plan, changes nothing
```

**Step 1. Back up.** `bash scripts/install_drm_electron.sh --yes` copies `electron/package.json`
and `electron/package-lock.json` to `~/Documents/DOURMOUSE/backups/drm-<timestamp>/` with a note
of the stock version.

**Step 2. Install ECS.** Same command, in the script:

```bash
cd electron
git ls-remote --tags https://github.com/castlabs/electron-releases | grep 'v44\..*+wvcus' | tail   # see what exists
npm install --no-save "electron@github:castlabs/electron-releases#v44.3.0+wvcus"                  # use a tag that exists
```

`--no-save` keeps `package.json` untouched (a later plain `npm install` goes back to stock, which
is the safest default). Pass `--save` to the script to write the dependency into
`electron/package.json`; that is then an owner decision, committed by the owner.

**Step 3. Check the runtime.** The script starts a throwaway app that prints
`process.versions` and `typeof components`, then waits for `components.whenReady()` and prints
`components.status()`. On the first run the CDM is downloaded, so it needs the network and can
take a minute. Expected: `components: "object"` and a Widevine entry.

**Step 4. Look at the app.** Quit and reopen Dourmouse. BROWSER, shield button (Site settings):
the DRM line reads "Widevine is available (software (L3))". The same answer without the UI:
`GET /api/os/browser/drm` (read-only; works only inside the Electron app).

**Step 5. VMP signing (only if you want Netflix or Disney+ class sites).**

```bash
bash scripts/install_drm_electron.sh --yes --vmp
```

This creates a private virtualenv at `~/.dourmouse/evs-venv`, installs `castlabs-evs`, and signs
`electron/node_modules/electron/dist`. **You** create the free EVS account and sign in; the script
never does that and never stores a credential:

```bash
~/.dourmouse/evs-venv/bin/python -m castlabs_evs.account signup    # once
~/.dourmouse/evs-venv/bin/python -m castlabs_evs.account reauth    # when it asks
```

By hand, the signing command itself is (the script runs the same one):

```bash
~/.dourmouse/evs-venv/bin/python -m castlabs_evs.vmp sign-pkg ~/dourmouse-recon/electron/node_modules/electron/dist
```

Order matters on macOS: VMP signing first, `codesign` after. `scripts/install_app.sh` already
ends with an ad-hoc `codesign --force --deep --sign -`, so the sequence "sign the runtime, then
run install_app.sh" is the right one. The installer keeps the runtime's executable name (it only
changes Info.plist and adds the payload), which is what lets the signature files stay valid. That
is my reading and has to be confirmed by Step 7's tests.

**Step 6. Rebuild the app.** `bash scripts/install_drm_electron.sh --yes --reinstall-app`, or
`bash scripts/install_app.sh` by hand. The old bundle goes to the Trash, not deleted.

## 5. What main.js does (already in place)

* `drm.startDrm(electron)` is called once after the app is ready and never blocks start-up. It
  uses `components.whenReady()` **only if `components` exists**; on stock Electron it returns at
  once with "no components". It has a 20 second limit and catches everything, so it cannot throw.
* `DOURMOUSE_DRM=0` turns it off even on an ECS build.
* The pane's permission handler answers `mediaKeySystem` (the key-system request) with "yes"
  **only** when the component reported ready, and only for the page the owner is on (never a
  frame from another origin). On stock Electron it stays refused.
* Reading the status is console-only over IPC (`drm:status`) and read-only over the bridge
  (`GET /drm`, `/status`). Nothing about DRM can be changed from the bridge.

## 6. Risks

1. **Supply chain.** castLabs is a third party; the build is their fork of Electron. Pin the tag,
   read the release notes, and compare its Chromium security level with the stock release of the
   same number (the fork can lag). Do not run `npm update` blindly on it.
2. **Terms.** The Widevine CDM and the services have their own terms. Personal use on the
   owner's own Mac is the case here; distributing a build to others is not covered by this plan
   (check castLabs's and Google's terms first). Some services may forbid third-party players
   whatever the signature says.
3. **Identifier and privacy.** A CDM can keep a per-site identifier. Allowing the key system
   silently is what Chrome does by default, but it is a tracking surface that stock Electron did
   not have. Mitigations: `DOURMOUSE_DRM=0`; use a separate profile for DRM sites; the
   per-profile partitions keep the identifiers apart.
4. **Signing.** The EVS signature is tied to an account of the owner, and the signed runtime
   may stop being accepted if the certificate or the account lapses. Re-sign when that happens.
5. **Ad-hoc re-signing.** If a service rejects the app after `install_app.sh`, the order of
   VMP signing and `codesign` is the first suspect (section 4, Step 5).
6. **Not a security feature of Dourmouse.** DRM does not weaken the owner secret, the permission
   prompts, the password vault or the model's sandbox, and none of them depends on it.

## 7. Rollback

At any point:

```bash
cd ~/dourmouse-recon/electron
cp ~/Documents/DOURMOUSE/backups/drm-<timestamp>/package.json package.json
cp ~/Documents/DOURMOUSE/backups/drm-<timestamp>/package-lock.json package-lock.json
npm ci                                            # puts stock Electron back
cd .. && bash scripts/install_app.sh              # re-clones the stock runtime into Dourmouse.app
rm -rf ~/.dourmouse/evs-venv                      # only if you did the signing step
```

If `--save` was never used, `cd electron && npm install` alone returns to stock. The old app
bundle is in `~/.Trash` as `Dourmouse-replaced-<timestamp>.app` if the new one will not start.
Quick off switch without reinstalling: start the app with `DOURMOUSE_DRM=0`.

## 8. Tests the owner should run after Step 4 (and again after Step 5)

Record the result of each; a failed test is information, not a reason to loosen anything.

1. **Status line.** BROWSER, Site settings: reads "Widevine is available". On stock it must read
   "DRM: not available ... stock Electron". Expected before the install: "not available".
2. **API answer.** `curl -s http://127.0.0.1:8765/api/os/browser/drm` inside the app session:
   `"widevine": "available"`, `"ready": true`. (Works only when the server was started by the Electron app, which is what gives it the bridge port.)
3. **A public DRM demo.** Shaka Player's demo page (`https://shaka-player-demo.appspot.com/`):
   choose an asset marked "Widevine" and play it. Bitmovin's DRM demo is a second check. Pass:
   it plays. Expected on stock: fails with a key-system error.
4. **Plain-L3 services** (your own accounts): YouTube (a rental or Premium title), Spotify Web.
   Pass: it plays. Fail on stock: a "protected content" error.
5. **VMP services** (after Step 5, your own accounts): Netflix, Disney+. Expected before
   signing: refused or "unsupported browser". Pass after signing: plays at software quality.
6. **Nothing else changed.** A prompt for the camera still appears in the bar and still obeys
   the kill switch; saved passwords still ask twice before they are shown; `GET /api/os/browser/permissions`
   still lists only the six permissions. DRM must not add to that list.
7. **Rollback drill (once).** Run Section 7 on a copy of the checkout or just read it through
   against the backup folder, and confirm the DRM line goes back to "not available".

## 9. Not done, on purpose

No download, no `npm`, no account, no signing, no edit to `electron/package.json`, no change to
`scripts/install_app.sh`. The script and this plan are the whole deliverable for DRM in B3.
