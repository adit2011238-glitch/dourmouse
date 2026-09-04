/**
 * main.ts — Electron main process.
 *
 * Owns every ClaudeSession and is the only place a child process is ever
 * spawned. The renderer can ask for a run and can cancel one; it can do
 * nothing else, and in particular it cannot influence the argv or the
 * environment the CLI is launched with.
 *
 * Security posture, deliberate:
 *   - contextIsolation on, nodeIntegration off, sandbox on.
 *   - No `remote`, no `nodeIntegrationInSubFrames`.
 *   - Every IPC channel is a fixed string. The renderer never names a
 *     binary, a flag, a cwd or an env var.
 *   - Events are addressed to the exact WebContents that started the run,
 *     so one window can never observe another window's stream.
 */

import { app, BrowserWindow, ipcMain, shell, type WebContents } from 'electron';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';

import { ClaudeSession, resolveClaudeCli, type ClaudeEvent } from './claude-session';

const CHANNEL = {
  start: 'claude:start',
  cancel: 'claude:cancel',
  status: 'claude:status',
  event: 'claude:event',
} as const;

/** sessionId -> { session, owner }. Owner is the window that started it. */
const sessions = new Map<string, { session: ClaudeSession; owner: WebContents }>();

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    backgroundColor: '#09090b',
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  // Anything trying to open a new window goes to the real browser instead,
  // and never becomes an Electron window with our preload attached.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) void shell.openExternal(url);
    return { action: 'deny' };
  });

  void win.loadFile(join(__dirname, '..', 'renderer', 'index.html'));
  return win;
}

/* -------------------------------------------------------------------------- */
/* IPC                                                                        */
/* -------------------------------------------------------------------------- */

ipcMain.handle(CHANNEL.status, () => {
  const cli = resolveClaudeCli();
  return {
    ready: cli !== null,
    cliPath: cli,
    // Stated for the UI's benefit; nothing here reads the credential itself.
    credentialSource:
      process.platform === 'darwin'
        ? 'macOS Keychain — service "Claude Code-credentials"'
        : '~/.claude/.credentials.json',
  };
});

ipcMain.handle(CHANNEL.start, (event, rawPrompt: unknown) => {
  const prompt = typeof rawPrompt === 'string' ? rawPrompt.trim() : '';
  if (!prompt) throw new Error('A prompt is required.');
  if (prompt.length > 100_000) throw new Error('Prompt is too large.');

  const sessionId = randomUUID();
  const owner = event.sender;

  const session = new ClaudeSession(sessionId, (ev: ClaudeEvent) => {
    // Addressed delivery: only the window that started this run is told.
    if (!owner.isDestroyed()) owner.send(CHANNEL.event, ev);
    if (ev.type === 'done' || ev.type === 'error') sessions.delete(sessionId);
  });

  sessions.set(sessionId, { session, owner });
  session.run({ prompt, timeoutMs: 10 * 60_000 });
  return sessionId;
});

ipcMain.handle(CHANNEL.cancel, (event, rawId: unknown) => {
  const id = typeof rawId === 'string' ? rawId : '';
  const entry = sessions.get(id);
  // A window may only cancel a run it actually started.
  if (!entry || entry.owner !== event.sender) return false;
  entry.session.cancel();
  sessions.delete(id);
  return true;
});

/* -------------------------------------------------------------------------- */
/* Lifecycle                                                                  */
/* -------------------------------------------------------------------------- */

function killSessionsOwnedBy(contents: WebContents): void {
  for (const [id, entry] of sessions) {
    if (entry.owner === contents) {
      entry.session.cancel('window closed');
      sessions.delete(id);
    }
  }
}

app.on('web-contents-created', (_e, contents) => {
  contents.on('destroyed', () => killSessionsOwnedBy(contents));
});

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// A CLI left running after quit would keep a detached child alive.
app.on('before-quit', () => {
  for (const [, entry] of sessions) entry.session.cancel('app quitting');
  sessions.clear();
});
