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
PDF_EXTS = {".pdf"}
LIST_CAP = 100
QUEUE_CAP = 200
HIGHLIGHT_CAP = 500
HIGHLIGHT_COLORS = ("yellow", "green", "blue", "pink")
NOTE_CAP = 500
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
    if ext in PDF_EXTS:
        return "pdf"
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
        raise ApiError(415, f"MEDIA plays audio, video and images and reads PDFs; {target.suffix or 'this file type'} is none of those. "
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
        # PDFium renders one page per request (the existing routes); the screen
        # appends the 0-based page number to pdf_page.
        "pdf_info": "/api/files/pdf-info?path=" + q,
        "pdf_page": "/api/files/pdf-page.png?path=" + q + "&page=",
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
    return {"audio": fmt(AUDIO_EXTS), "video": fmt(VIDEO_EXTS), "image": fmt(IMAGE_EXTS), "pdf": fmt(PDF_EXTS),
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
    extra: dict[str, Any] = {}
    if row["kind"] == "pdf":
        extra = _pdf_facts(target)
    with _lock:
        paths = [p for p in _read_recent() if p != row["path"]]
        paths.insert(0, row["path"])
        with contextlib.suppress(OSError):  # the recent list is a convenience; playing does not depend on it
            _write_recent(paths)
    return 200, {"ok": True, **row, **extra, "needs_convert": needs_convert, "urls": _urls(target),
                 "subtitles": subtitles,
                 "probe": {k: info.get(k) for k in ("video", "audio", "duration")} if info.get("ok") else None,
                 "probe_error": None if info.get("ok") else str(info.get("error") or "")[:200]}


def _pdf_facts(target: Path) -> dict[str, Any]:
    """Page count (real PDFium read) and the highlights already stored for this
    file. A PDF PDFium cannot open reports the reason instead of a page count."""
    out: dict[str, Any] = {"page_count": None, "pdf_error": None, "highlights": _highlights_for(str(target))}
    try:
        from dourmouse.pdf_reader import pdf_info

        info = pdf_info(target)
    except Exception as exc:  # noqa: BLE001 -- the reader must say why, not crash the open
        info = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if info.get("ok"):
        out["page_count"] = info.get("page_count")
    else:
        out["pdf_error"] = str(info.get("error") or "PDFium could not read this file")[:200]
    return out


# ---------------------------------------------------------------------------
# PDF highlights: an overlay list stored beside the recent list. A highlight is
# a rectangle in page-relative units (0..1 of the rendered page's width and
# height) because the reader draws PDFium page images, not Chromium's own PDF
# viewer, so there is no text layer to anchor to.
# ---------------------------------------------------------------------------


def _highlights_file() -> Path:
    from dourmouse.config import workspace_dir

    return workspace_dir() / "media_highlights.json"


def _read_highlights() -> dict[str, list[dict[str, Any]]]:
    try:
        data = json.loads(_highlights_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: [h for h in v if isinstance(h, dict)] for k, v in data.items() if isinstance(k, str) and isinstance(v, list)}


def _write_json(dest: Path, payload: Any, prefix: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, dest)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _highlights_for(path: str) -> list[dict[str, Any]]:
    return _read_highlights().get(path, [])


def _unit(value: Any, name: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"rect.{name} must be a number between 0 and 1") from None
    if not 0.0 <= num <= 1.0:
        raise ApiError(400, f"rect.{name} must be between 0 and 1")
    return round(num, 5)


def _clean_rect(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ApiError(400, "rect is required: {x, y, w, h} as fractions of the page")
    rect = {k: _unit(raw.get(k), k) for k in ("x", "y", "w", "h")}
    if rect["w"] < 0.005 or rect["h"] < 0.003:
        raise ApiError(400, "that highlight is too small to be a selection")
    if rect["x"] + rect["w"] > 1.0001 or rect["y"] + rect["h"] > 1.0001:
        raise ApiError(400, "the highlight runs off the page")
    return rect


def _pdf_target(raw: Any) -> Path:
    target = check_path(raw)
    if kind_of(target.suffix) != "pdf":
        raise ApiError(415, "highlights belong to PDF files")
    return target


@route("GET", "/api/os/media/highlights")
def highlights_list(req: Request) -> tuple[int, dict[str, Any]]:
    target = _pdf_target(req.arg("path"))
    return 200, {"ok": True, "path": str(target), "highlights": _highlights_for(str(target))}


@route("POST", "/api/os/media/highlights")
def highlights_write(req: Request) -> tuple[int, dict[str, Any]]:
    """``{path, op: "add", page, rect, color?, note?}``, ``{path, op: "remove", id}``
    or ``{path, op: "clear"}``. Returns the file's whole list afterwards."""
    body = req.body if isinstance(req.body, dict) else {}
    target = _pdf_target(body.get("path"))
    key = str(target)
    op = str(body.get("op") or "add").strip().lower()
    if op not in ("add", "remove", "clear"):
        raise ApiError(400, "op must be add, remove or clear")
    with _lock:
        everything = _read_highlights()
        mine = list(everything.get(key, []))
        if op == "add":
            try:
                page = int(body.get("page"))
            except (TypeError, ValueError):
                raise ApiError(400, "page must be a whole number (0 is the first page)") from None
            if not 0 <= page < 100000:
                raise ApiError(400, "page is out of range")
            if len(mine) >= HIGHLIGHT_CAP:
                raise ApiError(409, f"this file already has {HIGHLIGHT_CAP} highlights; remove some first")
            color = str(body.get("color") or "yellow").strip().lower()
            if color not in HIGHLIGHT_COLORS:
                raise ApiError(400, "color must be one of: " + ", ".join(HIGHLIGHT_COLORS))
            note = str(body.get("note") or "").strip()[:NOTE_CAP]
            import time
            import uuid

            mine.append({"id": uuid.uuid4().hex[:12], "page": page, "rect": _clean_rect(body.get("rect")),
                         "color": color, "note": note, "created": time.time()})
        elif op == "remove":
            hid = str(body.get("id") or "")
            if not any(h.get("id") == hid for h in mine):
                raise ApiError(404, "no highlight with that id")
            mine = [h for h in mine if h.get("id") != hid]
        else:
            mine = []
        if mine:
            everything[key] = mine
        else:
            everything.pop(key, None)
        try:
            _write_json(_highlights_file(), everything, ".media_highlights.")
        except OSError as exc:
            raise ApiError(500, f"could not save the highlights: {exc.strerror or exc}") from None
    return 200, {"ok": True, "path": key, "highlights": mine}


# ---------------------------------------------------------------------------
# Queue: an ordered list of audio and video paths, saved so it survives a
# restart. Every read re-checks each path, so a moved file drops out honestly.
# ---------------------------------------------------------------------------


def _queue_file() -> Path:
    from dourmouse.config import workspace_dir

    return workspace_dir() / "media_queue.json"


def _read_queue() -> list[str]:
    try:
        data = json.loads(_queue_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("paths") if isinstance(data, dict) else None
    return [p for p in items if isinstance(p, str)][:QUEUE_CAP] if isinstance(items, list) else []


def _queue_rows(paths: list[str]) -> list[dict[str, Any]]:
    rows = []
    for raw in paths:
        try:
            rows.append(_describe(check_path(raw)))
        except (ApiError, OSError):
            continue
    return rows


@route("GET", "/api/os/media/queue")
def queue_read(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "queue": _queue_rows(_read_queue())}


@route("POST", "/api/os/media/queue")
def queue_write(req: Request) -> tuple[int, dict[str, Any]]:
    """``{op: "add", path}`` (appends, no duplicates), ``{op: "remove", path}``,
    ``{op: "move", path, to}`` (``to`` is the new 0-based index) or ``{op: "clear"}``."""
    body = req.body if isinstance(req.body, dict) else {}
    op = str(body.get("op") or "").strip().lower()
    if op not in ("add", "remove", "move", "clear"):
        raise ApiError(400, "op must be add, remove, move or clear")
    with _lock:
        paths = [r["path"] for r in _queue_rows(_read_queue())]
        if op == "add":
            target = check_path(body.get("path"))
            if kind_of(target.suffix) not in ("audio", "video"):
                raise ApiError(415, "the queue holds audio and video; images and PDFs are opened directly")
            if str(target) not in paths:
                if len(paths) >= QUEUE_CAP:
                    raise ApiError(409, f"the queue is full ({QUEUE_CAP}); remove something first")
                paths.append(str(target))
        elif op in ("remove", "move"):
            raw = str(body.get("path") or "")
            try:
                key = str(Path(raw).expanduser().resolve())
            except (OSError, RuntimeError):
                raise ApiError(400, "that is not a usable file path") from None
            if key not in paths:
                raise ApiError(404, "that file is not in the queue")
            paths.remove(key)
            if op == "move":
                try:
                    to = int(body.get("to"))
                except (TypeError, ValueError):
                    raise ApiError(400, "to must be a whole number index") from None
                paths.insert(max(0, min(to, len(paths))), key)
        else:
            paths = []
        try:
            _write_json(_queue_file(), {"paths": paths}, ".media_queue.")
        except OSError as exc:
            raise ApiError(500, f"could not save the queue: {exc.strerror or exc}") from None
    return 200, {"ok": True, "queue": _queue_rows(paths)}


# ---------------------------------------------------------------------------
# Player state: what the MEDIA screen reports about itself, so something other
# than the screen (a tool, a status line) can ask what is playing. Held in
# memory only, with the time it was reported: a screen that was closed leaves
# a stale record, and the age says so instead of pretending it is live.
# ---------------------------------------------------------------------------

_player_state: dict[str, Any] = {}


def _num(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return round(num, 3) if num == num and num not in (float("inf"), float("-inf")) and num >= 0 else None


@route("POST", "/api/os/media/player-state")
def player_state_write(req: Request) -> tuple[int, dict[str, Any]]:
    import time

    body = req.body if isinstance(req.body, dict) else {}
    path = str(body.get("path") or "")[:MAX_PATH]
    state = {
        "path": path or None,
        "name": Path(path).name if path else None,
        "kind": body.get("kind") if body.get("kind") in ("audio", "video", "image", "pdf") else None,
        "playing": bool(body.get("playing")) and bool(path),
        "position": _num(body.get("position")),
        "duration": _num(body.get("duration")),
        "page": body.get("page") if isinstance(body.get("page"), int) and not isinstance(body.get("page"), bool) else None,
        "reported_at": time.time(),
    }
    with _lock:
        _player_state.clear()
        _player_state.update(state)
    return 200, {"ok": True}


@route("GET", "/api/os/media/player-state")
def player_state_read(req: Request) -> tuple[int, dict[str, Any]]:
    import time

    with _lock:
        state = dict(_player_state)
    if not state:
        return 200, {"ok": True, "reported": False, "note": "the MEDIA screen has not reported anything since the server started"}
    return 200, {"ok": True, "reported": True, **state, "age_s": round(time.time() - state["reported_at"], 1)}
