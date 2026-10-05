// Media control for the browser agent (phase C2): play, pause, seek, mute and volume of the page's
// main <video> or <audio>, and what it is playing. A YouTube watch page is driven through its
// HTML5 video element (video.html5-main-video), the same element YouTube's own buttons drive, so
// no YouTube API and no key is involved.
//
// Injected by dourmouse/browser_agent.py into the same ISOLATED WORLD as element_ids.js: it shares
// the DOM with the page but no JavaScript, so a page script cannot see or call it. It never writes
// to the DOM; it only calls the media element's own methods and setters.
(() => {
  if (globalThis.__dmMedia) return true;

  const round = (n, d) => (Number.isFinite(n) ? Math.round(n * 10 ** d) / 10 ** d : null);

  // The element to act on: YouTube's main video first, else the playing one, else the largest
  // visible video, else the first audio.
  function pick() {
    const main = document.querySelector("video.html5-main-video");
    if (main) return { el: main, count: document.querySelectorAll("video, audio").length };
    const all = Array.from(document.querySelectorAll("video, audio"));
    let best = null;
    let score = -1;
    for (const m of all) {
      const r = m.getBoundingClientRect();
      const area = m.tagName === "VIDEO" ? Math.max(0, r.width) * Math.max(0, r.height) : 0;
      const s = (m.paused ? 0 : 1e12) + area + (m.tagName === "AUDIO" ? 1 : 0);
      if (s > score) { best = m; score = s; }
    }
    return { el: best, count: all.length };
  }

  function title() {
    const h = document.querySelector("h1.ytd-watch-metadata yt-formatted-string, #title h1 yt-formatted-string, h1.title");
    const t = ((h && h.textContent) || document.title || "").trim();
    return t.replace(/^\(\d+\)\s*/, "").replace(/\s+-\s+YouTube$/, "").replace(/\s+/g, " ").slice(0, 200);
  }

  // YouTube keeps its own mute state and re-applies it to the element (seen live: an element-level
  // mute was undone by the player within half a second). Its own mute button is what sticks.
  function ytMuteButton(m) {
    const player = m.closest(".html5-video-player");
    const box = player ? player.querySelector(".ytp-mute-button") : null;
    if (!box) return null;
    // Seen live (2026-10): a div.ytp-mute-button wrapping the real <button> that carries the label.
    return box.tagName === "BUTTON" ? box : box.querySelector("button") || box;
  }
  function ytSaysMuted(btn) {
    const label = String(btn.getAttribute("data-title-no-tooltip") || btn.getAttribute("aria-label") || btn.getAttribute("title") || "").toLowerCase();
    if (label.startsWith("unmute")) return true;
    if (label.startsWith("mute")) return false;
    return null;
  }

  function status(m, count) {
    const player = m.closest(".html5-video-player");
    const btn = ytMuteButton(m);
    const yt = btn ? ytSaysMuted(btn) : null;
    return {
      ytMuted: yt,
      found: true, kind: m.tagName.toLowerCase(), count, title: title(), url: location.href,
      currentTime: round(m.currentTime, 2), duration: Number.isFinite(m.duration) ? round(m.duration, 2) : null,
      paused: m.paused, ended: m.ended, muted: m.muted, volume: round(m.volume, 2), rate: m.playbackRate,
      readyState: m.readyState, youtube: Boolean(player), ad: Boolean(player && player.classList.contains("ad-showing")),
    };
  }

  const settle = (ms) => new Promise((r) => setTimeout(r, ms));

  function waitFor(m, events, ms) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (why) => {
        if (done) return;
        done = true;
        for (const e of events) m.removeEventListener(e, on);
        resolve(why);
      };
      const on = (e) => finish(e.type);
      for (const e of events) m.addEventListener(e, on, { once: true });
      setTimeout(() => finish("timeout"), ms);
    });
  }

  async function run(op, p) {
    p = p || {};
    const { el: m, count } = pick();
    if (!m) return { found: false, count: 0, title: title(), url: location.href };
    if (op === "status") return status(m, count);
    if (op === "play") {
      let error = "";
      try {
        await m.play();
      } catch (e) {
        error = String((e && e.name) || e);
      }
      await settle(250);
      return { ...status(m, count), error };
    }
    if (op === "pause") {
      m.pause();
      await settle(100);
      return status(m, count);
    }
    if (op === "seek") {
      let to = p.by !== undefined && p.by !== null ? m.currentTime + Number(p.by) : Number(p.to);
      if (!Number.isFinite(to)) return { ...status(m, count), error: "no time to seek to" };
      if (Number.isFinite(m.duration)) to = Math.min(to, Math.max(0, m.duration - 0.05));
      to = Math.max(0, to);
      const seeked = waitFor(m, ["seeked"], 4000);
      m.currentTime = to;
      const how = await seeked;
      // Report where the media really is, not where it was asked to go: a stream the server does
      // not let the browser seek in stays where it was.
      const landed = Math.abs(m.currentTime - to) <= 0.5;
      let error = "";
      if (!landed) error = "the player did not move there (the page or its server may not allow seeking)";
      else if (how === "timeout") error = "the seek did not finish within 4 s";
      return { ...status(m, count), seekedTo: round(to, 2), landed, error };
    }
    if (op === "mute" || op === "unmute") {
      const want = op === "mute";
      const btn = ytMuteButton(m);
      if (btn) {
        const yt = ytSaysMuted(btn);
        if ((yt === null ? m.muted : yt) !== want) btn.click();
        await settle(150);
      }
      if (m.muted !== want) m.muted = want;
      await settle(600); // a player may change it back: report what holds, not what was asked
      const held = m.muted === want;
      return { ...status(m, count), held, error: held ? "" : "the page's player changed it back" };
    }
    if (op === "volume") {
      const level = Number(p.level);
      if (!Number.isFinite(level)) return { ...status(m, count), error: "no volume level" };
      const want = Math.max(0, Math.min(1, level / 100));
      m.volume = want;
      await settle(600);
      const held = Math.abs(m.volume - want) < 0.01;
      return { ...status(m, count), held, error: held ? "" : "the page's player changed the volume back" };
    }
    throw new Error("unknown media op " + op);
  }

  Object.defineProperty(globalThis, "__dmMedia", { value: { run }, enumerable: false, configurable: false, writable: false });
  return true;
})()
