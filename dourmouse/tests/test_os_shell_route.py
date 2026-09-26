"""Finding #144: /shell serves the OS shell page, its assets and its headers.

Route, CSP, injection rules and the static import graph. The page must carry
no inline script (the CSP is script-src 'self'), and everything it names must
exist and be served with a module-safe content type.
"""

from __future__ import annotations

import http.client
import re
import threading
from pathlib import Path

import pytest

from dourmouse.general_roster import build_general_registry

_ROOT = Path(__file__).resolve().parents[2]
_UI = _ROOT / "ui"
_OS = _UI / "assets" / "os"


@pytest.fixture
def server(monkeypatch, tmp_path):
    # function scoped on purpose: the suite's autouse fixtures (conftest.py) turn
    # off the background loops (sentry, netwatch, analyst, librarian, downloads
    # watch) per test; a module-scoped server would start them for real and
    # leave them running for the rest of the run.
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def get(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers, body


@pytest.fixture
def page(server):
    status, headers, body = get(server, "/shell")
    assert status == 200
    return headers, body.decode("utf-8")


class TestShellPage:
    def test_shell_is_served_as_html_on_both_names(self, server):
        for path in ("/shell", "/shell.html"):
            status, headers, body = get(server, path)
            assert status == 200 and headers["content-type"].startswith("text/html")
            assert b"<title>DOURMOUSE</title>" in body

    def test_csp_allows_only_this_origins_scripts_and_blocks_framing(self, page):
        headers, _ = page
        csp = headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
        assert "object-src 'none'" in csp and "base-uri 'none'" in csp and "frame-ancestors 'self'" in csp
        assert "connect-src 'self'" in csp
        assert headers["x-content-type-options"] == "nosniff"

    def test_the_page_has_no_inline_script_no_handlers_no_importmap(self, page):
        _, html = page
        scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I)
        assert scripts, "the shell needs its one module script"
        for attrs, body in scripts:
            assert "src=" in attrs and not body.strip(), f"inline script: {attrs} {body[:60]}"
        assert 'type="module"' in scripts[0][0]
        assert not re.search(r"<script[^>]*type=.importmap", html, re.I)
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), "an inline event handler would be blocked by the CSP"
        assert "javascript:" not in html.lower()

    def test_the_service_worker_path_rides_a_data_attribute(self, page):
        _, html = page
        assert 'data-sw="/sw.js"' in html and 'src="/assets/os/boot.js"' in html

    def test_pwa_head_tags_the_swap_will_need_are_already_here(self, page):
        _, html = page
        for needle in ('rel="manifest"', 'rel="apple-touch-icon"', "apple-mobile-web-app-capable"):
            assert needle in html

    def test_no_spotify_widget_and_no_startup_check_are_injected(self, page):
        _, html = page
        assert "dmSpotify" not in html and "spotify" not in html.lower()
        assert "startup_check.js" not in html

    def test_every_stylesheet_and_preload_the_page_names_is_served(self, server, page):
        _, html = page
        urls = re.findall(r'(?:href|src)="(/assets/[^"]+)"', html)
        assert len(urls) >= 15
        for url in urls:
            status, headers, body = get(server, url)
            assert status == 200, url
            if url.endswith(".js"):
                assert headers["content-type"].startswith("application/javascript"), url
            elif url.endswith(".css"):
                assert headers["content-type"].startswith("text/css"), url

    def test_the_dom_ids_the_boot_script_looks_up_exist_in_the_page(self, page):
        _, html = page
        boot = (_OS / "boot.js").read_text(encoding="utf-8")
        ids = set(re.findall(r"\$\('([A-Za-z]+)'\)", boot))
        # ids created by the chrome itself, not by the page skeleton
        made_by_chrome = {"ccBtn", "wallBtn"}
        assert ids, "boot.js looks nothing up?"
        for i in ids - made_by_chrome:
            assert f'id="{i}"' in html, f"boot.js needs #{i} but shell.html has none"


class TestStaticImportGraph:
    def _imports(self, path: Path):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"""(?:from|import)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""", text):
            yield m.group(1)

    def test_every_static_import_resolves_to_a_file_under_the_shell(self):
        checked = 0
        for path in sorted(_OS.rglob("*.js")):
            for spec in self._imports(path):
                target = (path.parent / spec).resolve()
                if path.name == "registry.js" and "/screens/" in spec:
                    continue  # lazy screen loaders point at folders that may not exist yet, by design
                assert target.is_file(), f"{path.relative_to(_ROOT)} imports {spec}"
                assert _OS in target.parents
                checked += 1
        assert checked > 60

    def test_every_built_screen_named_by_the_registry_has_an_entry_file(self):
        reg = (_OS / "core" / "registry.js").read_text(encoding="utf-8")
        named = set(re.findall(r"screens/([a-z0-9_-]+)/index\.js", reg))
        assert len(named) == 18
        for folder in (_OS / "screens").iterdir():
            if folder.is_dir():
                assert folder.name in named, f"screens/{folder.name} is not in the registry"
                assert (folder / "index.js").is_file()

    def test_module_files_are_served_by_the_ordinary_asset_rule(self, server):
        status, headers, _ = get(server, "/assets/os/boot.js")
        assert status == 200 and headers["content-type"].startswith("application/javascript")
        status, _, _ = get(server, "/assets/os/screens/nonexistent/index.js")
        assert status == 404


class TestNoEmDashesOrSlashSeparators:
    def test_shell_sources_carry_no_em_dash_and_no_decorative_double_slash(self):
        files = [p for p in _OS.rglob("*") if p.suffix in {".js", ".css"}] + [_UI / "shell.html"]
        for path in files:
            text = path.read_text(encoding="utf-8")
            assert "—" not in text, f"em dash in {path.relative_to(_ROOT)}"
            assert "–" not in text, f"en dash in {path.relative_to(_ROOT)}"
            assert not re.search(r"^\s*//\s*[-=/*]{3,}", text, re.M), f"decorative // separator in {path.relative_to(_ROOT)}"


class TestRootRouteAndElectronStartPath:
    """Finding #148: the shell can be the default page before the swap."""

    def test_the_root_serves_the_console_unless_the_owner_asks_for_the_shell(self, server, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")  # a configured install (else "/" goes to /setup)
        monkeypatch.delenv("DOURMOUSE_DEFAULT_SHELL", raising=False)
        status, _, body = get(server, "/")
        assert status == 200 and b'src="/assets/os/boot.js"' not in body and b"<title>DOURMOUSE</title>" in body
        monkeypatch.setenv("DOURMOUSE_DEFAULT_SHELL", "os")
        status, headers, body = get(server, "/")
        assert status == 200 and b'src="/assets/os/boot.js"' in body
        assert "script-src 'self'" in headers["content-security-policy"], "the shell keeps its CSP at /"

    def test_console_stays_at_its_own_address_when_the_shell_is_the_default(self, server, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LLM_BACKEND", "ollama")
        monkeypatch.setenv("DOURMOUSE_DEFAULT_SHELL", "os")
        for path in ("/console", "/console.html"):
            status, _, body = get(server, path)
            assert status == 200 and b'src="/assets/os/boot.js"' not in body, f"{path} must still be the console"

    def test_electron_reads_a_safe_start_path_from_the_environment(self):
        src = (_UI.parent / "electron" / "main.js").read_text(encoding="utf-8")
        assert "DOURMOUSE_ELECTRON_START_PATH" in src and src.count("${BASE_URL}${START_PATH}") == 2
        assert 'DEFAULT_START_PATH = "/workspace"' in src, "the default only changes in the swap commit"
