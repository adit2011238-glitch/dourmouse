"""Backend for the wallpaper setting (plan 1.2, finding 197).

Adit picks photos from their own files. The page sends the bytes, the server checks that they
really are a JPEG, PNG or WebP, shrinks them to at most 2560 px on the long edge, re-saves them
as JPEG (which also drops any EXIF block, so a photo's location never stays on disk) and keeps
the result under ``<workspace>/wallpapers/``. The page gets the picture back as a data URL, so no
file path from the Mac and no new static route is involved, and the shell's CSP (img-src data:)
already allows it.

Three slots exist: ``light`` and ``dark`` (one photo each) and ``rotation`` (up to 12 photos, one
shown per day). ``config.json`` holds the mode (off, single, rotate) and the places the photo
shows behind (default Home, Projects and Office).

Nothing here talks to the network or to a model.
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import io
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import ApiError, Request, route

SLOTS = ("light", "dark", "rotation")
MODES = ("off", "single", "rotate")
PLACES = ("HOME", "PROJECTS", "OFFICE", "RESEARCH", "LIBRARY", "BROWSER", "SECURITY", "SETTINGS")
DEFAULT_PLACES = ("HOME", "PROJECTS", "OFFICE")
ROTATION_MAX = 12
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_EDGE = 2560
_JPEG_QUALITY = 85
_lock = threading.RLock()


def _dir() -> Path:
    from dourmouse.config import workspace_dir

    return workspace_dir() / "wallpapers"


def _sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _default_config() -> dict[str, Any]:
    return {"mode": "off", "places": list(DEFAULT_PLACES)}


def _clean_config(raw: Any) -> dict[str, Any]:
    cfg = _default_config()
    if not isinstance(raw, dict):
        return cfg
    if raw.get("mode") in MODES:
        cfg["mode"] = raw["mode"]
    places = raw.get("places")
    if isinstance(places, list):
        cfg["places"] = [p for p in PLACES if p in places]
    return cfg


def _read_config() -> dict[str, Any]:
    path = _dir() / "config.json"
    try:
        return _clean_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return _default_config()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rotation_files() -> list[Path]:
    folder = _dir() / "rotation"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if re.fullmatch(r"\d{3}\.jpg", p.name))


def _slot_file(slot: str) -> Path | None:
    if slot in ("light", "dark"):
        path = _dir() / f"{slot}.jpg"
        return path if path.is_file() else None
    files = _rotation_files()
    if not files:
        return None
    # one photo per calendar day, in file order, so it holds still until midnight
    return files[_dt.date.today().toordinal() % len(files)]


def _reencode(data: bytes) -> bytes:
    """Shrink and re-save as JPEG. Pillow is required: without it a photo is refused rather than
    stored with its EXIF block (location) intact."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ApiError(501, "Pillow is not installed, so photos cannot be prepared safely") from exc
    try:
        Image.MAX_IMAGE_PIXELS = 120_000_000
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            if img.mode != "RGB":
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, "JPEG", quality=_JPEG_QUALITY, optimize=True)
            return out.getvalue()
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - a corrupt or oversized image is the caller's problem
        raise ApiError(400, "that file could not be read as a photo") from exc


def _summary() -> dict[str, Any]:
    base = _dir()
    return {
        "config": _read_config(),
        "light": (base / "light.jpg").is_file(),
        "dark": (base / "dark.jpg").is_file(),
        "rotation": len(_rotation_files()),
        "rotation_max": ROTATION_MAX,
        "places": list(PLACES),
        "default_places": list(DEFAULT_PLACES),
    }


@route("GET", "/api/os/wallpaper")
def get_wallpaper(request: Request) -> tuple[int, dict[str, Any]]:
    with _lock:
        return 200, _summary()


@route("GET", "/api/os/wallpaper/image")
def get_image(request: Request) -> tuple[int, dict[str, Any]]:
    slot = request.arg("slot")
    if slot not in SLOTS:
        raise ApiError(400, "slot must be light, dark or rotation")
    with _lock:
        path = _slot_file(slot)
        if path is None:
            return 200, {"slot": slot, "data_url": None}
        data = path.read_bytes()
    return 200, {"slot": slot, "data_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}


@route("POST", "/api/os/wallpaper/upload")
def upload(request: Request) -> tuple[int, dict[str, Any]]:
    slot = str(request.body.get("slot") or "")
    if slot not in SLOTS:
        raise ApiError(400, "slot must be light, dark or rotation")
    raw = request.body.get("data")
    if not isinstance(raw, str) or not raw:
        raise ApiError(400, "data is required (base64 of the photo)")
    if len(raw) > MAX_UPLOAD_BYTES * 4 // 3 + 16:
        raise ApiError(413, "that photo is larger than 25 MB")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(400, "data is not valid base64") from exc
    if _sniff(data) is None:
        raise ApiError(415, "only JPEG, PNG and WebP photos are supported")
    jpeg = _reencode(data)
    with _lock:
        if slot == "rotation":
            files = _rotation_files()
            if len(files) >= ROTATION_MAX:
                raise ApiError(409, f"the rotation holds at most {ROTATION_MAX} photos")
            used = {int(p.stem) for p in files}
            number = next(n for n in range(ROTATION_MAX) if n not in used)
            _write_atomic(_dir() / "rotation" / f"{number:03d}.jpg", jpeg)
        else:
            _write_atomic(_dir() / f"{slot}.jpg", jpeg)
        cfg = _read_config()
        if cfg["mode"] == "off":
            cfg["mode"] = "rotate" if slot == "rotation" else "single"
            _write_atomic(_dir() / "config.json", json.dumps(cfg).encode("utf-8"))
        return 200, {"ok": True, **_summary()}


@route("POST", "/api/os/wallpaper/config")
def set_config(request: Request) -> tuple[int, dict[str, Any]]:
    body = request.body
    if "mode" in body and body["mode"] not in MODES:
        raise ApiError(400, "mode must be off, single or rotate")
    if "places" in body:
        places = body["places"]
        if not isinstance(places, list) or any(p not in PLACES for p in places):
            raise ApiError(400, "places must be a list of place ids")
    with _lock:
        cfg = _read_config()
        if "mode" in body:
            cfg["mode"] = body["mode"]
        if "places" in body:
            cfg["places"] = [p for p in PLACES if p in body["places"]]
        _write_atomic(_dir() / "config.json", json.dumps(cfg).encode("utf-8"))
        return 200, {"ok": True, **_summary()}


@route("POST", "/api/os/wallpaper/remove")
def remove(request: Request) -> tuple[int, dict[str, Any]]:
    slot = str(request.body.get("slot") or "")
    if slot not in SLOTS:
        raise ApiError(400, "slot must be light, dark or rotation")
    with _lock:
        if slot == "rotation":
            for p in _rotation_files():
                p.unlink(missing_ok=True)
        else:
            (_dir() / f"{slot}.jpg").unlink(missing_ok=True)
        base = _dir()
        if not (base / "light.jpg").is_file() and not (base / "dark.jpg").is_file() and not _rotation_files():
            cfg = _read_config()
            cfg["mode"] = "off"
            _write_atomic(base / "config.json", json.dumps(cfg).encode("utf-8"))
        return 200, {"ok": True, **_summary()}
