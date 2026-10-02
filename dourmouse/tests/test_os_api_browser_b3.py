"""Phase B3: the three READ-ONLY OS API routes for profiles, extensions and DRM
(dourmouse/os_api/browser.py), against a stand-in pane bridge, plus proof that no route exists
that adds an extension, switches a profile or imports anything."""

from __future__ import annotations

import json

import pytest

from dourmouse.tests.test_browser_tabs_api import FakeBridge, bridge, call, server  # noqa: F401  (fixtures)


class TestProfilesExtensionsDrm:
    def test_profiles_lists_names_and_the_active_one_and_drops_everything_else(self, server, bridge):
        bridge.answer("GET", "/profiles", 200, {"ok": True, "active": "work", "profiles": [{"name": "default", "active": False, "isDefault": True}, {"name": "work", "active": True, "isDefault": False}], "max": 8, "path": "/Users/x/browser"})
        status, data = call(server, "GET", "/api/os/browser/profiles")
        assert status == 200 and data["ok"] is True and data["active"] == "work" and [p["name"] for p in data["profiles"]] == ["default", "work"]
        assert "path" not in data and "max" not in data and "/Users" not in json.dumps(data)

    def test_extensions_lists_name_version_and_state_only(self, server, bridge):
        bridge.answer("GET", "/extensions", 200, {"ok": True, "supported": True, "extensions": [{"name": "Tagger", "version": "1", "enabled": True, "loaded": True, "risk": "high"}], "secret": "x"})
        status, data = call(server, "GET", "/api/os/browser/extensions")
        assert status == 200 and data["supported"] is True and data["extensions"][0]["name"] == "Tagger" and "secret" not in data

    def test_drm_says_not_available_when_that_is_the_answer(self, server, bridge):
        bridge.answer("GET", "/drm", 200, {"ok": True, "build": "stock-electron", "widevine": "not available", "ready": False, "line": "DRM: not available.", "electron": "44.3.0", "plan": "B3_DRM_PLAN.md", "stray": 1})
        status, data = call(server, "GET", "/api/os/browser/drm")
        assert status == 200 and data["widevine"] == "not available" and data["ready"] is False and "stray" not in data and "plan" not in data

    def test_each_route_sends_a_plain_get_with_no_browser_headers(self, server, bridge):
        for route in ("profiles", "extensions", "drm"):
            call(server, "GET", f"/api/os/browser/{route}")
        assert [(r["method"], r["path"]) for r in bridge.requests] == [("GET", "/profiles"), ("GET", "/extensions"), ("GET", "/drm")]
        assert all(r["origin"] is None and r["site"] is None and r["body"] is None for r in bridge.requests)

    @pytest.mark.parametrize("route", ["profiles", "extensions", "drm"])
    def test_outside_the_electron_app_it_says_so_with_503(self, server, monkeypatch, route):
        monkeypatch.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
        status, data = call(server, "GET", f"/api/os/browser/{route}")
        assert status == 503 and "Electron" in data["error"]


class TestNothingCanChangeAnything:
    @pytest.mark.parametrize("path", [
        "profiles", "profiles/switch", "profiles/create", "profiles/remove", "extensions", "extensions/add", "extensions/enable",
        "extensions/disable", "extensions/remove", "extensions/install", "drm", "import", "import/chrome", "import/passwords",
    ])
    def test_no_post_route_exists_for_any_of_it(self, server, bridge, path):
        status, _data = call(server, "POST", f"/api/os/browser/{path}", {"name": "work", "id": "x", "path": "/tmp/x"})
        assert status in (404, 405)
        assert bridge.requests == []  # and nothing was forwarded to the shell

    def test_the_router_registers_only_get_for_the_three_new_paths(self):
        from dourmouse import os_api

        registered = {(m, p) for (m, p) in getattr(os_api, "_ROUTES", {}).keys()} if hasattr(os_api, "_ROUTES") else None
        if registered is None:
            pytest.skip("route table is not introspectable here; the POST tests above cover it")
        for p in ("profiles", "extensions", "drm"):
            assert ("GET", f"/api/os/browser/{p}") in registered and ("POST", f"/api/os/browser/{p}") not in registered
