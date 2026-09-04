"""Hermetic tests for the v5.25 browser agent (dourmouse/browser_agent.py).

No real Chrome is launched here: the network/browser boundary is exercised
only through the deterministic refusal paths (bad URLs, empty vault), and the
roster shape is asserted exactly. The live-drive paths are covered by the
smoke test in the commit note (example.com open/snapshot/screenshot).
"""

from __future__ import annotations

import json

import pytest

from dourmouse import browser_agent as ba
from dourmouse.dispatch import Permission
from dourmouse.general_roster import build_general_registry

_BROWSER_TOOLS = {
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
