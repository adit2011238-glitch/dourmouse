"""Secure self-update feed for the DOURMOUSE desktop app (v5.19, Phase 6).

One honest version-check surface: ``GET /api/version`` reports the app's
CURRENT version plus the latest release from a signed ``latest.json`` feed
served over HTTPS. The feed carries the release artifact's SHA-256; a
downloaded artifact is only accepted when its hash matches — never a
silently corrupted or tampered payload (Rule 2.6). "Signed" today means
HTTPS transport + content hash; an ed25519 signature over the feed is a
documented follow-up once a real release channel exists.

Config (env, read at call time):

- ``DOURMOUSE_UPDATE_FEED`` — https URL of ``latest.json``. Unset means NO
  update channel: ``/api/version`` honestly reports ``configured:false``
  and never fabricates a version.
- ``DOURMOUSE_UPDATE_CHANNEL`` — ``stable`` | ``beta`` (default ``stable``).
- ``DOURMOUSE_APP_VERSION`` — optional override of the reported current
  version (defaults to ``dourmouse.__version__``).

Deterministic (Rule 2.8): every feed field is validated (channel on the
allow-list, artifact url https, sha256 exactly 64 hex chars); anything
malformed is an honest error, never a guess. The feed check is cached per
URL with a TTL so the HUD never blocks on the network repeatedly.

``stage_artifact`` downloads + hash-verifies a release and stages it for
apply-on-restart (the OS-specific apply itself is the desktop shell's job,
Phase 6 of the portfolio). The previous staged release is kept under
``previous/`` rather than deleted — the rollback seat.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CHANNELS = frozenset({"stable", "beta"})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
#: Re-check the feed at most once per 6h per URL (never a per-request fetch).
_FEED_TTL_SECONDS = 6 * 3600
_DOWNLOAD_TIMEOUT = 60.0
#: Sanity cap for staged release archives (streamed, checked mid-download —
#: the Drive bounded-reader lesson applied to release artifacts).
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_CHUNK = 64 * 1024

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: feed url -> (fetched_at, validated payload | None, error | None). Both
#: successes AND failures are cached (a dead feed must not re-block every
#: /api/version call for the full timeout). Bounded: one entry per URL.
_feed_cache: dict[str, tuple[float, dict[str, Any] | None, str | None]] = {}


@dataclass(frozen=True)
class UpdateInfo:
    """The honest /api/version payload. ``error`` carries the real reason
    when the feed is configured but unreachable/invalid — never a guess."""

    configured: bool
    current: str
    channel: str
    latest: str | None = None
    url: str | None = None
    sha256: str | None = None
    notes: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "configured": self.configured,
            "current": self.current,
            "channel": self.channel,
        }
        if self.latest is not None:
            out["latest"] = self.latest
        if self.url is not None:
            out["url"] = self.url
        if self.sha256 is not None:
            out["sha256"] = self.sha256
        if self.notes is not None:
            out["notes"] = self.notes
        if self.error is not None:
            out["error"] = self.error
        return out


def current_version() -> str:
    """The reported current app version (env override else package version)."""
    raw = os.environ.get("DOURMOUSE_APP_VERSION", "").strip()
    if raw:
        return raw
    from dourmouse import __version__

    return __version__


def feed_url() -> str:
    return os.environ.get("DOURMOUSE_UPDATE_FEED", "").strip()


def update_channel() -> str:
    raw = os.environ.get("DOURMOUSE_UPDATE_CHANNEL", "stable").strip().lower()
    return raw if raw in _CHANNELS else "stable"


def _updates_root() -> Path:
    """<workspace>/updates (the staging + rollback seat)."""
    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else _PROJECT_ROOT / "workspace"
    return root / "updates"


def _validate_feed(data: Any) -> dict[str, Any]:
    """Validate a parsed latest.json; raises ValueError with the real reason.

    Deterministic: feed must be a JSON OBJECT, channel on the allow-list,
    https-only artifact url, exactly-64-hex sha256. A feed failing ANY
    check is an honest error — it is never served as if it were a valid
    release (Rule 2.2/2.8). The object guard matters: valid JSON that is a
    list or a string must not blow up with a raw AttributeError later.
    """
    if not isinstance(data, dict):
        raise ValueError("feed must be a JSON object")
    channel = str(data.get("channel") or update_channel()).strip().lower()
    if channel not in _CHANNELS:
        raise ValueError(f"feed channel {channel!r} not allowed (stable|beta)")
    latest = (data.get("version") or "").strip()
    if not latest:
        raise ValueError("feed is missing 'version'")
    url = (data.get("url") or "").strip()
    if not url.startswith("https://"):
        raise ValueError("feed 'url' must be https:// (the artifact is fetched over TLS)")
    sha = (data.get("sha256") or "").strip().lower()
    if not _SHA256_RE.match(sha):
        raise ValueError("feed 'sha256' must be 64 hex characters")
    notes = (data.get("notes") or "").strip()[:400]
    return {"channel": channel, "version": latest, "url": url,
            "sha256": sha, "notes": notes}


def fetch_feed(feed: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    """Fetch + validate the latest.json feed (uncached). Raises on failure
    (ValueError for invalid payloads, OSError/URLError for transport)."""
    url = feed or feed_url()
    if not url:
        raise ValueError("no update feed configured (DOURMOUSE_UPDATE_FEED)")
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 -- caller-opted URL
        data = json.loads(resp.read().decode("utf-8"))
    return _validate_feed(data)


def check_for_updates(timeout: float = 5.0) -> UpdateInfo:
    """The cached /api/version surface. NEVER raises — every failure mode is
    an honest ``error`` field, and an unset feed is ``configured:false``."""
    info = UpdateInfo(configured=False, current=current_version(),
                      channel=update_channel())
    feed = feed_url()
    if not feed:
        return info
    now = time.time()
    payload: dict[str, Any] | None = None
    cached = _feed_cache.get(feed)
    if cached is not None and now - cached[0] < _FEED_TTL_SECONDS:
        payload, error = cached[1], cached[2]
        if error is not None:
            # negative cache hit — replay the honest error without another
            # network attempt (reviewer-caught: a dead feed must not block
            # the shell for the full timeout on every call)
            return UpdateInfo(configured=True, current=info.current,
                              channel=info.channel, error=error)
    else:
        try:
            payload = fetch_feed(feed, timeout=timeout)
        except (ValueError, OSError, urllib.error.URLError,
                json.JSONDecodeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            _feed_cache[feed] = (now, None, error)
            return UpdateInfo(configured=True, current=info.current,
                              channel=info.channel, error=error)
        _feed_cache[feed] = (now, payload, None)
    assert payload is not None  # unreachable: both cache paths set it
    return UpdateInfo(
        configured=True,
        current=info.current,
        channel=payload["channel"],
        latest=payload["version"],
        url=payload["url"],
        sha256=payload["sha256"],
        notes=payload["notes"],
    )


def verify_sha256(data: bytes, expected: str) -> bool:
    """Constant-time SHA-256 check — the artifact gate (Rule 2.6)."""
    digest = hashlib.sha256(data).hexdigest()
    return hmac.compare_digest(digest, (expected or "").strip().lower())


def stage_artifact(
    feed: dict[str, Any], timeout: float = _DOWNLOAD_TIMEOUT
) -> Path:
    """Download + hash-verify a release and stage it for apply-on-restart.

    ``feed`` is an already-validated payload (from ``fetch_feed`` — its
    ``url`` is https and ``sha256`` is 64 hex). The bytes are verified
    BEFORE they are written; a mismatch deletes the partial download and
    raises ``ValueError``. The previous staged release (if any) is moved to
    ``previous/`` rather than deleted — the rollback seat for the shell.

    Returns the staged file path. The OS-specific apply (mount, copy into
    the .app, relaunch) is the desktop shell's job, not this module's.
    """
    version = (feed.get("version") or "").strip()
    url = (feed.get("url") or "").strip()
    sha = (feed.get("sha256") or "").strip().lower()
    if not version or not url or not _SHA256_RE.match(sha):
        raise ValueError("stage_artifact requires a validated feed payload")
    root = _updates_root()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"dourmouse-{version}"
    # Keep the previous staged release instead of deleting it (rollback).
    for old in list(root.glob("dourmouse-*")):
        if old.name == dest.name or old.name.endswith(".part"):
            continue
        if old.is_file():
            prev = root / "previous"
            prev.mkdir(exist_ok=True)
            target = prev / old.name
            target.unlink(missing_ok=True)
            old.rename(target)
    tmp = dest.with_suffix(".part")
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 -- validated https
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_ARTIFACT_BYTES:
                        raise ValueError(
                            f"artifact exceeds {_MAX_ARTIFACT_BYTES} bytes — "
                            "refusing to stage"
                        )
                    digest.update(chunk)
                    fh.write(chunk)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        tmp.unlink(missing_ok=True)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"artifact download failed: {exc}") from exc
    # O(1) memory: the digest was fed during the stream, and the bytes on
    # disk are verified against the feed's sha256 BEFORE the .part is
    # promoted — a mismatch is deleted, never staged.
    if not hmac.compare_digest(digest.hexdigest(), sha):
        tmp.unlink(missing_ok=True)
        raise ValueError(
            f"artifact sha256 mismatch: got {digest.hexdigest()}, expected {sha} — "
            "refusing to stage (tampered or corrupt download)"
        )
    tmp.rename(dest)
    return dest
