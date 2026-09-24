"""Dourmouse node service: the data node (Dell) and the compute workspace
(desktop) in one self-contained, standard-library-only file (finding #097).

Owner decision 2026-09-24: the desktop is the Python workspace where agents
run simulations, experiments and numbers; the Dell holds the data, papers and
documents; the Mac orchestrates; every model is cloud-hosted. This file is
what runs on the two Windows machines. It needs nothing but Python 3.10+:
deploying it is copying this one file, so a node never needs a Dourmouse
checkout or pip.

Roles (a node may run one or both):

  data     PUT/GET/HEAD /blobs/<sha256>   raw bytes, verified against the hash
           PUT/GET      /meta/<sha256>    JSON metadata beside a blob
           GET          /blobs            list of stored hashes
  compute  POST /jobs                     run Python code in a per-job workspace
           GET  /jobs/<id>                status, exit code, logs, metrics, artifacts
           GET  /jobs                     recent jobs

Security. Every request needs ``Authorization: Bearer <token>`` (constant-time
compared); there is no unauthenticated mode, unlike the old Dell server. The
service binds only to the address in its config, which is the node's
Tailscale address, never 0.0.0.0. Jobs run with a stripped environment (no
inherited secrets), in their own directory, with a wall-clock limit and a
memory limit (a Job Object on Windows, RLIMIT_AS elsewhere). That is process
isolation, not a container: job code runs as the node's user, so the code it
runs must come from Dourmouse's own agents, never from the network.

Run:  python node_server.py --config node_config.json
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

VERSION = "1"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_JOB_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_BLOB_BYTES = 512 * 1024 * 1024
MAX_JOB_SECONDS = 6 * 3600
_OUTPUT_TAIL = 64 * 1024


class _Server(ThreadingHTTPServer):
    """Refuse to share a port (see dourmouse/http_server.py, finding #088)."""

    daemon_threads = True
    if sys.platform == "win32":
        allow_reuse_address = False

        def server_bind(self) -> None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
            super().server_bind()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- #
# data role
# --------------------------------------------------------------------------- #

class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _blob(self, sha: str) -> Path:
        return self.root / sha[:2] / f"{sha}.bin"

    def _meta(self, sha: str) -> Path:
        return self.root / sha[:2] / f"{sha}.json"

    def has(self, sha: str) -> bool:
        return self._blob(sha).exists()

    def put(self, sha: str, data: bytes) -> bool:
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha:
            raise ValueError(f"body hashes to {actual}, not {sha}")
        if self.has(sha):
            return False
        _atomic_write(self._blob(sha), data)
        return True

    def get(self, sha: str) -> bytes:
        data = self._blob(sha).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"stored blob {sha} is corrupt")
        return data

    def put_meta(self, sha: str, meta: dict[str, Any]) -> None:
        _atomic_write(self._meta(sha), json.dumps(meta, sort_keys=True).encode("utf-8"))

    def get_meta(self, sha: str) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self._meta(sha).read_text(encoding="utf-8"))
        return loaded

    def list(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("??/*.bin"))

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free


# --------------------------------------------------------------------------- #
# compute role
# --------------------------------------------------------------------------- #

def _job_environment() -> dict[str, str]:
    """Only what Python needs to start; never the node's own environment,
    which may hold tokens and keys."""
    keep = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "PATH", "LANG", "LC_ALL", "HOME", "USERPROFILE")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    return env


# Windows-only process control, defined only on Windows so type checks on
# other platforms (and the CI lint ratchet) never see win32-only ctypes names.
if sys.platform == "win32":
    def _limit_memory_windows(pid: int, memory_mb: int) -> Any:
        """Put the job's process in a Windows Job Object with a memory cap; the
        OS kills it past the cap. Returns the job handle (keep it alive)."""
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Declared types: ctypes defaults to a 32-bit int return, which would
        # truncate 64-bit HANDLE values.
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)

        class IO_COUNTERS(ctypes.Structure):  # noqa: N801
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        # JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = 0x00000200 | 0x00002000
        info.JobMemoryLimit = memory_mb * 1024 * 1024
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        handle = k32.OpenProcess(0x1F0FFF, False, pid)
        if not k32.AssignProcessToJobObject(job, handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        k32.CloseHandle(handle)
        return job

    def _resume_windows(pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll")
        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1F0FFF, False, pid)
        ntdll.NtResumeProcess(handle)
        k32.CloseHandle(handle)


class JobRunner:
    def __init__(self, root: Path, python: str, blob_fetch: Any) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.python = python
        self.blob_fetch = blob_fetch  # callable(sha) -> bytes, for job inputs
        self._lock = threading.Lock()

    def _dir(self, job_id: str) -> Path:
        return self.root / job_id

    def _write_status(self, job_id: str, status: dict[str, Any]) -> None:
        _atomic_write(self._dir(job_id) / "status.json", json.dumps(status, sort_keys=True).encode("utf-8"))

    def status(self, job_id: str) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads((self._dir(job_id) / "status.json").read_text(encoding="utf-8"))
        return loaded

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        dirs = sorted((d for d in self.root.iterdir() if (d / "status.json").exists()),
                      key=lambda d: d.stat().st_mtime, reverse=True)[:limit]
        return [self.status(d.name) for d in dirs]

    def submit(self, spec: dict[str, Any]) -> dict[str, Any]:
        code = spec.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("a job needs Python 'code'")
        timeout = int(spec.get("timeout_s", 600))
        if not 1 <= timeout <= MAX_JOB_SECONDS:
            raise ValueError(f"timeout_s must be 1..{MAX_JOB_SECONDS}")
        memory_mb = int(spec.get("memory_mb", 2048))
        if not 64 <= memory_mb <= 32768:
            raise ValueError("memory_mb must be 64..32768")
        inputs = spec.get("inputs") or {}
        if not isinstance(inputs, dict) or not all(
            isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", k) and isinstance(v, str) and _SHA_RE.match(v)
            for k, v in inputs.items()
        ):
            raise ValueError("inputs must map safe file names to sha256 hashes")
        job_id = uuid.uuid4().hex
        d = self._dir(job_id)
        (d / "in").mkdir(parents=True)
        (d / "out").mkdir()
        (d / "main.py").write_text(code, encoding="utf-8")
        status = {
            "id": job_id, "state": "queued", "submitted_at": time.time(), "timeout_s": timeout,
            "memory_mb": memory_mb, "inputs": inputs, "label": str(spec.get("label", ""))[:200],
            "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        }
        self._write_status(job_id, status)
        threading.Thread(target=self._run, args=(job_id,), daemon=True, name=f"job-{job_id[:8]}").start()
        return status

    def _run(self, job_id: str) -> None:
        """Never leaves a job looking alive: any failure in here (the
        interpreter could not start, a platform limit could not be set) is
        recorded as the job's own failure. Found by the first test run,
        where macOS refused RLIMIT_AS inside preexec_fn, the thread died,
        and every job said "running" forever."""
        try:
            self._run_inner(job_id)
        except Exception as exc:  # noqa: BLE001 -- recorded on the job, never lost
            try:
                status = self.status(job_id)
            except (OSError, ValueError):
                status = {"id": job_id}
            status.update(state="failed", error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
            self._write_status(job_id, status)

    def _run_inner(self, job_id: str) -> None:
        d = self._dir(job_id)
        status = self.status(job_id)
        try:
            for name, sha in status["inputs"].items():
                (d / "in" / name).write_bytes(self.blob_fetch(sha))
        except Exception as exc:  # noqa: BLE001 -- recorded on the job, never lost
            status.update(state="failed", error=f"could not fetch inputs: {type(exc).__name__}: {exc}", finished_at=time.time())
            self._write_status(job_id, status)
            return
        status.update(state="running", started_at=time.time())
        self._write_status(job_id, status)
        stdout_path, stderr_path = d / "stdout.txt", d / "stderr.txt"
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000004  # CREATE_SUSPENDED: capped before it runs a line
            status["memory_limit"] = "enforced (Windows Job Object)"
        elif sys.platform.startswith("linux"):
            import resource

            limit = status["memory_mb"] * 1024 * 1024

            def _limits() -> None:
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

            kwargs["preexec_fn"] = _limits
            status["memory_limit"] = "enforced (RLIMIT_AS)"
        else:
            # macOS does not enforce RLIMIT_AS (setrlimit refuses it); say so
            # on the job rather than pretend. Nodes run on Windows anyway.
            status["memory_limit"] = f"NOT enforced on {sys.platform}"
        self._write_status(job_id, status)
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            proc = subprocess.Popen(  # noqa: S603 -- fixed interpreter, job's own file
                [self.python, "-I", "main.py"], cwd=d, env=_job_environment(),
                stdin=subprocess.DEVNULL, stdout=out, stderr=err, **kwargs,
            )
            job_handle = None
            if sys.platform == "win32":
                try:
                    job_handle = _limit_memory_windows(proc.pid, status["memory_mb"])
                finally:
                    _resume_windows(proc.pid)
            try:
                code = proc.wait(timeout=status["timeout_s"])
                state = "succeeded" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
                state = "timed_out"
            if sys.platform == "win32" and job_handle is not None:
                import ctypes
                from ctypes import wintypes

                k32 = ctypes.WinDLL("kernel32")
                k32.CloseHandle.argtypes = (wintypes.HANDLE,)
                k32.CloseHandle(job_handle)
        metrics: dict[str, Any] = {}
        metrics_file = d / "out" / "metrics.json"
        if metrics_file.exists():
            try:
                metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
            except ValueError:
                metrics = {"_error": "out/metrics.json is not valid JSON"}
        artifacts = {}
        for f in sorted((d / "out").rglob("*")):
            if f.is_file():
                artifacts[str(f.relative_to(d / "out")).replace("\\", "/")] = hashlib.sha256(f.read_bytes()).hexdigest()
        status.update(
            state=state, exit_code=code, finished_at=time.time(), metrics=metrics, artifacts=artifacts,
            stdout_tail=_tail(stdout_path), stderr_tail=_tail(stderr_path),
        )
        self._write_status(job_id, status)

    def artifact(self, job_id: str, rel: str) -> bytes:
        base = (self._dir(job_id) / "out").resolve()
        target = (base / rel).resolve()
        if base not in target.parents:
            raise ValueError("artifact path escapes the job's output folder")
        return target.read_bytes()


def _tail(path: Path) -> str:
    data = path.read_bytes()
    return data[-_OUTPUT_TAIL:].decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class NodeApp:
    def __init__(self, config: dict[str, Any]) -> None:
        self.token = str(config["token"])
        if len(self.token) < 32:
            raise ValueError("the node token must be at least 32 characters")
        self.name = str(config.get("name", socket.gethostname()))
        self.roles = set(config.get("roles", []))
        if not self.roles <= {"data", "compute"} or not self.roles:
            raise ValueError("roles must be a non-empty subset of {data, compute}")
        root = Path(config["root"])
        self.blobs = BlobStore(root / "blobs") if "data" in self.roles else None
        self.jobs = None
        if "compute" in self.roles:
            fetch = self.blobs.get if self.blobs is not None else _remote_fetcher(config.get("data_node"))
            self.jobs = JobRunner(root / "jobs", config.get("python", sys.executable), fetch)

    def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "version": VERSION, "roles": sorted(self.roles), "time": time.time()}
        if self.blobs is not None:
            out["blobs"] = len(self.blobs.list())
            out["free_bytes"] = self.blobs.free_bytes()
        if self.jobs is not None:
            out["python"] = self.jobs.python
        return out


def _remote_fetcher(data_node: dict[str, Any] | None) -> Any:
    def fetch(sha: str) -> bytes:
        if not data_node:
            raise RuntimeError("this compute node has no data node configured for job inputs")
        req = urllib.request.Request(  # noqa: S310 -- configured Tailscale address
            f"{data_node['url'].rstrip('/')}/blobs/{sha}", headers={"Authorization": f"Bearer {data_node['token']}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            data: bytes = resp.read()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"data node returned bytes that do not hash to {sha}")
        return data
    return fetch


def make_handler(app: NodeApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "dourmouse-node/" + VERSION

        def log_message(self, *a: Any) -> None:  # request bodies and tokens are never logged
            pass

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            ok = got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), app.token.encode())
            if not ok:
                self._json(401, {"error": "unauthorized"})
            return ok

        def _json(self, code: int, body: Any) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _bytes(self, code: int, data: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self, limit: int) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise ValueError(f"body larger than {limit} bytes")
            return self.rfile.read(n)

        def _route(self) -> tuple[str, list[str]]:
            parts = [p for p in self.path.split("?", 1)[0].split("/") if p]
            return (parts[0] if parts else ""), parts[1:]

        def do_HEAD(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            kind, rest = self._route()
            if kind == "blobs" and app.blobs is not None and len(rest) == 1 and _SHA_RE.match(rest[0]):
                self.send_response(200 if app.blobs.has(rest[0]) else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            kind, rest = self._route()
            try:
                if kind == "health" and not rest:
                    self._json(200, app.health())
                elif kind == "blobs" and app.blobs is not None:
                    if not rest:
                        self._json(200, {"blobs": app.blobs.list()})
                    elif len(rest) == 1 and _SHA_RE.match(rest[0]):
                        self._bytes(200, app.blobs.get(rest[0]))
                    else:
                        self._json(404, {"error": "not found"})
                elif kind == "meta" and app.blobs is not None and len(rest) == 1 and _SHA_RE.match(rest[0]):
                    self._json(200, app.blobs.get_meta(rest[0]))
                elif kind == "jobs" and app.jobs is not None:
                    if not rest:
                        self._json(200, {"jobs": app.jobs.list()})
                    elif len(rest) == 1 and _JOB_RE.match(rest[0]):
                        self._json(200, app.jobs.status(rest[0]))
                    elif len(rest) >= 3 and _JOB_RE.match(rest[0]) and rest[1] == "artifacts":
                        self._bytes(200, app.jobs.artifact(rest[0], "/".join(rest[2:])))
                    else:
                        self._json(404, {"error": "not found"})
                else:
                    self._json(404, {"error": "not found"})
            except FileNotFoundError:
                self._json(404, {"error": "not found"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})

        def do_PUT(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            kind, rest = self._route()
            try:
                if kind == "blobs" and app.blobs is not None and len(rest) == 1 and _SHA_RE.match(rest[0]):
                    created = app.blobs.put(rest[0], self._body(MAX_BLOB_BYTES))
                    self._json(201 if created else 200, {"sha256": rest[0], "created": created})
                elif kind == "meta" and app.blobs is not None and len(rest) == 1 and _SHA_RE.match(rest[0]):
                    if not app.blobs.has(rest[0]):
                        self._json(404, {"error": "no blob with that hash"})
                        return
                    app.blobs.put_meta(rest[0], json.loads(self._body(1024 * 1024)))
                    self._json(200, {"sha256": rest[0]})
                else:
                    self._json(404, {"error": "not found"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            kind, rest = self._route()
            try:
                if kind == "jobs" and app.jobs is not None and not rest:
                    self._json(202, app.jobs.submit(json.loads(self._body(4 * 1024 * 1024))))
                else:
                    self._json(404, {"error": "not found"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})

    return Handler


def serve(config: dict[str, Any]) -> _Server:
    app = NodeApp(config)
    host = str(config["bind"])
    if host in ("0.0.0.0", "::", ""):  # noqa: S104 -- this line REFUSES all-interface binds
        raise ValueError("bind to the node's Tailscale address, never to every interface")
    return _Server((host, int(config.get("port", 8770))), make_handler(app))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dourmouse node service")
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    server = serve(config)
    print(f"dourmouse node {config.get('name')} serving {sorted(config['roles'])} on "
          f"{config['bind']}:{server.server_address[1]}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
