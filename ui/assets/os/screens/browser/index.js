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
   screen). One pane, no tabs: there is no tab list behind it in main.js.

   Nothing here is a sample: the address, title, loading, back and forward all
   come from the pane's own state, or from the addresses opened in this window
   when the page is proxied. */

import { takePaneRequest } from '../../core/pane-inbox.js';
import { html, setHtml, raw } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { ago } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  PRESETS, normalizeAddress, eventTarget, lockInfo, hostOf, isWebUrl, paneModel, failLine, tabTitle,
  viewBounds, sameBounds, clampWidth, clampHeight, presetForWidth, parseStoredWidth, parseStoredHeight,
  watchLine, footnotes, makeHistory,
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

    /* ---------------- skeleton: built once, regions repaint ---------------- */
    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="bw-note" id="bwNote" role="status" hidden></div>
      <div class="cb-wrap" id="bwWrap">
        <div class="cb-frame">
          <div class="cb-tabs">
            <div class="cb-tab on" id="bwTab"><span class="fav">${ICON.globe}</span><span class="tt" id="bwTabTitle"></span></div>
            <span class="cb-one" id="bwOne"></span>
          </div>
          <div class="cb-bar" id="bwBar">
            <div class="cb-nav">
              <button type="button" class="cb-ico" id="bwBack" aria-label="Back" title="Back" data-spec="Back through the pane's real history. Outside Electron it goes back through the addresses opened in this window.">${ICON.back}</button>
              <button type="button" class="cb-ico" id="bwFwd" aria-label="Forward" title="Forward" data-spec="Forward through the pane's real history.">${ICON.fwd}</button>
              <button type="button" class="cb-ico" id="bwReload" aria-label="Reload" title="Reload" data-spec="Reloads the page. While a page is loading it stops the load instead.">${ICON.reload}</button>
            </div>
            <form class="cb-addr" id="bwAddrForm" data-lock="none" novalidate>
              <span id="bwLock" role="img" aria-label="No page loaded">${ICON.lock}</span>
              <input id="bwAddr" type="text" inputmode="url" autocomplete="off" spellcheck="false" aria-label="Address" placeholder="Type an address and press Enter" data-spec="The real current address of the page. Typing an address and pressing Enter opens it. Only http and https addresses open; file, javascript, data and the rest are refused. Alt+L focuses it.">
            </form>
            <span class="cb-size" id="bwSize" title="Size of the page area in pixels"></span>
            <div class="cb-vp" id="bwPresets" role="group" aria-label="Page area size" data-spec="Sets the width of the page area. Layout only: the page reflows because the view really is that wide, but there is no device emulation."></div>
          </div>
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
    const tabTitleEl = $('bwTabTitle');
    const oneEl = $('bwOne');
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
    oneEl.textContent = 'one shared pane';
    oneEl.dataset.spec = 'There is one pane, shared with the browser agent. Tabs are not built: they need a tab list in the Electron main process.';

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
      const want = surface === 'pane' && !paneProblem && Boolean(v.url) && !v.error && !ctx.overlays.open() && !dragging && Boolean(b);
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
      tabTitleEl.textContent = surface === 'pane' ? tabTitle({ title: v.title, url: v.url }) : (hostOf(v.url) || (v.url ? v.url : 'No page'));
      form.dataset.lock = lock.kind;
      lockEl.setAttribute('aria-label', lock.label);
      lockEl.title = lock.label;
      if (!dirty && addr.value !== v.url) addr.value = v.url;
      const usable = !(electron && !pane) && !paneProblem;
      backBtn.disabled = !usable || !v.back;
      fwdBtn.disabled = !usable || !v.fwd;
      reloadBtn.disabled = !usable || !v.url;
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
    ctx.keys.bind('Alt+l', () => {
      addr.focus();
      addr.select();
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
    }
    ctx.signal.addEventListener('abort', shutDown);

    /* ---------------- the pane and the agent ---------------- */
    function onPaneState(s) {
      if (disposed) return;
      model = paneModel(s);
      paneKnown = true;
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
    });

    hooks.set(ctx, {
      shutDown,
      async refresh() {
        await connectPane();
        await loadAttached();
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
