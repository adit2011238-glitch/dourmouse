"""Finding #150: the PROJECTS backend on a port-0 server (isolated HOME, workspace and config)."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from dourmouse.general_roster import build_general_registry


@pytest.fixture
def server(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    srv.test_home = home
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
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


class TestList:
    def test_an_empty_machine_gives_an_empty_list_not_an_error(self, server):
        status, data = call(server, "GET", "/api/os/projects/list")
        assert status == 200 and data["ok"] is True
        assert data["projects"] == []

    def test_a_bookkeeper_failure_is_an_error_with_the_reason(self, server, monkeypatch):
        import dourmouse.project_bookkeeper as pb

        def boom(*a, **k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(pb, "get_bookkeeper", boom)
        status, data = call(server, "GET", "/api/os/projects/list")
        assert status == 500
        assert "disk on fire" in json.dumps(data)


class TestPlanAndCreate:
    def test_plan_names_the_folder_and_creates_nothing(self, server):
        status, data = call(server, "GET", "/api/os/projects/plan?name=Alpha%20Lab")
        assert status == 200
        expected = server.test_home / "Documents" / "Dourmouse Projects" / "Alpha Lab"
        assert data["path"] == str(expected)
        assert not expected.exists()

    def test_create_makes_the_folder_and_lists_it(self, server):
        status, data = call(server, "POST", "/api/os/projects/create", {"name": "Alpha Lab", "description": "first"})
        assert status == 200 and data["ok"] is True
        folder = Path(data["project"]["path"])
        assert folder.is_dir() and folder.parent == server.test_home / "Documents" / "Dourmouse Projects"
        status, listing = call(server, "GET", "/api/os/projects/list")
        names = [p["name"] for p in listing["projects"]]
        assert "Alpha Lab" in names
        from dourmouse.project_bookkeeper import project_tab_id

        mine = next(p for p in listing["projects"] if p["name"] == "Alpha Lab")
        assert mine["tab_id"] == project_tab_id(data["project"]["path"])

    def test_a_second_project_with_the_same_name_gets_its_own_folder(self, server):
        a = call(server, "POST", "/api/os/projects/create", {"name": "Same"})[1]["project"]["path"]
        b = call(server, "POST", "/api/os/projects/create", {"name": "Same"})[1]["project"]["path"]
        assert a != b and Path(a).is_dir() and Path(b).is_dir()

    @pytest.mark.parametrize("name", ["", "   ", "x" * 121, "bad\x00name", "bad\nname"])
    def test_a_bad_name_is_refused_and_nothing_is_created(self, server, name):
        status, data = call(server, "POST", "/api/os/projects/create", {"name": name})
        assert status == 400 and data["ok"] is False
        assert not (server.test_home / "Documents").exists()

    def test_a_name_that_looks_like_a_path_stays_inside_the_projects_folder(self, server):
        status, data = call(server, "POST", "/api/os/projects/create", {"name": "../../etc/evil"})
        assert status == 200
        base = (server.test_home / "Documents" / "Dourmouse Projects").resolve()
        assert Path(data["project"]["path"]).resolve().parent == base

    def test_a_custom_path_is_not_accepted(self, server, tmp_path):
        status, data = call(server, "POST", "/api/os/projects/create", {"name": "P", "path": str(tmp_path / "elsewhere")})
        assert status == 400
        assert not (tmp_path / "elsewhere").exists()

    def test_the_description_is_bounded(self, server):
        status, _ = call(server, "POST", "/api/os/projects/create", {"name": "P", "description": "d" * 2001})
        assert status == 400

    def test_a_non_text_name_is_refused(self, server):
        assert call(server, "POST", "/api/os/projects/create", {"name": 7})[0] == 400

    def test_a_filesystem_error_is_an_honest_500(self, server, monkeypatch):
        import dourmouse.project_bookkeeper as pb

        def deny(*a, **k):
            raise PermissionError("no write access")

        monkeypatch.setattr(pb, "create_project", deny)
        status, data = call(server, "POST", "/api/os/projects/create", {"name": "P"})
        assert status == 500 and "no write access" in data["error"]

    def test_open_returns_the_tab_id_the_shell_scopes_chat_to(self, server):
        p = call(server, "POST", "/api/os/projects/create", {"name": "Scoped"})[1]["project"]
        status, data = call(server, "POST", "/api/projects/open", {"path": p["path"]})
        assert status == 200 and data["project"]["tab_id"].startswith("project-")
