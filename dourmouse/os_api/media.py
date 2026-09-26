"""Backend for the OS shell's MEDIA screen (finding #152).

The bytes are served by routes that already exist: ``GET /api/files/media``
(byte ranges, ffmpeg conversion), ``/api/files/media-status``,
``/api/files/subtitle.vtt`` and ``/api/files/image``. Those routes take any
absolute path with an extension allow-list (the same trust as ``open_path``).
The shell never builds such a URL from text the owner typed. It asks this
module first, and this module only vouches for a path that

* is absolute after ``~`` expansion and symlink resolution,
* is a real file whose extension is audio, video or an image, and
* lives inside one of a short, fixed list of folders (the workspace, the
  uploads folder, and the owner's Movies, Music, Downloads, Desktop and
  Documents folders).

Routes:

* ``GET  /api/os/media/library``: the recent files (re-checked on every read)
  and which of the fixed folders exist. With ``?root=<name>`` it lists that one
  folder, not recursively, at most 100 files. The name is one of a fixed set,
  never a path.
* ``POST /api/os/media/open {path}``: validates, records the file in the
  recent list and returns what the player needs: kind, size, the URLs, whether
  it needs converting, the subtitle sidecars, and the duration and codecs when
  ffmpeg can read them. It reads the file's header and changes nothing else
  except one small JSON list of recent paths in the workspace.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import tempfile
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from . import ApiError, Request, route

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".oga", ".ogg", ".opus", ".weba"}
VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".ogv", ".mov"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
LIST_CAP = 100
RECENT_CAP = 20
MAX_PATH = 1024

_lock = threading.Lock()


def roots() -> dict[str, Path]:
    """The folders a media path may live in, by fixed name."""
    from dourmouse.config import workspace_dir

    home = Path.home()
    out: dict[str, Path] = {"workspace": workspace_dir()}
    try:
        from dourmouse.webui import _uploads_root

        out["uploads"] = _uploads_root()
    except Exception as exc:  # noqa: BLE001 -- the list is still honest without it
        logging.getLogger(__name__).debug("uploads folder not listed: %s", exc)
    for name in ("Movies", "Music", "Downloads", "Desktop", "Documents"):
        out[name.lower()] = home / name
    return out


def _convert_exts() -> tuple[set[str], set[str]]:
    from dourmouse.media_convert import CONVERT_AUDIO_EXTS, CONVERT_VIDEO_EXTS

    return set(CONVERT_AUDIO_EXTS), set(CONVERT_VIDEO_EXTS)


def kind_of(ext: str) -> str | None:
    ext = ext.lower()
    conv_a, conv_v = _convert_exts()
    if ext in AUDIO_EXTS or ext in conv_a:
        return "audio"
    if ext in VIDEO_EXTS or ext in conv_v:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def is_inside(path: Path, base: Path) -> bool:
    try:
        return path.is_relative_to(base.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def check_path(raw: Any) -> Path:
    """The resolved path, or an ApiError that says why the shell will not play it."""
    text = str(raw or "").strip()
    if not text:
        raise ApiError(400, "path is required")
    if len(text) > MAX_PATH or "\x00" in text:
        raise ApiError(400, "that is not a usable file path")
    try:
        target = Path(text).expanduser()
    except (OSError, RuntimeError):
        raise ApiError(400, "that is not a usable file path") from None
    if not target.is_absolute():
        raise ApiError(400, "give the full path, starting with / or ~")
    try:
        target = target.resolve()
    except (OSError, RuntimeError):
        raise ApiError(400, "that path cannot be resolved") from None
    if not target.is_file():
        raise ApiError(404, "no file at that path")
    if kind_of(target.suffix) is None:
        raise ApiError(415, f"MEDIA plays audio, video and images; {target.suffix or 'this file type'} is none of those. "
                            "Ask on HOME to open it with open_path.")
    allowed = roots()
    if not any(is_inside(target, base) for base in allowed.values()):
        names = ", ".join(sorted(allowed))
        raise ApiError(403, f"that file is outside the folders MEDIA may read ({names})")
    return target


def _recent_file() -> Path:
    from dourmouse.config import workspace_dir

    return workspace_dir() / "media_recent.json"


def _read_recent() -> list[str]:
    try:
        data = json.loads(_recent_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("paths") if isinstance(data, dict) else None
    return [p for p in items if isinstance(p, str)][:RECENT_CAP] if isinstance(items, list) else []


def _write_recent(paths: list[str]) -> None:
    dest = _recent_file()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".media_recent.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"paths": paths[:RECENT_CAP]}, fh)
        os.replace(tmp, dest)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _describe(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {"name": path.name, "path": str(path), "kind": kind_of(path.suffix), "ext": path.suffix.lower(),
            "size": st.st_size, "mtime": st.st_mtime}


def _urls(path: Path) -> dict[str, str]:
    q = urllib.parse.quote(str(path))
    return {
        "media": "/api/files/media?path=" + q,
        "status": "/api/files/media-status?path=" + q,
        "image": "/api/files/image?path=" + q,
    }


@route("GET", "/api/os/media/library")
def library(req: Request) -> tuple[int, dict[str, Any]]:
    allowed = roots()
    which = req.arg("root").strip().lower()
    if which:
        if which not in allowed:
            raise ApiError(400, "unknown folder; one of: " + ", ".join(sorted(allowed)))
        base = allowed[which]
        if not base.is_dir():
            return 200, {"ok": True, "root": which, "path": str(base), "exists": False, "files": [], "truncated": False}
        try:
            entries = [p for p in base.iterdir() if p.is_file() and kind_of(p.suffix) and not p.name.startswith(".")]
        except OSError as exc:
            raise ApiError(403, f"cannot read {which}: {exc.strerror or exc}") from None
        rows = []
        for p in entries:
            try:
                rows.append(_describe(p))
            except OSError:
                continue
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return 200, {"ok": True, "root": which, "path": str(base), "exists": True,
                     "files": rows[:LIST_CAP], "truncated": len(rows) > LIST_CAP, "total": len(rows)}
    recent = []
    for raw in _read_recent():
        try:
            p = check_path(raw)
            recent.append(_describe(p))
        except (ApiError, OSError):
            continue
    return 200, {"ok": True, "recent": recent,
                 "roots": [{"name": n, "path": str(b), "exists": b.is_dir()} for n, b in allowed.items()],
                 "ffmpeg": _ffmpeg_ready(), "formats": formats()}


def formats() -> dict[str, list[str]]:
    conv_a, conv_v = _convert_exts()
    fmt = lambda s: sorted(e.lstrip(".") for e in s)  # noqa: E731
    return {"audio": fmt(AUDIO_EXTS), "video": fmt(VIDEO_EXTS), "image": fmt(IMAGE_EXTS),
            "convert_audio": fmt(conv_a), "convert_video": fmt(conv_v)}


def _ffmpeg_ready() -> bool:
    """Whether the package that carries ffmpeg is installed. It is NOT run: the
    package's own check executes the binary with no timeout, and a binary that
    hangs (it does on a machine whose first-run security scan has not finished)
    would hang this read. Whether it works is learned when a file is probed."""
    try:
        return importlib.util.find_spec("imageio_ffmpeg") is not None
    except (ImportError, ValueError):
        return False


PROBE_DEADLINE_S = 8.0


def _probe_bounded(target: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """probe() and the sidecar lookup on a worker thread with a deadline, so an
    ffmpeg that never answers costs the owner eight seconds, not the request."""
    from dourmouse.media_convert import probe, sidecar_subtitles

    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["info"] = probe(target)
            box["subs"] = [{"label": s["label"], "url": "/api/files/subtitle.vtt?path=" + urllib.parse.quote(s["path"])}
                           for s in sidecar_subtitles(target)]
        except Exception as exc:  # noqa: BLE001 -- the file can still play without a probe
            box["info"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    worker = threading.Thread(target=work, daemon=True, name="media-probe")
    worker.start()
    worker.join(PROBE_DEADLINE_S)
    if worker.is_alive():
        return {"ok": False, "error": f"ffmpeg did not answer within {int(PROBE_DEADLINE_S)} seconds"}, []
    return box.get("info") or {"ok": False, "error": "no probe result"}, box.get("subs") or []


@route("POST", "/api/os/media/open")
def open_file(req: Request) -> tuple[int, dict[str, Any]]:
    body = req.body if isinstance(req.body, dict) else {}
    target = check_path(body.get("path"))
    row = _describe(target)
    conv_a, conv_v = _convert_exts()
    needs_convert = target.suffix.lower() in (conv_a | conv_v)
    info: dict[str, Any] = {"ok": False, "error": "not probed"}
    subtitles: list[dict[str, str]] = []
    if row["kind"] in ("audio", "video"):
        info, subtitles = _probe_bounded(target)
    with _lock:
        paths = [p for p in _read_recent() if p != row["path"]]
        paths.insert(0, row["path"])
        with contextlib.suppress(OSError):  # the recent list is a convenience; playing does not depend on it
            _write_recent(paths)
    return 200, {"ok": True, **row, "needs_convert": needs_convert, "urls": _urls(target),
                 "subtitles": subtitles,
                 "probe": {k: info.get(k) for k in ("video", "audio", "duration")} if info.get("ok") else None,
                 "probe_error": None if info.get("ok") else str(info.get("error") or "")[:200]}
