/* The inspector (T6 milestone 1, finding #194): the right-hand pane for details of the selected
   item, and the alerts list (which replaces the floating Notification Centre and the dock).

   Hidden by default; the toolbar button, View, Show Inspector and Option Command I toggle it.
   A view puts its details here with ctx.chrome.inspect({ title, content }) and clears them with
   ctx.chrome.inspect(null); leaving the view clears them too. With nothing selected the Details
   tab says so in one line, never a placeholder that looks like data. */

import { html, setHtml, SafeHtml } from '../kit/html.js';

export function createInspector({ root, onVisible }) {
  setHtml(root, html`
    <div class="in-head" data-drag>
      <div class="segs" role="tablist" aria-label="Inspector">
        <button type="button" class="seg" role="tab" id="inTabDetails" data-tab="details" aria-selected="true" aria-controls="inDetails">Details</button>
        <button type="button" class="seg" role="tab" id="inTabAlerts" data-tab="alerts" aria-selected="false" aria-controls="inAlerts">Alerts<span class="nb" id="inAlertsN" hidden></span></button>
      </div>
    </div>
    <section class="in-body" id="inDetails" role="tabpanel" aria-labelledby="inTabDetails">
      <h2 class="in-title" id="inTitle" hidden></h2>
      <div id="inContent"></div>
    </section>
    <section class="in-body" id="inAlerts" role="tabpanel" aria-labelledby="inTabAlerts" hidden></section>`);

  const details = root.querySelector('#inDetails');
  const alerts = root.querySelector('#inAlerts');
  const titleEl = root.querySelector('#inTitle');
  const content = root.querySelector('#inContent');
  const countEl = root.querySelector('#inAlertsN');
  let tab = 'details';
  let shown = false;
  const tabListeners = new Set();

  function paintEmpty() {
    titleEl.hidden = true;
    titleEl.textContent = '';
    content.dataset.state = 'empty';
    setHtml(content, html`<div class="st st-empty"><div class="st-m">Select something to see its details here.</div></div>`);
  }
  paintEmpty();

  function setTab(next) {
    const prev = tab;
    tab = next === 'alerts' ? 'alerts' : 'details';
    root.querySelectorAll('[data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
    details.hidden = tab !== 'details';
    alerts.hidden = tab !== 'alerts';
    if (prev !== tab || shown) tabListeners.forEach((fn) => fn(shown ? tab : null));
  }
  root.querySelector('.segs').addEventListener('click', (e) => {
    const b = e.target.closest('[data-tab]');
    if (b) setTab(b.dataset.tab);
  });

  function setShown(on, { quiet = false } = {}) {
    shown = Boolean(on);
    root.hidden = !shown;
    root.closest('#shell').dataset.inspector = shown ? 'shown' : 'hidden';
    if (quiet) return; /* the first paint: nobody has subscribed yet and the caller is still being built */
    tabListeners.forEach((fn) => fn(shown ? tab : null));
    if (onVisible) onVisible(shown);
  }
  setShown(false, { quiet: true });

  return {
    alertsRoot: alerts,
    shown: () => shown,
    tab: () => tab,
    show(which) {
      if (which) setTab(which);
      if (!shown) setShown(true);
    },
    hide() {
      if (shown) setShown(false);
    },
    toggle() {
      setShown(!shown);
    },
    /* opens on Alerts, or closes when Alerts is already in front */
    toggleAlerts() {
      if (shown && tab === 'alerts') setShown(false);
      else {
        setTab('alerts');
        if (!shown) setShown(true);
      }
    },
    /* called with 'details' | 'alerts' while visible, null while hidden */
    onTab(fn) {
      tabListeners.add(fn);
      return () => tabListeners.delete(fn);
    },
    setAlertCount(n) {
      countEl.hidden = !n;
      countEl.textContent = n > 99 ? '99+' : String(n || '');
    },
    /* what a view selected: { title, content: SafeHtml | Node | Node[] } or null */
    inspect(item) {
      if (!item) {
        paintEmpty();
        return;
      }
      titleEl.hidden = !item.title;
      titleEl.textContent = item.title || '';
      content.dataset.state = 'populated';
      if (item.content instanceof SafeHtml) setHtml(content, item.content);
      else if (Array.isArray(item.content)) content.replaceChildren(...item.content);
      else if (item.content) content.replaceChildren(item.content);
      else content.replaceChildren();
    },
  };
}
