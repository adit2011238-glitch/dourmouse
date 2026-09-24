"""Play every media format (OS-10, finding #117).

A browser's <video>/<audio> decodes H.264/VP8/VP9/AV1 video and AAC/MP3/
Opus/Vorbis/FLAC/PCM audio, in mp4/webm/ogg containers. Everything else
(mkv, avi, wmv, flv, HEVC in any container, AC3/DTS/WMA audio, aiff, ...)
used to get an honest "cannot be played here". Now it is converted with
ffmpeg (the bundled binary from imageio-ffmpeg; one pip dependency, no
system install) into something the browser plays, with the cheapest route
first:

- **remux** when the streams are already browser-playable and only the
  container is not (most .mkv files are H.264 + AAC): stream copy into mp4,
  seconds, no quality change;
- **transcode** only the stream that needs it (HEVC -> H.264, AC3 -> AAC),
  in the background, with progress;
- the result is cached by the file's path, size and mtime, so a second play
  is instant, and fed to the existing byte-range player.

Anything ffmpeg itself cannot read is reported as such, never a blank player.
Sidecar subtitles (.srt/.vtt next to the file) are found and served as WebVTT.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir

#: Played as they are (kept in step with webui._PREVIEWABLE_MEDIA_EXTS).
NATIVE_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".oga", ".ogg", ".opus", ".weba",
               ".mp4", ".m4v", ".webm", ".ogv", ".mov"}
#: Played after conversion.
CONVERT_VIDEO_EXTS = {".mkv", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".3gp", ".vob",
                      ".divx", ".asf", ".f4v"}
CONVERT_AUDIO_EXTS = {".flac", ".wma", ".aiff", ".aif", ".ac3", ".dts", ".amr", ".ape", ".mka", ".alac", ".caf"}
CONVERT_EXTS = CONVERT_VIDEO_EXTS | CONVERT_AUDIO_EXTS

BROWSER_VIDEO = {"h264", "vp8", "vp9", "av1"}
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 -- not installed or no binary for this platform
        return None


def cache_dir() -> Path:
    d = workspace_dir() / "media_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(src: Path) -> str:
    st = src.stat()
    return hashlib.sha256(f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:24]


def probe(src: Path) -> dict[str, Any]:
    """Streams, duration and codecs, read from ffmpeg's own report."""
    exe = ffmpeg_exe()
    if exe is None:
        return {"ok": False, "error": "ffmpeg is not available (pip install imageio-ffmpeg)"}
    proc = subprocess.run([exe, "-hide_banner", "-i", str(src)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, check=False)
    text = proc.stderr
    video = re.findall(r"Stream #\d+:\d+(?:\[\w+\])?(?:\(\w+\))?: Video: (\w+)", text)
    audio = re.findall(r"Stream #\d+:\d+(?:\[\w+\])?(?:\(\w+\))?: Audio: (\w+)", text)
    subs = re.findall(r"Stream #\d+:(\d+)(?:\[\w+\])?(?:\((\w+)\))?: Subtitle: (\w+)", text)
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    if not video and not audio:
        last = [ln.strip() for ln in text.splitlines() if ln.strip()][-1:] or ["no streams found"]
        return {"ok": False, "error": f"ffmpeg cannot read this file: {last[0]}"}
    return {
        "ok": True, "video": [v.lower() for v in video], "audio": [a.lower() for a in audio],
        "subtitles": [{"index": int(i), "lang": lang or "", "codec": c} for i, lang, c in subs],
        "duration": (int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3))) if dur else None,
    }


def plan(info: dict[str, Any]) -> dict[str, Any]:
    """How to make it playable: which streams are copied and which re-encoded."""
    has_video = bool(info["video"])
    v_copy = has_video and info["video"][0] in BROWSER_VIDEO
    a_copy = not info["audio"] or info["audio"][0] in BROWSER_AUDIO
    if has_video:
        args = ["-map", "0:v:0", "-map", "0:a:0?"]
        args += ["-c:v", "copy"] if v_copy else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"]
        args += ["-c:a", "copy"] if a_copy else ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        args += ["-movflags", "+faststart", "-f", "mp4"]
        return {"out_ext": ".mp4", "content_type": "video/mp4", "args": args,
                "route": "remux" if (v_copy and a_copy) else "transcode"}
    copy_aac = info["audio"][0] == "aac"
    args = ["-map", "0:a:0", "-vn"] + (["-c:a", "copy"] if copy_aac else ["-c:a", "aac", "-b:a", "256k"])
    args += ["-movflags", "+faststart", "-f", "mp4"]
    return {"out_ext": ".m4a", "content_type": "audio/mp4", "args": args,
            "route": "remux" if copy_aac else "transcode"}


def _run(key: str, src: Path, out: Path, p: dict[str, Any], duration: float | None) -> None:
    job = _jobs[key]
    tmp = out.with_suffix(out.suffix + ".part")
    cmd = [ffmpeg_exe() or "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src), *p["args"],
           "-progress", "pipe:1", "-nostats", str(tmp)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace")
        job["pid"] = proc.pid
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("out_time_us=") and duration:
                with contextlib.suppress(ValueError):  # "N/A" before the first frame
                    job["progress"] = min(0.99, int(line.split("=", 1)[1]) / 1e6 / duration)
        err = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()
        if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            job.update(state="ready", progress=1.0, finished_at=time.time())
        else:
            tail = [ln for ln in err.splitlines() if ln.strip()][-3:]
            job.update(state="failed", error="ffmpeg could not convert it: " + " / ".join(tail))
            tmp.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 -- recorded on the job, never lost
        job.update(state="failed", error=f"{type(exc).__name__}: {exc}")
        tmp.unlink(missing_ok=True)


def ensure_playable(src: Path, *, wait_s: float = 0.0) -> dict[str, Any]:
    """Start (or find) the conversion for ``src``. Returns the job: state
    ready (with ``path``), converting (with ``progress``), or failed."""
    ext = src.suffix.lower()
    if ext not in CONVERT_EXTS:
        return {"state": "failed", "error": f"{ext} is not a convertible media format"}
    key = _key(src)
    with _lock:
        job = _jobs.get(key)
        if job is None:
            info = probe(src)
            if not info["ok"]:
                job = {"state": "failed", "error": info["error"]}
                _jobs[key] = job
                return dict(job)
            p = plan(info)
            out = cache_dir() / f"{key}{p['out_ext']}"
            job = {"state": "ready" if out.exists() else "converting", "route": p["route"], "path": str(out),
                   "content_type": p["content_type"], "progress": 1.0 if out.exists() else 0.0,
                   "started_at": time.time(), "streams": {"video": info["video"], "audio": info["audio"]},
                   "duration": info["duration"]}
            _jobs[key] = job
            if job["state"] == "converting":
                threading.Thread(target=_run, args=(key, src, out, p, info["duration"]), daemon=True,
                                 name=f"media-{key[:8]}").start()
    deadline = time.monotonic() + wait_s
    while job["state"] == "converting" and time.monotonic() < deadline:
        time.sleep(0.1)
    return dict(job)


# ------------------------------------------------------------------ subtitles

def _srt_to_vtt(text: str) -> str:
    body = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text.replace("\r\n", "\n").lstrip("﻿"))
    return "WEBVTT\n\n" + body


def sidecar_subtitles(src: Path) -> list[dict[str, str]]:
    """Subtitle files next to the media: movie.srt, movie.en.srt, movie.vtt."""
    out = []
    for f in sorted(src.parent.glob(src.stem + "*")):
        if f.suffix.lower() in (".srt", ".vtt") and f.is_file():
            label = f.name[len(src.stem):].rsplit(".", 1)[0].strip(".") or "default"
            out.append({"path": str(f), "label": label})
    return out


def subtitle_vtt(sub: Path) -> str:
    text = sub.read_text(encoding="utf-8", errors="replace")
    return text if sub.suffix.lower() == ".vtt" else _srt_to_vtt(text)
