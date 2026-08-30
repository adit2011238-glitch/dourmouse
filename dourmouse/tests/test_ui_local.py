# v4.0 UI guarantees — "fully free and local" is a tested invariant, not a claim.
# --------------------------------------------------------------------------- #
# Every page served by Dourmouse must be dependency-free: no CDNs, no remote fonts,
# no build step. The HUD features (particles, radar sweep, motion states, backend
# indicator) are also pinned here so a future refactor cannot silently drop them.

from pathlib import Path
import re


class TestFullyLocal:
    """The zero-network guarantee: pages render with no external fetch at all."""

    UI_DIR = Path(__file__).resolve().parents[2] / "ui"

    # The only legitimate matches: the SVG XML namespace and the markdown-link
    # renderer, which emits hrefs for *user-supplied* links, never ships one.
    _ALLOWED = (
        "http://www.w3.org/2000/svg",
        "http://127.0.0.1:",  # the app's own local ATLAS terminal (loopback)
        r"https?:"  # the link renderer regex is intentionally protocol-generic
    )

    def _pages(self) -> list[Path]:
        # Skip AppleDouble metadata junk (._index.html) that macOS can drop
        # next to real files on external/FAT volumes (audit fix v5.22.14).
        return sorted(
            p for p in self.UI_DIR.glob("*.html") if not p.name.startswith("._")
        )

    def _read_utf8(self, path: Path) -> str:
        """HTML is UTF-8 (declared in <meta charset>); the locale default
        (cp1252 on Windows) chokes on non-ASCII — read explicitly."""
        return path.read_text(encoding="utf-8")

    def test_pages_exist(self):
        names = {p.name for p in self._pages()}
        assert {"index.html", "map.html", "agent.html", "login.html"} <= names

    def test_no_external_resources_any_page(self):
        for page in self._pages():
            html = self._read_utf8(page)
            # Strip EVERY <script>...</script> block, not just the text after
            # the LAST <script> tag — a page can have several inline blocks
            # (index.html has 3, e.g.), and the old `html.split("<script>")[-1]`
            # only ever excluded the final one, silently leaving every
            # EARLIER block's own runtime code (a deliberate fetch() to a
            # named localhost URL, say) scanned as if it were static markup.
            # A URL a script constructs/calls at runtime is not a resource
            # reference the PAGE ships (an <img src>/<link href> is); this
            # check exists for the latter, so all real code must be excluded
            # from it, not just whichever script block happens to be last.
            stripped = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S)
            external = [
                m.group(0)
                for m in re.finditer(r"https?://[^\s\"')\]]+", stripped)
                if not any(a in m.group(0) for a in self._ALLOWED)
            ]
            assert not external, f"{page.name} references external resources: {external}"

    def test_no_cdn_script_tags(self):
        for page in self._pages():
            html = self._read_utf8(page)
            assert "cdn." not in html and "unpkg" not in html and "jsdelivr" not in html
            assert "fonts.googleapis" not in html and "googleapis" not in html

    def test_no_build_step_contract(self):
        """No package.json, node_modules, or bundler config anywhere in ui/."""
        assert not (self.UI_DIR / "package.json").exists()
        assert not (self.UI_DIR / "node_modules").exists()


class TestPremiumHud:
    """The v4.0 visual language — pinned so it cannot silently regress."""

    def _read(self, rel: str) -> str:
        return (Path(__file__).resolve().parents[2] / rel).read_text(
            encoding="utf-8"
        )

    def test_particle_field(self):
        html = self._read("ui/index.html")
        assert '<canvas id="particles"' in html
        assert "getElementById('particles')" in html

    def test_radar_sweep_and_sectors(self):
        html = self._read("ui/index.html")
        assert 'class="sweep"' in html
        assert 'class="sweepdot"' in html
        assert "SECTOR" in html or "sector s1" in html
        assert "@keyframes sweep" in html

    def test_motion_state_machine(self):
        html = self._read("ui/index.html")
        assert "function setCoreState" in html
        for state in ("idle", "thinking", "planning", "executing", "complete"):
            assert f"'{state}'" in html

    def test_backend_indicator_bound_to_real_api(self):
        html = self._read("ui/index.html")
        assert "backendline" in html
        assert "/api/backend" in html

    def test_phone_layout_present(self):
        for page in ("index.html", "map.html", "agent.html"):
            html = self._read(f"ui/{page}")
            assert "@media" in html, f"{page} has no responsive media queries"

    def test_login_page_served(self):
        html = self._read("ui/login.html")
        assert "DOURMOUSE" in html
        assert "token" in html.lower()

    def test_terminal_lines_driven_by_state(self):
        html = self._read("ui/index.html")
        # the state machine must reach the SSE handlers, not exist in a vacuum
        assert "setCoreState('executing')" in html
        assert "setCoreState('complete')" in html


class TestTaskDeckDenyPath:
    """The advanced hand-gesture engine this class used to cover (v5.29,
    ui/index.html's VISION screen) was removed in vision-hand-tracking-v2's
    consolidation pass — ui/workspace.html is now the only hand-tracking
    surface (see its own tests, dourmouse/tests/test_workspace_hand_gestures.py).
    What's kept here is the one piece of real, still-live coverage that
    happened to live in this class: the TASK DECK's own deny path, which
    has nothing to do with hand gestures and is not covered anywhere else."""

    def _read(self) -> str:
        return (Path(__file__).resolve().parents[2] / "ui/index.html").read_text(
            encoding="utf-8"
        )

    def test_deny_reaches_the_deck(self):
        html = self._read()
        assert "denyConfirm()" in html
        assert "approved: false" in html


class TestComputeNodeCard:
    """v5.31 — the home-dashboard COMPUTE NODE card (the Dell LAN node),
    pinned so a refactor cannot drop the panel, the honest online/offline
    states, or the 10s live-latency auto-refresh."""

    def _read(self) -> str:
        return (Path(__file__).resolve().parents[2] / "ui/index.html").read_text(
            encoding="utf-8"
        )

    def test_panel_markup_present(self):
        html = self._read()
        assert 'id="srvpanel"' in html
        assert 'id="srvbody"' in html
        assert 'id="srvCheckBtn"' in html
        assert 'id="srvbadge"' in html

    def test_bound_to_real_api_endpoint(self):
        html = self._read()
        assert "fetch('/api/server')" in html
        assert "renderServer(await r.json())" in html

    def test_auto_refresh_every_10s(self):
        html = self._read()
        assert "setInterval(pollServer, 10000)" in html

    def test_honest_online_state_shows_node_model_latency(self):
        html = self._read()
        assert "d.node" in html and "d.model" in html
        assert "d.latency_ms" in html and "LATENCY" in html
        assert "● ONLINE" in html

    def test_honest_offline_state_keeps_local_ai(self):
        html = self._read()
        assert "○ OFFLINE" in html
        assert "LOCAL AI IN CHARGE" in html


class TestAtlasMotion:
    """v5.31 — the Skiper-inspired motion integrated into the ATLAS view
    (perspective tape, count-up rolls, hover/tap expand, progressive blur,
    coverflow pair chips), pinned so a refactor cannot drop them."""

    def _read(self) -> str:
        return (Path(__file__).resolve().parents[2] / "ui/index.html").read_text(
            encoding="utf-8"
        )

    def test_perspective_telemetry_tape(self):
        html = self._read()
        assert 'class="atlas-ticker"' in html
        assert 'id="atlasTape"' in html
        assert "atlasTapeX" in html  # the marquee keyframes
        assert "tapeEl.innerHTML = copy + copy" in html  # seamless loop

    def test_count_up_rolls_on_real_values(self):
        html = self._read()
        assert "function rollNums" in html
        assert 'class="roll" data-n=' in html
        assert "rollNums(statusEl)" in html
        assert "rollNums(bootEl)" in html

    def test_hover_tap_expand_report(self):
        html = self._read()
        assert 'class="d exclamp"' in html  # full text clamped, not truncated
        assert ".dm-card:hover .exclamp" in html
        assert ".dm-card.expanded .exclamp" in html
        assert "atlasLatestCard" in html
        assert "classList.toggle('expanded')" in html

    def test_progressive_blur_on_run_output(self):
        html = self._read()
        assert 'class="run-scroll"' in html
        assert "mask-image: linear-gradient(to bottom, #000 58%, transparent 98%)" in html

    def test_coverflow_pair_chips(self):
        html = self._read()
        assert 'class="pairchips"' in html
        assert 'class="pairchip"' in html
        assert "rotateY(-16deg)" in html
        assert "pair_days" in html  # driven by real bootstrap data
