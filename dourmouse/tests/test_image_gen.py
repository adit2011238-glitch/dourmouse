"""dourmouse/image_gen.py — real image generation via Gemini, fake
transport.

Same hermetic discipline as test_gemini_backend.py (Rule 2.1): every test
injects a transport callable instead of touching the network. This
machine DOES have a real GEMINI_API_KEY configured (unlike when
gemini_backend.py's own tests were written) -- the autouse fixture below
strips it from the environment for every test in this file, so nothing
here can accidentally reach the real API through a real key.

What IS grounded in a real network call, done once by hand while writing
this module (not by any test here): a real GET /v1beta/models listing
confirmed gemini-3.1-flash-image exists for this account, and a real
POST to it came back with a real, well-formed Gemini error envelope --
HTTP 429 RESOURCE_EXHAUSTED, "free_tier_requests, limit: 0" -- proving
the request shape below is at least accepted well enough to be
quota-evaluated. The SUCCESS path (parsing a real inlineData image) has
NOT been exercised against a real 200 response; this account has zero
image-generation quota. The fake success responses below follow Google's
published REST shape for this model family, same honest caveat
gemini_backend.py's own tests already carry for its own untested-live
paths.
"""

from __future__ import annotations

import base64
import json

import pytest

from dourmouse import image_gen
from dourmouse.config import GEMINI_ENV_KEYS


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    for name in GEMINI_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-a-real-credential")
    return "test-key-not-a-real-credential"


#: A tiny real 1x1 PNG, base64-encoded -- small enough to embed literally,
#: real enough that base64.b64decode + Path.write_bytes round-trip it
#: exactly like a real generated image would.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)

#: The real 429 body captured live against this account (see module
#: docstring) -- used as fixture data for the honest-error test, not
#: reconstructed from memory.
_REAL_429_BODY = json.dumps({
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and "
            "billing details. Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_"
            "requests, limit: 0, model: gemini-3.1-flash-image"
        ),
        "status": "RESOURCE_EXHAUSTED",
    }
}).encode("utf-8")


class _RecordingTransport:
    """Fake transport capturing exactly what the module tried to send,
    returning either a canned success body or raising the given error --
    mirrors test_gemini_backend.py's own _RecordingTransport."""

    def __init__(self, body: bytes | None = None, error: Exception | None = None):
        self._body = body
        self._error = error
        self.url: str | None = None
        self.data: bytes | None = None
        self.headers: dict[str, str] = {}
        self.timeout: float | None = None
        self.calls = 0

    def __call__(self, url, *, data, headers, timeout):
        self.calls += 1
        self.url = url
        self.data = data
        self.headers = dict(headers)
        self.timeout = timeout
        if self._error is not None:
            raise self._error
        return self._body

    @property
    def sent_body(self) -> dict:
        assert self.data is not None
        return json.loads(self.data.decode("utf-8"))


def _success_response(*, mime: str = "image/png", data_b64: str = _TINY_PNG_B64) -> bytes:
    return json.dumps({
        "candidates": [
            {
                "content": {"parts": [{"inlineData": {"mimeType": mime, "data": data_b64}}]},
                "finishReason": "STOP",
            }
        ]
    }).encode("utf-8")


class TestNotConfigured:
    def test_no_key_raises_honest_not_configured(self):
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            image_gen.generate_image({"prompt": "a red circle"}, transport=_RecordingTransport())

    def test_no_key_never_sends_a_request(self):
        transport = _RecordingTransport()
        with pytest.raises(RuntimeError):
            image_gen.generate_image({"prompt": "a red circle"}, transport=transport)
        assert transport.calls == 0


class TestMissingPrompt:
    def test_blank_prompt_is_an_honest_error_not_a_request(self, with_key):
        transport = _RecordingTransport()
        out = image_gen.generate_image({"prompt": ""}, transport=transport)
        assert "ERROR" in out
        assert "prompt" in out
        assert transport.calls == 0


class TestRequestShape:
    def test_key_travels_in_the_header_never_the_url(self, with_key, tmp_path):
        transport = _RecordingTransport(body=_success_response())
        image_gen.generate_image({"prompt": "a red circle"}, transport=transport, images_dir=tmp_path)
        assert "key=" not in transport.url
        assert transport.headers["x-goog-api-key"] == with_key

    def test_default_model_targets_the_real_confirmed_model(self, with_key, tmp_path):
        transport = _RecordingTransport(body=_success_response())
        image_gen.generate_image({"prompt": "a red circle"}, transport=transport, images_dir=tmp_path)
        assert transport.url.endswith(f"/models/{image_gen.DEFAULT_IMAGE_MODEL}:generateContent")

    def test_explicit_model_overrides_the_default(self, with_key, tmp_path):
        transport = _RecordingTransport(body=_success_response())
        image_gen.generate_image(
            {"prompt": "a red circle", "model": "gemini-3-pro-image"},
            transport=transport, images_dir=tmp_path,
        )
        assert transport.url.endswith("/models/gemini-3-pro-image:generateContent")

    def test_response_modalities_requests_an_image(self, with_key, tmp_path):
        transport = _RecordingTransport(body=_success_response())
        image_gen.generate_image({"prompt": "a red circle"}, transport=transport, images_dir=tmp_path)
        assert transport.sent_body["generationConfig"]["responseModalities"] == ["IMAGE"]
        assert transport.sent_body["contents"][0]["parts"][0]["text"] == "a red circle"


class TestSuccessPath:
    """Follows Google's published REST shape for this model family -- see
    the module and file docstrings for the honest live-verification
    boundary (request shape confirmed live, success parsing is not)."""

    def test_saves_a_real_file_and_returns_a_markdown_image_link(self, with_key, tmp_path):
        transport = _RecordingTransport(body=_success_response())
        out = image_gen.generate_image({"prompt": "a red circle"}, transport=transport, images_dir=tmp_path)
        assert "IMAGE saved:" in out
        assert "![a red circle](/api/images/generated?name=" in out
        saved = list(tmp_path.glob("*.png"))
        assert len(saved) == 1
        assert saved[0].read_bytes() == base64.b64decode(_TINY_PNG_B64)

    def test_jpeg_mime_type_gets_a_jpg_extension(self, with_key, tmp_path):
        transport = _RecordingTransport(body=_success_response(mime="image/jpeg"))
        out = image_gen.generate_image({"prompt": "a photo"}, transport=transport, images_dir=tmp_path)
        assert list(tmp_path.glob("*.jpg")), out

    def test_repeat_prompts_never_overwrite_a_prior_image(self, with_key, tmp_path):
        transport = _RecordingTransport(body=_success_response())
        image_gen.generate_image({"prompt": "same prompt"}, transport=transport, images_dir=tmp_path)
        image_gen.generate_image({"prompt": "same prompt"}, transport=transport, images_dir=tmp_path)
        assert len(list(tmp_path.glob("*.png"))) == 2

    def test_filename_is_sandboxed_against_a_hostile_prompt(self, with_key, tmp_path):
        transport = _RecordingTransport(body=_success_response())
        image_gen.generate_image(
            {"prompt": "../../../etc/passwd; rm -rf /"}, transport=transport, images_dir=tmp_path,
        )
        saved = list(tmp_path.glob("*.png"))
        assert len(saved) == 1
        assert saved[0].parent == tmp_path
        assert ".." not in saved[0].name


class TestHonestErrors:
    def test_a_real_429_surfaces_the_providers_own_message(self, with_key, tmp_path):
        """Real fixture data, not invented: this exact body was returned
        live by Google's API for this exact model on this account's real
        key (see module docstring)."""
        transport = _RecordingTransport(
            error=RuntimeError(
                "Gemini API HTTP 429 Too Many Requests: " + _REAL_429_BODY.decode("utf-8")
            )
        )
        with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
            image_gen.generate_image({"prompt": "x"}, transport=transport, images_dir=tmp_path)

    def test_no_candidates_is_an_honest_error(self, with_key, tmp_path):
        transport = _RecordingTransport(body=json.dumps({"candidates": []}).encode("utf-8"))
        with pytest.raises(RuntimeError, match="no candidates"):
            image_gen.generate_image({"prompt": "x"}, transport=transport, images_dir=tmp_path)

    def test_blocked_prompt_reports_the_real_block_reason(self, with_key, tmp_path):
        transport = _RecordingTransport(body=json.dumps({
            "candidates": [],
            "promptFeedback": {"blockReason": "SAFETY"},
        }).encode("utf-8"))
        with pytest.raises(RuntimeError, match="SAFETY"):
            image_gen.generate_image({"prompt": "x"}, transport=transport, images_dir=tmp_path)

    def test_a_text_only_response_with_no_image_part_is_an_honest_error(self, with_key, tmp_path):
        transport = _RecordingTransport(body=json.dumps({
            "candidates": [{"content": {"parts": [{"text": "I cannot draw that."}]}, "finishReason": "STOP"}],
        }).encode("utf-8"))
        with pytest.raises(RuntimeError, match="no image"):
            image_gen.generate_image({"prompt": "x"}, transport=transport, images_dir=tmp_path)
        assert not list(tmp_path.glob("*"))


class TestResolveGeneratedImage:
    def test_finds_a_real_saved_image(self, tmp_path, monkeypatch):
        monkeypatch.setattr(image_gen, "IMAGES_DIR", tmp_path)
        (tmp_path / "a-red-circle.png").write_bytes(b"fake-png-bytes")
        found = image_gen.resolve_generated_image("a-red-circle.png")
        assert found == tmp_path / "a-red-circle.png"

    def test_missing_file_returns_none_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(image_gen, "IMAGES_DIR", tmp_path)
        assert image_gen.resolve_generated_image("nope.png") is None

    def test_blank_name_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(image_gen, "IMAGES_DIR", tmp_path)
        assert image_gen.resolve_generated_image("") is None

    def test_path_traversal_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(image_gen, "IMAGES_DIR", tmp_path)
        outside = tmp_path.parent / "secret.png"
        outside.write_bytes(b"not yours")
        try:
            assert image_gen.resolve_generated_image("../secret.png") is None
        finally:
            outside.unlink(missing_ok=True)
