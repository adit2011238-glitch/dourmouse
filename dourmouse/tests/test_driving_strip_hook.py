"""Phase F2: the app_driver indicator reaches the shell's strip through the
server's existing SSE hub (os_api/apps.py bind_indicator_hub), and no
owner-only route was weakened."""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from dourmouse import app_driver, request_guard
from dourmouse.app_driver import safety
from dourmouse.os_api import apps as apps_api


class Hub:
    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, payload):
        self.events.append(payload)


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setattr(safety, "_killed", False)
    monkeypatch.setattr(safety, "_state", {"active": False, "app": None, "action": None, "since": None, "last_action_at": None})
    safety._recent.clear()
    yield
    apps_api.bind_indicator_hub(Hub())  # leave nothing bound to a hub a test still holds


def wait_for(predicate, seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestIndicatorEvent:
    def test_an_active_action_is_driving_with_its_app_and_action(self, clean):
        hub = Hub()
        apps_api.bind_indicator_hub(hub)
        safety.begin_action("TextEdit", "click")
        event = hub.events[-1]
        assert event["type"] == apps_api.INDICATOR_EVENT == "app_driver_indicator"
        assert (event["driving"], event["active"], event["app"], event["action"], event["killed"]) == (True, True, "TextEdit", "click", False)

    def test_after_the_action_it_lingers_then_a_second_event_ends_it(self, clean, monkeypatch):
        monkeypatch.setattr(safety, "LINGER_SECONDS", 0.2)
        hub = Hub()
        apps_api.bind_indicator_hub(hub)
        safety.begin_action("TextEdit", "type")
        safety.end_action()
        lingering = hub.events[-1]
        assert lingering["driving"] is True and lingering["active"] is False and lingering["app"] == "TextEdit"
        # the indicator calls no listener when the linger lapses; the hook sends the closing event itself
        assert wait_for(lambda: hub.events[-1]["driving"] is False), hub.events
        assert hub.events[-1]["app"] is None and hub.events[-1]["killed"] is False

    def test_a_new_action_cancels_the_pending_lapse_event(self, clean, monkeypatch):
        monkeypatch.setattr(safety, "LINGER_SECONDS", 0.3)
        hub = Hub()
        apps_api.bind_indicator_hub(hub)
        safety.begin_action("TextEdit", "click")
        safety.end_action()
        safety.begin_action("TextEdit", "scroll")  # still driving well past the first linger
        time.sleep(0.55)
        assert hub.events[-1]["driving"] is True and hub.events[-1]["active"] is True
        assert not any(e["driving"] is False for e in hub.events)

    def test_the_kill_switch_is_pushed_and_hides_the_app(self, clean):
        hub = Hub()
        apps_api.bind_indicator_hub(hub)
        safety.begin_action("TextEdit", "click")
        app_driver.engage_kill("test stop", by="owner")
        event = hub.events[-1]
        assert event["killed"] is True and event["driving"] is False and event["app"] is None
        app_driver.release_kill(by="owner")
        assert hub.events[-1]["killed"] is False

    def test_binding_again_replaces_the_old_hub(self, clean):
        first, second = Hub(), Hub()
        apps_api.bind_indicator_hub(first)
        apps_api.bind_indicator_hub(second)
        safety.begin_action("TextEdit", "click")
        assert second.events and not first.events

    def test_a_broken_hub_never_breaks_an_action(self, clean):
        class Broken:
            def broadcast(self, payload):
                raise RuntimeError("hub down")

        apps_api.bind_indicator_hub(Broken())
        safety.begin_action("TextEdit", "click")
        safety.end_action()
        assert safety.indicator()["app"] == "TextEdit"

    def test_the_event_for_a_state_that_is_not_driving_names_no_app(self):
        event = apps_api.indicator_event({"driving": False, "app": "Notes", "action": "click", "active": False, "killed": False})
        assert event["driving"] is False and event["app"] is None and event["action"] is None


class TestOwnerRoutesAreUnchanged:
    def test_allow_deny_act_resume_stay_owner_only_and_kill_stays_open(self):
        for path in ("/api/os/apps/allow", "/api/os/apps/deny", "/api/os/apps/act", "/api/os/apps/resume"):
            assert request_guard.is_owner_route("POST", path), path
        assert not request_guard.is_owner_route("POST", "/api/os/apps/kill")

    def test_no_new_route_was_added_to_the_apps_router(self):
        from dourmouse import os_api

        mounted = sorted(path for (method, path) in os_api._ROUTES if path.startswith("/api/os/apps/"))
        assert mounted == sorted([
            "/api/os/apps/status", "/api/os/apps/allowed", "/api/os/apps/allow", "/api/os/apps/deny",
            "/api/os/apps/kill", "/api/os/apps/resume", "/api/os/apps/snapshot", "/api/os/apps/act",
        ])


class TestThroughTheRealServer:
    def test_run_server_binds_the_hub_and_a_real_sse_client_receives_the_event(self, clean, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        from dourmouse.general_roster import build_general_registry
        from dourmouse.webui import run_server

        srv = run_server(build_general_registry(), port=0, client=None, config=None)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        events: list[dict] = []
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        try:
            conn.request("GET", "/api/events")
            resp = conn.getresponse()

            def read_loop():
                try:
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        if line.startswith(b"data:"):
                            events.append(json.loads(line[len(b"data:"):].strip()))
                except (OSError, ValueError):
                    return  # the test closed the connection, or it idled out; either way the read is over

            threading.Thread(target=read_loop, daemon=True).start()
            time.sleep(0.2)  # let the connection register with the hub
            safety.begin_action("TextEdit", "click")
            assert wait_for(lambda: any(e.get("type") == "app_driver_indicator" for e in events)), events
            event = next(e for e in events if e.get("type") == "app_driver_indicator")
            assert event["driving"] is True and event["app"] == "TextEdit"
        finally:
            conn.close()
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
