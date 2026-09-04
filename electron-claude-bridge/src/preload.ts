/**
 * preload.ts — the only bridge between main and renderer.
 *
 * Exposes four functions and nothing else. In particular it does NOT expose
 * `ipcRenderer` itself: handing the renderer a general-purpose IPC object
 * would let any script it loads reach every channel in the app, which is the
 * whole failure mode contextIsolation exists to prevent.
 *
 * The channel names are constants here, not parameters, so the renderer
 * cannot address a channel of its own choosing.
 */

import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron';

const CHANNEL = {
  start: 'claude:start',
  cancel: 'claude:cancel',
  status: 'claude:status',
  event: 'claude:event',
} as const;

export type ClaudeEvent =
  | { type: 'start'; sessionId: string; model?: string }
  | { type: 'thinking'; sessionId: string; text: string }
  | { type: 'text'; sessionId: string; text: string }
  | { type: 'tool'; sessionId: string; name: string }
  | {
      type: 'usage';
      sessionId: string;
      costUsd: number | null;
      inputTokens: number;
      outputTokens: number;
      cacheCreationInputTokens: number;
      cacheReadInputTokens: number;
    }
  | { type: 'done'; sessionId: string; finalText: string }
  | { type: 'error'; sessionId: string; message: string };

export interface ClaudeStatus {
  ready: boolean;
  cliPath: string | null;
  credentialSource: string;
}

const api = {
  /** Is the CLI present, and where does its login come from? */
  status: (): Promise<ClaudeStatus> => ipcRenderer.invoke(CHANNEL.status),

  /** Start a run. Resolves to the session id used to address its events. */
  start: (prompt: string): Promise<string> => ipcRenderer.invoke(CHANNEL.start, prompt),

  /** Cancel a run this window started. */
  cancel: (sessionId: string): Promise<boolean> =>
    ipcRenderer.invoke(CHANNEL.cancel, sessionId),

  /**
   * Subscribe to this window's stream. Returns an unsubscribe function.
   *
   * The raw IpcRendererEvent is deliberately not passed through — the
   * renderer gets the payload only, never a handle it could use to reply on
   * an arbitrary channel.
   */
  onEvent: (handler: (event: ClaudeEvent) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, payload: ClaudeEvent) => handler(payload);
    ipcRenderer.on(CHANNEL.event, listener);
    return () => ipcRenderer.removeListener(CHANNEL.event, listener);
  },
};

contextBridge.exposeInMainWorld('claude', api);

export type ClaudeApi = typeof api;

declare global {
  interface Window {
    claude: ClaudeApi;
  }
}
