/* The wallpaper picker: four built-in gradients, one photo from disk, dim and
   motion. A photo is read with FileReader and never uploaded: the bytes stay in
   this browser. A photo too large for localStorage still applies for this
   session; the picker then says plainly that it will not be remembered. */

import { html, setHtml } from '../kit/html.js';
import { WALLPAPERS } from '../core/prefs.js';

const LOOK = {
  aurora: ['Aurora', 'linear-gradient(135deg,#0d3a2c,#0a2a22)', "Aurora. The product's own canvas colour pushed to a horizon."],
  slate: ['Slate', 'linear-gradient(135deg,#1a2233,#0b0e14)', 'Slate. Cooler, lower contrast, for long sessions.'],
  ember: ['Ember', 'linear-gradient(135deg,#3a1f0c,#150b08)', 'Ember. Warm, higher contrast.'],
  mono: ['Mono', 'linear-gradient(135deg,#1a1a1c,#0a0a0b)', 'Mono. Neutral, no hue, maximum text contrast.'],
};

export function createWallpaperPicker({ root, prefs, toasts, reducedMotion = false }) {
  setHtml(root, html`
    <div class="wp-head">Wallpaper</div>
    <div class="wp-grid">${WALLPAPERS.map((w) => html`<button type="button" class="wp" data-wall="${w}" aria-pressed="false" aria-label="${LOOK[w][0]}" style="background:${LOOK[w][1]}" data-spec="${LOOK[w][2]}"></button>`)}</div>
    <label class="os-btn" style="width:100%;justify-content:center;margin-top:8px" data-spec="Pick any image from disk. It is read locally with FileReader and never uploaded: the bytes stay in this browser. Stored as a data URL against your profile.">
      UPLOAD PHOTO<input type="file" id="wallFile" accept="image/*" hidden>
    </label>
    <div class="wp-row"><span>Dim</span><input type="range" id="wallDim" min="0" max="80" aria-label="Wallpaper dim" data-spec="Darkens the photo behind the UI. A real photo has arbitrary brightness, and body text needs 4.5:1 over whatever is behind it, so this is a readability control rather than a taste one."></div>
    <div class="wp-row"><span>Motion</span><button type="button" class="os-switch" id="wallAnim" role="switch" aria-checked="true" aria-label="Wallpaper motion" data-spec="The slow 46s parallax drift. Off automatically when the OS asks for reduced motion."></button></div>
    <div class="wp-msg" id="wallMsg" role="status"></div>`);

  const grid = root.querySelector('.wp-grid');
  const file = root.querySelector('#wallFile');
  const dim = root.querySelector('#wallDim');
  const anim = root.querySelector('#wallAnim');
  const msg = root.querySelector('#wallMsg');

  function sync() {
    const photo = prefs.wallPhoto() && prefs.read(prefs.keys.wall) === 'photo';
    grid.querySelectorAll('.wp').forEach((b) => b.setAttribute('aria-pressed', String(!photo && b.dataset.wall === prefs.wall())));
    dim.value = String(prefs.dim());
    const motionOn = prefs.motion() && !reducedMotion;
    anim.setAttribute('aria-checked', String(motionOn));
    anim.disabled = reducedMotion;
  }

  async function saved(name, value) {
    const r = await prefs.save(name, value);
    if (!r.ok) msg.textContent = 'Saved in this browser only. The server said: ' + r.error;
    else msg.textContent = '';
  }

  grid.addEventListener('click', (e) => {
    const b = e.target.closest('.wp');
    if (!b) return;
    prefs.remove(prefs.keys.wallPhoto);
    prefs.applyWall(b.dataset.wall);
    sync();
    saved('wall', b.dataset.wall);
  });
  dim.addEventListener('input', () => prefs.applyDim(dim.value));
  dim.addEventListener('change', () => saved('dim', dim.value));
  anim.addEventListener('click', () => {
    const on = anim.getAttribute('aria-checked') === 'true';
    prefs.applyMotion(!on);
    anim.setAttribute('aria-checked', String(!on));
    saved('motion', !on);
  });
  file.addEventListener('change', () => {
    const f = file.files && file.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onerror = () => {
      msg.textContent = 'Could not read that file: ' + ((reader.error && reader.error.message) || 'unknown error');
    };
    reader.onload = () => {
      const url = String(reader.result || '');
      if (!url.startsWith('data:image/')) {
        msg.textContent = 'That file is not an image the browser can show.';
        return;
      }
      const stored = prefs.write(prefs.keys.wallPhoto, url) && prefs.write(prefs.keys.wall, 'photo');
      if (!stored) prefs.remove(prefs.keys.wallPhoto);
      prefs.applyWall('photo', url);
      sync();
      msg.textContent = stored ? '' : 'Photo applied, but it is too large to remember across restarts.';
      if (!stored) toasts.show({ level: 'warn', title: 'Wallpaper photo', detail: 'Applied for this session only: too large for browser storage.' });
    };
    reader.readAsDataURL(f);
  });

  return { sync };
}
