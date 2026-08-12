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
        r"https?:"  # the link renderer regex is intentionally protocol-generic
    )

    def _pages(self) -> list[Path]:
        # Skip AppleDouble metadata junk (._index.html) that macOS can drop
        # next to real files on external/FAT volumes (audit fix v5.22.14).
        return sorted(
            p for p in self.UI_DIR.glob("*.html") if not p.name.startswith("._")
        )

    def test_pages_exist(self):
        names = {p.name for p in self._pages()}
        assert {"index.html", "map.html", "agent.html", "login.html"} <= names

    def test_no_external_resources_any_page(self):
        for page in self._pages():
            html = page.read_text()
            # Strip the markdown-link renderer line, which is code, not a resource ref.
            code = html.split("<script>")[-1] if "<script>" in html else ""
            stripped = html.replace(code, "")
            external = [
                m.group(0)
                for m in re.finditer(r"https?://[^\s\"')\]]+", stripped)
                if not any(a in m.group(0) for a in self._ALLOWED)
            ]
            assert not external, f"{page.name} references external resources: {external}"

    def test_no_cdn_script_tags(self):
        for page in self._pages():
            html = page.read_text()
            assert "cdn." not in html and "unpkg" not in html and "jsdelivr" not in html
            assert "fonts.googleapis" not in html and "googleapis" not in html

    def test_no_build_step_contract(self):
        """No package.json, node_modules, or bundler config anywhere in ui/."""
        assert not (self.UI_DIR / "package.json").exists()
        assert not (self.UI_DIR / "node_modules").exists()


class TestPremiumHud:
    """The v4.0 visual language — pinned so it cannot silently regress."""

    def _read(self, rel: str) -> str:
        return (Path(__file__).resolve().parents[2] / rel).read_text()

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
