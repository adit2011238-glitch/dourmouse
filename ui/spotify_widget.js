/*!
 * Floating Spotify widget (backlog item 8) — self-mounting.
 *
 * Drop `<script src="spotify_widget.js" defer></script>` on any page and it
 * builds its own DOM (no markup, no new HTML file), pulling
 * spotify_widget.css from the same directory as this script.
 *
 * Backend contract (dourmouse/webui.py — none of it touched here, all of
 * it already existed before this widget):
 *   GET  /api/spotify            -> {configured, linked, detail, hint,
 *                                     now_playing?}
 *   POST /api/spotify/login      -> {ok, message}      (starts linking)
 *   POST /api/spotify/search     {query, limit} -> {ok, results:[{name,artists,uri}]}
 *   POST /api/spotify/playlists  {} -> {ok, playlists:[{name,uri,tracks}]}
 *   POST /api/spotify/play       {uri} -> {ok, message}
 *   POST /api/spotify/control    {action: next|previous|pause|resume} -> {ok, message}
 *
 * Rule 2.2 (honest capability report): every empty/failure state below
 * renders the server's own message — nothing here fabricates a track,
 * playlist, or "now playing" line.
 */
(function () {
  "use strict";
  if (window.__dmSpotifyWidgetMounted) return; // idempotent if included twice
  window.__dmSpotifyWidgetMounted = true;

  const STORAGE_KEY = "dourmouse.spotifyWidget.v1";
  const NOW_PLAYING_POLL_MS = 6000;
  const PLAYLISTS_TTL_MS = 60000;

  // ---- CSS: load the companion stylesheet relative to this script -------
  function ownScriptUrl() {
    if (document.currentScript && document.currentScript.src) return document.currentScript.src;
    const scripts = document.getElementsByTagName("script");
    for (let i = scripts.length - 1; i >= 0; i--) {
      if (/spotify_widget\.js(\?.*)?$/.test(scripts[i].src)) return scripts[i].src;
    }
    return "spotify_widget.js";
  }
  function loadCss() {
    const href = ownScriptUrl().replace(/spotify_widget\.js(\?.*)?$/, "spotify_widget.css");
    if (document.querySelector('link[data-dm-spotify-widget-css]')) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    link.setAttribute("data-dm-spotify-widget-css", "1");
    document.head.appendChild(link);
  }

  // ---- tiny fetch helpers -------------------------------------------------
  async function apiGet(path) {
    const r = await fetch(path, { credentials: "same-origin" });
    return r.json().catch(() => ({}));
  }
  async function apiPost(path, body) {
    const r = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return r.json().catch(() => ({}));
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ---- position persistence ------------------------------------------------
  function loadPos() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (typeof parsed.x === "number" && typeof parsed.y === "number") return parsed;
    } catch (e) { /* private mode / cleared storage — fall back to default */ }
    return null;
  }
  function savePos(x, y) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ x, y })); } catch (e) { /* best-effort only */ }
  }
  function loadCollapsed() {
    try { return localStorage.getItem(STORAGE_KEY + ".collapsed") === "1"; } catch (e) { return false; }
  }
  function saveCollapsed(v) {
    try { localStorage.setItem(STORAGE_KEY + ".collapsed", v ? "1" : "0"); } catch (e) { /* best-effort */ }
  }

  // ---- icons (inline SVG, no icon font / image request) -------------------
  const ICON_PREV = '<svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm12.5 12L10 12l8.5-6z"/></svg>';
  const ICON_NEXT = '<svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM5.5 6 14 12l-8.5 6z"/></svg>';
  const ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
  const ICON_PAUSE = '<svg viewBox="0 0 24 24"><path d="M7 5h4v14H7zm6 0h4v14h-4z"/></svg>';

  function build() {
    loadCss();

    const root = document.createElement("div");
    root.className = "dm-spotify-widget";
    root.id = "dmSpotifyWidget";
    root.setAttribute("role", "region");
    root.setAttribute("aria-label", "Spotify");
    root.innerHTML =
      '<div class="dm-sw-header" data-drag-handle>' +
        '<span class="dm-sw-dot" id="dmSwDot"></span>' +
        '<span class="dm-sw-title">Spotify</span>' +
        '<button type="button" class="dm-sw-iconbtn" id="dmSwCollapse" aria-label="Collapse widget" title="Collapse">–</button>' +
      '</div>' +
      '<div class="dm-sw-body" id="dmSwBody"></div>';
    document.body.appendChild(root);

    // ---- placement: restore saved position, else dock bottom-right -------
    function clamp(x, y) {
      const w = root.offsetWidth || 300, h = root.offsetHeight || 200;
      const maxX = Math.max(0, window.innerWidth - w - 8);
      const maxY = Math.max(0, window.innerHeight - h - 8);
      return { x: Math.min(Math.max(8, x), maxX), y: Math.min(Math.max(8, y), maxY) };
    }
    function place(x, y) {
      root.style.transform = "translate(" + x + "px, " + y + "px)";
      root.dataset.x = String(x);
      root.dataset.y = String(y);
    }
    const saved = loadPos();
    if (saved) {
      const c = clamp(saved.x, saved.y);
      place(c.x, c.y);
    } else {
      place(Math.max(8, window.innerWidth - 324), Math.max(8, window.innerHeight - 380));
    }
    window.addEventListener("resize", () => {
      const x = Number(root.dataset.x || 0), y = Number(root.dataset.y || 0);
      const c = clamp(x, y);
      place(c.x, c.y);
    });

    // ---- drag (pointer events; position persisted to localStorage) -------
    const handle = root.querySelector('[data-drag-handle]');
    let dragging = false, startPX = 0, startPY = 0, startX = 0, startY = 0;
    handle.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".dm-sw-iconbtn")) return; // let the collapse button work
      dragging = true;
      root.classList.add("dm-sw-dragging");
      startPX = e.clientX; startPY = e.clientY;
      startX = Number(root.dataset.x || 0); startY = Number(root.dataset.y || 0);
      handle.setPointerCapture(e.pointerId);
    });
    handle.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const c = clamp(startX + (e.clientX - startPX), startY + (e.clientY - startPY));
      place(c.x, c.y);
    });
    function endDrag(e) {
      if (!dragging) return;
      dragging = false;
      root.classList.remove("dm-sw-dragging");
      savePos(Number(root.dataset.x || 0), Number(root.dataset.y || 0));
    }
    handle.addEventListener("pointerup", endDrag);
    handle.addEventListener("pointercancel", endDrag);

    // ---- collapse toggle ---------------------------------------------------
    const body = root.querySelector("#dmSwBody");
    const collapseBtn = root.querySelector("#dmSwCollapse");
    function setCollapsed(v) {
      body.classList.toggle("dm-sw-hidden", v);
      collapseBtn.textContent = v ? "+" : "–";
      collapseBtn.title = v ? "Expand" : "Collapse";
      saveCollapsed(v);
    }
    setCollapsed(loadCollapsed());
    collapseBtn.addEventListener("click", () => setCollapsed(!body.classList.contains("dm-sw-hidden")));

    return { root, body, dot: root.querySelector("#dmSwDot") };
  }

  // ---- app state -------------------------------------------------------------
  const state = {
    initialized: false,
    linked: false,
    activeTab: "mine", // "mine" | "all"
    playlists: null,
    playlistsAt: 0,
    isPlaying: null, // null = unknown yet
    nowPlayingText: "",
  };

  function renderConnect(body, status) {
    const configured = !!status.configured;
    const detail = esc(status.detail || (configured ? "not linked yet" : "Spotify is not configured"));
    body.innerHTML =
      '<div class="dm-sw-connect">' +
        '<p>' + detail + '</p>' +
        (configured
          ? '<button type="button" class="dm-sw-connect-btn" id="dmSwConnectBtn">Connect Spotify</button>' +
            '<div class="dm-sw-connect-hint" id="dmSwConnectHint"></div>'
          : '<div class="dm-sw-connect-hint">' + esc(status.hint || "Set SPOTIFY_CLIENT_ID, then reopen this widget.") + '</div>') +
      '</div>';
    if (configured) {
      const btn = body.querySelector("#dmSwConnectBtn");
      const hint = body.querySelector("#dmSwConnectHint");
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "Opening Spotify…";
        try {
          const d = await apiPost("/api/spotify/login", {});
          hint.textContent = d.message || "Check your browser to finish linking, then reopen this widget.";
        } catch (e) {
          hint.textContent = "Could not reach the server.";
        }
        btn.disabled = false;
        btn.textContent = "Connect Spotify";
      });
    }
  }

  function rowsHtml(items, kind) {
    if (!items.length) {
      const label = kind === "mine" ? "NO PLAYLISTS (HONEST)" : "NO MATCHES (HONEST)";
      return '<div class="dm-sw-empty">' + label + '</div>';
    }
    return items.map((it) => {
      const sub = kind === "mine"
        ? (it.tracks == null ? "" : it.tracks + " tracks")
        : (it.artists || "");
      return (
        '<button type="button" class="dm-sw-row" data-uri="' + esc(it.uri || "") + '">' +
          '<span class="dm-sw-row-name">' + esc(it.name || "—") + '</span>' +
          (sub ? '<span class="dm-sw-row-sub">' + esc(sub) + '</span>' : "") +
        '</button>'
      );
    }).join("");
  }

  function renderPlayer(body) {
    body.innerHTML =
      '<input type="text" class="dm-sw-search" id="dmSwSearch" placeholder="' +
        (state.activeTab === "mine" ? "Filter your playlists…" : "Search all songs…") + '">' +
      '<div class="dm-sw-tabs" role="tablist">' +
        '<button type="button" class="dm-sw-tab" data-tab="mine" role="tab" aria-pressed="' + (state.activeTab === "mine") + '">My playlists</button>' +
        '<button type="button" class="dm-sw-tab" data-tab="all" role="tab" aria-pressed="' + (state.activeTab === "all") + '">All songs</button>' +
      '</div>' +
      '<div class="dm-sw-results" id="dmSwResults" role="list"><div class="dm-sw-empty">Loading…</div></div>' +
      '<div class="dm-sw-nowplaying" id="dmSwNowPlaying"></div>' +
      '<div class="dm-sw-controls">' +
        '<button type="button" class="dm-sw-ctrl" data-action="previous" aria-label="Previous track">' + ICON_PREV + '</button>' +
        '<button type="button" class="dm-sw-ctrl dm-sw-ctrl-play" id="dmSwPlayPause" aria-label="Play or pause">' + ICON_PLAY + '</button>' +
        '<button type="button" class="dm-sw-ctrl" data-action="next" aria-label="Next track">' + ICON_NEXT + '</button>' +
      '</div>' +
      '<div class="dm-sw-error" id="dmSwErr" hidden></div>';

    const searchEl = body.querySelector("#dmSwSearch");
    const resultsEl = body.querySelector("#dmSwResults");
    const errEl = body.querySelector("#dmSwErr");
    const playPauseBtn = body.querySelector("#dmSwPlayPause");

    function showError(msg) {
      if (!msg) { errEl.hidden = true; errEl.textContent = ""; return; }
      errEl.hidden = false; errEl.textContent = msg;
    }

    async function ensurePlaylists(force) {
      const fresh = state.playlists && (Date.now() - state.playlistsAt) < PLAYLISTS_TTL_MS;
      if (fresh && !force) return state.playlists;
      const d = await apiPost("/api/spotify/playlists", {});
      if (!d.ok) { showError(d.error || "Playlists unavailable."); state.playlists = []; return state.playlists; }
      showError("");
      state.playlists = d.playlists || [];
      state.playlistsAt = Date.now();
      return state.playlists;
    }

    async function runMine() {
      resultsEl.innerHTML = '<div class="dm-sw-empty">Loading…</div>';
      const all = await ensurePlaylists(false);
      const q = (searchEl.value || "").trim().toLowerCase();
      const filtered = q ? all.filter((p) => (p.name || "").toLowerCase().includes(q)) : all;
      resultsEl.innerHTML = rowsHtml(filtered, "mine");
    }

    let searchDebounce = null;
    async function runAll() {
      const q = (searchEl.value || "").trim();
      if (!q) { resultsEl.innerHTML = '<div class="dm-sw-empty">Type to search all songs.</div>'; return; }
      resultsEl.innerHTML = '<div class="dm-sw-empty">Searching…</div>';
      const d = await apiPost("/api/spotify/search", { query: q, limit: 8 });
      if (!d.ok) { showError(d.error || "Search failed."); resultsEl.innerHTML = ""; return; }
      showError("");
      resultsEl.innerHTML = rowsHtml(d.results || [], "all");
    }

    function runActiveTab() { state.activeTab === "mine" ? runMine() : runAll(); }

    body.querySelectorAll(".dm-sw-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        state.activeTab = tab.dataset.tab;
        body.querySelectorAll(".dm-sw-tab").forEach((t) => t.setAttribute("aria-pressed", String(t === tab)));
        searchEl.placeholder = state.activeTab === "mine" ? "Filter your playlists…" : "Search all songs…";
        searchEl.value = "";
        runActiveTab();
      });
    });

    searchEl.addEventListener("input", () => {
      if (state.activeTab === "mine") { runMine(); return; } // client-side filter, no debounce needed
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(runAll, 300);
    });

    resultsEl.addEventListener("click", async (e) => {
      const row = e.target.closest(".dm-sw-row");
      if (!row) return;
      const uri = row.dataset.uri;
      if (!uri) return;
      const d = await apiPost("/api/spotify/play", { uri });
      showError(d.ok ? "" : (d.message || "Could not start playback."));
      setTimeout(refreshNowPlaying, 800);
    });

    body.querySelectorAll(".dm-sw-ctrl[data-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const d = await apiPost("/api/spotify/control", { action: btn.dataset.action });
        showError(d.ok ? "" : (d.message || "Playback command failed."));
        setTimeout(refreshNowPlaying, 500);
      });
    });

    playPauseBtn.addEventListener("click", async () => {
      const action = state.isPlaying ? "pause" : "resume";
      const d = await apiPost("/api/spotify/control", { action });
      showError(d.ok ? "" : (d.message || "Playback command failed."));
      setTimeout(refreshNowPlaying, 500);
    });

    function applyNowPlaying(text) {
      state.nowPlayingText = text || "";
      const npEl = body.querySelector("#dmSwNowPlaying");
      if (npEl) npEl.textContent = state.nowPlayingText;
      if (/▶/.test(state.nowPlayingText)) state.isPlaying = true;
      else if (/⏸/.test(state.nowPlayingText)) state.isPlaying = false;
      if (playPauseBtn) playPauseBtn.innerHTML = state.isPlaying === false ? ICON_PLAY : ICON_PAUSE;
    }

    async function refreshNowPlaying() {
      const d = await apiGet("/api/spotify");
      if (!d.linked) return; // link may have been revoked mid-session; connect view takes over on next full refresh
      applyNowPlaying(d.now_playing || "");
    }

    runMine();
    refreshNowPlaying();
    return { refreshNowPlaying };
  }

  async function refreshStatus(ui) {
    let status;
    try {
      status = await apiGet("/api/spotify");
    } catch (e) {
      ui.dot.classList.remove("dm-sw-on");
      return;
    }
    const linked = !!status.linked;
    ui.dot.classList.toggle("dm-sw-on", linked);
    const unchanged = state.initialized && linked === state.linked;
    state.initialized = true;
    state.linked = linked;
    if (unchanged) return; // same state as last poll — the now-playing poller (when linked) covers the rest
    if (linked) {
      const player = renderPlayer(ui.body);
      state._poll = setInterval(player.refreshNowPlaying, NOW_PLAYING_POLL_MS);
    } else {
      if (state._poll) { clearInterval(state._poll); state._poll = null; }
      renderConnect(ui.body, status);
    }
  }

  function mount() {
    const ui = build();
    refreshStatus(ui);
    setInterval(() => refreshStatus(ui), 15000); // catches a fresh link/unlink without a page reload
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
