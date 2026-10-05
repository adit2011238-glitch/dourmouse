/* The first-run permission walkthrough (phase F2): which macOS permissions
   Dourmouse uses, what each one enables, and where to switch it on.

   Only permissions a feature really uses get a button. Screen Recording is
   listed because people ask, and says plainly that nothing uses it today: no
   feature here asks for a permission it does not use.

   Whether the guide has been seen is kept in the server's own settings store
   (POST /api/state/prefs, read back from GET /api/state), so it survives a
   restart and is shared by every window. */

import { paneUrl } from './helpers.js';

export const SEEN_KEY = 'os.permissions_walkthrough_seen';

export const PERMISSIONS = [
  {
    id: 'accessibility',
    name: 'Accessibility',
    pane: 'Privacy_Accessibility',
    paneName: 'Accessibility',
    used: true,
    uses: 'App driving and control',
    enables: 'Lets Dourmouse read and operate other apps: press buttons, type, scroll, click menus. App driving only works on apps you allow on this screen and never reaches a terminal, System Settings, a password manager or Dourmouse itself. Without this permission Dourmouse cannot control any other app.',
    how: 'Switch Dourmouse on in the list. If it is not listed, add it with the plus button.',
  },
  {
    id: 'screen',
    name: 'Screen Recording',
    pane: 'Privacy_ScreenCapture',
    paneName: 'Screen Recording',
    used: false,
    uses: 'Not used today',
    enables: 'Dourmouse does not record or capture your screen and does not ask for this. If a future feature ever needs it, it will say so first and macOS will ask then.',
    how: '',
  },
  {
    id: 'microphone',
    name: 'Microphone',
    pane: 'Privacy_Microphone',
    paneName: 'Microphone',
    used: true,
    uses: 'Voice commands',
    enables: 'Lets you speak to Dourmouse on the VOICE screen. The optional wake word listener (off unless you turn it on) also needs it. A website in BROWSER can also ask to use it, and asks you for that site first. Nothing listens until you start it.',
    how: 'Switch Dourmouse on in the list.',
  },
  {
    id: 'camera',
    name: 'Camera',
    pane: 'Privacy_Camera',
    paneName: 'Camera',
    used: true,
    uses: 'Hand tracking',
    enables: 'Used by the optional hand tracking in the classic console, and only after you start it. A website in BROWSER can also ask to use it, and asks you for that site first.',
    how: 'Switch Dourmouse on in the list.',
  },
  {
    id: 'files',
    name: 'Files and Folders',
    pane: 'Privacy_FilesAndFolders',
    paneName: 'Files & Folders',
    used: true,
    uses: 'Folder access',
    enables: 'Dourmouse checks new files in Downloads for risk, and the file librarian reads Documents, Desktop and Downloads to index them. macOS asks the first time each folder is touched. Say yes only to the folders you want covered.',
    how: 'Switch Dourmouse on for each folder you want covered.',
  },
];

/* What a click on a button does: try the host, and when this window cannot open
   System Settings, say where to go instead of pretending it worked. */
export function openPane(ctx, perm) {
  if (ctx.host.openExternal(paneUrl(perm.pane))) return { opened: true, text: 'System Settings is open on ' + perm.paneName + '.' };
  return {
    opened: false,
    text: 'This window cannot open System Settings by itself. Open System Settings yourself, then Privacy & Security, then ' + perm.paneName + '.',
  };
}

/* true: seen. false: not yet. null: could not tell (the state read failed or came from a stale copy). */
export async function readSeen(ctx) {
  try {
    const state = await ctx.api.get('/api/state');
    if (ctx.api.isStale(state)) return null;
    const prefs = state && state.prefs && typeof state.prefs === 'object' ? state.prefs : {};
    return prefs[SEEN_KEY] === true;
  } catch (_err) {
    return null;
  }
}

/* Records it. Returns '' on success or the server's own words. */
export async function markSeen(ctx) {
  try {
    await ctx.api.post('/api/state/prefs', { key: SEEN_KEY, value: true });
    return '';
  } catch (err) {
    return err && err.message ? err.message : String(err);
  }
}
