/**
 * Supabase Auth session persistence for the Electron desktop app.
 *
 * PROBLEM
 * -------
 * supabase-js defaults `auth.storage` to `window.localStorage`. In Electron
 * that is a plaintext LevelDB under the app's userData directory: the refresh
 * token -- a long-lived, silently-renewing credential -- sits on disk in the
 * clear, readable by anything that can read the user's files. It also does not
 * exist at all in the main process, so any main-process code path gets
 * "localStorage is not defined" rather than a session.
 *
 * SHAPE OF THE FIX
 * ----------------
 * supabase-js accepts any object implementing getItem/setItem/removeItem, and
 * -- this is the part that makes an OS keystore usable at all -- each may
 * return a Promise. So the adapter can do real async IPC.
 *
 *   renderer  --IPC-->  main process  -->  safeStorage  -->  OS keystore
 *
 * THE CONSTRAINT PEOPLE GET WRONG
 * -------------------------------
 * `safeStorage` is a MAIN-PROCESS-ONLY API. A renderer cannot call it, and
 * with `contextIsolation: true` + `nodeIntegration: false` (the correct
 * settings, and the ones this assumes) the renderer cannot even require
 * `electron`. There is no way around this and no renderer-side shim that makes
 * it work. The renderer therefore MUST go through IPC to a main-process
 * handler. That handler is `registerSecureStorageIpc()` in
 * `./secure-storage.main.ts`; the bridge that exposes it is
 * `exposeSecureStorageBridge()` below, which runs in a preload script.
 *
 * WHAT IS AND IS NOT ENCRYPTED AT REST -- read this before trusting it
 * -------------------------------------------------------------------
 * When `safeStorage.isEncryptionAvailable()` is true, the ciphertext written
 * to disk is produced by:
 *   * macOS   -- Keychain-held AES key.
 *   * Windows -- DPAPI, scoped to the logged-in Windows user account.
 *   * Linux   -- libsecret (GNOME Keyring / KWallet) IF one is present.
 *
 * What that actually buys, stated honestly:
 *   * A DIFFERENT OS user, or someone reading the raw disk / a backup / a
 *     synced copy of userData, cannot recover the token. This is the real,
 *     worthwhile win, and it is the whole reason for this file.
 *   * It does NOT protect against code already running as THIS user. Any
 *     process with this user's identity can ask the same keystore to decrypt
 *     the same blob. Malware in the user's session defeats this, and no
 *     client-side scheme can prevent that. Do not describe this as making the
 *     token "safe" -- it makes it not-plaintext-on-disk.
 *   * On Linux with no keyring, Chromium falls back to a hardcoded-password
 *     "basic_text" backend. That is obfuscation, not encryption, and
 *     `isEncryptionAvailable()` still reports true for it. This is a real
 *     documented Electron behaviour, not a hypothetical; see
 *     `secureStorageBackendNote()` below, which surfaces it rather than
 *     letting the app claim protection it does not have.
 *
 * FALLBACK
 * --------
 * If encryption is genuinely unavailable, the main-process handler writes the
 * value to `electron-store` UNENCRYPTED and flags it. It is flagged in the
 * stored record itself (`enc: false`), reported by `secureStorageBackendNote()`
 * and logged once at startup. The alternative -- refusing to persist -- would
 * mean the user re-authenticates on every launch, which is the thing this
 * whole file exists to prevent. Degrading loudly beats degrading silently, and
 * beats not working at all; but the app must not tell the user their session
 * is encrypted when this branch is live.
 */

/** IPC channels. Namespaced so they cannot collide with app channels. */
export const SECURE_STORAGE_CHANNELS = {
  get: 'dourmouse:secure-storage:get',
  set: 'dourmouse:secure-storage:set',
  remove: 'dourmouse:secure-storage:remove',
  backend: 'dourmouse:secure-storage:backend',
} as const

/** What the preload script puts on `window`. */
export interface SecureStorageBridge {
  get(key: string): Promise<string | null>
  set(key: string, value: string): Promise<void>
  remove(key: string): Promise<void>
  /** `{ encrypted: false }` means the token is on disk in the clear. */
  backend(): Promise<{ encrypted: boolean; backend: string }>
}

declare global {
  interface Window {
    dourmouseSecureStorage?: SecureStorageBridge
  }
}

/**
 * Called from a PRELOAD script (not the renderer, not main).
 *
 * Kept here beside the adapter deliberately: the channel names, the argument
 * shapes and the consuming adapter drift apart the moment they live in
 * different files, and a drifted channel name fails as a hung Promise rather
 * than a visible error.
 *
 * Usage in preload.ts:
 *   import { contextBridge, ipcRenderer } from 'electron'
 *   import { exposeSecureStorageBridge } from '../supabase/secure-storage'
 *   exposeSecureStorageBridge(contextBridge, ipcRenderer)
 */
export function exposeSecureStorageBridge(
  contextBridge: { exposeInMainWorld(key: string, api: unknown): void },
  ipcRenderer: { invoke(channel: string, ...args: unknown[]): Promise<unknown> },
): void {
  const bridge: SecureStorageBridge = {
    get: (key) => ipcRenderer.invoke(SECURE_STORAGE_CHANNELS.get, key) as Promise<string | null>,
    set: (key, value) => ipcRenderer.invoke(SECURE_STORAGE_CHANNELS.set, key, value) as Promise<void>,
    remove: (key) => ipcRenderer.invoke(SECURE_STORAGE_CHANNELS.remove, key) as Promise<void>,
    backend: () =>
      ipcRenderer.invoke(SECURE_STORAGE_CHANNELS.backend) as Promise<{
        encrypted: boolean
        backend: string
      }>,
  }
  // Only these four methods cross the bridge -- never `ipcRenderer` itself,
  // which would hand the renderer the ability to invoke every channel the main
  // process listens on.
  contextBridge.exposeInMainWorld('dourmouseSecureStorage', bridge)
}

/**
 * The storage adapter handed to `createClient`.
 *
 * supabase-js's SupportedStorage interface, satisfied with async methods.
 *
 * Failure policy, and it is deliberate: a storage error is swallowed and
 * treated as "no stored session", never rethrown. supabase-js calls getItem
 * during client construction and on every token refresh; letting an IPC
 * failure propagate turns a recoverable "you'll have to sign in again" into an
 * unhandled rejection inside the auth refresh timer, where nothing is
 * listening. The user-visible consequence of returning null is a login prompt,
 * which is the correct behaviour when the session genuinely cannot be read.
 */
export function createSecureStorageAdapter(): {
  getItem(key: string): Promise<string | null>
  setItem(key: string, value: string): Promise<void>
  removeItem(key: string): Promise<void>
} {
  const bridge = (): SecureStorageBridge | null =>
    (typeof window !== 'undefined' && window.dourmouseSecureStorage) || null

  return {
    async getItem(key) {
      const b = bridge()
      if (!b) return fallbackLocalStorage.getItem(key)
      try {
        return await b.get(key)
      } catch {
        return null
      }
    },
    async setItem(key, value) {
      const b = bridge()
      if (!b) return fallbackLocalStorage.setItem(key, value)
      try {
        await b.set(key, value)
      } catch {
        /* not persisted: the user signs in again next launch */
      }
    },
    async removeItem(key) {
      const b = bridge()
      if (!b) return fallbackLocalStorage.removeItem(key)
      try {
        await b.remove(key)
      } catch {
        /* see above */
      }
    },
  }
}

/**
 * Used only when the preload bridge is absent: a plain browser tab, a test
 * runner, or an Electron window misconfigured without the preload script.
 *
 * This is NOT encrypted. It exists so the same client module works in a normal
 * browser during development instead of throwing; in the packaged desktop app
 * the bridge is always present, and if it is not, that is a wiring bug worth
 * noticing -- hence the warning.
 */
const fallbackLocalStorage = {
  getItem(key: string): string | null {
    if (typeof localStorage === 'undefined') return null
    warnOnce()
    return localStorage.getItem(key)
  },
  setItem(key: string, value: string): void {
    if (typeof localStorage === 'undefined') return
    warnOnce()
    localStorage.setItem(key, value)
  },
  removeItem(key: string): void {
    if (typeof localStorage === 'undefined') return
    localStorage.removeItem(key)
  },
}

let warned = false
function warnOnce(): void {
  if (warned) return
  warned = true
  console.warn(
    '[supabase] secure storage bridge not found -- falling back to plaintext ' +
      'localStorage. In the desktop app this means the preload script did not ' +
      'run: the Supabase refresh token is being written to disk UNENCRYPTED.',
  )
}

/**
 * Ask the main process what protection is actually in force, so the UI can
 * state the true answer instead of assuming the good one. Returns the
 * pessimistic answer when it cannot tell.
 */
export async function secureStorageBackendNote(): Promise<{
  encrypted: boolean
  backend: string
}> {
  const b = typeof window !== 'undefined' ? window.dourmouseSecureStorage : null
  if (!b) return { encrypted: false, backend: 'plaintext-localstorage' }
  try {
    return await b.backend()
  } catch {
    return { encrypted: false, backend: 'unknown' }
  }
}
