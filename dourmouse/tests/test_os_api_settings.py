"""Finding #150: the SETTINGS backend on a port-0 server (isolated workspace and config)."""

from __future__ import annotations

import http.client
import json
import os
import threading

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse.settings_registry import FEATURES

SECRET = "sk-test-do-not-leak-0123456789"


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    # save_feature and the shell saver write os.environ: register every key so it is restored
    for key in [f["key"] for f in FEATURES] + ["DOURMOUSE_SHELL", "DOURMOUSE_ALLOW_INSECURE_BIND"]:
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    from dourmouse.webui import run_server

    # Every background loop reads its switch when the server starts. conftest.py keeps them all off
    # for each test; the loop above just removed those values, so put "off" back for the start and
    # clear it again afterwards, or the sentry, network watcher, analyst, downloads watcher and the
    # librarian (which walks the real Documents folder and outlives the test) start for real.
    loops = [f["key"] for f in FEATURES if f["kind"] == "bool"]
    for key in loops:
        monkeypatch.setenv(key, "0")
    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    for key in loops:
        monkeypatch.delenv(key)
    srv.cfg_dir = tmp_path / "cfg"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw or b"{}"), raw.decode("utf-8", "replace")


def summary(srv):
    return call(srv, "GET", "/api/os/settings/summary")[1]


class TestSummary:
    def test_the_bind_host_and_port_are_read_from_the_running_socket(self, server):
        access = summary(server)["access"]
        assert access["host"] == server.server_address[0]
        assert access["port"] == server.server_address[1]
        assert access["loopback"] is True and access["token_gate"] is False

    def test_the_token_gate_is_reported_and_the_token_is_not(self, server):
        server.access_token = "tok-should-never-appear"
        status, data, raw = call(server, "GET", "/api/os/settings/summary?token=tok-should-never-appear")
        assert data["access"]["token_gate"] is True
        assert "tok-should-never-appear" not in raw

    def test_an_api_key_is_reported_as_set_but_never_returned(self, server, monkeypatch):
        monkeypatch.setenv("OLLAMA_API_KEY", SECRET)
        status, data, raw = call(server, "GET", "/api/os/settings/summary")
        assert data["keys"]["OLLAMA_API_KEY"] == "environment"
        assert SECRET not in raw

    def test_a_saved_key_says_saved_and_is_not_returned(self, server, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        from dourmouse import config

        config.save_api_key_setting("OLLAMA_API_KEY", SECRET)
        status, data, raw = call(server, "GET", "/api/os/settings/summary")
        assert data["keys"]["OLLAMA_API_KEY"] == "saved" and SECRET not in raw

    def test_every_switch_and_toggle_is_listed(self, server):
        data = summary(server)
        keys = {f["key"] for f in data["features"]["items"]}
        assert {f["key"] for f in FEATURES} == keys
        assert {t["id"] for t in data["toggles"]["items"]} >= {"auto_approve", "claude_front_mode"}
        auto = next(t for t in data["toggles"]["items"] if t["id"] == "auto_approve")
        assert auto["value"] is False and auto["danger"] is True

    def test_models_report_the_unconfigured_state_honestly(self, server):
        models = summary(server)["models"]
        assert models["researcher"]["agent"] == "research_info"
        assert models["local_active"] is False and models["base_url"] is None

    def test_one_broken_section_does_not_blank_the_others(self, server, monkeypatch):
        import dourmouse.os_api.settings as s

        def boom():
            raise RuntimeError("keys unreadable")

        monkeypatch.setattr(s, "_keys_section", boom)
        data = summary(server)
        assert "keys unreadable" in data["keys"]["error"]
        assert data["access"]["host"]

    def test_the_shell_section_says_what_the_next_launch_will_use(self, server, monkeypatch):
        import dourmouse.desktop as desktop

        monkeypatch.setattr(desktop, "_electron_shell_argv", lambda: None)
        shell = summary(server)["shell"]
        assert shell["requested"] == "auto" and shell["electron_available"] is False
        assert shell["effective_next_launch"] == "pywebview"


class TestToggle:
    def test_auto_approve_round_trips_through_the_real_config(self, server):
        status, data, _ = call(server, "POST", "/api/os/settings/toggle", {"id": "auto_approve", "enabled": True})
        assert status == 200 and data["enabled"] is True
        assert call(server, "GET", "/api/settings/auto-approve")[1]["enabled"] is True
        assert call(server, "POST", "/api/os/settings/toggle", {"id": "auto_approve", "enabled": False})[1]["enabled"] is False
        assert call(server, "GET", "/api/settings/auto-approve")[1]["enabled"] is False

    def test_only_a_real_boolean_is_accepted(self, server):
        for bad in ("true", 1, None, "yes"):
            status, data, _ = call(server, "POST", "/api/os/settings/toggle", {"id": "auto_approve", "enabled": bad})
            assert status == 400 and data["ok"] is False
        assert call(server, "GET", "/api/settings/auto-approve")[1]["enabled"] is False

    def test_an_unknown_id_is_refused(self, server):
        status, data, _ = call(server, "POST", "/api/os/settings/toggle", {"id": "OLLAMA_API_KEY", "enabled": True})
        assert status == 400
        assert not (server.cfg_dir / ".env").exists() or "OLLAMA_API_KEY" not in (server.cfg_dir / ".env").read_text()

    def test_a_failed_write_is_a_real_error_status(self, server, monkeypatch):
        from dourmouse import config

        monkeypatch.setattr(config, "save_auto_approve_setting", lambda enabled: {"ok": False, "detail": "could not write config: read-only"})
        status, data, _ = call(server, "POST", "/api/os/settings/toggle", {"id": "auto_approve", "enabled": True})
        assert status == 500 and "read-only" in data["error"]


class TestFeature:
    def test_a_switch_is_saved_and_says_it_needs_a_restart(self, server):
        status, data, _ = call(server, "POST", "/api/os/settings/feature", {"key": "DOURMOUSE_NETWATCH", "value": False})
        assert status == 200 and data["value"] is False and data["restart_required"] is True

    def test_an_unknown_key_and_a_wrong_type_are_refused(self, server):
        assert call(server, "POST", "/api/os/settings/feature", {"key": "OLLAMA_API_KEY", "value": True})[0] == 400
        assert call(server, "POST", "/api/os/settings/feature", {"key": "DOURMOUSE_NETWATCH", "value": "off"})[0] == 400

    def test_librarian_folders_must_be_real_absolute_folders(self, server, tmp_path):
        assert call(server, "POST", "/api/os/settings/feature", {"key": "DOURMOUSE_LIBRARIAN_ROOTS", "value": ["relative/dir"]})[0] == 400
        assert call(server, "POST", "/api/os/settings/feature", {"key": "DOURMOUSE_LIBRARIAN_ROOTS", "value": [str(tmp_path / "missing")]})[0] == 400
        ok = call(server, "POST", "/api/os/settings/feature", {"key": "DOURMOUSE_LIBRARIAN_ROOTS", "value": [str(tmp_path)]})
        assert ok[0] == 200 and ok[1]["value"] == [str(tmp_path)]


class TestShell:
    def test_an_unknown_shell_is_refused(self, server):
        assert call(server, "POST", "/api/os/settings/shell", {"value": "netscape"})[0] == 400

    def test_electron_is_refused_when_it_is_not_installed(self, server, monkeypatch):
        import dourmouse.desktop as desktop

        monkeypatch.setattr(desktop, "_electron_shell_argv", lambda: None)
        status, data, _ = call(server, "POST", "/api/os/settings/shell", {"value": "electron"})
        assert status == 400 and "npm install" in data["error"]
        assert "DOURMOUSE_SHELL" not in (server.cfg_dir / ".env").read_text() if (server.cfg_dir / ".env").exists() else True

    def test_pywebview_is_saved_for_the_next_launch(self, server):
        status, data, _ = call(server, "POST", "/api/os/settings/shell", {"value": "pywebview"})
        assert status == 200 and data["requested"] == "pywebview" and data["effective_next_launch"] == "pywebview"
        assert "DOURMOUSE_SHELL=pywebview" in (server.cfg_dir / ".env").read_text()


class TestReset:
    def test_it_needs_the_features_scope(self, server):
        assert call(server, "POST", "/api/os/settings/reset", {})[0] == 400
        assert call(server, "POST", "/api/os/settings/reset", {"scope": "everything"})[0] == 400

    def test_it_clears_the_switches_and_leaves_keys_and_other_settings_alone(self, server):
        from dourmouse import config

        config.save_api_key_setting("OLLAMA_API_KEY", SECRET)
        config.save_auto_approve_setting(True)
        call(server, "POST", "/api/os/settings/feature", {"key": "DOURMOUSE_NETWATCH", "value": False})
        env_file = server.cfg_dir / ".env"
        assert "DOURMOUSE_NETWATCH=0" in env_file.read_text()
        status, data, _ = call(server, "POST", "/api/os/settings/reset", {"scope": "features"})
        assert status == 200 and "DOURMOUSE_NETWATCH" in data["cleared"]
        text = env_file.read_text()
        assert "DOURMOUSE_NETWATCH" not in text
        assert f"OLLAMA_API_KEY={SECRET}" in text and "DOURMOUSE_AUTO_APPROVE=1" in text
        assert (env_file.stat().st_mode & 0o777) == 0o600
        assert "DOURMOUSE_NETWATCH" not in os.environ

    def test_a_switch_set_by_the_real_environment_is_reported_not_claimed(self, server, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LIBRARIAN", "0")
        status, data, _ = call(server, "POST", "/api/os/settings/reset", {"scope": "features"})
        assert status == 200 and "DOURMOUSE_LIBRARIAN" in data["still_overridden"]
        assert os.environ["DOURMOUSE_LIBRARIAN"] == "0"

    def test_with_nothing_saved_it_clears_nothing(self, server):
        status, data, _ = call(server, "POST", "/api/os/settings/reset", {"scope": "features"})
        assert status == 200 and data["cleared"] == []
