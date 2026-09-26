"""Backend for the OS shell's CODE screen (finding #151). Read-only.

Every route reads a git repository and changes nothing: no checkout, reset,
stash, add or commit. ``GIT_OPTIONAL_LOCKS=0`` keeps even ``git status`` from
taking the index lock, so watching a repo never blocks the owner's own git.

The caller never sends a path. It names a project by an id taken from
``GET /api/os/code/projects``, which lists only:

* ``self``: the checkout this server runs from, and
* the projects on the owner's bookshelf (the project bookkeeper's store) whose
  directory exists and is itself the top of a git work tree.

An id that is not in that list is a 404. A file path inside a project must be
relative, without ``..``, without a leading ``-`` or ``:``, and is passed to git
after ``--`` with ``--literal-pathspecs``.

Routes: ``projects``, ``status`` (working tree against HEAD: changed files with
counts), ``diff`` (one file, working tree or one commit, with ``context`` lines),
``log`` (recent commits) and ``commit`` (the files one commit changed).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from . import ApiError, Request, route

_TIMEOUT = 15.0
_HASH = re.compile(r"^[0-9a-fA-F]{4,40}$")
_FILE_CAP = 200
_DIFF_BYTES = 200_000
_NEW_FILE_BYTES = 100_000
_CONTEXT_MAX = 200


def _git(root: Path, args: list[str], timeout: float = _TIMEOUT) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return subprocess.run(
        ["git", "--no-pager", "--literal-pathspecs", "-c", "core.quotepath=off", "-c", "core.fsmonitor=false", *args],
        cwd=str(root), capture_output=True, timeout=timeout, env=env, check=False,
    )


def _text(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def _pid(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]


def _repo_top(path: Path) -> bool:
    """True only when ``path`` is itself the top of a git work tree."""
    try:
        p = _git(path, ["rev-parse", "--show-toplevel"], timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if p.returncode != 0:
        return False
    try:
        return Path(_text(p.stdout).strip()).resolve() == path.resolve()
    except OSError:
        return False


def _self_root() -> Path:
    from dourmouse.webui import _PROJECT_ROOT

    return Path(_PROJECT_ROOT)


def _bookshelf() -> list[dict[str, Any]]:
    try:
        from dourmouse.project_bookkeeper import _load_store, _public_view, _store_path

        return list(_public_view(_load_store(_store_path())).get("projects") or [])
    except Exception:  # noqa: BLE001 - an unreadable bookshelf means no extra projects
        return []


def known_projects() -> list[dict[str, Any]]:
    """Every repo this screen may read: id, name, resolved path, is_repo."""
    out = []
    root = _self_root().resolve()
    out.append({"id": "self", "name": "DOURMOUSE (this app)", "path": str(root), "kind": "self"})
    seen = {str(root)}
    for rec in _bookshelf():
        raw = str(rec.get("path") or "")
        if not raw:
            continue
        try:
            p = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if str(p) in seen or not p.is_dir():
            continue
        seen.add(str(p))
        out.append({"id": _pid(str(p)), "name": str(rec.get("name") or p.name)[:80], "path": str(p), "kind": "project"})
    return out


def _resolve(req: Request) -> tuple[dict[str, Any], Path]:
    pid = (req.arg("project") or "self").strip()
    for rec in known_projects():
        if rec["id"] == pid:
            root = Path(rec["path"])
            if not _repo_top(root):
                raise ApiError(409, f"{rec['name']} is not the top of a git repository")
            return rec, root
    raise ApiError(404, "unknown project")


def _clean_path(value: str) -> str:
    value = (value or "").strip()
    if (not value or len(value) > 500 or "\x00" in value or value.startswith(("-", ":", "/", "~"))
            or ".." in Path(value).parts or "\\" in value):
        raise ApiError(400, "bad file path")
    return value


def _context(req: Request) -> int:
    try:
        n = int(req.arg("context", "3") or 3)
    except ValueError:
        raise ApiError(400, "context must be a number") from None
    return max(0, min(n, _CONTEXT_MAX))


def _base(root: Path) -> list[str]:
    """What the working tree is compared with: HEAD, or for a repo with no commit
    yet the index (writing the empty tree object would be a write)."""
    p = _git(root, ["rev-parse", "--verify", "-q", "HEAD"], timeout=5.0)
    return ["HEAD"] if p.returncode == 0 else ["--cached"]


@route("GET", "/api/os/code/projects")
def projects(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "projects": known_projects()}


def _numstat(root: Path, base: list[str]) -> dict[str, tuple[int, int, bool]]:
    p = _git(root, ["diff", *base, "--numstat", "--no-renames", "-z", "--no-ext-diff", "--no-textconv"])
    out: dict[str, tuple[int, int, bool]] = {}
    for rec in _text(p.stdout).split("\x00"):
        parts = rec.split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, path = parts
        binary = a == "-" and d == "-"
        out[path] = (0 if binary else int(a), 0 if binary else int(d), binary)
    return out


@route("GET", "/api/os/code/status")
def status(req: Request) -> tuple[int, dict[str, Any]]:
    rec, root = _resolve(req)
    base = _base(root)
    st = _git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"])
    if st.returncode != 0:
        raise ApiError(500, _text(st.stderr).strip()[:300] or "git status failed")
    counts = _numstat(root, base)
    files: list[dict[str, Any]] = []
    for entry in _text(st.stdout).split("\x00"):
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        untracked = code == "??"
        added, deleted, binary = counts.get(path, (0, 0, False))
        files.append({"path": path, "status": "?" if untracked else (code.strip() or "M")[0],
                      "untracked": untracked, "added": added, "deleted": deleted, "binary": binary})
    files.sort(key=lambda f: f["path"])
    total = len(files)
    br = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5.0)
    head = _git(root, ["log", "-1", "--pretty=format:%H%x1f%h%x1f%an%x1f%aI%x1f%s"], timeout=5.0)
    h = _text(head.stdout).split("\x1f") if head.returncode == 0 else []
    reg = getattr(req.server, "registry", None)
    try:
        gated = set(reg.gated_tool_names) if reg is not None else None
    except Exception:  # noqa: BLE001
        gated = None
    # Which of the model's file-editing tools ask first. None when unknown.
    edit_gate = None if gated is None else {t: t in gated for t in ("write_file", "edit_file")}
    return 200, {
        "ok": True, "project": {"id": rec["id"], "name": rec["name"]}, "edit_gate": edit_gate,
        "branch": _text(br.stdout).strip() if br.returncode == 0 else "",
        "head": {"hash": h[0], "short": h[1], "author": h[2], "date": h[3], "subject": h[4]} if len(h) == 5 else None,
        "files": files[:_FILE_CAP], "total": total, "truncated": total > _FILE_CAP,
        "added": sum(f["added"] for f in files), "deleted": sum(f["deleted"] for f in files),
    }


def _untracked_diff(root: Path, path: str) -> tuple[str, bool]:
    """A new file as a unified diff (every line added). Regular files inside the
    repo only: a symlink or an oversized file is described, not read."""
    target = root / path
    if target.is_symlink():
        raise ApiError(409, "a symbolic link, so no diff is shown")
    try:
        real = target.resolve()
        real.relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        raise ApiError(400, "path is outside the project") from None
    if target.is_symlink() or not target.is_file():
        raise ApiError(409, "not a regular file, so no diff is shown")
    size = target.stat().st_size
    if size > _NEW_FILE_BYTES:
        raise ApiError(409, f"new file is {size} bytes, over the {_NEW_FILE_BYTES} byte limit for an inline diff")
    data = target.read_bytes()
    if b"\x00" in data:
        raise ApiError(409, "binary file, no text diff")
    lines = _text(data).splitlines()
    body = "\n".join("+" + ln for ln in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n", True


@route("GET", "/api/os/code/diff")
def diff(req: Request) -> tuple[int, dict[str, Any]]:
    rec, root = _resolve(req)
    path = _clean_path(req.arg("path"))
    ctx = _context(req)
    commit = req.arg("hash").strip()
    if commit:
        if not _HASH.match(commit):
            raise ApiError(400, "bad commit hash")
        p = _git(root, ["show", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", f"-U{ctx}", "--pretty=format:", commit, "--", path])
    else:
        st = _git(root, ["status", "--porcelain=v1", "-z", "--no-renames", "--", path])
        if _text(st.stdout).startswith("??"):
            text, _ = _untracked_diff(root, path)
            return 200, {"ok": True, "path": path, "diff": text, "truncated": False, "context": ctx, "untracked": True}
        p = _git(root, ["diff", *_base(root), "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", f"-U{ctx}", "--", path])
    if p.returncode != 0:
        raise ApiError(500, _text(p.stderr).strip()[:300] or "git diff failed")
    raw = p.stdout
    truncated = len(raw) > _DIFF_BYTES
    return 200, {"ok": True, "path": path, "diff": _text(raw[:_DIFF_BYTES]), "truncated": truncated, "context": ctx, "untracked": False}


@route("GET", "/api/os/code/log")
def log(req: Request) -> tuple[int, dict[str, Any]]:
    rec, root = _resolve(req)
    try:
        limit = max(1, min(int(req.arg("limit", "20") or 20), 100))
    except ValueError:
        raise ApiError(400, "limit must be a number") from None
    if _base(root) != ["HEAD"]:
        return 200, {"ok": True, "commits": []}
    p = _git(root, ["log", f"-n{limit}", "--pretty=format:%H%x1f%h%x1f%an%x1f%aI%x1f%s", "--no-color"])
    if p.returncode != 0:
        raise ApiError(500, _text(p.stderr).strip()[:300] or "git log failed")
    commits = []
    for line in _text(p.stdout).splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            commits.append({"hash": parts[0], "short": parts[1], "author": parts[2], "date": parts[3], "subject": parts[4]})
    return 200, {"ok": True, "commits": commits}


@route("GET", "/api/os/code/commit")
def commit(req: Request) -> tuple[int, dict[str, Any]]:
    rec, root = _resolve(req)
    h = req.need("hash")
    if not _HASH.match(h):
        raise ApiError(400, "bad commit hash")
    ns = _git(root, ["show", "--numstat", "--no-renames", "-z", "--pretty=format:", h])
    if ns.returncode != 0:
        raise ApiError(404, _text(ns.stderr).strip()[:300] or "unknown commit")
    files = []
    for entry in _text(ns.stdout).split("\x00"):
        parts = entry.strip("\n").split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, path = parts
        binary = a == "-" and d == "-"
        files.append({"path": path, "added": 0 if binary else int(a), "deleted": 0 if binary else int(d), "binary": binary})
    subj = _git(root, ["log", "-1", "--pretty=format:%H%x1f%h%x1f%an%x1f%aI%x1f%s", h])
    m = _text(subj.stdout).split("\x1f")
    meta = {"hash": m[0], "short": m[1], "author": m[2], "date": m[3], "subject": m[4]} if len(m) == 5 else None
    return 200, {"ok": True, "commit": meta, "files": files[:_FILE_CAP], "total": len(files), "truncated": len(files) > _FILE_CAP}
