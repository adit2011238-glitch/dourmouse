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


# --------------------------------------------------------------------------- #
# S19, S21, S22: every job runs inside the Seatbelt sandbox, with limits
# --------------------------------------------------------------------------- #

import os  # noqa: E402
import time  # noqa: E402

from dourmouse import sandbox as sb  # noqa: E402

_needs_sandbox = pytest.mark.skipif(not sb.sandbox_available(), reason="sandbox-exec unavailable")


def _run(node, code, **kw):
    client, _ = node
    return client.wait_job(client.submit_job(code, **kw)["id"], poll_s=0.1, max_wait_s=60)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@_needs_sandbox
class TestJobSandbox:
    def test_a_job_cannot_read_a_dotenv_or_ssh_in_home_or_reach_the_network(self, node, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "id_rsa").write_text("KEYMATERIAL")
        (home / ".env").write_text("TOKEN=abc")
        monkeypatch.setenv("HOME", str(home))
        outside = tmp_path / "outside.txt"
        code = f"""
import json, socket, os
r = {{}}
def attempt(name, fn):
    try:
        fn(); r[name] = 'allowed'
    except OSError:
        r[name] = 'denied'
attempt('ssh_key', lambda: open({str(home / '.ssh' / 'id_rsa')!r}).read())
attempt('ssh_list', lambda: os.listdir({str(home / '.ssh')!r}))
attempt('dotenv', lambda: open({str(home / '.env')!r}).read())
attempt('write_outside', lambda: open({str(outside)!r}, 'w').write('x'))
def net():
    s = socket.socket(); s.settimeout(3); s.connect(('1.1.1.1', 53))
attempt('network', net)
open('own.txt', 'w').write('fine')
r['own_write'] = 'ok'
json.dump(r, open('out/metrics.json', 'w'))
"""
        st = _run(node, code)
        assert st["state"] == "succeeded", st
        assert st["metrics"] == {"ssh_key": "denied", "ssh_list": "denied", "dotenv": "denied",
                                 "write_outside": "denied", "network": "denied", "own_write": "ok"}
        assert not outside.exists()
        assert st["sandbox"].startswith("seatbelt")

    def test_the_job_environment_is_an_allowlist_with_home_in_the_job_folder(self, node, monkeypatch):
        for key in ("ANTHROPIC_API_KEY", "OLLAMA_API_KEY", "GEMINI_API_KEY", "GITHUB_TOKEN"):
            monkeypatch.setenv(key, "should-not-leak")
        st = _run(node, "import os, json; json.dump({'env': sorted(os.environ), 'home': os.environ['HOME'], "
                        "'cwd': os.getcwd()}, open('out/metrics.json', 'w'))")
        assert st["state"] == "succeeded", st
        leaked = [k for k in st["metrics"]["env"] if "KEY" in k or "TOKEN" in k or "SECRET" in k]
        assert leaked == []
        assert os.path.realpath(st["metrics"]["home"]) == os.path.realpath(st["metrics"]["cwd"])

    def test_normal_numeric_work_still_runs(self, node):
        st = _run(node, "import math, statistics, json\n"
                        "json.dump({'m': statistics.mean([1, 2, 3]), 's': math.sqrt(16)}, open('out/metrics.json', 'w'))")
        assert st["state"] == "succeeded", st
        assert st["metrics"] == {"m": 2, "s": 4.0}

    def test_a_child_process_dies_when_the_job_times_out(self, node):
        code = (
            "import subprocess, time, os\n"
            "p = subprocess.Popen(['/bin/sleep', '120'])\n"
            "q = subprocess.Popen(['/bin/sleep', '120'], start_new_session=True)\n"
            "open('out/pids.txt', 'w').write(f'{p.pid} {q.pid}')\n"
            "time.sleep(120)\n"
        )
        client, _ = node
        st = _run(node, code, timeout_s=3)
        assert st["state"] == "timed_out"
        pids = [int(x) for x in client.artifact(st["id"], "pids.txt").decode().split()]
        deadline = time.monotonic() + 5
        while any(_pid_alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not any(_pid_alive(p) for p in pids), pids

    def test_runaway_output_is_stopped_and_the_job_failed(self, node, monkeypatch):
        monkeypatch.setattr(node_server, "MAX_JOB_OUTPUT_BYTES", 200_000)
        st = _run(node, "import sys\nwhile True:\n    sys.stdout.write('x' * 10000)\n    sys.stdout.flush()\n", timeout_s=60)
        assert st["state"] == "failed"
        assert "output" in st["error"]
        assert len(st["stdout_tail"].encode()) <= node_server._OUTPUT_TAIL

    def test_a_job_folder_over_the_cap_is_failed(self, node, monkeypatch):
        monkeypatch.setattr(node_server, "MAX_JOB_DIR_BYTES", 100_000)
        st = _run(node, "open('out/big.bin', 'wb').write(b'x' * 500_000)")
        assert st["state"] == "failed" and "job folder" in st["error"]

    def test_a_symlink_in_out_is_not_followed_for_metrics_or_hashing(self, node, tmp_path):
        secret = tmp_path / "secret.json"
        secret.write_text('{"leak": 1}')
        # The job's own sandbox cannot read the target, but it can create the link.
        st = _run(node, f"import os; os.symlink({str(secret)!r}, 'out/metrics.json'); os.symlink({str(secret)!r}, 'out/l.txt')")
        assert st["state"] == "succeeded", st
        assert "leak" not in st["metrics"]
        assert "l.txt" not in st["artifacts"]

    def test_a_job_hits_its_cpu_and_process_rlimits_are_applied(self, node):
        st = _run(node, "import resource, json\n"
                        "json.dump({'fsize': resource.getrlimit(resource.RLIMIT_FSIZE)[0], "
                        "'core': resource.getrlimit(resource.RLIMIT_CORE)[0], "
                        "'nofile': resource.getrlimit(resource.RLIMIT_NOFILE)[0], "
                        "'cpu': resource.getrlimit(resource.RLIMIT_CPU)[0], "
                        "'nproc': resource.getrlimit(resource.RLIMIT_NPROC)[0]}, open('out/metrics.json', 'w'))", timeout_s=30)
        m = st["metrics"]
        assert m["fsize"] == node_server._MAX_FILE_BYTES and m["core"] == 0
        assert m["nofile"] == node_server._MAX_OPEN_FILES
        assert 0 < m["cpu"] < resource_unlimited() and 0 < m["nproc"] < resource_unlimited()


def resource_unlimited() -> int:
    import resource

    return resource.RLIM_INFINITY


class TestJobsRefuseWithoutASandbox:
    def test_no_sandbox_exec_means_the_job_is_refused_not_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(node_server, "_sandbox_exe", lambda: None)
        monkeypatch.delenv("DOURMOUSE_UNSANDBOXED_JOBS", raising=False)
        runner = node_server.JobRunner(tmp_path / "jobs", sys.executable, lambda sha: b"")
        with pytest.raises(RuntimeError, match="sandbox-exec"):
            runner.submit({"code": "open('ran.txt', 'w').write('x')"})
        assert not list((tmp_path / "jobs").glob("*/main.py"))

    def test_the_explicit_override_is_the_only_way_to_run_unsandboxed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(node_server, "_sandbox_exe", lambda: None)
        monkeypatch.setenv("DOURMOUSE_UNSANDBOXED_JOBS", "1")
        runner = node_server.JobRunner(tmp_path / "jobs", sys.executable, lambda sha: b"")
        job_id = runner.submit({"code": "print(1)"})["id"]
        deadline = time.monotonic() + 60
        while runner.status(job_id)["state"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.05)
        st = runner.status(job_id)
        assert st["state"] == "succeeded" and st["sandbox"].startswith("NONE")

    def test_the_job_environment_holds_no_secrets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("SOME_TOKEN", "t")
        monkeypatch.setenv("LC_CTYPE", "UTF-8")
        env = node_server._job_environment(tmp_path)
        assert "ANTHROPIC_API_KEY" not in env and "SOME_TOKEN" not in env
        assert env["HOME"] == str(tmp_path) and env["LC_CTYPE"] == "UTF-8"
        assert env["PATH"] in ("/usr/bin:/bin:/usr/sbin:/sbin", os.environ.get("PATH", ""))
