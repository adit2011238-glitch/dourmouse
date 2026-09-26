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
import signal
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

from dourmouse import sandbox as _sandbox

VERSION = "2"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_JOB_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_BLOB_BYTES = 512 * 1024 * 1024
MAX_JOB_SECONDS = 6 * 3600
_OUTPUT_TAIL = 64 * 1024
# Job resource caps (S22). Each stdout and stderr file, and the whole job
# folder, are bounded; a job over either is killed and marked failed.
MAX_JOB_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_JOB_DIR_BYTES = 2 * 1024 * 1024 * 1024
_MAX_FILE_BYTES = 1024 * 1024 * 1024  # RLIMIT_FSIZE: one file a job writes
_MAX_OPEN_FILES = 1024
_NPROC_HEADROOM = 256  # forks a job may add beyond the user's current process count
# Every job runs inside the Seatbelt sandbox in dourmouse/sandbox.py (S19).
# Setting DOURMOUSE_UNSANDBOXED_JOBS=1 in the node's environment is the ONE
# explicit override that lets jobs run without it (for a host that has no
# sandbox-exec); without it a job is refused rather than run unsandboxed.
_UNSANDBOXED_ENV = "DOURMOUSE_UNSANDBOXED_JOBS"


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

def _job_environment(home: str | Path | None = None, tmpdir: str | Path | None = None) -> dict[str, str]:
    """ALLOWLIST only: what Python needs to start, never the node's own
    environment, which may hold tokens and keys. HOME is the job's own
    directory when given (S19)."""
    # PROCESSOR_ARCHITECTURE / NUMBER_OF_PROCESSORS: Windows' platform.machine()
    # and numerical libraries read them (live, machine() came back empty
    # without it); they hold no secrets.
    keep = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep or k.startswith("LC_")}
    env["PATH"] = os.environ.get("PATH", "") if sys.platform == "win32" else "/usr/bin:/bin:/usr/sbin:/sbin"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
    if tmpdir is not None:
        env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmpdir)
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
        self._environment: dict[str, Any] | None = None
        self._env_ready = threading.Event()
        # Probed in the background from startup: listing the interpreter's
        # packages takes seconds, and a health check must never wait on it.
        threading.Thread(target=self._probe_environment, daemon=True, name="env-probe").start()

    def environment(self, wait_s: float = 120.0) -> dict[str, Any]:
        """What the job interpreter actually is (version, platform, installed
        packages), plus a hash of it: the experiment record's "environment
        hash" (finding #098). A run is only reproducible if you know what it
        ran on."""
        if not self._env_ready.wait(wait_s):
            return {"pending": True}
        assert self._environment is not None
        return self._environment

    def _probe_environment(self) -> None:
        try:
            self._environment = self._run_probe()
        finally:
            self._env_ready.set()

    def _run_probe(self) -> dict[str, Any]:
        probe = (
            "import json, platform, sys\n"
            "from importlib import metadata\n"
            "pk = sorted(f\"{d.metadata['Name']}=={d.version}\" for d in metadata.distributions() if d.metadata['Name'])\n"
            "print(json.dumps({'python': sys.version.split()[0], 'implementation': platform.python_implementation(),"
            " 'platform': platform.platform(), 'machine': platform.machine(), 'packages': pk}))"
        )
        try:
            out = subprocess.run(  # noqa: S603 -- fixed interpreter, fixed probe
                [self.python, "-I", "-c", probe], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60, env=_job_environment(), check=True,
            ).stdout
            env: dict[str, Any] = json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            env = {"error": f"environment probe failed: {type(exc).__name__}: {exc}"}
        env["sha256"] = hashlib.sha256(json.dumps(env, sort_keys=True).encode("utf-8")).hexdigest()
        return env

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
        if not _sandbox_ready():
            raise RuntimeError(
                "refusing to run a job: sandbox-exec (macOS Seatbelt) is unavailable, and jobs never run "
                f"unsandboxed. Set {_UNSANDBOXED_ENV}=1 in the node's environment only if you accept that."
            )
        job_id = uuid.uuid4().hex
        d = self._dir(job_id)
        (d / "in").mkdir(parents=True)
        (d / "out").mkdir()
        (d / "main.py").write_text(code, encoding="utf-8")
        status = {
            "id": job_id, "state": "queued", "submitted_at": time.time(), "timeout_s": timeout,
            "memory_mb": memory_mb, "inputs": inputs, "label": str(spec.get("label", ""))[:200],
            "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "environment": {k: v for k, v in self.environment().items() if k != "packages"},
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
        if not os.path.isfile(self.python):
            raise FileNotFoundError(f"job interpreter not found: {self.python}")
        (d / "tmp").mkdir(exist_ok=True)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000004  # CREATE_SUSPENDED: capped before it runs a line
            status["memory_limit"] = "enforced (Windows Job Object)"
        else:
            import resource

            # Own session and process group, so a timeout or a memory breach
            # can kill the job's children too (S21).
            kwargs["start_new_session"] = True
            as_limit = status["memory_mb"] * 1024 * 1024 if sys.platform.startswith("linux") else None
            cpu_seconds = min(status["timeout_s"] * (os.cpu_count() or 1), MAX_JOB_SECONDS * 4)
            nproc = _nproc_limit()

            def _limits() -> None:
                # CPU seconds, one file's size, forks, open files, no core dumps (S22).
                if as_limit is not None:
                    resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
                resource.setrlimit(resource.RLIMIT_FSIZE, (_MAX_FILE_BYTES, _MAX_FILE_BYTES))
                resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
                resource.setrlimit(resource.RLIMIT_NOFILE, (_MAX_OPEN_FILES, _MAX_OPEN_FILES))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

            kwargs["preexec_fn"] = _limits
            if sys.platform.startswith("linux"):
                status["memory_limit"] = "enforced (RLIMIT_AS)"
            else:
                # macOS does not enforce RLIMIT_AS (setrlimit refuses it). Dourmouse
                # runs on the Mac alone (owner, 2026-09-24), so the limit is
                # enforced here by a resident-memory watchdog instead (finding
                # #098); without psutil the job says so rather than pretend.
                status["memory_limit"] = (
                    "enforced (RSS watchdog, 250 ms)" if _psutil() is not None else f"NOT enforced on {sys.platform}"
                )
        argv = [self.python, "-I", "-B", "main.py"]
        profile_path = None
        if _sandbox_exe() is not None:
            profile_path = _sandbox.write_profile(_sandbox.build_job_profile(d, self.python))
            argv = [str(_sandbox_exe()), "-f", profile_path, *argv]
            status["sandbox"] = "seatbelt (reads allowlisted, writes job folder only, no network)"
        else:
            status["sandbox"] = f"NONE ({_UNSANDBOXED_ENV}=1)"
        self._write_status(job_id, status)
        over_limit = threading.Event()
        exceeded: list[str] = []
        try:
            with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
                proc = subprocess.Popen(  # noqa: S603 -- fixed interpreter, job's own file
                    argv, cwd=d, env=_job_environment(d, d / "tmp"),
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, **kwargs,
                )
                job_handle = None
                if sys.platform == "win32":
                    try:
                        job_handle = _limit_memory_windows(proc.pid, status["memory_mb"])
                    finally:
                        _resume_windows(proc.pid)
                if status["memory_limit"].startswith("enforced (RSS"):
                    threading.Thread(
                        target=_rss_watchdog, args=(proc, status["memory_mb"], over_limit), daemon=True,
                    ).start()
                threading.Thread(target=_disk_guard, args=(proc, d, exceeded), daemon=True).start()
                try:
                    code = proc.wait(timeout=status["timeout_s"])
                    state = "succeeded" if code == 0 else "failed"
                except subprocess.TimeoutExpired:
                    _kill_tree(proc)
                    code = proc.wait()
                    state = "timed_out"
                # Whatever the job left running in its group dies with it.
                _kill_tree(proc)
                if over_limit.is_set():
                    state = "failed"
                    status["error"] = f"memory limit exceeded ({status['memory_mb']} MB)"
                if exceeded:
                    state = "failed"
                    status["error"] = exceeded[0]
                if sys.platform == "win32" and job_handle is not None:
                    import ctypes
                    from ctypes import wintypes

                    k32 = ctypes.WinDLL("kernel32")
                    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
                    k32.CloseHandle(job_handle)
        finally:
            if profile_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(profile_path)
        used = _dir_bytes(d)
        if used > MAX_JOB_DIR_BYTES and not exceeded:
            state = "failed"
            status["error"] = f"job folder grew to {used} bytes, over the {MAX_JOB_DIR_BYTES} byte limit"
        metrics: dict[str, Any] = {}
        metrics_file = d / "out" / "metrics.json"
        if not metrics_file.exists() and (d / "metrics.json").exists():
            # Finding #130: code often writes metrics.json next to main.py
            # instead of into out/ (seen live from the research agent); the
            # numbers are real either way, so they are read from there and
            # the fallback is noted on the job.
            metrics_file = d / "metrics.json"
            status["metrics_source"] = "metrics.json (job folder, not out/)"
        if metrics_file.is_symlink():
            metrics = {"_error": "metrics.json is a symlink; refused"}
        elif metrics_file.exists():
            try:
                metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
            except ValueError:
                metrics = {"_error": "out/metrics.json is not valid JSON"}
        artifacts = {}
        for f in sorted((d / "out").rglob("*")):
            if f.is_file() and not f.is_symlink():  # a link could point at a file the job may not read itself
                artifacts[str(f.relative_to(d / "out")).replace("\\", "/")] = _sha256_file(f)
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


def _psutil() -> Any:
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def _rss_watchdog(proc: subprocess.Popen[bytes], memory_mb: int, over: threading.Event) -> None:
    """Kill the job (and its children) once their combined resident memory
    passes the limit. Polls every 250 ms, so a burst faster than that can
    overshoot briefly before the kill; that is the honest limit of this
    method, and the only one macOS allows."""
    psutil = _psutil()
    limit = memory_mb * 1024 * 1024
    try:
        root = psutil.Process(proc.pid)
        while proc.poll() is None:
            procs = [root, *root.children(recursive=True)]
            rss = 0
            for p in procs:
                with contextlib.suppress(psutil.Error):
                    rss += p.memory_info().rss
            if rss > limit:
                over.set()
                _kill_tree(proc)
                for p in reversed(procs):
                    with contextlib.suppress(psutil.Error):
                        p.kill()
                return
            time.sleep(0.25)
    except psutil.Error:
        return


def _tail(path: Path) -> str:
    """The last _OUTPUT_TAIL bytes, read by seeking (a job may have written
    gigabytes) and never through a symlink the job swapped in."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        fh.seek(max(0, size - _OUTPUT_TAIL))
        return fh.read(_OUTPUT_TAIL).decode("utf-8", errors="replace")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_bytes(root: Path) -> int:
    """Bytes under root, without following symlinks."""
    total = 0
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            with contextlib.suppress(OSError):  # a file the job deleted mid-walk
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill the job's whole process group plus any descendant that left it
    with its own session (S21). Safe to call on a job that already exited:
    it then only sweeps stragglers left in the group."""
    psutil = _psutil()
    descendants: list[Any] = []
    if psutil is not None:
        with contextlib.suppress(psutil.Error):
            descendants = psutil.Process(proc.pid).children(recursive=True)
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    if proc.poll() is None:
        proc.kill()
    for p in descendants:
        with contextlib.suppress(psutil.Error):
            p.kill()


def _disk_guard(proc: subprocess.Popen[bytes], job_dir: Path, exceeded: list[str]) -> None:
    """Kill the job as soon as its output files or its folder pass the caps,
    so a runaway print loop or write loop cannot fill the disk (S22)."""
    while proc.poll() is None:
        for name in ("stdout.txt", "stderr.txt"):
            with contextlib.suppress(OSError):
                if os.lstat(job_dir / name).st_size > MAX_JOB_OUTPUT_BYTES:
                    exceeded.append(f"{name} passed {MAX_JOB_OUTPUT_BYTES} bytes of output; job killed")
        if not exceeded and _dir_bytes(job_dir) > MAX_JOB_DIR_BYTES:
            exceeded.append(f"job folder passed {MAX_JOB_DIR_BYTES} bytes; job killed")
        if exceeded:
            _kill_tree(proc)
            return
        time.sleep(0.25)


def _nproc_limit() -> int:
    """RLIMIT_NPROC counts the whole user's processes on macOS, so the cap is
    the current count plus headroom rather than a fixed small number."""
    psutil = _psutil()
    current = len(psutil.pids()) if psutil is not None else 1024
    return current + _NPROC_HEADROOM


def _sandbox_exe() -> str | None:
    return _sandbox.sandbox_exe()


def _sandbox_ready() -> bool:
    return _sandbox_exe() is not None or os.environ.get(_UNSANDBOXED_ENV) == "1"


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
            out["environment"] = self.jobs.environment(wait_s=0)  # never block a health check
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
