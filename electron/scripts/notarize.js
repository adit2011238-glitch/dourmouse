// electron-builder afterSign hook (Stage E — see
// ~/.claude/plans/sorted-wiggling-pearl.md). Real notarization via
// @electron/notarize, not a stub — but only when real Apple credentials
// are present (Rule 2.1/2.2 honesty: NOT CONFIGURED, never a fabricated
// "notarized" claim), matching build_dist.sh's own existing codesign/
// notarize section's exact fallback discipline.
//
// Three real credential shapes are supported, checked in this order:
//   1. DOURMOUSE_NOTARY_PROFILE -- a keychain profile created ONCE via
//      `xcrun notarytool store-credentials "dourmouse-notary" --apple-id
//      you@... --team-id TEAMID --password <app-specific password>`.
//      Same env var name AND same one-time setup build_dist.sh's own
//      comment already documents -- if you already did that for the
//      pywebview build, it works here unchanged, no new setup.
//   2. APPLE_ID + APPLE_APP_SPECIFIC_PASSWORD + APPLE_TEAM_ID -- Apple's
//      own plain credential triple, @electron/notarize's other supported
//      shape.
//   3. Neither present -- skips notarization with an honest, actionable
//      message. The app is still signed (if DOURMOUSE_SIGN_IDENTITY/
//      CSC_NAME was set) but Gatekeeper will warn on other Macs until a
//      real notarization completes, exactly like the unnotarized branch
//      of build_dist.sh's own codesign section.
//
// Neither an Apple Developer Program membership nor a notarytool
// credential profile is something this script (or Claude) can create --
// both need the account holder's own Apple ID login, same disclosed
// limitation build_dist.sh's own comment already carries.

const { notarize } = require("@electron/notarize");

module.exports = async function afterSign(context) {
  const { electronPlatformName, appOutDir, packager } = context;
  if (electronPlatformName !== "darwin") return;

  const appName = packager.appInfo.productFilename;
  const appPath = `${appOutDir}/${appName}.app`;

  const keychainProfile = process.env.DOURMOUSE_NOTARY_PROFILE;
  const appleId = process.env.APPLE_ID;
  const applePassword = process.env.APPLE_APP_SPECIFIC_PASSWORD;
  const teamId = process.env.APPLE_TEAM_ID;

  if (!keychainProfile && !(appleId && applePassword && teamId)) {
    console.log(
      "[notarize] DOURMOUSE_NOTARY_PROFILE not set (and no APPLE_ID/" +
        "APPLE_APP_SPECIFIC_PASSWORD/APPLE_TEAM_ID triple either) -- " +
        "skipping notarization. The app is still signed if " +
        "DOURMOUSE_SIGN_IDENTITY was set, but Gatekeeper will warn on " +
        "other Macs until it's really notarized. One-time setup: " +
        '`xcrun notarytool store-credentials "dourmouse-notary" ' +
        "--apple-id you@... --team-id TEAMID --password <app-specific " +
        "password from appleid.apple.com>`, then set " +
        "DOURMOUSE_NOTARY_PROFILE=dourmouse-notary."
    );
    return;
  }

  console.log(`[notarize] submitting ${appPath} to Apple (this uploads and waits — a few minutes)...`);
  const options = keychainProfile
    ? { appPath, keychainProfile, tool: "notarytool" }
    : { appPath, appleId, appleIdPassword: applePassword, teamId, tool: "notarytool" };

  await notarize(options);
  console.log("[notarize] done — this build now opens cleanly on any Mac.");
};
