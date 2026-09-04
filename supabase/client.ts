/**
 * The one and only place a Supabase client is constructed.
 *
 * Tier 1 rule 4, enforced rather than merely documented: this module can
 * only ever hold a restricted key. The service_role key bypasses every RLS
 * policy in the database, so a single leak of it into a client bundle
 * hands anyone who unzips that bundle full read/write on every row. The
 * guard below fails the build loudly if one is ever pasted in, instead of
 * shipping quietly.
 *
 * Regenerate the types after ANY schema change:
 *   supabase gen types typescript --project-id hpmruavpiloegvhdvcib \
 *     > supabase/types/database.types.ts
 */
import { createClient } from '@supabase/supabase-js'
import type { Database } from './types/database.types'
import { createSecureStorageAdapter } from './secure-storage'

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL
const supabaseKey = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY

if (!supabaseUrl || !supabaseKey) {
  throw new Error(
    'NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY must both be set.',
  )
}

/**
 * A service_role key is a JWT whose payload carries `"role":"service_role"`,
 * or a `sb_secret_...` string. Either one appearing here is a real incident,
 * not a typo to shrug at -- so refuse to construct the client at all.
 */
function assertNotAServiceRoleKey(key: string): void {
  if (key.startsWith('sb_secret_')) {
    throw new Error('Refusing to start: a Supabase SECRET key was passed to the client.')
  }
  const parts = key.split('.')
  if (parts.length === 3) {
    try {
      const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
      if (payload?.role === 'service_role') {
        throw new Error('Refusing to start: the SERVICE_ROLE key was passed to the client.')
      }
    } catch (err) {
      // A key we cannot decode is not automatically a service_role key --
      // re-throw only our own assertion, never a JSON/base64 parse failure.
      if (err instanceof Error && err.message.startsWith('Refusing to start')) throw err
    }
  }
}

assertNotAServiceRoleKey(supabaseKey)

/**
 * Typed against the real generated schema -- never `createClient(url, key)` untyped.
 *
 * The `auth` block is what makes a signed-in session survive an Electron
 * restart. Each option is deliberate:
 *
 * - `storage`: the default is `window.localStorage`, which in Electron writes
 *   the refresh token to disk as plaintext under userData. The adapter routes
 *   it through the main process to the OS keystore instead. It is async, which
 *   supabase-js supports. See ./secure-storage.ts, and note in particular the
 *   section on what OS encryption does NOT protect against -- do not let the
 *   UI overstate it.
 * - `persistSession`: the whole point; without it `storage` is never consulted.
 * - `autoRefreshToken`: a desktop app is left open for days, far longer than an
 *   access token's lifetime.
 * - `detectSessionInUrl`: OFF. That option exists to parse an OAuth fragment
 *   out of `window.location` after a web redirect. A packaged Electron renderer
 *   loads from `file://` or a fixed app URL and has no such redirect, so
 *   leaving it on only means parsing an unrelated URL on every construction.
 * - `flowType: 'pkce'`: the correct flow for a public client that cannot hold a
 *   secret, and the one that works with a desktop deep-link callback.
 */
export const supabase = createClient<Database>(supabaseUrl, supabaseKey, {
  auth: {
    storage: createSecureStorageAdapter(),
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: false,
    flowType: 'pkce',
  },
})

/**
 * Tier 3 rule 11: realtime is subscribed narrowly, never table-wide.
 * `public.facts` is the only table in the realtime publication, and even
 * that is filtered down to one user's rows before it reaches the socket.
 */
export function subscribeToOwnFacts(
  userId: string,
  onChange: (payload: unknown) => void,
) {
  return supabase
    .channel(`facts:${userId}`)
    .on(
      'postgres_changes',
      { event: '*', schema: 'public', table: 'facts', filter: `user_id=eq.${userId}` },
      onChange,
    )
    .subscribe()
}

/**
 * Tier 3 rule 10: the multi-row sync is a single server-side transaction
 * with a deterministic conflict rule, not a client-side read-compare-write
 * loop that two devices can interleave and corrupt.
 */
export async function syncFacts(
  deviceId: string | null,
  facts: Array<{ source: string; title: string; body: string; updated_at?: string }>,
) {
  const { data, error } = await supabase.rpc('sync_facts', {
    p_device_id: deviceId as string,
    p_facts: facts,
  })
  if (error) throw error
  return data?.[0] ?? { synced: 0, skipped: 0 }
}

/**
 * Tier 3: cosine ANN search over the caller's OWN documents.
 *
 * `match_documents` is SECURITY INVOKER, so RLS is what scopes the result --
 * there is no user_id argument here and there must not be one. A SECURITY
 * DEFINER version would run as the function owner and happily return every
 * user's private corpus to any authenticated caller.
 *
 * `embedding` must be 768-dimensional and must come from `nomic-embed-text`.
 * The column's typmod rejects a wrong LENGTH outright; `matchModel` is what
 * pins the vector SPACE, because two different 768-dim models are equally
 * incomparable under cosine. See the migration comment for the full reasoning.
 */
export async function matchDocuments(
  embedding: number[],
  opts: { matchCount?: number; filter?: Record<string, unknown>; matchModel?: string } = {},
) {
  if (embedding.length !== 768) {
    throw new Error(
      `match_documents expects a 768-dimensional nomic-embed-text vector, got ${embedding.length}. ` +
        'A vector from another model lives in a different space -- cosine similarity ' +
        'across two spaces is meaningless, not merely worse, so this is refused rather ' +
        'than sent.',
    )
  }
  const { data, error } = await supabase.rpc('match_documents', {
    query_embedding: embedding as unknown as string,
    match_count: opts.matchCount ?? 10,
    filter: (opts.filter ?? {}) as never,
    match_model: opts.matchModel ?? 'nomic-embed-text',
  })
  if (error) throw error
  return data ?? []
}
