"""The Mac's side of the device network (NET-3, finding #097): which nodes
exist, whether they are up, and a client for each.

Nodes are the owner's own machines reached over Tailscale (100.64.0.0/10), so
this client deliberately does NOT go through net_guard, which refuses exactly
that address space for model-supplied URLs. Only addresses written in the
registry file are ever contacted; nothing here takes a URL from a model.

Registry: ``<user config dir>/nodes.json`` (never .env, never the repo)::

    {"nodes": {"dell":    {"url": "http://100.64.102.59:8770", "token": "...", "roles": ["data"]},
               "desktop": {"url": "http://100.98.97.23:8770",  "token": "...", "roles": ["compute"]}}}
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dourmouse.config import user_config_dir


class NodeUnavailable(ConnectionError):
    """The node could not be reached, or refused the request."""


def registry_path() -> Path:
    return user_config_dir() / "nodes.json"


@dataclass(frozen=True)
class NodeInfo:
    name: str
    url: str
    token: str
    roles: tuple[str, ...]


def load_registry(path: Path | None = None) -> dict[str, NodeInfo]:
    p = path or registry_path()
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8")).get("nodes", {})
    return {
        name: NodeInfo(name, str(v["url"]).rstrip("/"), str(v["token"]), tuple(v.get("roles", ())))
        for name, v in raw.items()
    }


def node_for(role: str, path: Path | None = None) -> NodeInfo | None:
    """The first registered node that serves ``role`` ("data" or "compute")."""
    for info in load_registry(path).values():
        if role in info.roles:
            return info
    return None


class NodeClient:
    def __init__(self, info: NodeInfo, *, timeout: float = 30.0) -> None:
        self.info = info
        self.timeout = timeout

    def _request(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None) -> tuple[int, bytes]:
        headers = {"Authorization": f"Bearer {self.info.token}"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(  # noqa: S310 -- registry address only, see module docstring
            f"{self.info.url}{path}", data=body, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() if exc.fp else b""
        except (urllib.error.URLError, OSError) as exc:
            raise NodeUnavailable(f"node {self.info.name} ({self.info.url}) is unreachable: {exc}") from exc

    def _json(self, method: str, path: str, payload: Any = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, data = self._request(method, path, body, "application/json" if body else None)
        if status == 401:
            raise NodeUnavailable(f"node {self.info.name} refused the token (401)")
        if status >= 400:
            detail = json.loads(data).get("error", "") if data else ""
            raise NodeUnavailable(f"node {self.info.name} answered {status}: {detail}")
        return json.loads(data)

    # health
    def health(self) -> dict[str, Any]:
        result: dict[str, Any] = self._json("GET", "/health")
        return result

    # data role
    def put_blob(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        status, body = self._request("PUT", f"/blobs/{sha}", data, "application/octet-stream")
        if status not in (200, 201):
            raise NodeUnavailable(f"node {self.info.name} did not store the blob ({status}): {body[:200]!r}")
        return sha

    def has_blob(self, sha: str) -> bool:
        status, _ = self._request("HEAD", f"/blobs/{sha}")
        return status == 200

    def get_blob(self, sha: str) -> bytes:
        status, data = self._request("GET", f"/blobs/{sha}")
        if status != 200:
            raise NodeUnavailable(f"node {self.info.name} has no blob {sha} ({status})")
        if hashlib.sha256(data).hexdigest() != sha:
            raise NodeUnavailable(f"node {self.info.name} returned bytes that do not hash to {sha}")
        return data

    def put_meta(self, sha: str, meta: dict[str, Any]) -> None:
        self._json("PUT", f"/meta/{sha}", meta)

    def get_meta(self, sha: str) -> dict[str, Any]:
        result: dict[str, Any] = self._json("GET", f"/meta/{sha}")
        return result

    # compute role
    def submit_job(self, code: str, *, inputs: dict[str, str] | None = None, timeout_s: int = 600,
                   memory_mb: int = 2048, label: str = "") -> dict[str, Any]:
        result: dict[str, Any] = self._json("POST", "/jobs", {
            "code": code, "inputs": inputs or {}, "timeout_s": timeout_s, "memory_mb": memory_mb, "label": label,
        })
        return result

    def job(self, job_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._json("GET", f"/jobs/{job_id}")
        return result

    def wait_job(self, job_id: str, *, poll_s: float = 1.0, max_wait_s: float = 3600.0) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait_s
        while True:
            st = self.job(job_id)
            if st["state"] not in ("queued", "running"):
                return st
            if time.monotonic() > deadline:
                raise TimeoutError(f"job {job_id} still {st['state']} after {max_wait_s}s")
            time.sleep(poll_s)

    def artifact(self, job_id: str, name: str) -> bytes:
        status, data = self._request("GET", f"/jobs/{job_id}/artifacts/{name}")
        if status != 200:
            raise NodeUnavailable(f"no artifact {name} on job {job_id} ({status})")
        return data


def network_status(path: Path | None = None) -> list[dict[str, Any]]:
    """Every registered node and whether it answers right now: honest about
    an offline node, never a cached "online"."""
    out = []
    for info in load_registry(path).values():
        row: dict[str, Any] = {"name": info.name, "url": info.url, "roles": list(info.roles)}
        try:
            t0 = time.monotonic()
            row["health"] = NodeClient(info, timeout=5).health()
            row["online"] = True
            row["latency_ms"] = round((time.monotonic() - t0) * 1000)
        except NodeUnavailable as exc:
            row["online"] = False
            row["error"] = str(exc)
        out.append(row)
    return out
