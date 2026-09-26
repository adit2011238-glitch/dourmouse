"""Finding #151: the CODE screen's read-only git routes (os_api/code.py)."""

from __future__ import annotations

import http.client
import json
import subprocess
import threading

import pytest

from dourmouse.general_roster import build_general_registry


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.py").write_text("one\ntwo\nthree\nfour\nfive\nsix\nseven\n", encoding="utf-8")
    (r / "b.txt").write_text("keep\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "first commit")
    return r


@pytest.fixture
def server(monkeypatch, tmp_path, repo):
    ws = tmp_path / "ws"
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.project_bookkeeper import create_project

    create_project(name="Demo proj", path=str(repo))
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def call(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def pid(srv):
    return [p for p in call(srv, "/api/os/code/projects")[1]["projects"] if p["kind"] == "project"][0]["id"]


class TestProjects:
    def test_lists_self_and_the_bookshelf_project(self, server):
        status, data = call(server, "/api/os/code/projects")
        assert status == 200
        kinds = [p["kind"] for p in data["projects"]]
        assert kinds[0] == "self" and "project" in kinds

    def test_an_unknown_project_id_is_a_404_and_a_path_is_never_accepted(self, server, tmp_path):
        assert call(server, "/api/os/code/status?project=nope")[0] == 404
        assert call(server, "/api/os/code/status?project=" + str(tmp_path))[0] == 404
        assert call(server, "/api/os/code/status?project=../..")[0] == 404

    def test_a_bookshelf_directory_that_is_not_a_repo_top_is_refused(self, server, tmp_path):
        from dourmouse.project_bookkeeper import create_project

        plain = tmp_path / "plain"
        plain.mkdir()
        create_project(name="plain", path=str(plain))
        pids = {p["name"]: p["id"] for p in call(server, "/api/os/code/projects")[1]["projects"]}
        status, data = call(server, "/api/os/code/status?project=" + pids["plain"])
        assert status == 409 and "not the top of a git repository" in data["error"]


class TestStatusAndDiff:
    def test_clean_repo_has_no_changes(self, server):
        status, data = call(server, "/api/os/code/status?project=" + pid(server))
        assert status == 200 and data["files"] == [] and data["total"] == 0
        assert data["head"]["subject"] == "first commit" and data["branch"]

    def test_modified_and_untracked_files_with_real_counts(self, server, repo):
        (repo / "a.py").write_text("one\nTWO\nthree\nfour\nfive\nsix\nseven\nextra\n", encoding="utf-8")
        (repo / "new.txt").write_text("x\ny\n", encoding="utf-8")
        _, data = call(server, "/api/os/code/status?project=" + pid(server))
        by = {f["path"]: f for f in data["files"]}
        assert by["a.py"]["added"] == 2 and by["a.py"]["deleted"] == 1 and by["a.py"]["status"] == "M"
        assert by["new.txt"]["untracked"] is True and by["new.txt"]["status"] == "?"
        assert data["added"] == 2

    def test_diff_of_one_file_and_context_is_bounded(self, server, repo):
        (repo / "a.py").write_text("one\ntwo\nthree\nfour\nFIVE\nsix\nseven\n", encoding="utf-8")
        p = pid(server)
        _, d0 = call(server, f"/api/os/code/diff?project={p}&path=a.py&context=0")
        _, d3 = call(server, f"/api/os/code/diff?project={p}&path=a.py&context=3")
        assert "-five" in d0["diff"] and "+FIVE" in d0["diff"]
        assert len(d3["diff"]) > len(d0["diff"])
        assert call(server, f"/api/os/code/diff?project={p}&path=a.py&context=abc")[0] == 400
        _, huge = call(server, f"/api/os/code/diff?project={p}&path=a.py&context=99999")
        assert huge["context"] == 200

    def test_untracked_file_diff_is_all_added_and_a_symlink_is_not_read(self, server, repo):
        (repo / "new.txt").write_text("x\ny\n", encoding="utf-8")
        (repo / "link").symlink_to("/etc/hosts")
        p = pid(server)
        _, d = call(server, f"/api/os/code/diff?project={p}&path=new.txt")
        assert d["untracked"] and "+x" in d["diff"] and "@@ -0,0 +1,2 @@" in d["diff"]
        status, e = call(server, f"/api/os/code/diff?project={p}&path=link")
        assert status == 409 and "localhost" not in json.dumps(e)

    @pytest.mark.parametrize("bad", ["../x", "-rf", ":(top)a.py", "/etc/passwd", "a/../../b", "~/x", "a%5Cb"])
    def test_bad_paths_are_refused(self, server, bad):
        assert call(server, f"/api/os/code/diff?project={pid(server)}&path={bad}")[0] == 400

    def test_a_repo_with_no_commits_still_reports_its_files(self, server, tmp_path):
        from dourmouse.project_bookkeeper import create_project

        fresh = tmp_path / "fresh"
        fresh.mkdir()
        _git(fresh, "init", "-q")
        (fresh / "f.txt").write_text("hi\n", encoding="utf-8")
        _git(fresh, "add", "f.txt")
        create_project(name="fresh", path=str(fresh))
        fid = {p["name"]: p["id"] for p in call(server, "/api/os/code/projects")[1]["projects"]}["fresh"]
        _, data = call(server, "/api/os/code/status?project=" + fid)
        assert data["head"] is None and data["files"][0]["path"] == "f.txt" and data["files"][0]["added"] == 1
        assert call(server, "/api/os/code/log?project=" + fid)[1]["commits"] == []


class TestHistory:
    def test_log_and_commit(self, server, repo):
        p = pid(server)
        _, lg = call(server, f"/api/os/code/log?project={p}&limit=5")
        assert [c["subject"] for c in lg["commits"]] == ["first commit"]
        h = lg["commits"][0]["hash"]
        _, cm = call(server, f"/api/os/code/commit?project={p}&hash={h}")
        assert {f["path"] for f in cm["files"]} == {"a.py", "b.txt"} and cm["commit"]["short"]
        _, d = call(server, f"/api/os/code/diff?project={p}&path=b.txt&hash={h}")
        assert "+keep" in d["diff"]

    def test_bad_hash_refused_and_unknown_commit_is_404(self, server):
        p = pid(server)
        assert call(server, f"/api/os/code/commit?project={p}&hash=--output=x")[0] == 400
        assert call(server, f"/api/os/code/commit?project={p}&hash=deadbeef")[0] == 404
        assert call(server, f"/api/os/code/diff?project={p}&path=a.py&hash=zz")[0] == 400
        assert call(server, f"/api/os/code/log?project={p}&limit=x")[0] == 400

    def test_routes_never_change_the_working_tree(self, server, repo):
        (repo / "a.py").write_text("changed\n", encoding="utf-8")
        p = pid(server)
        for path in ("status", "log", "projects"):
            call(server, f"/api/os/code/{path}?project={p}")
        call(server, f"/api/os/code/diff?project={p}&path=a.py")
        assert (repo / "a.py").read_text(encoding="utf-8") == "changed\n"
        out = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
        assert out.strip() == "M a.py"


class TestEditGate:
    def test_status_reports_which_edit_tools_are_gated_from_the_real_registry(self, server):
        _, data = call(server, "/api/os/code/status?project=" + pid(server))
        assert data["edit_gate"] == {"write_file": "write_file" in server.registry.gated_tool_names,
                                     "edit_file": "edit_file" in server.registry.gated_tool_names}
