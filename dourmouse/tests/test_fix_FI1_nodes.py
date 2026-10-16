"""FI1 fixes for dourmouse/nodes: P5-33 (handlers answer instead of dropping
the connection), P4-41 (client survives malformed node replies and registry
entries), P5-34 (no preexec_fn in a threaded server), P5-35 (metrics.json read
is capped). Everything runs against a real node on 127.0.0.1; jobs run under
the explicit unsandboxed override so the tests do not depend on Seatbelt."""

from __future__ import annotations

import http.client
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dourmouse.nodes import node_server
from dourmouse.nodes.client import NodeClient, NodeInfo, NodeUnavailable, load_registry, network_status
from dourmouse.tests.test_nodes import TOKEN, node  # noqa: F401 -- shared real-node fixture


@pytest.fixture
def unsandboxed(monkeypatch):
    monkeypatch.setenv(node_server._UNSANDBOXED_ENV, "1")


def _raw(info, method, path, body=b"", headers=None):
    host = info.url.split("//", 1)[1]
    conn = http.client.HTTPConnection(host, timeout=10)
    hdrs = {"Authorization": f"Bearer {TOKEN}", **(headers or {})}
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


# --------------------------------------------------------------------------- #
# P5-33
# --------------------------------------------------------------------------- #

def test_job_refusal_without_a_sandbox_is_a_503_with_the_reason(node, monkeypatch):
    _, info = node
    monkeypatch.setattr(node_server, "_sandbox_exe", lambda: None)
    monkeypatch.delenv(node_server._UNSANDBOXED_ENV, raising=False)
    status, data = _raw(info, "POST", "/jobs", json.dumps({"code": "print(1)"}).encode(),
                        {"Content-Type": "application/json"})
    assert status == 503
    assert "refusing to run a job" in json.loads(data)["error"]


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"123", b"null",
                                  b'{"code": "x", "timeout_s": null}',
                                  b'{"code": "x", "timeout_s": [1]}',
                                  b'{"code": "x", "memory_mb": {}}'])
def test_job_spec_that_is_not_a_valid_object_is_a_400(node, body):
    _, info = node
    status, data = _raw(info, "POST", "/jobs", body, {"Content-Type": "application/json"})
    assert status == 400, data
    assert "error" in json.loads(data)


def test_directory_under_artifacts_is_a_404_not_a_dropped_connection(node, unsandboxed):
    client, info = node
    st = client.wait_job(client.submit_job("import os\nos.makedirs('out/sub')\nopen('out/sub/a.txt','w').write('a')")["id"],
                         poll_s=0.1, max_wait_s=60)
    assert st["state"] == "succeeded", st
    status, data = _raw(info, "GET", f"/jobs/{st['id']}/artifacts/sub")
    assert status == 404
    assert json.loads(data)["error"]


def test_negative_content_length_is_refused(node):
    _, info = node
    host = info.url.split("//", 1)[1]
    conn = http.client.HTTPConnection(host, timeout=5)
    conn.putrequest("PUT", "/blobs/" + "a" * 64)
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Content-Length", "-1")
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 400
    conn.close()


def test_garbage_content_length_is_refused(node):
    _, info = node
    host = info.url.split("//", 1)[1]
    conn = http.client.HTTPConnection(host, timeout=5)
    conn.putrequest("POST", "/jobs")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Content-Length", "abc")
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 400
    conn.close()


def test_meta_body_that_is_not_an_object_is_a_400(node):
    client, info = node
    sha = client.put_blob(b"hello")
    status, _ = _raw(info, "PUT", f"/meta/{sha}", b"[1]", {"Content-Type": "application/json"})
    assert status == 400


# --------------------------------------------------------------------------- #
# P4-41
# --------------------------------------------------------------------------- #

class _Stub:
    """A server answering every request with a fixed (status, content type, body)."""

    def __init__(self, status, ctype, body):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_HEAD = _do

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_html_error_page_is_node_unavailable_not_json_error():
    stub = _Stub(502, "text/html", b"<html>Bad gateway</html>")
    try:
        c = NodeClient(NodeInfo("n", stub.url, TOKEN, ("data",)))
        with pytest.raises(NodeUnavailable, match="502"):
            c.health()
    finally:
        stub.close()


def test_non_json_success_reply_is_node_unavailable():
    stub = _Stub(200, "text/html", b"<html>captive portal</html>")
    try:
        c = NodeClient(NodeInfo("n", stub.url, TOKEN, ("data",)))
        with pytest.raises(NodeUnavailable, match="not JSON"):
            c.health()
    finally:
        stub.close()


def test_one_bad_node_does_not_hide_the_healthy_ones(tmp_path, node):
    _, info = node
    stub = _Stub(200, "text/html", b"<html>nope</html>")
    try:
        path = tmp_path / "nodes.json"
        path.write_text(json.dumps({"nodes": {
            "up": {"url": info.url, "token": TOKEN, "roles": ["data"]},
            "weird": {"url": stub.url, "token": TOKEN, "roles": ["compute"]},
        }}), encoding="utf-8")
        rows = {r["name"]: r for r in network_status(path)}
        assert rows["up"]["online"] is True
        assert rows["weird"]["online"] is False and rows["weird"]["error"]
    finally:
        stub.close()


def test_registry_entry_without_url_or_token_is_reported_not_raised(tmp_path, node):
    _, info = node
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"nodes": {
        "up": {"url": info.url, "token": TOKEN, "roles": ["data"]},
        "notoken": {"url": "http://127.0.0.1:9", "roles": ["data"]},
        "nourl": {"token": TOKEN},
        "junk": "text",
    }}), encoding="utf-8")
    assert set(load_registry(path)) == {"up"}
    rows = {r["name"]: r for r in network_status(path)}
    assert rows["up"]["online"] is True
    for bad in ("notoken", "nourl", "junk"):
        assert rows[bad]["online"] is False
        assert "registry entry" in rows[bad]["error"]


def test_unreadable_registry_file_is_one_honest_row(tmp_path):
    path = tmp_path / "nodes.json"
    path.write_text("{not json", encoding="utf-8")
    rows = network_status(path)
    assert len(rows) == 1 and rows[0]["online"] is False and "registry" in rows[0]["error"]


# --------------------------------------------------------------------------- #
# P5-34
# --------------------------------------------------------------------------- #

def test_jobs_are_started_without_preexec_fn_and_limits_still_apply(node, unsandboxed, monkeypatch):
    client, _ = node
    seen = []
    real = subprocess.Popen

    def spy(*a, **k):
        if "cwd" in k and str(k["cwd"]).find("jobs") >= 0:
            seen.append(k)
        return real(*a, **k)

    monkeypatch.setattr(node_server.subprocess, "Popen", spy)
    code = ("import resource, json\n"
            "json.dump({'core': resource.getrlimit(resource.RLIMIT_CORE)[0],"
            " 'fsize': resource.getrlimit(resource.RLIMIT_FSIZE)[0],"
            " 'nofile': resource.getrlimit(resource.RLIMIT_NOFILE)[0]}, open('out/metrics.json', 'w'))")
    st = client.wait_job(client.submit_job(code, timeout_s=30)["id"], poll_s=0.1, max_wait_s=60)
    assert st["state"] == "succeeded", st
    assert seen and all("preexec_fn" not in k for k in seen)
    m = st["metrics"]
    assert m["core"] == 0
    assert m["fsize"] == node_server._MAX_FILE_BYTES
    assert m["nofile"] == node_server._MAX_OPEN_FILES


def test_the_job_interpreter_arguments_survive_the_launcher(node, unsandboxed):
    client, _ = node
    st = client.wait_job(client.submit_job(
        "import sys, os\nprint(sys.flags.isolated, sys.argv, os.getcwd().endswith(os.sep.join(['jobs', os.path.basename(os.getcwd())])))")["id"],
        poll_s=0.1, max_wait_s=60)
    assert st["state"] == "succeeded", st
    assert "1 ['main.py']" in st["stdout_tail"]


# --------------------------------------------------------------------------- #
# P5-35
# --------------------------------------------------------------------------- #

def test_oversized_metrics_file_is_refused_unread(node, unsandboxed):
    client, _ = node
    code = ("import json\nblob = {'x': 'a' * (2 * 1024 * 1024)}\njson.dump(blob, open('out/metrics.json', 'w'))")
    st = client.wait_job(client.submit_job(code, timeout_s=30)["id"], poll_s=0.1, max_wait_s=60)
    assert st["state"] == "succeeded", st
    assert list(st["metrics"]) == ["_error"]
    assert "too large" in st["metrics"]["_error"]


def test_normal_metrics_file_is_still_read(node, unsandboxed):
    client, _ = node
    st = client.wait_job(client.submit_job("import json\njson.dump({'sharpe': 1.5}, open('out/metrics.json', 'w'))")["id"],
                         poll_s=0.1, max_wait_s=60)
    assert st["metrics"] == {"sharpe": 1.5}
