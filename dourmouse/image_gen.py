"""Real image generation via Gemini's native image-output models,
stdlib only.

2026-09-14, user-directed: "give it the ability to display and generate
screenshots and images." Display already works (browser_screenshot's
own markdown-image return text, rendered by console.html's md()).
Generation was the real gap — this module closes it.

Reuses ``gemini_backend.py``'s already-built key/config plumbing
(``_require_key()``, ``GEMINI_DEFAULT_BASE_URL``) rather than adding a
second, drifting copy of Gemini auth — the same "one config source"
discipline that module's own docstring documents (Integration Rule 7).
Saves the decoded image under this module's OWN dedicated directory,
served back by a new ``/api/images/generated`` route in webui.py — the
exact same shape ``browser_agent.py``'s ``_SHOTS_DIR`` /
``/api/browser/screenshot`` already use, not a reuse of the unrelated
user-upload sandbox (uploads are user-provided; generated images are
model-created — kept as two directories on purpose, same separation
this repo already draws between "imported" and "manual" projects).

**Live-verified as far as an account with zero image quota allows**
(2026-09-14): a real, correctly-shaped request against three real image
models on this machine's actual configured GEMINI_API_KEY
(``gemini-3.1-flash-image``, ``gemini-2.5-flash-image``,
``gemini-3-pro-image``, confirmed to exist via a real
``GET /v1beta/models`` call) reached Google's real API and came back
with a real, well-formed error envelope: HTTP 429 RESOURCE_EXHAUSTED,
"Quota exceeded ... free_tier_requests, limit: 0". That confirms the
request shape below is accepted well enough to be quota-evaluated, but
it means the actual SUCCESS path (parsing a real ``inlineData`` image
out of a 200 response) has not been exercised against a live response,
only against Google's own REST documentation for this model family —
the same honest gap ``gemini_backend.py``'s own docstring already
discloses for its text path. This is an account-side quota limit (the
user's own billing/plan to raise), not a code defect — do not chase it
further from here, same standing convention as the Ollama Cloud 401
noted elsewhere in this codebase.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from dourmouse.gemini_backend import GEMINI_DEFAULT_BASE_URL, _require_key

#: Real, live-confirmed-to-exist model for this account (see module
#: docstring) — a non-preview, "flash" tier native image-output model.
#: Not the only one available; kept here (not hardcoded inline below) so
#: a future operator can retarget without hunting through request code.
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IMAGES_DIR = _DATA_DIR / "images" / "generated"

#: Same cap rationale as gemini_backend._ERROR_BODY_CAP — Gemini's error
#: envelope is small JSON; this is only a guard against an intercepting
#: proxy's HTML error page dumping unbounded text into a RuntimeError.
_ERROR_BODY_CAP = 2000

#: Filenames are derived from the prompt; sandboxed to this exact
#: character set (same convention as webui.py's own _UPLOAD_NAME_RE) so a
#: prompt can never smuggle a path separator into the save location.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_name(prompt: str) -> str:
    slug = _SAFE_NAME_RE.sub("-", prompt.strip().lower())[:40].strip("-") or "image"
    return slug


def resolve_generated_image(name: str) -> Path | None:
    """Sandboxed lookup for the /api/images/generated GET route — same
    resolve()+relative_to() re-check webui.py's own _sandboxed_upload_path
    uses, not a bare character allowlist alone, since a generated
    filename (unlike a screenshot's fixed .png) carries a real extension
    and a naive allowlist plus string concatenation is exactly the shape
    of bug a "../" could slip through. Returns None (never raises) for
    anything missing or outside IMAGES_DIR — the route turns that into an
    honest 404, matching latest_screenshot's own contract."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        candidate = (IMAGES_DIR / name).resolve()
        candidate.relative_to(IMAGES_DIR.resolve())
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


def _urllib_transport(url: str, *, data: bytes, headers: dict[str, str], timeout: float) -> bytes:
    """Real, non-streaming POST over stdlib urllib. Same honest-error
    contract as gemini_backend._urllib_transport (an HTTP error, a
    network error, and a timeout each raise a RuntimeError naming what
    actually happened, quoting Google's own real error body) — this is
    the plain request/response sibling of that generator, since
    generateContent without alt=sse returns one JSON object, not a
    stream."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https API host
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = (exc.read() or b"").decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - the status line is still real news
            body = ""
        raise RuntimeError(
            f"Gemini API HTTP {exc.code} {exc.reason}: "
            f"{body[:_ERROR_BODY_CAP].strip() or '(empty response body)'}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error calling Gemini: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"timeout calling Gemini after {timeout}s: {exc}") from exc


def generate_image(
    arguments: dict[str, Any],
    *,
    transport: Callable[..., bytes] = _urllib_transport,
    images_dir: Path | None = None,
) -> str:
    """Generate one real image from a text prompt via Gemini, save it,
    and return a real markdown image link so console.html's existing
    md() renders it inline (no new frontend work — the exact same fix
    browser_screenshot's own return text relies on).

    ``transport``/``images_dir`` are injectable seams for hermetic tests
    (Rule 2.1) — real callers never pass either.
    """
    prompt = (arguments.get("prompt") or "").strip()
    if not prompt:
        return "ERROR: tool 'generate_image' failed: missing required argument(s) prompt."
    key = _require_key()  # raises the honest NOT CONFIGURED RuntimeError when no key is set
    model = (arguments.get("model") or DEFAULT_IMAGE_MODEL).strip() or DEFAULT_IMAGE_MODEL
    url = f"{GEMINI_DEFAULT_BASE_URL}/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }).encode("utf-8")
    # Key travels in the header, never the URL -- same choice
    # gemini_backend.py's own request builder already made (Google
    # documents both as equivalent; a URL query string is one that can
    # end up in a server access log or a proxy's history, a header is
    # not).
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    raw = transport(url, data=body, headers=headers, timeout=120.0)
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Gemini returned an unparseable response: {exc}") from exc
    candidates = data.get("candidates") or []
    if not candidates:
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise RuntimeError(f"Gemini blocked this prompt: {block_reason}")
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:_ERROR_BODY_CAP]}")
    cand = candidates[0]
    finish_reason = cand.get("finishReason")
    parts = (cand.get("content") or {}).get("parts") or []
    image_part = next((p for p in parts if "inlineData" in p), None)
    if image_part is None:
        raise RuntimeError(
            f"Gemini returned no image (finishReason={finish_reason!r}): "
            f"{json.dumps(cand)[:_ERROR_BODY_CAP]}"
        )
    inline = image_part["inlineData"]
    image_bytes = base64.b64decode(inline["data"])
    mime = inline.get("mimeType", "image/png")
    ext = "jpg" if "jpeg" in mime else mime.split("/")[-1] or "png"
    out_dir = images_dir if images_dir is not None else IMAGES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(prompt)
    path = out_dir / f"{safe}.{ext}"
    n = 2
    while path.exists():
        path = out_dir / f"{safe}-{n}.{ext}"
        n += 1
    path.write_bytes(image_bytes)
    api_url = f"/api/images/generated?name={path.name}"
    return f"IMAGE saved: {path}\n\n![{prompt[:80]}]({api_url})"
