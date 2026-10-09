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
import tempfile
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

#: A failed job is served from the cache for this long, then tried again (an
#: installed ffmpeg, freed disk space or a network share coming back should
#: not need an app restart).
_RETRY_FAILED_S = 30.0
#: One conversion may run for at least this long, or this many times the
#: media's duration, before ffmpeg is killed and the job fails.
_MIN_RUN_LIMIT_S = 1800.0
_RUN_LIMIT_X_DURATION = 8.0


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 -- not installed or no binary for this platform
        return None


#: finding #157 N3, defence in depth: a media file's own playlist (.m3u8, .sdp,
#: a concat list) can name other inputs. Recent ffmpeg builds already refuse a
#: network address from a local playlist, but an older or different build might
#: not, and local files, pipes and the crypto and data wrappers are all this
#: feature needs, so the list is stated explicitly.
_FFMPEG_PROTOCOLS = "file,pipe,crypto,data"


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
    proc = subprocess.run([exe, "-hide_banner", "-protocol_whitelist", _FFMPEG_PROTOCOLS, "-i", str(src)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, check=False)
    text = proc.stderr
    video: list[str] = []
    audio: list[str] = []
    for line in text.splitlines():
        m = re.search(r"Stream #\d+:\d+(?:\[\w+\])?(?:\(\w+\))?: (Video|Audio): (\w+)", line)
        if m is None:
            continue
        if m.group(1) == "Audio":
            audio.append(m.group(2))
        elif "(attached pic)" not in line:  # cover art in a .flac/.m4a is not a video track
            video.append(m.group(2))
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
    cmd = [ffmpeg_exe() or "ffmpeg", "-hide_banner", "-nostdin", "-y", "-protocol_whitelist", _FFMPEG_PROTOCOLS, "-i", str(src), *p["args"],
           "-progress", "pipe:1", "-nostats", str(tmp)]
    limit = max(_MIN_RUN_LIMIT_S, (duration or 0.0) * _RUN_LIMIT_X_DURATION)
    timed_out = threading.Event()
    watchdog: threading.Timer | None = None
    try:
        # stderr goes to a file, not a pipe: ffmpeg warnings can exceed the pipe
        # buffer while only stdout is being read, which blocked ffmpeg forever.
        with tempfile.TemporaryFile() as err_file:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_file, text=True, encoding="utf-8",
                                    errors="replace")
            job["pid"] = proc.pid

            def _kill() -> None:
                timed_out.set()
                proc.kill()

            watchdog = threading.Timer(limit, _kill)
            watchdog.daemon = True
            watchdog.start()
            assert proc.stdout is not None
            for line in proc.stdout:
                if line.startswith("out_time_us=") and duration:
                    with contextlib.suppress(ValueError):  # "N/A" before the first frame
                        job["progress"] = min(0.99, int(line.split("=", 1)[1]) / 1e6 / duration)
            code = proc.wait()
            err_file.seek(0)
            err = err_file.read().decode("utf-8", errors="replace")
        if timed_out.is_set():
            job.update(state="failed", failed_at=time.time(), error=f"ffmpeg timed out after {limit:g}s and was stopped")
            tmp.unlink(missing_ok=True)
        elif code == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            job.update(state="ready", progress=1.0, finished_at=time.time())
        else:
            tail = [ln for ln in err.splitlines() if ln.strip()][-3:]
            job.update(state="failed", failed_at=time.time(), error="ffmpeg could not convert it: " + " / ".join(tail))
            tmp.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 -- recorded on the job, never lost
        job.update(state="failed", failed_at=time.time(), error=f"{type(exc).__name__}: {exc}")
        tmp.unlink(missing_ok=True)
    finally:
        if watchdog is not None:
            watchdog.cancel()


def ensure_playable(src: Path, *, wait_s: float = 0.0) -> dict[str, Any]:
    """Start (or find) the conversion for ``src``. Returns the job: state
    ready (with ``path``), converting (with ``progress``), or failed."""
    ext = src.suffix.lower()
    if ext not in CONVERT_EXTS:
        return {"state": "failed", "error": f"{ext} is not a convertible media format"}
    key = _key(src)
    with _lock:
        job = _jobs.get(key)
        if job is not None and job["state"] == "failed" and time.time() - job.get("failed_at", 0.0) >= _RETRY_FAILED_S:
            job = None  # a failure is remembered for a while, not forever
        if job is None:
            # Another request for this file sees this placeholder while the
            # probe runs below, outside the lock.
            job = {"state": "converting", "route": None, "progress": 0.0, "started_at": time.time()}
            _jobs[key] = job
            mine = True
        else:
            mine = False
    if mine:
        try:
            info = probe(src)
        except (subprocess.SubprocessError, OSError) as exc:
            info = {"ok": False, "error": f"ffmpeg could not inspect the file: {type(exc).__name__}: {exc}"}
        if not info["ok"]:
            with _lock:
                if ffmpeg_exe() is None:
                    _jobs.pop(key, None)  # nothing was tried: do not remember it
                    return {"state": "failed", "error": info["error"]}
                job.update(state="failed", failed_at=time.time(), error=info["error"])
            return dict(job)
        p = plan(info)
        out = cache_dir() / f"{key}{p['out_ext']}"
        with _lock:
            job.update(state="ready" if out.exists() else "converting", route=p["route"], path=str(out),
                       content_type=p["content_type"], progress=1.0 if out.exists() else 0.0,
                       streams={"video": info["video"], "audio": info["audio"]}, duration=info["duration"])
            start = job["state"] == "converting"
        if start:
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
    stem = src.stem
    # Not glob(stem + "*"): [ ] ? * in a release name are pattern syntax, and a
    # bare prefix also matched another film's file ("Matrix" / "Matrix Reloaded").
    for f in sorted(src.parent.iterdir()):
        if f.name.startswith(stem + ".") and f.suffix.lower() in (".srt", ".vtt") and f.is_file():
            label = f.name[len(stem):].rsplit(".", 1)[0].strip(".") or "default"
            out.append({"path": str(f), "label": label})
    return out


def subtitle_vtt(sub: Path) -> str:
    text = sub.read_text(encoding="utf-8", errors="replace")
    return text if sub.suffix.lower() == ".vtt" else _srt_to_vtt(text)
