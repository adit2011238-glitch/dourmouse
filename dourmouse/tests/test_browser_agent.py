"""Hermetic tests for the v5.25 browser agent (dourmouse/browser_agent.py).

No real Chrome is launched here: the network/browser boundary is exercised
only through the deterministic refusal paths (bad URLs, empty vault), and the
roster shape is asserted exactly. The live-drive paths are covered by the
smoke test in the commit note (example.com open/snapshot/screenshot).
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dourmouse import browser_agent as ba
from dourmouse.dispatch import Permission
from dourmouse.general_roster import build_general_registry


def _free_local_port() -> int:
    """A real, currently-unbound port — bind-then-close, the standard way
    to get one nothing else can grab in the meantime for this test's
    purposes (matches this project's own preference for real sockets over
    guessed/hardcoded port numbers in tests)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

_BROWSER_TOOLS = {
    "open_browser_pane",
    "browser_open",
    "browser_snapshot",
    "browser_fill",
    "browser_fill_form",
    "browser_click",
    "browser_select",
    "browser_press",
    "browser_submit",
    "browser_wait",
    "browser_back",
    "browser_extract",
    "browser_screenshot",
    "browser_creds_store",
    "browser_creds_list",
    "browser_creds_forget",
    "browser_signin",
    "browser_pane_show",
    "browser_pane_hide",
    # query_shared_memory (shared_rag.py) rides every non-orchestrator
    # subagent — see build_general_registry's own comment.
    "query_shared_memory",
    # v13.7: query_desktop_vault (desktop_rag.py) rides alongside it too,
    # extended onto every real agent so a "check the RAG database" request
    # never mis-routes to an agent that can't answer it.
    "query_desktop_vault",
}


class TestUrlGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "http://example.com/path?q=1",
            "https://sub.example.co.uk:8443/x",
        ],
    )
    def test_http_urls_allowed(self, url):
        assert ba._is_http_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "chrome://settings",
            "javascript:alert(1)",
            "data:text/html,<h1>x</h1>",
            "ftp://example.com",
            "example.com",  # no scheme
            "",
        ],
    )
    def test_non_http_refused(self, url):
        assert not ba._is_http_url(url)

    def test_browser_open_refuses_bad_schemes_without_launching(self, monkeypatch):
        """The refusal happens BEFORE the browser is ever touched."""
        called = []

        def _boom(*a, **k):
            called.append(1)
            raise AssertionError("must never reach the browser")

        monkeypatch.setattr(ba, "_call", _boom)
        for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x"):
            out = ba.browser_open({"url": url})
            assert out.startswith("REFUSED"), out
            assert "http(s)" in out
        assert called == []

    def test_browser_open_requires_url(self, monkeypatch):
        monkeypatch.setattr(ba, "_call", lambda f: (_ for _ in ()).throw(AssertionError("nope")))
        out = ba.browser_open({})
        assert "requires a url" in out


class TestVault:
    def _patch_vault(self, monkeypatch, tmp_path):
        vault = tmp_path / "creds.json"
        monkeypatch.setattr(ba, "_VAULT_PATH", vault)
        return vault

    def test_creds_store_rejects_bad_site(self, monkeypatch, tmp_path):
        self._patch_vault(monkeypatch, tmp_path)
        out = ba.browser_creds_store({"site": "not a url", "username": "u", "password": "p"})
        assert out.startswith("REFUSED")
        assert not ba._VAULT_PATH.exists()

    def test_creds_store_list_forget_round_trip(self, monkeypatch, tmp_path):
        vault = self._patch_vault(monkeypatch, tmp_path)
        out = ba.browser_creds_store(
            {"site": "https://example.com", "username": "dourmouse", "password": "s3cret"}
        )
        assert "CREDENTIALS STORED" in out
        assert vault.exists()
        data = json.loads(vault.read_text(encoding="utf-8"))
        assert data["example.com"]["username"] == "dourmouse"
        # Password never leaks through the listing
        listing = ba.browser_creds_list({})
        assert "dourmouse" in listing
        assert "s3cret" not in listing
        assert ba.browser_creds_list({}).startswith("VAULT")
        out = ba.browser_creds_forget({"site": "example.com"})
        assert "REMOVED" in out
        assert ba.browser_creds_list({}) == "VAULT: empty — no credentials stored yet (browser_creds_store)."

    def test_creds_forget_unknown_site(self, monkeypatch, tmp_path):
        self._patch_vault(monkeypatch, tmp_path)
        out = ba.browser_creds_forget({"site": "nowhere.com"})
        assert "no credentials stored" in out or "empty" in out

    def test_signin_without_creds_is_honest(self, monkeypatch, tmp_path):
        self._patch_vault(monkeypatch, tmp_path)
        out = ba.browser_signin({"site": "example.com"})
        assert out.startswith("NO CREDENTIALS")

    def test_status_never_launches(self, monkeypatch, tmp_path):
        self._patch_vault(monkeypatch, tmp_path)
        called = []

        def _boom(*a, **k):
            called.append(1)
            raise AssertionError("status must never launch the browser")

        monkeypatch.setattr(ba, "_call", _boom)
        s = ba.browser_status()
        assert "engine" in s and "ready" in s and "sites" in s and "activity" in s
        assert called == []


class TestRosterWiring:
    def test_browser_subagent_registered_with_all_tools(self):
        registry = build_general_registry()
        assert "browser" in registry.subagent_names
        sub = registry.get_subagent("browser")
        assert {t.name for t in sub.tools} == _BROWSER_TOOLS

    def test_gated_browser_tools_require_confirmation(self):
        registry = build_general_registry()
        sub = registry.get_subagent("browser")
        gated = {t.name for t in sub.tools if t.permission == Permission.REQUIRES_CONFIRMATION}
        assert gated == {"browser_submit", "browser_signin", "browser_creds_store", "browser_creds_forget"}
        for t in sub.tools:
            if t.permission == Permission.REQUIRES_CONFIRMATION:
                assert t.confirm_prompt is not None, f"{t.name} lacks a confirm_prompt"

    def test_gated_tools_flagged_at_engine_level(self):
        registry = build_general_registry()
        for name in ("browser_submit", "browser_signin", "browser_creds_store", "browser_creds_forget"):
            assert name in registry.gated_tool_names, name


class TestAdMediaBlocking:
    """backlog #8, Phase 4 of the user's own spec: block heavy media and
    known trackers for speed. Pure predicate — no real Chrome needed."""

    def test_video_audio_streams_are_blocked(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_BROWSER_BLOCK_MEDIA", raising=False)
        assert ba._should_block_request("https://example.com/video.mp4", "media") is True

    def test_known_tracker_domains_are_blocked(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_BROWSER_BLOCK_MEDIA", raising=False)
        assert ba._should_block_request("https://www.google-analytics.com/collect", "script") is True
        assert ba._should_block_request("https://doubleclick.net/pixel", "image") is True

    def test_ordinary_page_resources_are_not_blocked(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_BROWSER_BLOCK_MEDIA", raising=False)
        assert ba._should_block_request("https://example.com/index.html", "document") is False
        assert ba._should_block_request("https://example.com/style.css", "stylesheet") is False
        assert ba._should_block_request("https://example.com/logo.png", "image") is False

    def test_env_override_disables_blocking_entirely(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_BROWSER_BLOCK_MEDIA", "0")
        assert ba._should_block_request("https://example.com/video.mp4", "media") is False
        assert ba._should_block_request("https://doubleclick.net/pixel", "image") is False


class _FakePaneBridgeHandler(BaseHTTPRequestHandler):
    """Stands in for electron/main.js's real pane-bridge server (see that
    file's startPaneBridge) — real HTTP, real sockets, just a fake
    responder instead of a real Electron process, same spirit as this
    whole test suite's other real-local-server fixtures (e.g.
    test_webui.py's `server`)."""

    calls: list[tuple[str, str]] = []
    responses: dict[str, dict] = {}

    def _handle(self) -> None:
        type(self).calls.append((self.command, self.path))
        body = json.dumps(
            type(self).responses.get(self.path, {"ok": False, "error": "not found"})
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        self._handle()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming
        self._handle()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # silence per-request access logging in test output


@pytest.fixture
def fake_pane_bridge():
    _FakePaneBridgeHandler.calls = []
    _FakePaneBridgeHandler.responses = {"/show": {"ok": True}, "/hide": {"ok": True}}
    server = HTTPServer(("127.0.0.1", 0), _FakePaneBridgeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port, _FakePaneBridgeHandler
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


class TestElectronPaneConfigured:
    """_electron_pane_configured() — pure env-var parsing, the same
    both-or-nothing discovery electron/main.js's spawnServer() sets when
    (and only when) IT started this process."""

    def test_both_vars_set_returns_the_real_pair(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "9333")
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", "9334")
        assert ba._electron_pane_configured() == (9333, 9334)

    def test_neither_set_returns_none(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
        monkeypatch.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
        assert ba._electron_pane_configured() is None

    def test_only_cdp_port_set_returns_none(self, monkeypatch):
        """Both-or-nothing, deliberately — a half-set pair is not a real,
        supported configuration (see the function's own docstring)."""
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "9333")
        monkeypatch.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
        assert ba._electron_pane_configured() is None

    def test_only_pane_port_set_returns_none(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", "9334")
        assert ba._electron_pane_configured() is None

    def test_non_integer_value_returns_none_not_a_crash(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "not-a-port")
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", "9334")
        assert ba._electron_pane_configured() is None


class TestPaneBridgeRequest:
    """_pane_bridge_request() — the real HTTP call to electron/main.js's
    tiny local server, exercised here against a real (fake-responder)
    local HTTP server, not a mock."""

    def test_real_round_trip_against_a_real_local_server(self, fake_pane_bridge):
        port, handler = fake_pane_bridge
        result = ba._pane_bridge_request(port, "POST", "/show")
        assert result == {"ok": True}
        assert handler.calls == [("POST", "/show")]

    def test_unreachable_bridge_raises_a_real_connection_error(self):
        free_port = _free_local_port()  # bound-then-closed -- genuinely nothing listening
        with pytest.raises(urllib.error.URLError):
            ba._pane_bridge_request(free_port, "GET", "/status")


class TestBrowserPaneShowHide:
    """browser_pane_show/browser_pane_hide — the tool-level handlers.
    Honest NOT CONFIGURED without the Electron shell; real success/failure
    against a real (fake-responder) local bridge server otherwise."""

    def _isolate(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
        monkeypatch.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)

    def test_show_not_configured_without_electron(self, monkeypatch):
        self._isolate(monkeypatch)
        result = ba.browser_pane_show({})
        assert "NOT CONFIGURED" in result
        assert "Electron" in result

    def test_hide_not_configured_without_electron(self, monkeypatch):
        self._isolate(monkeypatch)
        result = ba.browser_pane_hide({})
        assert "NOT CONFIGURED" in result

    def test_show_real_success_against_a_real_local_bridge(self, monkeypatch, fake_pane_bridge):
        self._isolate(monkeypatch)
        port, handler = fake_pane_bridge
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "9333")
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(port))
        result = ba.browser_pane_show({})
        assert "visible" in result.lower()
        assert ("POST", "/show") in handler.calls

    def test_hide_real_success_against_a_real_local_bridge(self, monkeypatch, fake_pane_bridge):
        self._isolate(monkeypatch)
        port, handler = fake_pane_bridge
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "9333")
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(port))
        result = ba.browser_pane_hide({})
        assert "hidden" in result.lower()
        assert ("POST", "/hide") in handler.calls

    def test_show_raises_when_bridge_reports_not_ok(self, monkeypatch, fake_pane_bridge):
        self._isolate(monkeypatch)
        port, handler = fake_pane_bridge
        handler.responses["/show"] = {"ok": False, "error": "no main window yet"}
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "9333")
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(port))
        with pytest.raises(RuntimeError, match="BROWSER PANE SHOW FAILED"):
            ba.browser_pane_show({})

    def test_show_raises_a_readable_error_when_bridge_is_unreachable(self, monkeypatch):
        self._isolate(monkeypatch)
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "9333")
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(_free_local_port()))
        with pytest.raises(RuntimeError, match="BROWSER PANE SHOW FAILED"):
            ba.browser_pane_show({})

    def test_hide_raises_a_readable_error_when_bridge_is_unreachable(self, monkeypatch):
        self._isolate(monkeypatch)
        monkeypatch.setenv("DOURMOUSE_ELECTRON_CDP_PORT", "9333")
        monkeypatch.setenv("DOURMOUSE_ELECTRON_PANE_PORT", str(_free_local_port()))
        with pytest.raises(RuntimeError, match="BROWSER PANE HIDE FAILED"):
            ba.browser_pane_hide({})
