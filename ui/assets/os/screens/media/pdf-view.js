/* The PDF reader inside MEDIA.

   It draws one page at a time as an image from the server's PDFium route (the
   page URL comes from POST /api/os/media/open, never from typed text) and lays
   a highlight layer over it. This is NOT Chromium's built-in PDF viewer: that
   viewer cannot be framed under the app's Content-Security-Policy
   (object-src 'none', no frame-src), and it has no API for annotations. So
   the page has no text layer: text cannot be selected, and a highlight is a
   rectangle dragged over the page, stored as fractions of the page so it
   stays put at any zoom. Highlights are saved through
   /api/os/media/highlights and come back with the file. */

import { states } from '../../kit/states.js';
import { isAbort } from '../../core/api.js';
import { HL_COLORS, rectFromDrag, pageClamp, highlightsOnPage } from './helpers.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function btn(label, title, aria) {
  const b = el('button', 'os-btn', label);
  b.type = 'button';
  b.dataset.spec = title;
  if (aria) b.setAttribute('aria-label', aria);
  return b;
}

/* host: { stage, bar, note(text), scroller? (the element that scrolls the screen) } */
export function createPdfReader(ctx, host) {
  const st = { row: null, page: 0, count: 0, hl: [], mode: false, color: 'yellow', seq: 0, drag: null };

  /* ---- toolbar ---- */
  const prev = btn('◀', 'Previous page.', 'Previous page');
  const next = btn('▶', 'Next page.', 'Next page');
  const pageIn = el('input', 'os-field med-pageno');
  pageIn.type = 'number';
  pageIn.min = '1';
  pageIn.step = '1';
  pageIn.setAttribute('aria-label', 'Page number');
  pageIn.dataset.spec = 'Type a page number and press Enter to jump to it.';
  const ofEl = el('span', 'muted', '');
  const mark = btn('HIGHLIGHT', 'Turns highlight mode on or off. While it is on, drag across the page to mark a rectangle; it is saved with this file. Text cannot be selected: the page is an image.');
  mark.setAttribute('aria-pressed', 'false');
  const swatches = HL_COLORS.map((c) => {
    const s = el('button', 'med-swatch med-hl--' + c);
    s.type = 'button';
    s.setAttribute('aria-label', 'Highlight colour ' + c);
    s.dataset.spec = 'Sets the colour of the next highlight to ' + c + '.';
    s.addEventListener('click', () => {
      st.color = c;
      paintSwatches();
    });
    return s;
  });
  const noteIn = el('input', 'os-field med-hlnote');
  noteIn.type = 'text';
  noteIn.maxLength = 500;
  noteIn.placeholder = 'note for the next highlight (optional)';
  noteIn.setAttribute('aria-label', 'Note for the next highlight');
  noteIn.dataset.spec = 'Optional text saved with the next highlight you draw.';
  host.bar.replaceChildren(prev, pageIn, ofEl, next, mark, ...swatches, noteIn);

  function paintSwatches() {
    swatches.forEach((s, i) => s.setAttribute('aria-pressed', String(HL_COLORS[i] === st.color)));
  }
  paintSwatches();

  /* ---- stage ---- */
  const scroll = el('div', 'med-pdf');
  const frame = el('div', 'med-page');
  const img = el('img', 'med-pageimg');
  img.draggable = false;
  img.alt = '';
  const layer = el('div', 'med-hl-layer');
  frame.append(img, layer);
  const listEl = el('div', 'med-hllist');
  listEl.dataset.region = '';
  scroll.append(frame, listEl);

  function paintBar() {
    prev.disabled = st.page <= 0;
    next.disabled = st.page >= st.count - 1;
    pageIn.max = String(st.count || 1);
    pageIn.value = String(st.page + 1);
    ofEl.textContent = st.count ? 'of ' + st.count : '';
    mark.setAttribute('aria-pressed', String(st.mode));
    mark.classList.toggle('on', st.mode);
    layer.classList.toggle('drawing', st.mode);
  }

  function paintLayer() {
    layer.replaceChildren();
    highlightsOnPage(st.hl, st.page).forEach((h) => {
      const d = el('div', 'med-hl med-hl--' + h.color);
      d.style.left = h.rect.x * 100 + '%';
      d.style.top = h.rect.y * 100 + '%';
      d.style.width = h.rect.w * 100 + '%';
      d.style.height = h.rect.h * 100 + '%';
      if (h.note) d.title = h.note;
      layer.append(d);
    });
  }

  function paintList() {
    if (!st.hl.length) {
      states.empty(listEl, 'No highlights in this file yet.', { hint: 'Turn on HIGHLIGHT, then drag across a page.' });
      return;
    }
    const wrap = el('div');
    st.hl
      .slice()
      .sort((a, b) => a.page - b.page || a.rect.y - b.rect.y)
      .forEach((h) => {
        const row = el('div', 'os-row med-hlrow');
        const sw = el('span', 'med-swatch med-hl--' + h.color);
        const go = el('button', 'os-btn', 'PAGE ' + (h.page + 1));
        go.type = 'button';
        go.dataset.spec = 'Goes to page ' + (h.page + 1) + ' where this highlight is.';
        go.addEventListener('click', () => goTo(h.page));
        const del = el('button', 'os-btn', 'REMOVE');
        del.type = 'button';
        del.dataset.spec = 'Deletes this highlight from the saved list. The PDF file itself is never changed.';
        del.addEventListener('click', () => remove(h.id));
        row.append(sw, go, el('span', 'rt', h.note || 'no note'), del);
        wrap.append(row);
      });
    states.populated(listEl, wrap);
  }

  /* ---- page image ---- */
  function showPage() {
    const my = ++st.seq;
    paintBar();
    paintLayer();
    frame.dataset.state = 'loading';
    img.onload = () => {
      if (my === st.seq) frame.dataset.state = 'populated';
    };
    img.onerror = () => {
      if (my !== st.seq) return;
      frame.dataset.state = 'error';
      host.note('Page ' + (st.page + 1) + ' could not be rendered by the server.');
    };
    img.alt = st.row.name + ', page ' + (st.page + 1);
    img.src = st.row.urls.pdf_page + st.page;
  }

  function goTo(page) {
    if (!st.row || !st.count) return;
    st.page = pageClamp(page, st.count);
    showPage();
  }

  /* ---- highlights ---- */
  async function save(body) {
    const innerTop = scroll.scrollTop;
    const outer = host.scroller || null;
    const outerTop = outer ? outer.scrollTop : 0;
    try {
      const d = await ctx.api.post('/api/os/media/highlights', { path: st.row.path, ...body });
      if (ctx.signal.aborted || !st.row) return;
      st.hl = d.highlights || [];
      paintLayer();
      paintList();
      /* repainting the list changes the height of the scrolled area; keep the
         reader where it was so the page does not jump after every highlight */
      scroll.scrollTop = innerTop;
      if (outer) outer.scrollTop = outerTop;
    } catch (err) {
      if (!isAbort(err)) host.note('Could not save the highlight: ' + err.message);
    }
  }

  function remove(id) {
    return save({ op: 'remove', id });
  }

  let draft = null;
  layer.addEventListener('pointerdown', (e) => {
    if (!st.mode || e.button !== 0) return;
    e.preventDefault();
    layer.setPointerCapture(e.pointerId);
    st.drag = { x: e.clientX, y: e.clientY };
    draft = el('div', 'med-hl med-hl--' + st.color + ' draft');
    layer.append(draft);
  });
  layer.addEventListener('pointermove', (e) => {
    if (!st.drag || !draft) return;
    const box = layer.getBoundingClientRect();
    const r = rectFromDrag(st.drag, { x: e.clientX, y: e.clientY }, box);
    if (!r) {
      draft.style.width = '0';
      return;
    }
    draft.style.left = r.x * 100 + '%';
    draft.style.top = r.y * 100 + '%';
    draft.style.width = r.w * 100 + '%';
    draft.style.height = r.h * 100 + '%';
  });
  const finish = (e, commit) => {
    if (!st.drag) return;
    const start = st.drag;
    st.drag = null;
    if (draft) draft.remove();
    draft = null;
    if (!commit) return;
    const r = rectFromDrag(start, { x: e.clientX, y: e.clientY }, layer.getBoundingClientRect());
    if (!r) return;
    save({ op: 'add', page: st.page, rect: r, color: st.color, note: noteIn.value.trim() });
    noteIn.value = '';
  };
  layer.addEventListener('pointerup', (e) => finish(e, true));
  layer.addEventListener('pointercancel', (e) => finish(e, false));

  /* ---- controls ---- */
  prev.addEventListener('click', () => goTo(st.page - 1));
  next.addEventListener('click', () => goTo(st.page + 1));
  pageIn.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    goTo(Number(pageIn.value) - 1);
  });
  mark.addEventListener('click', () => {
    st.mode = !st.mode;
    paintBar();
  });

  return {
    open(row) {
      st.row = row;
      st.page = 0;
      st.count = Number(row.page_count) > 0 ? Number(row.page_count) : 0;
      st.hl = Array.isArray(row.highlights) ? row.highlights : [];
      st.mode = false;
      if (!st.count) {
        host.stage.dataset.state = 'error';
        host.stage.replaceChildren(el('div', 'med-msg bad', row.pdf_error || 'PDFium could not count the pages of this file.'));
        return false;
      }
      host.stage.dataset.state = 'populated';
      host.stage.replaceChildren(scroll);
      paintList();
      showPage();
      return true;
    },
    close() {
      st.seq += 1;
      st.row = null;
      st.hl = [];
      st.drag = null;
      img.removeAttribute('src');
    },
    /* arrow keys while a PDF is open */
    step(delta) {
      goTo(st.page + delta);
    },
    state: () => ({ page: st.page, count: st.count, highlights: st.hl.length }),
  };
}
