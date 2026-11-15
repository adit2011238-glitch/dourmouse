/* The wallpaper rows of Settings > Appearance (plan 1.2, finding 197). Pure markup from a summary,
   so node can test it. The controls: Off, Photo or Rotating; a photo for light and one for dark; a
   rotation of up to 12 photos (one a day); and the places the photo shows behind. */

import { html } from '../../kit/html.js';

export const MODE_LABELS = [
  { id: 'off', name: 'Off' },
  { id: 'single', name: 'Photo' },
  { id: 'rotate', name: 'Rotating' },
];

export const PLACE_LABELS = {
  HOME: 'Home', PROJECTS: 'Projects', OFFICE: 'Office', RESEARCH: 'Research',
  LIBRARY: 'Library', BROWSER: 'Browser', SECURITY: 'Security', SETTINGS: 'Settings',
};

function photoRow(slot, label, has, thumb) {
  return html`<div class="kv wp-row"><span>${label}</span><span class="wp-ctl">
    ${thumb ? html`<img class="wp-thumb" alt="" src="${thumb}">` : ''}
    <button type="button" class="os-btn" data-wp-pick="${slot}" data-spec="Choose a photo from your files for the ${slot} appearance. It is copied into Dourmouse, shrunk, and its location data is dropped.">${has ? 'Change' : 'Choose'}</button>
    ${has ? html`<button type="button" class="os-btn" data-wp-remove="${slot}" data-spec="Delete the ${slot} photo that Dourmouse keeps.">Remove</button>` : ''}</span></div>`;
}

export function wallpaperRows(sum, thumbs = {}) {
  if (!sum || !sum.config) return html`<div class="kv"><span>Wallpaper</span><span class="muted">Not available</span></div>`;
  const mode = sum.config.mode;
  const places = Array.isArray(sum.config.places) ? sum.config.places : [];
  const list = Array.isArray(sum.places) ? sum.places : Object.keys(PLACE_LABELS);
  return html`<div class="kv"><span>Wallpaper</span><span class="os-seg" role="group" aria-label="Wallpaper">${MODE_LABELS.map((m) => html`<button type="button" aria-selected="${String(m.id === mode)}" data-wp-mode="${m.id}" data-spec="${m.id === 'off' ? 'No photo behind the window.' : m.id === 'single' ? 'One photo for light and one for dark.' : 'A different photo each day, from a set you add.'}">${m.name}</button>`)}</span></div>
    ${mode === 'single' ? html`${photoRow('light', 'Light', sum.light, thumbs.light)}${photoRow('dark', 'Dark', sum.dark, thumbs.dark)}` : ''}
    ${mode === 'rotate' ? html`<div class="kv wp-row"><span>Photos <span class="muted">${sum.rotation} of ${sum.rotation_max}</span></span><span class="wp-ctl">
      <button type="button" class="os-btn" data-wp-pick="rotation" ${sum.rotation >= sum.rotation_max ? 'disabled' : ''} data-spec="Add photos from your files. One shows each day.">Add</button>
      ${sum.rotation ? html`<button type="button" class="os-btn" data-wp-remove="rotation" data-spec="Delete every photo in the rotation.">Remove all</button>` : ''}</span></div>` : ''}
    ${mode === 'off' ? '' : html`<div class="wp-places" role="group" aria-label="Show behind">${list.map((id) => html`<label class="wp-place"><input type="checkbox" data-wp-place="${id}" ${places.includes(id) ? 'checked' : ''}> ${PLACE_LABELS[id] || id}</label>`)}</div>`}`;
}
