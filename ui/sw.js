/* DOURMOUSE // OFFLINE SHELL — service worker (v5.20, desktop portfolio Phase 5)
 *
 * What it caches, and the honesty rules that govern it:
 *   - SHELL (/, /index.html, /login.html, /map, /map.html, /assets/*):
 *     stale-while-revalidate — the shell opens instantly offline and
 *     revalidates in the background. A stale shell is still honest: it is
 *     the UI, not data.
 *   - /api/state: network-first. On success the SHARED-scope snapshot is
 *     cached (see the X-Dourmouse-Scope rule); OFFLINE, the cached snapshot
 *     is served with an explicit `X-Dourmouse-Stale: 1` header that the UI
 *     turns into a visible STALE banner. Cached data is NEVER presented as
 *     live, and never presented without the marker.
 *   - EVERYTHING else (/api/* live data, /api/events SSE, /uploads/*):
 *     network-only. Never cached, never replayed, never presented as fresh.
 *
 * Cross-user rule: SW caches are shared by every client of this origin
 * (same browser profile). To prevent user A's personal snapshot being
 * replayed to user B offline, ONLY shared-scope responses (no signed-in
 * user) ever enter the cache. A signed-in user going offline sees the
 * shared bucket — honestly marked stale — never someone's personal data.
 */
const CACHE = 'dourmouse-shell-v3';  // v5.31: ATLAS motion + compute-node card joined the shell
// v5.22.3: the PWA manifest + icons join the shell so the INSTALLED app
// opens instantly with its icon even offline.
const SHELL = ['/', '/index.html', '/login.html', '/map', '/map.html',
  '/manifest.json', '/assets/icon-192.png', '/assets/icon-512.png',
  '/assets/apple-touch-icon.png'];
const ASSET_PREFIX = '/assets/';
const STALE_HEADER = 'X-Dourmouse-Stale';
const SCOPE_HEADER = 'X-Dourmouse-Scope';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // Per-entry precache: ONE missing asset must not brick the whole
      // offline shell (all-or-nothing addAll would fail the install and
      // silently never activate). A missing page degrades; the rest cache.
      .then((cache) => Promise.allSettled(SHELL.map((path) => cache.add(path))))
      .then((results) => {
        results.forEach((result, i) => {
          if (result.status === 'rejected') {
            console.error('[DOURMOUSE SW] precache failed: ' + SHELL[i], result.reason);
          }
        });
      })
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Background refresh of a cached copy; never fails the surrounding event.
function revalidate(req) {
  return fetch(req).then((resp) => {
    if (resp.ok) {
      const copy = resp.clone();
      caches.open(CACHE).then((cache) => cache.put(req, copy)).catch(() => {});
    }
    return resp;
  }).catch(() => undefined);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // The live fan-out stream is NEVER cached.
  if (url.pathname === '/api/events') return;

  // Cross-device state: network-first; offline fallback marked stale.
  if (url.pathname === '/api/state') {
    event.respondWith(
      fetch(req).then((resp) => {
        // Only the SHARED scope may enter the cache — a signed-in user's
        // personal snapshot must never be persisted or replayed to another
        // client of this origin (v5.17 per-user parity, offline).
        if (resp.ok && resp.headers.get(SCOPE_HEADER) === 'shared') {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy)).catch(() => {});
        }
        return resp;
      }).catch(() =>
        caches.match(req).then((cached) => {
          if (!cached) return Response.error();
          const headers = new Headers(cached.headers);
          headers.set(STALE_HEADER, '1');
          return new Response(cached.body, {
            status: cached.status, statusText: cached.statusText, headers,
          });
        })
      )
    );
    return;
  }

  // Shell + static assets: stale-while-revalidate.
  if (url.pathname === '/' || SHELL.includes(url.pathname) ||
      url.pathname.startsWith(ASSET_PREFIX)) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const live = revalidate(req);
        return cached || live;
      })
    );
    return;
  }

  // Agent windows are live views — fail honestly when offline, never a
  // mismatched shell. Every other API/upload stays network-only.
});
