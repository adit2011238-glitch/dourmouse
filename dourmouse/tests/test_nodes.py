"""Finding #097 (NET-1/2/3): the node service and the Mac-side client, run for
real on 127.0.0.1. On the Windows CI runner this also exercises the real Job
Object memory limit."""

from __future__ import annotations

import hashlib
import json
import sys
import threading

import pytest

from dourmouse.nodes import node_server
from dourmouse.nodes.client import NodeClient, NodeInfo, NodeUnavailable, load_registry, network_status, node_for

TOKEN = "t" * 40


@pytest.fixture
def node(tmp_path):
    config = {"name": "testnode", "roles": ["data", "compute"], "root": str(tmp_path / "node"),
              "token": TOKEN, "bind": "127.0.0.1", "port": 0, "python": sys.executable}
    server = node_server.serve(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    info = NodeInfo("testnode", f"http://127.0.0.1:{server.server_address[1]}", TOKEN, ("data", "compute"))
    try:
        yield NodeClient(info), info
    finally:
        server.shutdown()
        server.server_close()


class TestConfigSafety:
    def test_binding_every_interface_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="Tailscale address"):
            node_server.serve({"roles": ["data"], "root": str(tmp_path), "token": TOKEN, "bind": "0.0.0.0"})

    def test_a_short_token_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="32 characters"):
            node_server.serve({"roles": ["data"], "root": str(tmp_path), "token": "short", "bind": "127.0.0.1"})

    def test_unknown_roles_are_refused(self, tmp_path):
        with pytest.raises(ValueError, match="roles"):
            node_server.serve({"roles": ["model"], "root": str(tmp_path), "token": TOKEN, "bind": "127.0.0.1"})


class TestAuth:
    def test_a_wrong_token_is_refused(self, node):
        _, info = node
        bad = NodeClient(NodeInfo(info.name, info.url, "x" * 40, info.roles))
        with pytest.raises(NodeUnavailable, match="401"):
            bad.health()

    def test_health_reports_roles(self, node):
        client, _ = node
        h = client.health()
        assert h["roles"] == ["compute", "data"] and h["blobs"] == 0


class TestDataRole:
    def test_blobs_round_trip_and_are_verified(self, node):
        client, _ = node
        sha = client.put_blob(b"a paper")
        assert sha == hashlib.sha256(b"a paper").hexdigest()
        assert client.has_blob(sha) and client.get_blob(sha) == b"a paper"
        assert not client.has_blob("0" * 64)

    def test_a_body_that_does_not_match_its_hash_is_refused(self, node):
        client, _ = node
        status, body = client._request("PUT", f"/blobs/{'0' * 64}", b"not zero", "application/octet-stream")
        assert status == 400 and b"hashes to" in body

    def test_metadata_sits_beside_its_blob(self, node):
        client, _ = node
        sha = client.put_blob(b"doc")
        client.put_meta(sha, {"final_url": "https://x.example"})
        assert client.get_meta(sha) == {"final_url": "https://x.example"}
        with pytest.raises(NodeUnavailable, match="404"):
            client.put_meta("1" * 64, {"x": 1})


class TestComputeRole:
    def test_a_job_runs_and_returns_metrics_and_artifacts(self, node):
        client, _ = node
        data_sha = client.put_blob(b"3,4,5\n")
        code = (
            "import json, statistics, pathlib\n"
            "nums = [int(x) for x in pathlib.Path('in/data.csv').read_text().strip().split(',')]\n"
            "json.dump({'mean': statistics.mean(nums)}, open('out/metrics.json', 'w'))\n"
            "pathlib.Path('out/report.txt').write_text('mean computed')\n"
            "print('done')\n"
        )
        st = client.wait_job(client.submit_job(code, inputs={"data.csv": data_sha}, label="mean")["id"], poll_s=0.1, max_wait_s=60)
        assert st["state"] == "succeeded" and st["exit_code"] == 0
        assert st["metrics"] == {"mean": 4}
        assert "done" in st["stdout_tail"]
        assert client.artifact(st["id"], "report.txt") == b"mean computed"
        assert st["artifacts"]["report.txt"] == hashlib.sha256(b"mean computed").hexdigest()

    def test_a_job_never_sees_the_nodes_secrets(self, node, monkeypatch):
        client, _ = node
        monkeypatch.setenv("DOURMOUSE_SECRET_PROBE", "should-not-leak")
        code = "import os, json; json.dump(sorted(os.environ), open('out/metrics.json','w'))"
        st = client.wait_job(client.submit_job(code)["id"], poll_s=0.1, max_wait_s=60)
        assert st["state"] == "succeeded"
        assert "DOURMOUSE_SECRET_PROBE" not in st["metrics"]

    def test_a_job_past_its_time_limit_is_killed(self, node):
        client, _ = node
        st = client.wait_job(client.submit_job("import time; time.sleep(60)", timeout_s=1)["id"], poll_s=0.1, max_wait_s=30)
        assert st["state"] == "timed_out"

    def test_a_failing_job_reports_its_error(self, node):
        client, _ = node
        st = client.wait_job(client.submit_job("raise SystemExit('bad input')")["id"], poll_s=0.1, max_wait_s=30)
        assert st["state"] == "failed" and "bad input" in st["stderr_tail"]

    def test_the_job_says_whether_its_memory_limit_is_enforced(self, node):
        client, _ = node
        st = client.wait_job(client.submit_job("print(1)")["id"], poll_s=0.1, max_wait_s=30)
        assert st["memory_limit"].startswith("enforced (")

    def test_a_job_that_cannot_start_is_recorded_as_failed_not_left_running(self, tmp_path):
        runner = node_server.JobRunner(tmp_path / "jobs", "/no/such/python", lambda sha: b"")
        job_id = runner.submit({"code": "print(1)"})["id"]
        import time
        deadline = time.monotonic() + 10
        while runner.status(job_id)["state"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.05)
        st = runner.status(job_id)
        assert st["state"] == "failed" and "error" in st

    def test_a_job_over_its_memory_limit_is_stopped(self, node):
        client, _ = node
        # Grows gradually (as real workloads do) so the Mac's 250 ms RSS
        # watchdog sees it; Windows and Linux stop it at the OS level.
        code = ("import time\nchunks = []\nfor _ in range(60):\n"
                "    chunks.append(bytearray(20 * 1024 * 1024)); time.sleep(0.05)\nprint('allocated')")
        st = client.wait_job(client.submit_job(code, memory_mb=128)["id"], poll_s=0.1, max_wait_s=60)
        assert st["state"] == "failed"
        assert "allocated" not in st["stdout_tail"]

    def test_bad_job_specs_are_refused(self, node):
        client, _ = node
        for spec in ({"code": ""}, {"code": "x", "timeout_s": 0}, {"code": "x", "memory_mb": 1},
                     {"code": "x", "inputs": {"../evil": "0" * 64}}):
            status, _ = client._request("POST", "/jobs", json.dumps(spec).encode(), "application/json")
            assert status == 400

    def test_an_artifact_path_cannot_escape_the_job_folder(self, node):
        client, _ = node
        st = client.wait_job(client.submit_job("open('out/a.txt','w').write('a')")["id"], poll_s=0.1, max_wait_s=30)
        with pytest.raises(NodeUnavailable):
            client.artifact(st["id"], "../main.py")


class TestRegistry:
    def test_registry_lookup_and_honest_offline_status(self, tmp_path, node):
        _, info = node
        path = tmp_path / "nodes.json"
        path.write_text(json.dumps({"nodes": {
            "up": {"url": info.url, "token": TOKEN, "roles": ["data"]},
            "down": {"url": "http://127.0.0.1:9", "token": TOKEN, "roles": ["compute"]},
        }}), encoding="utf-8")
        assert set(load_registry(path)) == {"up", "down"}
        assert node_for("compute", path).name == "down"
        rows = {r["name"]: r for r in network_status(path)}
        assert rows["up"]["online"] is True
        assert rows["down"]["online"] is False and "unreachable" in rows["down"]["error"]

    def test_no_registry_means_no_nodes(self, tmp_path):
        assert load_registry(tmp_path / "missing.json") == {}


def test_every_job_records_the_environment_it_ran_on(node):
    client, _ = node
    st = client.wait_job(client.submit_job("print(1)")["id"], poll_s=0.1, max_wait_s=30)
    env = st["environment"]
    assert env["python"] == sys.version.split()[0]
    assert len(env["sha256"]) == 64
    health_env = client.health()["environment"]
    assert health_env["sha256"] == env["sha256"] and isinstance(health_env["packages"], list)
