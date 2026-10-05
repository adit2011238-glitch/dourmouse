/* BROWSER: the one shared browser pane.

   Inside the Electron app the page is a native BrowserView, a real top-level
   browsing context (cookies and logins work, X-Frame-Options never applies) and
   the SAME view the browser agent drives over CDP. That view is composited
   ABOVE this page's DOM, so this screen owns three duties: place it over the
   page area (bounds follow every resize, scroll, sidebar and stage change),
   hide it while a panel or toast is drawn over the stage, and ALWAYS hide it
   when the screen is left.

   Outside Electron the fallback is a sandboxed iframe fed by
   /api/browser-pane/proxy, with the limits that path really has (said on the
   screen). That fallback has one page and none of the tab, find, zoom, download,
   history or bookmark features, because those live in the Electron shell.

   Inside Electron the pane holds a list of tabs (Phase B1). The strip, the find
   bar, zoom, print, the downloads shelf and the history list all show what the
   shell reports; bookmarks and history are read and written through ctx.api (the
   server forwards them to the shell, which stores them in its userData folder).
   The downloads shelf and the history list are drawn IN the page area, in place
   of the page, because the native view is composited above this page's DOM: a
   panel laid over it would be invisible. While one is open the native view is
   hidden, exactly as for any other panel.

   Nothing here is a sample: the address, title, loading, back and forward all
   come from the pane's own state, or from the addresses opened in this window
   when the page is proxied. */

import { takePaneRequest } from '../../core/pane-inbox.js';
import { html, setHtml, raw } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { createPrivacy } from './privacy-ui.js';
import { createControl } from './control-ui.js';
import { createManage } from './manage-ui.js';
import { profileLabel } from './manage-model.js';
import { ago, clock } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  PRESETS, normalizeAddress, eventTarget, lockInfo, hostOf, isWebUrl, paneModel, failLine,
  viewBounds, sameBounds, clampWidth, clampHeight, presetForWidth, parseStoredWidth, parseStoredHeight,
  watchLine, footnotes, makeHistory, sameAddress, tabsModel, tabsKey, tabLabel, zoomLabel, findLabel,
  downloadsModel, activeDownloads, downloadLine, formatBytes, groupHistory, bookmarksModel, bookmarkFor,
} from './helpers.js';

const SVG = (body, extra = '') => raw('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ' + extra + '>' + body + '</svg>');
const ICON = {
  back: SVG('<path d="M15 6l-6 6 6 6"/>'),
  fwd: SVG('<path d="M9 6l6 6-6 6"/>'),
  reload: SVG('<path d="M4 12a8 8 0 018-8c3 0 5.5 1.6 7 4M20 4v4h-4"/>'),
  stop: SVG('<path d="M6 6l12 12M18 6L6 18"/>'),
  lock: SVG('<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>'),
  globe: SVG('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.6 3 14.4 0 18M12 3c-3 3.6-3 14.4 0 18"/>'),
  eye: SVG('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="2.5"/>', 'width="14" height="14"'),
  phone: SVG('<rect x="8" y="3" width="8" height="18" rx="2"/><path d="M11 18h2"/>'),
  tablet: SVG('<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M11 18h2"/>'),
  desktop: SVG('<rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M9 20h6M12 16v4"/>'),
  fill: SVG('<path d="M4 9V4h5M20 15v5h-5M4 15v5h5M20 9V4h-5"/>'),
  plus: SVG('<path d="M12 5v14M5 12h14"/>'),
  minus: SVG('<path d="M5 12h14"/>'),
  star: SVG('<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9-5.2-2.8-5.2 2.8 1-5.9L3.5 9.7l5.9-.8z"/>'),
  find: SVG('<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>'),
  up: SVG('<path d="M6 15l6-6 6 6"/>'),
  down: SVG('<path d="M6 9l6 6 6-6"/>'),
  print: SVG('<path d="M7 9V4h10v5M7 17H5a1 1 0 01-1-1v-5a2 2 0 012-2h12a2 2 0 012 2v5a1 1 0 01-1 1h-2M7 14h10v6H7z"/>'),
  pdf: SVG('<path d="M7 3h7l5 5v13H7zM14 3v5h5M9.5 15h5M9.5 18h3"/>'),
  download: SVG('<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>'),
  clock: SVG('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  shield: SVG('<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9.5 12l2 2 3.5-4"/>'),
  key: SVG('<circle cx="8" cy="15" r="4"/><path d="M11 12l8-8M16 7l3 3M14 9l2 2"/>'),
  puzzle: SVG('<path d="M10 4a2 2 0 014 0v2h3a1 1 0 011 1v3h-2a2 2 0 100 4h2v3a1 1 0 01-1 1h-3v-2a2 2 0 10-4 0v2H7a1 1 0 01-1-1v-3H4a2 2 0 110-4h2V7a1 1 0 011-1h3z"/>'),
  user: SVG('<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-4 3.2-6.5 7-6.5s7 2.5 7 6.5"/>'),
};

const PRESET_SPEC = {
  phone: 'Makes the page area 380 pixels wide so the page reflows. Layout only, not a phone emulator.',
  tablet: 'Makes the page area 560 pixels wide so the page reflows. Layout only, not a tablet emulator.',
  desktop: 'Makes the page area 840 pixels wide so the page reflows. Layout only.',
  fill: 'Lets the page area take all the width and height the screen has.',
};

/* per-mount hooks for refresh and unmount, keyed by the ctx the shell passes to both */
const hooks = new WeakMap();

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export default {
  id: 'BROWSER',
  sub: 'embedded browser',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const pane = ctx.host.pane || null; /* the Electron pane bridge, or null */
    const electron = ctx.host.kind === 'electron';
    const fallbackKind = electron ? 'electron' : ctx.host.kind;
    const hasB1 = Boolean(pane) && typeof pane.newTab === 'function' && typeof pane.onDownloads === 'function'; /* an older shell has the pane but not tabs */
    const hasB2 = hasB1 && Boolean(pane.privacy) && typeof pane.privacy.state === 'function'; /* an older shell has tabs but not permissions and passwords */
    const hasB3 = hasB2 && Boolean(pane.manage) && Boolean(pane.manage.extensions) && Boolean(pane.manage.profiles); /* an older shell has no extensions, profiles or import */
    const hasC2 = hasB1 && Boolean(pane.control) && typeof pane.control.state === 'function'; /* an older shell has no owner/model lock */

    /* ---------------- state ---------------- */
    let disposed = false;
    let surface = pane ? 'pane' : 'frame'; /* 'pane': the native view. 'frame': the iframe. */
    let model = paneModel(null); /* the pane's own state */
    let paneKnown = false; /* the pane has answered at least once */
    let paneProblem = ''; /* why the pane cannot be used, when it cannot */
    let pendingUrl = ''; /* asked for, its load not yet finished */
    let sawLoading = false;
    let viewShown = false;
    let lastBounds = null;
    let dragging = false;
    let dirty = false; /* the owner is typing in the address field */
    let att = null; /* GET /api/os/browser/attached */
    let attBusy = false;
    let attAgain = false;
    let widthPx = parseStoredWidth(ctx.prefs.get('width'));
    let heightPx = parseStoredHeight(ctx.prefs.get('height'));
    let frameEl = null;
    let frameLoading = false;
    let frameProxied = false;
    let raf = 0;
    let regionKey = '';
    let actionKey = '';
    let reloadMode = '';
    let offPane = null;
    const hist = makeHistory(50);
    /* Phase B1 */
    let tm = tabsModel(null); /* the tab list and the active tab's zoom and find state */
    let tabsSig = '';
    let lastActiveId = 0;
    let panel = ''; /* '' | 'history' | 'downloads': drawn in the page area in place of the page */
    let dl = []; /* the downloads shelf */
    let dlStruct = '';
    let dlLive = new Map();
    let offDl = null;
    let offCmd = null;
    let bms = [];
    let bmsError = '';
    let findOpen = false;
    let offFindEsc = null;
    let offPanelEsc = null;
    let histQuery = '';
    let histSeq = 0;
    let histTimer = 0;
    let histBody = null;
    let histConfirm = null;
    let dlBody = null;
    let offPrivacy = null;
    let panelHandle = null; /* the open Site settings or Passwords panel, so closing it can wipe what it showed */
    let privacyUi = null;
    let manageUi = null;
    let controlUi = null; /* Phase C2: who has the browser, and Stop and Take control */
    let offControl = null;
    const panelRoot = el('div', 'bw-panel');
    const MOD = /Mac/i.test(String(globalThis.navigator && globalThis.navigator.platform)) ? 'Meta' : 'Ctrl';

    /* ---------------- skeleton: built once, regions repaint ---------------- */
    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="bw-note" id="bwNote" role="status" hidden></div>
      <div class="cb-wrap" id="bwWrap">
        <div class="cb-frame">
          <div class="cb-tabs">
            <div class="cb-tablist" id="bwTabs" role="tablist" aria-label="Open tabs"></div>
            <button type="button" class="cb-ico cb-new" id="bwNewTab" aria-label="New tab" title="New tab (Cmd+T)" data-spec="Opens a new empty tab next to the others. Cmd+T does the same. At most 30 tabs: a page that opens tabs in a loop is cut off after five in ten seconds.">${ICON.plus}</button>
            <button type="button" class="cb-reopen" id="bwReopen" hidden title="Reopen the last closed tab (Cmd+Shift+T)" data-spec="Reopens the tab you closed last, in the place it was. Cmd+Shift+T does the same. Only web pages are remembered, and only until the app closes.">REOPEN CLOSED TAB</button>
          </div>
          <div class="cb-bar" id="bwBar">
            <div class="cb-nav">
              <button type="button" class="cb-ico" id="bwBack" aria-label="Back" title="Back" data-spec="Back through the pane's real history. Outside Electron it goes back through the addresses opened in this window.">${ICON.back}</button>
              <button type="button" class="cb-ico" id="bwFwd" aria-label="Forward" title="Forward" data-spec="Forward through the pane's real history.">${ICON.fwd}</button>
              <button type="button" class="cb-ico" id="bwReload" aria-label="Reload" title="Reload" data-spec="Reloads the page. While a page is loading it stops the load instead.">${ICON.reload}</button>
            </div>
            <form class="cb-addr" id="bwAddrForm" data-lock="none" novalidate>
              <span id="bwLock" role="img" aria-label="No page loaded">${ICON.lock}</span>
              <input id="bwAddr" type="text" inputmode="url" autocomplete="off" spellcheck="false" aria-label="Address" placeholder="Type an address and press Enter" data-spec="The real current address of the page. Typing an address and pressing Enter opens it. Only http and https addresses open; file, javascript, data and the rest are refused. Alt+L or Cmd+L focuses it.">
              <button type="button" class="cb-star" id="bwStar" aria-label="Bookmark this page" aria-pressed="false" title="Bookmark this page" data-spec="Adds this page to the bookmarks bar, or removes it when it is already there. Bookmarks are kept by the Electron app on this Mac and are shared by every tab.">${ICON.star}</button>
            </form>
            <div class="cb-tools" id="bwTools" role="group" aria-label="Page tools">
              <button type="button" class="cb-ico" id="bwFindBtn" aria-label="Find in page" title="Find in page (Cmd+F)" data-spec="Opens the find bar for the page in this tab: it highlights every match and shows which one you are on. Cmd+F does the same.">${ICON.find}</button>
              <button type="button" class="cb-ico" id="bwZoomOut" aria-label="Zoom out" title="Zoom out (Cmd+-)" data-spec="Makes this site smaller. The level is remembered for this site only, the way Chrome does it.">${ICON.minus}</button>
              <button type="button" class="cb-zoom" id="bwZoom" aria-label="Reset zoom" title="Reset zoom (Cmd+0)" data-spec="The zoom level of this site. Press it to go back to 100 percent.">100%</button>
              <button type="button" class="cb-ico" id="bwZoomIn" aria-label="Zoom in" title="Zoom in (Cmd+=)" data-spec="Makes this site larger. The level is remembered for this site only.">${ICON.plus}</button>
              <button type="button" class="cb-ico" id="bwPrint" aria-label="Print" title="Print (Cmd+P)" data-spec="Opens the system print dialog for this page. It has Save as PDF in its own menu. Nothing prints until you confirm there.">${ICON.print}</button>
              <button type="button" class="cb-ico" id="bwPdf" aria-label="Save page as PDF" title="Save page as PDF" data-spec="Saves this page as a PDF in your Downloads folder and lists it on the downloads shelf. Nothing is opened.">${ICON.pdf}</button>
              <button type="button" class="cb-ico cb-badged" id="bwDl" aria-label="Downloads" aria-pressed="false" title="Downloads" data-spec="Shows what this browser has downloaded and what is downloading now. Files go to your Downloads folder, where the downloads watcher checks them. Nothing is ever opened by itself.">${ICON.download}<span class="cb-count" id="bwDlCount" hidden></span></button>
              <button type="button" class="cb-ico" id="bwHist" aria-label="History" aria-pressed="false" title="History" data-spec="Lists the pages this browser has visited, newest first, with a search box. Click one to open it, remove one, or clear all of it.">${ICON.clock}</button>
              <button type="button" class="cb-ico" id="bwSites" aria-label="Site settings" aria-pressed="false" title="Site settings" data-spec="Lists what each site may use (camera, microphone, location, notifications, clipboard, full screen), as you answered it. Change an answer or reset it so the site asks again.">${ICON.shield}</button>
              <button type="button" class="cb-ico" id="bwPass" aria-label="Passwords and autofill" aria-pressed="false" title="Passwords and autofill" data-spec="Your saved logins and addresses. They are encrypted with a key in this Mac's Keychain and stay on this Mac. No AI tool can list or open them, but a password you fill sits in the page's field. Showing a password asks twice.">${ICON.key}</button>
              <button type="button" class="cb-ico" id="bwExt" aria-label="Extensions" aria-pressed="false" title="Extensions" data-spec="Unpacked Chrome extensions you add yourself. Adding one opens a macOS folder picker and a macOS confirmation that lists everything it asks for. Only part of Chrome's extension support exists here, and the Chrome Web Store is not available.">${ICON.puzzle}</button>
              <button type="button" class="cb-ico cb-profile" id="bwProf" aria-label="Profiles and import" aria-pressed="false" title="Profiles and import" data-spec="Switch between profiles (each has its own logins, history, bookmarks, permissions and saved passwords) and import bookmarks, history and passwords from Chrome. Every import asks in a macOS dialog first.">${ICON.user}<span class="cb-prof" id="bwProfName" hidden></span></button>
            </div>
            <span class="cb-size" id="bwSize" title="Size of the page area in pixels"></span>
            <div class="cb-vp" id="bwPresets" role="group" aria-label="Page area size" data-spec="Sets the width of the page area. Layout only: the page reflows because the view really is that wide, but there is no device emulation."></div>
          </div>
          <div class="cb-find" id="bwFind" role="search" hidden>
            <input id="bwFindInput" type="text" autocomplete="off" spellcheck="false" aria-label="Find in page" placeholder="Find in page" data-spec="Type to highlight every match on the page. Enter goes to the next match, Shift+Enter to the one before, Escape closes the bar.">
            <span class="cb-findcount" id="bwFindCount" role="status" aria-live="polite"></span>
            <button type="button" class="cb-ico" id="bwFindPrev" aria-label="Previous match" title="Previous match (Shift+Enter)" data-spec="Goes to the match before this one.">${ICON.up}</button>
            <button type="button" class="cb-ico" id="bwFindNext" aria-label="Next match" title="Next match (Enter)" data-spec="Goes to the next match.">${ICON.down}</button>
            <button type="button" class="cb-ico" id="bwFindClose" aria-label="Close find bar" title="Close (Escape)" data-spec="Closes the find bar and clears the highlights.">${ICON.stop}</button>
          </div>
          <div class="cb-bm" id="bwBm" role="toolbar" aria-label="Bookmarks" hidden></div>
          <div class="cb-privacy-slot" id="bwPrivacy"></div>
          <div class="cb-control-slot" id="bwControl"></div>
          <div class="bw-proxy" id="bwProxy" hidden></div>
          <div class="cb-page" id="bwPage" data-region></div>
          <div class="cb-watch" id="bwWatch" data-region></div>
        </div>
        <div class="bres cb-div" id="bwResR" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Page area width" data-spec="Drag to change the width of the page area, or use the left and right arrow keys (Shift for bigger steps). The page reflows live."></div>
        <div class="bres cb-divb" id="bwResB" role="separator" aria-orientation="horizontal" tabindex="0" aria-label="Page area height" data-spec="Drag to change the height of the page area, or use the up and down arrow keys (Shift for bigger steps)."></div>
      </div>
      <div class="bw-foot" id="bwFoot"></div>`);

    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('bwNote');
    const wrap = $('bwWrap');
    const tabsEl = $('bwTabs');
    const newTabBtn = $('bwNewTab');
    const reopenBtn = $('bwReopen');
    const starBtn = $('bwStar');
    const toolsEl = $('bwTools');
    const findBtn = $('bwFindBtn');
    const zoomOutBtn = $('bwZoomOut');
    const zoomBtn = $('bwZoom');
    const zoomInBtn = $('bwZoomIn');
    const printBtn = $('bwPrint');
    const pdfBtn = $('bwPdf');
    const dlBtn = $('bwDl');
    const dlCount = $('bwDlCount');
    const histBtn = $('bwHist');
    const sitesBtn = $('bwSites');
    const passBtn = $('bwPass');
    const extBtn = $('bwExt');
    const profBtn = $('bwProf');
    const profName = $('bwProfName');
    const privacySlot = $('bwPrivacy');
    const controlSlot = $('bwControl');
    const findBar = $('bwFind');
    const findInput = $('bwFindInput');
    const findCount = $('bwFindCount');
    const findPrev = $('bwFindPrev');
    const findNext = $('bwFindNext');
    const findClose = $('bwFindClose');
    const bmEl = $('bwBm');
    const barEl = $('bwBar');
    const backBtn = $('bwBack');
    const fwdBtn = $('bwFwd');
    const reloadBtn = $('bwReload');
    const form = $('bwAddrForm');
    const lockEl = $('bwLock');
    const addr = $('bwAddr');
    const sizeEl = $('bwSize');
    const presetsEl = $('bwPresets');
    const proxyEl = $('bwProxy');
    const pageEl = $('bwPage');
    const watchEl = $('bwWatch');
    const resR = $('bwResR');
    const resB = $('bwResB');
    const bodyEl = root.parentElement || root;

    PRESETS.forEach((p) => {
      const b = el('button');
      b.type = 'button';
      b.dataset.preset = p.id;
      b.title = p.label + (p.width ? ' (' + p.width + ')' : '');
      b.setAttribute('aria-label', p.label + ' width');
      b.dataset.spec = PRESET_SPEC[p.id];
      setHtml(b, html`${ICON[p.id]}`);
      b.addEventListener('click', () => setWidth(p.width, true));
      presetsEl.append(b);
    });

    setHtml($('bwFoot'), html`${footnotes(fallbackKind).map((f) => html`<span data-spec="${f.spec}"><b>${f.head}</b> &middot; ${f.text}</span>`)}`);
    ctx.chrome.setSub(pane ? 'real browser, shared with the AI' : 'proxied view, not shared with the AI');
    ctx.chrome.setLive(Boolean(pane));

    /* ---------------- small helpers ---------------- */
    const safe = (p) => {
      try {
        Promise.resolve(p).catch((err) => paneFailed(err));
      } catch (err) {
        paneFailed(err);
      }
    };
    function paneFailed(err) {
      if (disposed) return;
      note('The browser view did not answer: ' + (err && err.message ? err.message : String(err)), 'error');
    }
    function note(text, tone) {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    }

    /* what the page area shows right now */
    function cur() {
      if (surface === 'pane') {
        return { url: pendingUrl || model.url, title: model.url && !pendingUrl ? model.title : '', loading: model.loading || Boolean(pendingUrl && !model.error), back: model.canGoBack, fwd: model.canGoForward, error: model.error };
      }
      return { url: hist.current(), title: '', loading: frameLoading, back: hist.canBack(), fwd: hist.canForward(), error: null };
    }

    /* ---------------- native view: place, show, hide ---------------- */
    function computeBounds() {
      if (!root.isConnected || root.offsetParent === null) return null;
      const pr = pageEl.getBoundingClientRect();
      const br = bodyEl.getBoundingClientRect();
      return viewBounds(pr, br);
    }

    function applyView() {
      if (disposed || !pane) return;
      const b = computeBounds();
      if (b && !sameBounds(b, lastBounds)) {
        lastBounds = b;
        safe(pane.bounds(b)); /* sent even while hidden, so a show lands in place */
      }
      const v = cur();
      const want = surface === 'pane' && !paneProblem && !panel && Boolean(v.url) && !v.error && !ctx.overlays.open() && !dragging && Boolean(b);
      if (want && !viewShown) {
        viewShown = true;
        safe(pane.show());
      } else if (!want && viewShown) {
        viewShown = false;
        safe(pane.hide());
      }
      pageEl.dataset.viewShown = viewShown ? '1' : '0';
    }

    function paintSize() {
      const r = pageEl.getBoundingClientRect();
      sizeEl.textContent = r.width > 0 && r.height > 0 ? Math.round(r.width) + ' × ' + Math.round(r.height) : '';
      const w = Math.round(wrap.getBoundingClientRect().width);
      resR.setAttribute('aria-valuenow', String(w));
      resR.setAttribute('aria-valuemin', '280');
      resR.setAttribute('aria-valuemax', String(Math.round(root.clientWidth) || w));
      const h = Math.round(pageEl.getBoundingClientRect().height);
      resB.setAttribute('aria-valuenow', String(h));
      resB.setAttribute('aria-valuemin', '120');
      resB.setAttribute('aria-valuemax', '1600');
    }

    function schedule() {
      if (raf || disposed) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        if (disposed) return;
        applyView();
        paintSize();
      });
    }

    /* ---------------- painting ---------------- */
    function paintRegion(v) {
      let key;
      if (electron && !pane) key = 'unavailable:nopane';
      else if (paneProblem) key = 'unavailable:' + paneProblem;
      else if (surface === 'pane' && !paneKnown) key = 'loading';
      else if (panel && surface === 'pane') key = 'panel:' + panel;
      else if (v.error) key = 'error:' + v.error.code + ':' + v.error.url;
      else if (!v.url) key = 'empty:' + surface;
      else key = 'populated:' + surface;
      pageEl.dataset.surface = surface;
      if (key === regionKey) {
        if (key.startsWith('populated:pane')) {
          const cover = pageEl.querySelector('.bw-cover');
          if (cover) cover.textContent = viewShown ? '' : v.loading ? 'Loading' : 'The page is hidden while a panel is open.';
        }
        return;
      }
      regionKey = key;
      if (key === 'unavailable:nopane') {
        states.unavailable(pageEl, 'This Electron window has no browser pane bridge.', {
          detail: 'The preload script did not expose window.dourmouseShell.pane, so the native browser view cannot be used. Restart the app from the current build.',
        });
      } else if (key.startsWith('unavailable:')) {
        states.unavailable(pageEl, 'The browser view did not answer.', { detail: paneProblem, retry: () => connectPane() });
      } else if (key === 'loading') {
        states.loading(pageEl, 'Asking the browser view for its state');
      } else if (key.startsWith('panel:')) {
        states.populated(pageEl, panelRoot);
        buildPanel();
      } else if (key.startsWith('error:')) {
        states.error(pageEl, { message: failLine(v.error), path: v.error.url }, {
          title: 'This page did not load',
          retry: () => reload(),
        });
      } else if (key.startsWith('empty:')) {
        states.empty(pageEl, 'Nothing is loaded.', { hint: 'Type an address above and press Enter. Only http and https pages open here.' });
      } else if (surface === 'pane') {
        const cover = el('div', 'bw-cover');
        states.populated(pageEl, cover);
        cover.textContent = viewShown ? '' : 'Loading';
      } else {
        states.populated(pageEl, frameEl);
      }
    }

    function paint() {
      if (disposed) return;
      const v = cur();
      const lock = lockInfo(v.url);
      form.dataset.lock = lock.kind;
      lockEl.setAttribute('aria-label', lock.label);
      lockEl.title = lock.label;
      if (!dirty && addr.value !== v.url) addr.value = v.url;
      const usable = !(electron && !pane) && !paneProblem;
      backBtn.disabled = !usable || !v.back;
      fwdBtn.disabled = !usable || !v.fwd;
      reloadBtn.disabled = !usable || (!v.url && !v.error); /* a crashed tab has no address but can still be reloaded */
      addr.disabled = !usable;
      barEl.dataset.loading = v.loading ? '1' : '0';
      const mode = surface === 'pane' && v.loading ? 'stop' : 'reload';
      if (mode !== reloadMode) {
        reloadMode = mode;
        setHtml(reloadBtn, html`${mode === 'stop' ? ICON.stop : ICON.reload}`);
        reloadBtn.setAttribute('aria-label', mode === 'stop' ? 'Stop loading' : 'Reload');
        reloadBtn.title = mode === 'stop' ? 'Stop loading' : 'Reload';
      }
      const proxied = surface === 'frame' && frameProxied && Boolean(v.url);
      proxyEl.hidden = !proxied;
      if (proxied) {
        proxyEl.textContent = 'Proxied view: this server fetched the page for you and shows it in a sandbox. No cookies or logins reach it, only this exact address is fetched, and links clicked inside it do not open here. Type an address in the bar, or use OPEN EXTERNALLY.';
      }
      paintRegion(v);
      renderTabs(v);
      paintTools(v);
      const web = isWebUrl(v.url);
      const key = String(web);
      if (key !== actionKey) {
        actionKey = key;
        ctx.chrome.setActions([
          {
            id: 'ext', label: 'OPEN EXTERNALLY', disabled: !web,
            spec: 'Opens the address this pane is showing in the system browser. It changes nothing here.',
            onClick: () => openExternally(),
          },
        ]);
      }
      applyView();
      paintSize();
    }

    function paintWatch(err) {
      if (disposed) return;
      if (err) {
        states.error(watchEl, err, { retry: () => loadAttached(), title: 'Could not read the browser agent state' });
        return;
      }
      if (!att) {
        states.loading(watchEl, 'Reading the browser agent state');
        return;
      }
      const w = watchLine(att, cur().url, electron ? 'electron' : ctx.host.kind, (t) => ago(t));
      const line = el('div', 'bw-watchline');
      line.style.display = 'flex';
      line.style.gap = '8px';
      const eye = el('span');
      setHtml(eye, html`${ICON.eye}`);
      const text = el('span', '', w.text);
      line.append(eye, text);
      states.populated(watchEl, line);
      watchEl.dataset.tone = w.tone === 'warn' ? 'warn' : w.tone === 'live' ? 'live' : '';
    }

    /* ---------------- navigation ---------------- */
    function teardownFrame() {
      if (frameEl) {
        frameEl.remove();
        frameEl = null;
      }
      frameLoading = false;
      frameProxied = false;
      regionKey = '';
    }

    function loadFrame(src, proxied) {
      if (!frameEl) {
        frameEl = document.createElement('iframe');
        /* scripts run, but never with the same-origin grant beside them: that pair
           lets a framed page lift its own sandbox. */
        frameEl.setAttribute('sandbox', 'allow-scripts allow-forms');
        frameEl.setAttribute('referrerpolicy', 'no-referrer');
        frameEl.setAttribute('title', 'Embedded page');
        frameEl.addEventListener('load', () => {
          frameLoading = false;
          if (!disposed) paint();
        });
        regionKey = '';
      }
      frameProxied = proxied;
      frameLoading = true;
      frameEl.src = src;
      paint();
    }

    async function openWeb(url) {
      note('', '');
      closePanel();
      if (pane && !paneProblem) {
        if (surface === 'frame') teardownFrame();
        surface = 'pane';
        pendingUrl = url;
        sawLoading = false;
        paint();
        applyView(); /* bounds first, so the show that navigate causes lands in place */
        try {
          const r = await pane.navigate(url);
          if (r && r.ok === false) {
            pendingUrl = '';
            note('The browser view refused that address: ' + (r.error || 'no reason given'), 'error');
          }
        } catch (err) {
          pendingUrl = '';
          paneFailed(err);
        }
        if (!disposed) paint();
        return;
      }
      if (viewShown && pane) {
        viewShown = false;
        safe(pane.hide());
      }
      surface = 'frame';
      hist.push(url);
      loadFrame('/api/browser-pane/proxy?url=' + encodeURIComponent(url), true);
    }

    function openApp(path) {
      note('', '');
      closePanel();
      surface = 'frame';
      pendingUrl = '';
      if (viewShown && pane) {
        viewShown = false;
        safe(pane.hide());
      }
      hist.push(path);
      loadFrame(path, false);
    }

    function openFromRequest(rawUrl) {
      const t = eventTarget(rawUrl);
      if (!t.ok) {
        note('A request to open a page was refused: ' + t.reason, 'error');
        return;
      }
      if (t.kind === 'app') openApp(t.url);
      else openWeb(t.url);
      note('Opened because the server asked this pane to: ' + t.url, 'ok');
    }

    function submitAddress() {
      const n = normalizeAddress(addr.value);
      if (!n.ok) {
        addr.setAttribute('aria-invalid', 'true');
        note(n.reason, 'error');
        return;
      }
      addr.removeAttribute('aria-invalid');
      dirty = false;
      addr.value = n.url;
      openWeb(n.url);
    }

    function reload() {
      if (surface === 'pane') {
        if (pane) safe(pane.nav(cur().loading && !model.error ? 'stop' : 'reload'));
      } else if (frameEl && hist.current()) {
        loadFrame(hist.current(), frameProxied ? true : false);
      }
    }

    function goHistory(dir) {
      if (surface === 'pane') {
        if (pane) safe(pane.nav(dir));
        return;
      }
      const target = dir === 'back' ? hist.back() : hist.forward();
      if (!target) return;
      if (target.startsWith('/') && !target.startsWith('//')) loadFrame(target, false);
      else loadFrame('/api/browser-pane/proxy?url=' + encodeURIComponent(target), true);
    }

    function openExternally() {
      const v = cur();
      if (!isWebUrl(v.url)) return;
      if (!ctx.host.openExternal(v.url)) {
        ctx.notify({ level: 'warn', title: 'Could not open it', detail: 'This window has no way to open the system browser.' });
      }
    }

    /* ---------------- tabs (Phase B1) ---------------- */
    function virtualTab(v) {
      return { id: 0, url: v.url, title: v.title, favicon: '', loading: v.loading, active: true, audible: false };
    }

    function tabNode(t, agentUrl) {
      const holder = el('div', 'cb-tab' + (t.active ? ' on' : ''));
      holder.dataset.tab = String(t.id);
      const pick = el('button', 'cb-pick');
      pick.type = 'button';
      pick.setAttribute('role', 'tab');
      pick.setAttribute('aria-selected', String(t.active));
      pick.dataset.role = 'pick';
      const label = tabLabel(t);
      pick.title = label + (t.url ? '\n' + t.url : '');
      const fav = el('span', 'fav');
      if (t.loading) {
        fav.classList.add('spin');
      } else if (t.favicon) {
        const img = el('img');
        img.alt = '';
        img.src = t.favicon;
        fav.append(img);
      } else {
        setHtml(fav, html`${ICON.globe}`);
      }
      const tt = el('span', 'tt', label);
      pick.append(fav, tt);
      if (t.audible) pick.append(el('span', 'snd', 'sound'));
      if (agentUrl && t.url && sameAddress(t.url, agentUrl)) {
        const eye = el('span', 'eye');
        setHtml(eye, html`${ICON.eye}`);
        eye.title = 'The browser agent is attached to this tab';
        pick.append(eye);
      }
      pick.addEventListener('click', () => selectTab(t.id));
      holder.append(pick);
      if (pane && t.id > 0) {
        const x = el('button', 'cb-x');
        x.type = 'button';
        x.dataset.role = 'close';
        x.setAttribute('aria-label', 'Close tab ' + label);
        x.title = 'Close tab';
        setHtml(x, html`${ICON.stop}`);
        x.addEventListener('click', (e) => {
          e.stopPropagation();
          closeTab(t.id);
        });
        holder.append(x);
        holder.addEventListener('auxclick', (e) => {
          if (e.button === 1) {
            e.preventDefault();
            closeTab(t.id);
          }
        });
      }
      return holder;
    }

    function renderTabs(v) {
      const list = pane && tm.tabs.length ? tm.tabs : [virtualTab(v)];
      const agentUrl = electron && att && att.attached ? att.page_url : '';
      const sig = tabsKey({ tabs: list }, agentUrl) + (pane ? 'p' : 'f') + (surface === 'frame' ? hostOf(v.url) : '');
      if (sig === tabsSig) return;
      tabsSig = sig;
      const focused = tabsEl.contains(document.activeElement) ? document.activeElement : null;
      const keep = focused ? { id: focused.closest('[data-tab]').dataset.tab, role: focused.dataset.role } : null;
      const shown = surface === 'frame' && !pane ? [{ ...list[0], url: v.url, title: hostOf(v.url) }] : list;
      tabsEl.replaceChildren(...shown.map((t) => tabNode(t, agentUrl)));
      if (keep) {
        const again = tabsEl.querySelector('[data-tab="' + keep.id + '"] [data-role="' + keep.role + '"]');
        if (again) again.focus();
      }
    }

    function selectTab(id) {
      if (!pane || id === 0 || id === tm.activeId) {
        closePanel();
        return;
      }
      closePanel();
      closeFindUi();
      safe(pane.selectTab(id));
    }

    async function newTab() {
      if (!pane || paneProblem) return;
      closePanel();
      try {
        const r = await pane.newTab();
        if (r && r.ok === false) {
          note('A new tab did not open: ' + (r.error || 'no reason given'), 'error');
          return;
        }
        dirty = true;
        addr.value = '';
        addr.focus();
      } catch (err) {
        paneFailed(err);
      }
    }

    function closeTab(id) {
      if (!pane) return;
      safe(pane.closeTab(id));
    }

    async function reopenTab() {
      if (!pane) return;
      try {
        const r = await pane.reopenTab();
        if (r && r.ok === false) note(r.error || 'No closed tab to reopen.', 'error');
        else closePanel();
      } catch (err) {
        paneFailed(err);
      }
    }

    /* ---------------- tools: find, zoom, print, star (Phase B1) ---------------- */
    function paintTools(v) {
      const usable = hasB1 && !paneProblem;
      const page = usable && Boolean(v.url);
      toolsEl.hidden = !hasB1;
      newTabBtn.hidden = !hasB1;
      starBtn.hidden = !hasB1;
      newTabBtn.disabled = !usable;
      reopenBtn.hidden = !(usable && tm.closed > 0);
      findBtn.disabled = !page;
      zoomOutBtn.disabled = !page;
      zoomInBtn.disabled = !page;
      zoomBtn.disabled = !page;
      printBtn.disabled = !page;
      pdfBtn.disabled = !page;
      dlBtn.disabled = !usable;
      histBtn.disabled = !usable;
      sitesBtn.hidden = !hasB2;
      passBtn.hidden = !hasB2;
      sitesBtn.disabled = !usable;
      passBtn.disabled = !usable;
      extBtn.hidden = !hasB3;
      profBtn.hidden = !hasB3;
      extBtn.disabled = !usable;
      profBtn.disabled = !usable;
      extBtn.setAttribute('aria-pressed', String(panel === 'extensions'));
      profBtn.setAttribute('aria-pressed', String(panel === 'profiles'));
      const pl = profileLabel(tm.profile);
      profName.hidden = !pl;
      profName.textContent = pl;
      profBtn.title = pl ? 'Profile: ' + pl + ' (profiles and import)' : 'Profiles and import';
      sitesBtn.setAttribute('aria-pressed', String(panel === 'sites'));
      passBtn.setAttribute('aria-pressed', String(panel === 'passwords'));
      zoomBtn.textContent = zoomLabel(tm.zoom);
      zoomBtn.dataset.changed = Math.abs(tm.zoom - 1) > 0.001 ? '1' : '0';
      dlBtn.setAttribute('aria-pressed', String(panel === 'downloads'));
      histBtn.setAttribute('aria-pressed', String(panel === 'history'));
      const busy = activeDownloads(dl);
      dlCount.hidden = busy === 0;
      dlCount.textContent = String(busy);
      const marked = Boolean(bookmarkFor(bms, v.url));
      starBtn.disabled = !usable || !isWebUrl(v.url);
      starBtn.setAttribute('aria-pressed', String(marked));
      starBtn.setAttribute('aria-label', marked ? 'Remove this bookmark' : 'Bookmark this page');
      starBtn.title = marked ? 'Remove this bookmark' : 'Bookmark this page (Cmd+D)';
      findBar.hidden = !findOpen;
      findCount.textContent = findLabel(tm.find, findInput.value);
      findCount.dataset.none = tm.find && tm.find.text === findInput.value && tm.find.matches === 0 && findInput.value ? '1' : '0';
    }

    function openFind() {
      if (!pane || paneProblem || !cur().url) return;
      closePanel();
      findOpen = true;
      if (!offFindEsc) offFindEsc = ctx.keys.pushEsc(() => closeFind());
      paint();
      findInput.focus();
      findInput.select();
    }

    function closeFindUi() {
      findOpen = false;
      findBar.hidden = true;
      if (offFindEsc) {
        offFindEsc();
        offFindEsc = null;
      }
    }

    function closeFind() {
      if (!findOpen) return;
      closeFindUi();
      if (pane) {
        safe(pane.findStop());
        safe(pane.focusPage());
      }
      schedule();
    }

    function runFind(forward, next) {
      if (!pane) return;
      const q = findInput.value;
      if (!q) {
        safe(pane.findStop());
        paintTools(cur());
        return;
      }
      safe(pane.find(q, { forward, findNext: next }));
    }

    function zoom(action) {
      if (pane && cur().url) safe(pane.zoom(action));
    }

    async function printPage(asPdf) {
      if (!pane || !cur().url) return;
      try {
        const r = await pane.print(asPdf ? { pdf: true } : undefined);
        if (r && r.ok === false) note('Printing did not start: ' + (r.error || 'no reason given'), 'error');
        else if (asPdf && r && r.path) ctx.notify({ level: 'ok', title: 'Saved as PDF', detail: r.path });
      } catch (err) {
        paneFailed(err);
      }
    }

    /* ---------------- bookmarks (Phase B1) ---------------- */
    async function loadBookmarks() {
      if (!hasB1) return;
      try {
        const d = await ctx.api.get('/api/os/browser/bookmarks');
        if (disposed || ctx.signal.aborted) return;
        bms = bookmarksModel(d.bookmarks);
        bmsError = '';
      } catch (err) {
        if (isAbort(err) || disposed) return;
        bmsError = err && err.message ? err.message : String(err);
      }
      paintBookmarks();
      paintTools(cur());
    }

    function paintBookmarks() {
      bmEl.hidden = !hasB1;
      if (!hasB1) return;
      if (bmsError) {
        bmEl.replaceChildren(el('span', 'cb-bmnote', 'Bookmarks are not available: ' + bmsError));
        return;
      }
      if (!bms.length) {
        bmEl.replaceChildren(el('span', 'cb-bmnote', 'Bookmarks you add with the star in the address bar appear here.'));
        return;
      }
      bmEl.replaceChildren(
        ...bms.map((b) => {
          const holder = el('span', 'cb-bmk');
          const open = el('button', 'cb-bmopen', b.title);
          open.type = 'button';
          open.title = b.url;
          open.addEventListener('click', () => openWeb(b.url));
          const x = el('button', 'cb-bmx');
          x.type = 'button';
          x.setAttribute('aria-label', 'Remove bookmark ' + b.title);
          x.title = 'Remove bookmark';
          setHtml(x, html`${ICON.stop}`);
          x.addEventListener('click', () => removeBookmark(b));
          holder.append(open, x);
          return holder;
        }),
      );
    }

    async function toggleBookmark() {
      const v = cur();
      if (!pane || !isWebUrl(v.url)) return;
      const have = bookmarkFor(bms, v.url);
      try {
        if (have) await ctx.api.post('/api/os/browser/bookmarks/remove', { id: have.id });
        else await ctx.api.post('/api/os/browser/bookmarks/add', { url: v.url, title: v.title || hostOf(v.url) });
      } catch (err) {
        if (!isAbort(err)) note('The bookmark was not changed: ' + (err && err.message ? err.message : String(err)), 'error');
        return;
      }
      await loadBookmarks();
    }

    async function removeBookmark(b) {
      try {
        await ctx.api.post('/api/os/browser/bookmarks/remove', { id: b.id });
      } catch (err) {
        if (!isAbort(err)) note('The bookmark was not removed: ' + (err && err.message ? err.message : String(err)), 'error');
        return;
      }
      await loadBookmarks();
    }

    /* ---------------- history and downloads panels (Phase B1) ---------------- */
    function openPanel(name) {
      if (!hasB1 || paneProblem) return;
      if (panel === name) {
        closePanel();
        return;
      }
      closeFind();
      panel = name;
      if (!offPanelEsc) offPanelEsc = ctx.keys.pushEsc(() => closePanel());
      regionKey = '';
      paint();
    }

    function closePanel() {
      if (!panel) return;
      if (panelHandle) {
        panelHandle.dispose();
        panelHandle = null;
      }
      panel = '';
      histBody = null;
      dlBody = null;
      histConfirm = null;
      if (histTimer) {
        clearTimeout(histTimer);
        histTimer = 0;
      }
      if (offPanelEsc) {
        offPanelEsc();
        offPanelEsc = null;
      }
      regionKey = '';
      paint();
    }

    function panelHead(title, ...controls) {
      const head = el('div', 'bw-ph');
      head.append(el('h3', '', title), ...controls);
      return head;
    }

    function panelButton(label, onClick, spec) {
      const b = el('button', 'os-btn', label);
      b.type = 'button';
      if (spec) b.dataset.spec = spec;
      b.addEventListener('click', onClick);
      return b;
    }

    function buildPanel() {
      if (panel === 'history') buildHistoryPanel();
      else if (panel === 'downloads') buildDownloadsPanel();
      else if (privacyUi && (panel === 'sites' || panel === 'passwords')) {
        const close = panelButton('CLOSE', () => closePanel(), 'Goes back to the page.');
        panelHandle = privacyUi.panels[panel](panelRoot, { head: panelHead, button: panelButton, close });
      } else if (manageUi && (panel === 'extensions' || panel === 'profiles')) {
        const close = panelButton('CLOSE', () => closePanel(), 'Goes back to the page.');
        panelHandle = manageUi.panels[panel](panelRoot, { head: panelHead, button: panelButton, close });
      }
    }

    function buildHistoryPanel() {
      const search = el('input', 'bw-search');
      search.type = 'search';
      search.placeholder = 'Search history';
      search.setAttribute('aria-label', 'Search history');
      search.autocomplete = 'off';
      search.value = histQuery;
      search.dataset.spec = 'Filters the list by words in the page title or the address. It searches everything the browser has recorded, not only what is shown.';
      search.addEventListener('input', () => {
        histQuery = search.value;
        if (histTimer) clearTimeout(histTimer);
        histTimer = setTimeout(() => {
          histTimer = 0;
          loadHistory();
        }, 250);
      });
      const clear = panelButton('CLEAR ALL', () => askClearHistory(), 'Removes every recorded visit from this Mac after you confirm. Bookmarks and downloads are not touched.');
      const close = panelButton('CLOSE', () => closePanel(), 'Goes back to the page.');
      histConfirm = el('div', 'bw-pc');
      histBody = el('div', 'bw-pb');
      panelRoot.dataset.panel = 'history';
      panelRoot.replaceChildren(panelHead('History', search, clear, close), histConfirm, histBody);
      loadHistory();
    }

    async function loadHistory() {
      const body = histBody;
      if (!body) return;
      const seq = ++histSeq;
      states.loading(body, 'Reading history');
      const q = histQuery.trim();
      try {
        const d = await ctx.api.get('/api/os/browser/history?limit=300' + (q ? '&q=' + encodeURIComponent(q) : ''));
        if (disposed || seq !== histSeq || body !== histBody) return;
        const groups = groupHistory(d.history);
        if (!groups.length) {
          states.empty(body, q ? 'Nothing in the history matches that.' : 'No history yet.', { hint: q ? '' : 'Pages you visit in this browser are listed here.' });
          return;
        }
        const nodes = [];
        groups.forEach((g) => {
          nodes.push(el('h4', 'bw-day', g.label));
          g.items.forEach((e) => nodes.push(historyRow(e)));
        });
        states.populated(body, nodes);
      } catch (err) {
        if (isAbort(err) || disposed || body !== histBody) return;
        states.error(body, err, { retry: () => loadHistory(), title: 'Could not read the history' });
      }
    }

    function historyRow(e) {
      const row = el('div', 'bw-hrow');
      const open = el('button', 'bw-hopen');
      open.type = 'button';
      open.title = e.url;
      open.append(el('span', 'tm', clock(e.at).slice(0, 5)), el('span', 'ti', e.title || hostOf(e.url) || e.url), el('span', 'ur', hostOf(e.url) || e.url));
      open.addEventListener('click', () => {
        closePanel();
        openWeb(e.url);
      });
      const x = el('button', 'cb-x');
      x.type = 'button';
      x.setAttribute('aria-label', 'Remove from history');
      x.title = 'Remove from history';
      setHtml(x, html`${ICON.stop}`);
      x.addEventListener('click', async () => {
        try {
          await ctx.api.post('/api/os/browser/history/remove', { id: e.id });
        } catch (err) {
          if (!isAbort(err)) note('That entry was not removed: ' + (err && err.message ? err.message : String(err)), 'error');
          return;
        }
        loadHistory();
      });
      row.append(open, x);
      return row;
    }

    function askClearHistory() {
      if (!histConfirm) return;
      confirmHere(
        histConfirm,
        'Clear all browsing history? Every visit this browser has recorded is removed from this Mac. Bookmarks and downloads are not touched. This cannot be undone.',
        () => ctx.api.post('/api/os/browser/history/clear', {}),
        {
          onDone: (ok) => {
            if (histConfirm) histConfirm.replaceChildren();
            if (ok) loadHistory();
          },
        },
      );
    }

    function buildDownloadsPanel() {
      const clear = panelButton('CLEAR FINISHED', () => {
        if (pane) safe(pane.clearDownloads());
      }, 'Removes finished, cancelled and failed downloads from this list. The files themselves stay where they are.');
      const close = panelButton('CLOSE', () => closePanel(), 'Goes back to the page.');
      dlBody = el('div', 'bw-pb');
      panelRoot.dataset.panel = 'downloads';
      panelRoot.replaceChildren(
        panelHead('Downloads', clear, close),
        el('p', 'bw-pnote', 'Saved to your Downloads folder, flagged for Gatekeeper, and checked by the downloads watcher. Nothing is opened by itself, and a file that can run code is only ever shown in Finder.'),
        dlBody,
      );
      dlStruct = '';
      paintDownloads();
    }

    function dlAction(id, act) {
      if (!pane) return;
      Promise.resolve(pane.downloadAction(id, act))
        .then((r) => {
          if (r && r.ok === false && !disposed) note(r.error || 'That did not work.', 'error');
        })
        .catch((err) => paneFailed(err));
    }

    function downloadRow(d) {
      const row = el('div', 'bw-dl');
      row.dataset.state = d.state;
      const name = el('div', 'nm', d.filename);
      name.title = d.url;
      const line = el('div', 'ln', downloadLine(d));
      const bar = el('div', 'bar');
      const fill = el('i');
      bar.append(fill);
      bar.hidden = d.state !== 'progressing';
      const acts = el('div', 'ac');
      const btn = (label, act, extra) => {
        const b = el('button', 'os-btn', label);
        b.type = 'button';
        if (extra && extra.disabled) b.disabled = true;
        if (extra && extra.title) b.title = extra.title;
        b.addEventListener('click', () => dlAction(d.id, act));
        acts.append(b);
      };
      if (d.state === 'progressing') {
        btn(d.paused ? 'RESUME' : 'PAUSE', d.paused ? 'resume' : 'pause');
        btn('CANCEL', 'cancel');
      } else {
        if (d.state === 'completed') {
          btn('OPEN', 'open', d.openable ? null : { disabled: true, title: 'This kind of file can run code, so it is not opened from here. Use Show in folder.' });
          btn('SHOW IN FOLDER', 'reveal');
        }
        btn('REMOVE', 'remove');
      }
      row.append(name, line, bar, acts);
      dlLive.set(d.id, { line, fill, bar });
      return row;
    }

    function paintDownloads() {
      const body = dlBody;
      if (!body) return;
      if (!dl.length) {
        dlStruct = '';
        dlLive = new Map();
        states.empty(body, 'No downloads yet.', { hint: 'Anything this browser downloads is listed here.' });
        return;
      }
      const struct = dl.map((d) => [d.id, d.state, d.paused ? 1 : 0, d.openable ? 1 : 0, String(d.quarantined)].join('|')).join('~');
      if (struct === dlStruct && body.dataset.state === 'populated') {
        /* the same rows: only the numbers moved, so update them in place and keep the buttons the owner may be pressing */
        dl.forEach((d) => {
          const live = dlLive.get(d.id);
          if (!live) return;
          live.line.textContent = downloadLine(d);
          live.fill.style.width = (d.percent === null ? 100 : d.percent) + '%';
        });
        return;
      }
      dlStruct = struct;
      dlLive = new Map();
      const rows = dl.map((d) => downloadRow(d));
      dl.forEach((d) => {
        const live = dlLive.get(d.id);
        if (live) live.fill.style.width = (d.percent === null ? 100 : d.percent) + '%';
      });
      states.populated(body, rows);
    }

    function onDownloads(list) {
      if (disposed) return;
      dl = downloadsModel(list);
      paintTools(cur());
      if (panel === 'downloads') paintDownloads();
    }

    function onCommand(cmd) {
      if (disposed) return;
      if (cmd === 'find') openFind();
      else if (cmd === 'address') {
        addr.focus();
        addr.select();
      } else if (cmd === 'find-next') runFind(true, true);
      else if (cmd === 'find-prev') runFind(false, true);
    }

    /* ---------------- sizing ---------------- */
    function setWidth(w, persist) {
      widthPx = w;
      wrap.style.width = w ? w + 'px' : '100%';
      presetsEl.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.preset === presetForWidth(widthPx))));
      if (persist) ctx.prefs.set('width', w === null ? 'fill' : String(w));
      schedule();
    }

    function setHeight(h, persist) {
      heightPx = h;
      if (h) {
        wrap.style.height = h + 'px';
        wrap.dataset.fixedHeight = '1';
      } else {
        wrap.style.height = '';
        delete wrap.dataset.fixedHeight;
      }
      if (persist) ctx.prefs.set('height', h === null ? 'fill' : String(h));
      schedule();
    }

    function chromeHeight() {
      return wrap.getBoundingClientRect().height - pageEl.getBoundingClientRect().height;
    }

    function bindDrag(handle, axis) {
      let start = null;
      handle.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        try {
          handle.setPointerCapture(e.pointerId);
        } catch (err) {
          /* a synthetic pointer cannot be captured; the drag still works */
        }
        start = { x: e.clientX, y: e.clientY, w: wrap.getBoundingClientRect().width, h: wrap.getBoundingClientRect().height };
        dragging = true;
        handle.classList.add('drag');
        pageEl.dataset.dragging = '1';
        applyView(); /* the native view would swallow the pointer, so it steps aside */
        e.preventDefault();
      });
      handle.addEventListener('pointermove', (e) => {
        if (!start) return;
        if (axis === 'x') setWidth(clampWidth(start.w + 2 * (e.clientX - start.x), root.clientWidth), false);
        else setHeight(clampHeight(start.h + (e.clientY - start.y), 1600 + chromeHeight()), false);
      });
      const end = () => {
        if (!start) return;
        start = null;
        dragging = false;
        handle.classList.remove('drag');
        delete pageEl.dataset.dragging;
        if (axis === 'x') setWidth(widthPx, true);
        else setHeight(heightPx, true);
        applyView();
      };
      handle.addEventListener('pointerup', end);
      handle.addEventListener('pointercancel', end);
      handle.addEventListener('keydown', (e) => {
        const step = e.shiftKey ? 100 : 20;
        if (axis === 'x' && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
          const cw = wrap.getBoundingClientRect().width;
          setWidth(clampWidth(cw + (e.key === 'ArrowRight' ? step : -step), root.clientWidth), true);
          e.preventDefault();
        } else if (axis === 'y' && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
          const ch = wrap.getBoundingClientRect().height;
          setHeight(clampHeight(ch + (e.key === 'ArrowDown' ? step : -step), 1600 + chromeHeight()), true);
          e.preventDefault();
        }
      });
    }
    bindDrag(resR, 'x');
    bindDrag(resB, 'y');

    /* ---------------- wiring ---------------- */
    backBtn.addEventListener('click', () => goHistory('back'));
    fwdBtn.addEventListener('click', () => goHistory('forward'));
    reloadBtn.addEventListener('click', () => reload());
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      submitAddress();
    });
    addr.addEventListener('input', () => {
      dirty = true;
      addr.removeAttribute('aria-invalid');
    });
    addr.addEventListener('blur', () => {
      dirty = false;
      paint();
    });
    addr.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        dirty = false;
        addr.value = cur().url;
        addr.removeAttribute('aria-invalid');
        note('', '');
      }
    });
    const focusAddress = () => {
      addr.focus();
      addr.select();
    };
    ctx.keys.bind('Alt+l', focusAddress);
    newTabBtn.addEventListener('click', () => newTab());
    reopenBtn.addEventListener('click', () => reopenTab());
    starBtn.addEventListener('click', () => toggleBookmark());
    findBtn.addEventListener('click', () => (findOpen ? closeFind() : openFind()));
    zoomOutBtn.addEventListener('click', () => zoom('out'));
    zoomInBtn.addEventListener('click', () => zoom('in'));
    zoomBtn.addEventListener('click', () => zoom('reset'));
    printBtn.addEventListener('click', () => printPage(false));
    pdfBtn.addEventListener('click', () => printPage(true));
    dlBtn.addEventListener('click', () => openPanel('downloads'));
    histBtn.addEventListener('click', () => openPanel('history'));
    sitesBtn.addEventListener('click', () => openPanel('sites'));
    passBtn.addEventListener('click', () => openPanel('passwords'));
    extBtn.addEventListener('click', () => openPanel('extensions'));
    profBtn.addEventListener('click', () => openPanel('profiles'));
    findInput.addEventListener('input', () => runFind(true, false));
    findInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        runFind(!e.shiftKey, true);
      }
    });
    findNext.addEventListener('click', () => runFind(true, true));
    findPrev.addEventListener('click', () => runFind(false, true));
    findClose.addEventListener('click', () => closeFind());
    /* Chrome's keys while the console has the keyboard. While the PAGE has it, the shell handles the same keys itself. */
    ctx.keys.bind(MOD + '+t', () => newTab());
    ctx.keys.bind(MOD + '+Shift+t', () => reopenTab());
    ctx.keys.bind(MOD + '+l', focusAddress);
    ctx.keys.bind(MOD + '+f', () => openFind());
    ctx.keys.bind(MOD + '+g', () => runFind(true, true));
    ctx.keys.bind(MOD + '+Shift+g', () => runFind(false, true));
    ctx.keys.bind(MOD + '+p', () => printPage(false));
    ctx.keys.bind(MOD + '+d', () => toggleBookmark());
    ctx.keys.bind(MOD + '+=', () => zoom('in'));
    ctx.keys.bind(MOD + '+-', () => zoom('out'));
    ctx.keys.bind(MOD + '+0', () => zoom('reset'));
    /* the keymap ignores keys typed into a field, so the two fields of this screen answer these themselves */
    addr.addEventListener('keydown', (e) => {
      const m = MOD === 'Meta' ? e.metaKey : e.ctrlKey;
      if (m && !e.altKey && !e.shiftKey && String(e.key).toLowerCase() === 'f') {
        e.preventDefault();
        openFind();
      }
    });
    findInput.addEventListener('keydown', (e) => {
      const m = MOD === 'Meta' ? e.metaKey : e.ctrlKey;
      if (m && !e.altKey && String(e.key).toLowerCase() === 'g') {
        e.preventDefault();
        runFind(!e.shiftKey, true);
      }
    });

    /* the native view follows every change of the page area */
    const ro = new ResizeObserver(() => schedule());
    ro.observe(pageEl);
    ro.observe(bodyEl);
    ro.observe(document.documentElement);
    const mo = new MutationObserver(() => schedule());
    [root.closest('#shell'), root.closest('#stage')].forEach((n) => {
      if (n) mo.observe(n, { attributes: true, attributeFilter: ['data-min', 'data-sidebar', 'data-dock', 'class', 'style', 'hidden'] });
    });
    bodyEl.addEventListener('scroll', schedule, { passive: true });
    ctx.overlays.onChange(() => paint());

    function shutDown() {
      if (disposed) return;
      disposed = true;
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
      mo.disconnect();
      bodyEl.removeEventListener('scroll', schedule);
      if (typeof offPane === 'function') offPane();
      offPane = null;
      /* A BrowserView left visible after leaving this screen is a defect. */
      viewShown = false;
      if (pane) {
        try {
          Promise.resolve(pane.hide()).catch(() => {});
        } catch (err) {
          /* the window is going away */
        }
      }
      if (frameEl) frameEl.remove();
      if (hasB1 && typeof pane.screen === 'function') {
        try {
          Promise.resolve(pane.screen(false)).catch(() => {});
        } catch (err) {
          /* the window is going away */
        }
      }
      if (typeof offDl === 'function') offDl();
      if (typeof offCmd === 'function') offCmd();
      offDl = null;
      offCmd = null;
      if (typeof offPrivacy === 'function') offPrivacy();
      offPrivacy = null;
      if (panelHandle) panelHandle.dispose();
      panelHandle = null;
      if (privacyUi) privacyUi.dispose();
      if (typeof offControl === 'function') offControl();
      offControl = null;
      if (controlUi) controlUi.dispose();
      controlUi = null;
      manageUi = null;
      if (histTimer) clearTimeout(histTimer);
      if (offFindEsc) offFindEsc();
      if (offPanelEsc) offPanelEsc();
      offFindEsc = null;
      offPanelEsc = null;
    }
    ctx.signal.addEventListener('abort', shutDown);

    /* ---------------- the pane and the agent ---------------- */
    function onPaneState(s) {
      if (disposed) return;
      model = paneModel(s);
      const profileBefore = tm.profile;
      tm = tabsModel(s);
      paneKnown = true;
      if (profileBefore && profileBefore !== tm.profile) loadBookmarks(); /* another profile has its own bookmarks */
      if (tm.activeId && lastActiveId && tm.activeId !== lastActiveId) {
        /* another tab came forward (a click, Cmd+1, a pop-up, the agent): a panel or find bar of the old one is stale */
        findInput.value = '';
        if (panel) closePanel();
      }
      lastActiveId = tm.activeId || lastActiveId;
      if (pendingUrl) {
        /* the address asked for stays in the bar until that load has run its course, as a browser does */
        if (model.loading) sawLoading = true;
        if (model.error || (sawLoading && !model.loading) || (model.url === pendingUrl && !model.loading)) {
          pendingUrl = '';
          sawLoading = false;
        }
      }
      /* somebody else (the agent over the bridge) showed or hid the view */
      viewShown = model.open;
      paint();
      if (att) paintWatch();
    }

    async function connectPane() {
      if (!pane) return;
      paneProblem = '';
      regionKey = '';
      try {
        const s = await pane.state();
        if (disposed || ctx.signal.aborted) return;
        model = paneModel(s);
        tm = tabsModel(s);
        lastActiveId = tm.activeId;
        paneKnown = true;
        viewShown = model.open;
      } catch (err) {
        if (disposed) return;
        paneProblem = err && err.message ? err.message : String(err);
        paneKnown = true;
      }
      paint();
    }

    async function loadAttached() {
      if (attBusy) {
        attAgain = true;
        return;
      }
      attBusy = true;
      try {
        const d = await ctx.api.get('/api/os/browser/attached');
        if (disposed || ctx.signal.aborted) return;
        att = d;
        paintWatch();
        renderTabs(cur());
      } catch (err) {
        if (!isAbort(err) && !disposed) paintWatch(err);
      } finally {
        attBusy = false;
        if (attAgain && !disposed) {
          attAgain = false;
          loadAttached();
        }
      }
    }

    /* the shell fans the pane's state out but does not remove a screen's listener: this one is removed in shutDown */
    if (pane) offPane = ctx.host.onPaneState(onPaneState);
    ctx.events.on('browser_pane_open', (evt) => openFromRequest(evt && evt.url));
    ctx.events.on('tool_result', (evt) => {
      if (String((evt && evt.name) || '').startsWith('browser')) loadAttached();
    });
    ctx.events.onResync(() => {
      connectPane();
      loadAttached();
      loadBookmarks();
    });

    hooks.set(ctx, {
      shutDown,
      async refresh() {
        await connectPane();
        await loadAttached();
        await loadBookmarks();
      },
    });
    setWidth(widthPx, false);
    setHeight(heightPx, false);
    if (pane) {
      states.loading(pageEl, 'Asking the browser view for its state');
      regionKey = 'loading';
    }
    paintWatch();
    paint();
    if (pane && !hasB1) note('This window is running an older shell, so tabs, find, zoom, downloads, history and bookmarks are off. Quit and reopen the app to get them.', 'warn');
    if (hasB1) {
      if (typeof pane.screen === 'function') safe(pane.screen(true));
      offDl = pane.onDownloads(onDownloads);
      offCmd = pane.onCommand(onCommand);
      if (hasB2) {
        privacyUi = createPrivacy({ ctx, privacy: pane.privacy, note });
        privacySlot.replaceChildren(privacyUi.root);
        offPrivacy = pane.privacy.onUpdate((s) => {
          if (!disposed) privacyUi.update(s);
        });
        Promise.resolve(pane.privacy.state()).then((s) => { if (!disposed && s) privacyUi.update(s); }).catch((err) => paneFailed(err));
      }
      if (hasC2) {
        controlUi = createControl({ control: pane.control, note });
        controlSlot.replaceChildren(controlUi.root);
        offControl = pane.control.onUpdate((s) => {
          if (!disposed && controlUi) controlUi.update(s);
        });
        Promise.resolve(pane.control.state()).then((s) => { if (!disposed && controlUi && s) controlUi.update(s); }).catch((err) => paneFailed(err));
      }
      if (hasB3) manageUi = createManage({ manage: pane.manage, note, onChange: () => loadBookmarks() });
      Promise.resolve(pane.downloads()).then(onDownloads).catch((err) => paneFailed(err));
      paintBookmarks();
      loadBookmarks();
    }
    await connectPane();
    loadAttached();
    /* a page the server asked for while another screen was showing (NEWS, the agent's browser tools) */
    const waiting = takePaneRequest();
    if (waiting) openFromRequest(waiting);
  },

  async refresh(ctx, reason) {
    /* The pane and the agent report themselves; this only re-reads them. */
    const h = hooks.get(ctx);
    if (h) await h.refresh(reason);
  },

  unmount(ctx) {
    const h = hooks.get(ctx);
    if (h) h.shutDown();
  },
};
