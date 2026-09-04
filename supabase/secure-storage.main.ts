/**
 * MAIN-PROCESS half of the Supabase session store. Import this ONLY from the
 * Electron main process -- it requires `electron`'s main-process APIs and will
 * not load in a renderer.
 *
 * Call `registerSecureStorageIpc()` once, AFTER `app.whenReady()`. Before the
 * ready event `safeStorage.isEncryptionAvailable()` is not reliable on Linux
 * (the keyring backend has not been resolved yet) and on macOS can trigger a
 * premature Keychain prompt.
 *
 *   import { app } from 'electron'
 *   import { registerSecureStorageIpc } from '../supabase/secure-storage.main'
 *   app.whenReady().then(() => { registerSecureStorageIpc(); createWindow() })
 *
 * See ./secure-storage.ts for the full note on what OS-level encryption does
 * and does not protect against. The short version: it defends the token
 * against another OS user and against anyone reading the disk or a backup; it
 * does not defend against code already running as this user.
 */
import { ipcMain, safeStorage } from 'electron'
import Store from 'electron-store'

/**
 * One record per key. `enc` records how THIS value was written, rather than
 * being inferred at read time from the current platform state -- those two can
 * disagree. A real case: a value written on a Linux box while gnome-keyring
 * was running, then read after a reboot where it was not. Reading a
 * ciphertext as though it were plaintext yields mojibake that would be handed
 * to JSON.parse as if it were a session; reading plaintext as ciphertext
 * throws. Storing the flag alongside the value makes the read deterministic.
 */
interface StoredValue {
  enc: boolean
  /** base64 ciphertext when enc, else the raw string. */
  v: string
}

/**
 * `electron-store` is the on-disk container, NOT the encryption. Its own
 * `encryptionKey` option is explicitly documented as obfuscation only -- the
 * key ships inside the app bundle -- so it is deliberately not used here; it
 * would add the appearance of protection and none of the substance.
 * safeStorage does the real work; this just holds bytes.
 */
const store = new Store<Record<string, StoredValue>>({
  name: 'dourmouse-auth',
  // The session belongs to this machine and this OS user. Never sync it.
  clearInvalidConfig: true,
})

let encryptionAvailable = false
let backendLabel = 'uninitialised'

function resolveBackend(): void {
  try {
    encryptionAvailable = safeStorage.isEncryptionAvailable()
  } catch {
    encryptionAvailable = false
  }

  if (!encryptionAvailable) {
    backendLabel = 'none (plaintext on disk)'
    return
  }

  if (process.platform === 'linux') {
    // Electron >= 25 exposes the resolved backend. `basic_text` is Chromium's
    // no-keyring fallback: a HARDCODED password, i.e. obfuscation. It still
    // reports isEncryptionAvailable() === true, which is exactly why this is
    // checked explicitly instead of trusting that boolean.
    const linuxBackend = (
      safeStorage as unknown as { getSelectedStorageBackend?: () => string }
    ).getSelectedStorageBackend?.()
    if (linuxBackend === 'basic_text') {
      encryptionAvailable = true // we still use it -- it is better than raw plaintext
      backendLabel = 'basic_text (NOT real encryption -- no keyring present)'
      return
    }
    backendLabel = linuxBackend ? `linux:${linuxBackend}` : 'linux:unknown'
    return
  }

  backendLabel = process.platform === 'darwin' ? 'macos:keychain' : 'windows:dpapi'
}

/** True only when the value on disk is protected by a real OS-held key. */
function isTrulyEncrypted(): boolean {
  return encryptionAvailable && !backendLabel.startsWith('basic_text')
}

export function registerSecureStorageIpc(): void {
  resolveBackend()

  if (!isTrulyEncrypted()) {
    console.warn(
      `[supabase] session persistence is NOT encrypted at rest (backend: ${backendLabel}). ` +
        'The Supabase refresh token is recoverable by anything that can read ' +
        `${store.path}.`,
    )
  }

  ipcMain.handle(
    'dourmouse:secure-storage:get',
    (_event, key: string): string | null => {
      if (typeof key !== 'string') return null
      const rec = store.get(key) as StoredValue | undefined
      if (!rec || typeof rec.v !== 'string') return null
      if (!rec.enc) return rec.v
      try {
        return safeStorage.decryptString(Buffer.from(rec.v, 'base64'))
      } catch {
        // Undecryptable: the OS key is gone (Keychain reset, new Windows
        // profile, keyring wiped). The stored bytes are permanently useless,
        // so drop them rather than returning a value that will fail to parse
        // on every future launch. The user signs in once and recovers.
        store.delete(key)
        return null
      }
    },
  )

  ipcMain.handle(
    'dourmouse:secure-storage:set',
    (_event, key: string, value: string): void => {
      if (typeof key !== 'string' || typeof value !== 'string') return
      if (encryptionAvailable) {
        try {
          store.set(key, {
            enc: true,
            v: safeStorage.encryptString(value).toString('base64'),
          })
          return
        } catch {
          // Fall through to the plaintext branch rather than losing the
          // session entirely -- flagged as enc:false so the read path is
          // still correct.
        }
      }
      store.set(key, { enc: false, v: value })
    },
  )

  ipcMain.handle('dourmouse:secure-storage:remove', (_event, key: string): void => {
    if (typeof key !== 'string') return
    store.delete(key)
  })

  ipcMain.handle('dourmouse:secure-storage:backend', () => ({
    encrypted: isTrulyEncrypted(),
    backend: backendLabel,
  }))
}
