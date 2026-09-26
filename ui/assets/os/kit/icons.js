/* One custom icon per screen. Stroke-based, 24x24, 1.5 weight, drawn on the
   same grid so they read as one family (os_mockup.html:454-473). */

import { html, raw } from './html.js';

export const ICON = {
  HOME: 'M4 17l6-5-6-5M12 19h8',
  COMMS: 'M3 6.5A1.5 1.5 0 014.5 5h15A1.5 1.5 0 0121 6.5v11a1.5 1.5 0 01-1.5 1.5h-15A1.5 1.5 0 013 17.5zM3.5 7l8.5 6 8.5-6',
  RESEARCH: 'M9 3v5.5L4.5 17A2 2 0 006.3 20h11.4a2 2 0 001.8-3L15 8.5V3M8 3h8M7.5 14h9',
  CODE: 'M8.5 8.5L5 12l3.5 3.5M15.5 8.5L19 12l-3.5 3.5M13.5 6l-3 12',
  MEDIA: 'M3 6a2 2 0 012-2h14a2 2 0 012 2v12a2 2 0 01-2 2H5a2 2 0 01-2-2zM10 9l5 3-5 3zM3 8h18',
  PROJECTS: 'M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2zM3 11h18',
  SETTINGS: 'M12 9a3 3 0 110 6 3 3 0 010-6M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M19.8 4.2l-2.1 2.1M6.3 17.7l-2.1 2.1',
  ATLAS: 'M12 2a10 10 0 100 20 10 10 0 000-20M2 12h20M12 2c3 3.6 3 14.4 0 20M5 6c2 1.2 12 1.2 14 0M5 18c2-1.2 12-1.2 14 0',
  BROWSER: 'M12 3a9 9 0 100 18 9 9 0 000-18M3 12h18M12 3c3 3.6 3 14.4 0 18M12 3c-3 3.6-3 14.4 0 18',
  NEWS: 'M4 5h13v14H5a1 1 0 01-1-1zM17 9h3v8a2 2 0 01-2 2M7 8.5h7M7 12h7M7 15.5h4',
  VOICE: 'M12 3a3 3 0 013 3v6a3 3 0 01-6 0V6a3 3 0 013-3M5.5 11.5a6.5 6.5 0 0013 0M12 18v3M9 21h6',
  ORCHESTRATION: 'M12 3.5a2 2 0 110 4 2 2 0 010-4M5 16.5a2 2 0 110 4 2 2 0 010-4M19 16.5a2 2 0 110 4 2 2 0 010-4M12 7.5v4M12 11.5l-6 5M12 11.5l6 5',
  GOALS: 'M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19M12 7a5 5 0 100 10 5 5 0 000-10M12 11a1 1 0 110 2 1 1 0 010-2',
  TIMETABLE: 'M4 6a2 2 0 012-2h12a2 2 0 012 2v13a1 1 0 01-1 1H5a1 1 0 01-1-1zM4 9h16M8 3v4M16 3v4M8 13h3M8 16.5h6',
  AGENTSMITH: 'M12 3l2.2 4.6 5 .7-3.6 3.5.9 5-4.5-2.4-4.5 2.4.9-5L4.8 8.3l5-.7zM12 14.5v6M9 20.5h6',
  WIKI: 'M4 5.5A2.5 2.5 0 016.5 3H20v16H6.5A2.5 2.5 0 004 21.5zM20 19v2H6.5M8 7.5h8M8 11h8M8 14.5h5',
  SECURITY: 'M12 2.8l7.5 3.1v6c0 4.4-3.1 7.6-7.5 9.3-4.4-1.7-7.5-4.9-7.5-9.3v-6zM9 12l2.2 2.2L15.5 10',
  OFFICE: 'M3 20.5h18M5 20.5V9l7-5 7 5v11.5M9.5 20.5V15h5v5.5M9 11h1.5M13.5 11H15',
  /* screens that stay in the classic console (owner decision) */
  VISION: 'M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12zM12 9a3 3 0 110 6 3 3 0 010-6',
  GLOBE: 'M12 2a10 10 0 100 20 10 10 0 000-20M2 12h20M12 2c3 3.6 3 14.4 0 20M5 6c2 1.2 12 1.2 14 0M5 18c2-1.2 12-1.2 14 0',
  DESIGN3D: 'M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12L4 7.5M12 12v9',
  /* chrome-only glyphs */
  BELL: 'M6 9a6 6 0 1112 0c0 5 2 6 2 6H4s2-1 2-6zM10 20a2 2 0 004 0',
  AGENTS: 'M12 5a3 3 0 110 6 3 3 0 010-6M5.5 20a6.5 6.5 0 0113 0',
  SHIELD: 'M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z',
  WIFI: 'M3 9c5-5 13-5 18 0M6 12.5c3.5-3.5 8.5-3.5 12 0M9 16c1.7-1.7 4.3-1.7 6 0M12 19h.01',
  CHECK: 'M20 6L9 17l-5-5',
  CLOCK: 'M12 8v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18',
  CHAT: 'M4 4h16v12H5.2L4 17.5z',
  X: 'M6 6l12 12M18 6L6 18',
};

/* An inline SVG from a path table entry (or a raw path string). The path
   table is trusted static data, so it goes in with raw(). */
export function icon(name, cls = '', { stroke = 'currentColor', width = 1.5 } = {}) {
  const d = ICON[name] || name;
  return html`<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="${stroke}" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${raw('<path d="' + String(d).replace(/"/g, '') + '"/>')}</svg>`;
}
