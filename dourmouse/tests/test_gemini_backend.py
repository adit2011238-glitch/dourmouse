"""Google AI Studio (Gemini) backend tests — real module, fake transport.

Exercises ``dourmouse.gemini_backend`` end to end WITHOUT ever touching the
network (Rule 2.1 hermetic): every test injects a ``transport`` callable
that yields literal Server-Sent Event lines of the shape Google's REST docs
describe, so the code under test is the real SSE parser, the real request
builder and the real error paths — only the socket is replaced.

What is NOT covered here, stated plainly rather than implied by a green
bar: no test in this file has ever spoken to the live Gemini API. This
machine has no GEMINI_API_KEY / GOOGLE_AI_STUDIO_KEY, so the wire format
these fakes reproduce is taken from Google's published documentation, not
from an observed response.
"""

from __future__ import annotations

import json

import pytest

from dourmouse import gemini_backend
from dourmouse.config import GEMINI_ENV_KEYS, load_gemini_config

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    """This developer's real .env is loaded into os.environ by config.py at
    import time, so a key added there later would silently make the
    "missing key" tests pass for the wrong reason (and, worse, could let a
    non-fake path try to build a real request). Every test starts from a
    genuinely key-free environment and opts in explicitly.
    """
    for name in GEMINI_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-a-real-credential")
    return "test-key-not-a-real-credential"


def _sse(*payloads: dict) -> list[str]:
    """Render payloads as an ``alt=sse`` line stream, blank separators and
    all — the framing a real response actually carries."""
    lines: list[str] = []
    for payload in payloads:
        lines.append(f"data: {json.dumps(payload)}")
        lines.append("")
    return lines


def _text_chunk(text: str, *, finish: str | None = None) -> dict:
    chunk: dict = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "index": 0}
        ],
        "modelVersion": "gemini-3.5-flash",
    }
    if finish:
        chunk["candidates"][0]["finishReason"] = finish
    return chunk


class _RecordingTransport:
    """Fake transport capturing exactly what the module tried to send."""

    def __init__(self, lines: list[str]):
        self._lines = lines
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
        return iter(self._lines)

    @property
    def body(self) -> dict:
        assert self.data is not None
        return json.loads(self.data.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Honest NOT CONFIGURED with no key (the real state of this machine)
# --------------------------------------------------------------------------- #

def test_gemini_configured_is_false_without_a_key():
    assert gemini_backend.gemini_configured() is False


def test_status_reports_missing_key_with_a_hint():
    status = gemini_backend.gemini_status()
    assert status["ok"] is False
    assert "GEMINI_API_KEY" in status["detail"]
    assert "MISSING" in status["detail"]
    assert status["hint"]
    # connections.py's exact contract shape — nothing more, nothing less.
    assert set(status) == {"ok", "detail", "hint"}


def test_status_never_leaks_the_key(with_key):
    status = gemini_backend.gemini_status()
    assert status["ok"] is True
    assert with_key not in json.dumps(status)
    assert "GEMINI_API_KEY" in status["detail"]


def test_status_names_the_fallback_env_var_when_that_is_the_one_set(monkeypatch):
    monkeypatch.setenv("GOOGLE_AI_STUDIO_KEY", "fallback-key")
    status = gemini_backend.gemini_status()
    assert status["ok"] is True
    assert "GOOGLE_AI_STUDIO_KEY" in status["detail"]


def test_gemini_api_key_wins_over_the_fallback(monkeypatch):
    monkeypatch.setenv("GOOGLE_AI_STUDIO_KEY", "fallback-key")
    monkeypatch.setenv("GEMINI_API_KEY", "primary-key")
    assert load_gemini_config().api_key == "primary-key"
    assert gemini_backend._key_source() == "GEMINI_API_KEY"


def test_stream_without_a_key_raises_not_configured_and_sends_nothing():
    transport = _RecordingTransport(_sse(_text_chunk("hi")))
    with pytest.raises(RuntimeError) as excinfo:
        gemini_backend.stream_gemini(
            "hello", on_delta=lambda _t: None, transport=transport
        )
    message = str(excinfo.value)
    assert message.startswith("NOT CONFIGURED:")
    assert "GEMINI_API_KEY" in message
    assert "GOOGLE_AI_STUDIO_KEY" in message
    # The honest part: no request was even attempted.
    assert transport.calls == 0


def test_call_without_a_key_raises_not_configured_and_sends_nothing():
    transport = _RecordingTransport(_sse(_text_chunk("hi")))
    with pytest.raises(RuntimeError) as excinfo:
        gemini_backend.call_gemini("hello", transport=transport)
    assert str(excinfo.value).startswith("NOT CONFIGURED:")
    assert transport.calls == 0


# --------------------------------------------------------------------------- #
# A successful stream: real deltas, real usage
# --------------------------------------------------------------------------- #

def test_stream_emits_every_delta_and_returns_the_joined_text(with_key):
    transport = _RecordingTransport(
        _sse(
            _text_chunk("Hello"),
            _text_chunk(", "),
            _text_chunk("world", finish="STOP"),
        )
    )
    deltas: list[str] = []
    result = gemini_backend.stream_gemini(
        "greet me", on_delta=deltas.append, transport=transport
    )
    assert deltas == ["Hello", ", ", "world"]
    assert result == "Hello, world"


def test_stream_routes_thought_parts_to_on_thinking_not_on_delta(with_key):
    chunk = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "weighing the options", "thought": True},
                        {"text": "42"},
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }
    transport = _RecordingTransport(_sse(chunk))
    deltas: list[str] = []
    thoughts: list[str] = []
    result = gemini_backend.stream_gemini(
        "answer",
        on_delta=deltas.append,
        on_thinking=thoughts.append,
        transport=transport,
    )
    assert deltas == ["42"]
    assert thoughts == ["weighing the options"]
    # The reasoning must not leak into the answer — the exact bug the
    # brevity fix and personality_profile both hit on other backends.
    assert result == "42"


def test_usage_uses_usage_trackers_key_names_and_fires_exactly_once(with_key):
    """Gemini repeats a GROWING usageMetadata on successive chunks, and
    usage_tracker's recorders INCREMENT a persisted total — so on_usage
    must fire once, with the last (complete) accounting, or the user's
    usage bar inflates by the number of frames."""
    first = _text_chunk("part one")
    first["usageMetadata"] = {
        "promptTokenCount": 11,
        "candidatesTokenCount": 3,
        "totalTokenCount": 14,
    }
    second = _text_chunk(" and two", finish="STOP")
    second["usageMetadata"] = {
        "promptTokenCount": 11,
        "candidatesTokenCount": 7,
        "totalTokenCount": 18,
    }
    transport = _RecordingTransport(_sse(first, second))
    seen: list[dict] = []
    gemini_backend.stream_gemini(
        "two parts",
        on_delta=lambda _t: None,
        on_usage=seen.append,
        transport=transport,
    )
    assert len(seen) == 1
    assert seen[0] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }


def test_usage_dict_is_accepted_by_usage_tracker_unchanged(with_key, tmp_path, monkeypatch):
    """The shape contract, enforced against the REAL recorder rather than
    asserted about in a comment: whatever on_usage hands back must be
    something usage_tracker already accepts, with no translation."""
    from dourmouse import usage_tracker

    monkeypatch.setattr(usage_tracker, "_usage_path", lambda: tmp_path / "usage.json")
    chunk = _text_chunk("ok", finish="STOP")
    chunk["usageMetadata"] = {
        "promptTokenCount": 5,
        "candidatesTokenCount": 2,
        "totalTokenCount": 7,
    }
    transport = _RecordingTransport(_sse(chunk))
    gemini_backend.stream_gemini(
        "hi",
        on_delta=lambda _t: None,
        on_usage=usage_tracker.record_ollama_usage,
        transport=transport,
    )
    totals = usage_tracker.get_totals()["ollama"]
    assert totals["requests"] == 1
    assert totals["prompt_tokens"] == 5
    assert totals["completion_tokens"] == 2


def test_usage_omits_fields_the_response_did_not_report(with_key):
    """Missing counts are absent, never zero-filled (Rule 2.2)."""
    chunk = _text_chunk("ok", finish="STOP")
    chunk["usageMetadata"] = {"promptTokenCount": 9}
    transport = _RecordingTransport(_sse(chunk))
    seen: list[dict] = []
    gemini_backend.stream_gemini(
        "hi", on_delta=lambda _t: None, on_usage=seen.append, transport=transport
    )
    assert seen == [{"prompt_tokens": 9}]


def test_no_usage_metadata_means_on_usage_never_fires(with_key):
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    seen: list[dict] = []
    gemini_backend.stream_gemini(
        "hi", on_delta=lambda _t: None, on_usage=seen.append, transport=transport
    )
    assert seen == []


def test_a_failing_on_usage_callback_never_breaks_the_real_reply(with_key):
    chunk = _text_chunk("real answer", finish="STOP")
    chunk["usageMetadata"] = {"promptTokenCount": 1, "candidatesTokenCount": 1}
    transport = _RecordingTransport(_sse(chunk))

    def _boom(_usage):
        raise OSError("disk full")

    assert (
        gemini_backend.stream_gemini(
            "hi", on_delta=lambda _t: None, on_usage=_boom, transport=transport
        )
        == "real answer"
    )


# --------------------------------------------------------------------------- #
# What actually goes on the wire
# --------------------------------------------------------------------------- #

def test_request_targets_the_sse_streaming_endpoint_for_the_default_model(with_key):
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.stream_gemini("hi", on_delta=lambda _t: None, transport=transport)
    assert transport.url == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{gemini_backend.GEMINI_DEFAULT_MODEL}:streamGenerateContent?alt=sse"
    )


def test_default_model_is_not_a_shut_down_model_id():
    """gemini-2.0-flash is listed as SHUT DOWN in Google's current model
    docs (read 2026-09-04). Pinning it would 404 on the first real call."""
    assert gemini_backend.GEMINI_DEFAULT_MODEL == "gemini-3.5-flash"


def test_the_key_travels_in_the_header_never_in_the_url(with_key):
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.stream_gemini("hi", on_delta=lambda _t: None, transport=transport)
    assert transport.headers["x-goog-api-key"] == with_key
    # A URL ends up in proxy logs and in this module's own error strings.
    assert with_key not in (transport.url or "")
    assert "key=" not in (transport.url or "")
    assert transport.headers["Accept"] == "text/event-stream"


def test_explicit_model_and_base_url_override_the_defaults(with_key):
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.stream_gemini(
        "hi",
        model="gemini-3.8-flash",
        base_url="https://example.invalid/v1beta",
        on_delta=lambda _t: None,
        transport=transport,
    )
    assert transport.url == (
        "https://example.invalid/v1beta/models/"
        "gemini-3.8-flash:streamGenerateContent?alt=sse"
    )


def test_gemini_model_env_var_selects_the_model(with_key, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.stream_gemini("hi", on_delta=lambda _t: None, transport=transport)
    assert "gemini-3.8-flash:streamGenerateContent" in (transport.url or "")


def test_system_prompt_goes_to_systemInstruction_not_a_system_role_turn(with_key):
    """Gemini has no "system" role; sending one would be rejected."""
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.stream_gemini(
        "hi", system="be terse", on_delta=lambda _t: None, transport=transport
    )
    body = transport.body
    assert body["systemInstruction"] == {"parts": [{"text": "be terse"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert all(turn["role"] != "system" for turn in body["contents"])


def test_streaming_sends_no_max_output_token_cap_at_all(with_key):
    """The anti-truncation rule this repo has paid for three times: with no
    explicit cap, NO maxOutputTokens is sent and the model uses its own
    full ceiling."""
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.stream_gemini("hi", on_delta=lambda _t: None, transport=transport)
    assert "generationConfig" not in transport.body


def test_call_gemini_sends_no_cap_by_default_and_honours_an_explicit_one(with_key):
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.call_gemini("hi", transport=transport)
    assert "generationConfig" not in transport.body

    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.call_gemini("hi", max_tokens=1024, transport=transport)
    assert transport.body["generationConfig"] == {"maxOutputTokens": 1024}


def test_an_explicit_cap_is_clamped_into_the_models_real_range(with_key):
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.call_gemini("hi", max_tokens=10, transport=transport)
    assert transport.body["generationConfig"]["maxOutputTokens"] == 256

    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.call_gemini("hi", max_tokens=10_000_000, transport=transport)
    # gemini-3.5-flash's real documented output limit.
    assert transport.body["generationConfig"]["maxOutputTokens"] == 65_536


def test_timeout_is_clamped_to_the_house_range(with_key):
    transport = _RecordingTransport(_sse(_text_chunk("ok", finish="STOP")))
    gemini_backend.stream_gemini(
        "hi", on_delta=lambda _t: None, timeout=99_999, transport=transport
    )
    assert transport.timeout == 600.0


def test_call_gemini_returns_the_full_text(with_key):
    transport = _RecordingTransport(
        _sse(_text_chunk("one "), _text_chunk("two", finish="STOP"))
    )
    assert gemini_backend.call_gemini("hi", transport=transport) == "one two"


# --------------------------------------------------------------------------- #
# Errors are surfaced verbatim, never swallowed
# --------------------------------------------------------------------------- #

def test_an_http_error_from_the_transport_is_surfaced_verbatim(with_key):
    """The real transport raises RuntimeError carrying the provider's own
    status line and response body. Nothing above it may downgrade that into
    an empty string or a friendlier message."""
    real_error = (
        "Gemini API HTTP 400 Bad Request: "
        '{"error": {"code": 400, "message": "API key not valid. Please pass '
        'a valid API key.", "status": "INVALID_ARGUMENT"}}'
    )

    def _failing_transport(url, *, data, headers, timeout):
        raise RuntimeError(real_error)
        yield  # pragma: no cover - makes this a generator like the real one

    with pytest.raises(RuntimeError) as excinfo:
        gemini_backend.stream_gemini(
            "hi", on_delta=lambda _t: None, transport=_failing_transport
        )
    assert str(excinfo.value) == real_error
    assert "API key not valid" in str(excinfo.value)


def test_call_gemini_also_surfaces_the_http_error_verbatim(with_key):
    def _failing_transport(url, *, data, headers, timeout):
        raise RuntimeError("Gemini API HTTP 429 Too Many Requests: quota exceeded")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="429 Too Many Requests: quota exceeded"):
        gemini_backend.call_gemini("hi", transport=_failing_transport)


def test_the_real_urllib_transport_quotes_the_http_status_and_body(monkeypatch):
    """Exercises _urllib_transport's own error path with a fake urlopen —
    still no network, but the string the rest of the system will actually
    see is produced by the real code, not by a test's stand-in."""
    import io
    import urllib.error

    def _boom(_req, timeout=None):
        # A real HTTPError with a real body file, so exc.read() is the
        # genuine stdlib path rather than a patched-on stand-in.
        raise urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/v1beta/x",
            403,
            "Forbidden",
            {},
            io.BytesIO(
                b'{"error": {"code": 403, "message": "Permission denied."}}'
            ),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    stream = gemini_backend._urllib_transport(
        "https://generativelanguage.googleapis.com/v1beta/x",
        data=b"{}",
        headers={},
        timeout=5.0,
    )
    with pytest.raises(RuntimeError) as excinfo:
        next(stream)
    message = str(excinfo.value)
    assert "HTTP 403 Forbidden" in message
    assert "Permission denied." in message


def test_the_real_urllib_transport_reports_a_network_error_honestly(monkeypatch):
    import urllib.error

    def _boom(_req, timeout=None):
        raise urllib.error.URLError("nodename nor servname provided")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    stream = gemini_backend._urllib_transport(
        "https://generativelanguage.googleapis.com/v1beta/x",
        data=b"{}",
        headers={},
        timeout=5.0,
    )
    with pytest.raises(RuntimeError, match="network error calling Gemini"):
        next(stream)


def test_an_error_frame_inside_the_stream_is_raised_not_ignored(with_key):
    transport = _RecordingTransport(
        _sse(
            _text_chunk("partial"),
            {
                "error": {
                    "code": 500,
                    "status": "INTERNAL",
                    "message": "backend overloaded",
                }
            },
        )
    )
    with pytest.raises(RuntimeError) as excinfo:
        gemini_backend.stream_gemini(
            "hi", on_delta=lambda _t: None, transport=transport
        )
    message = str(excinfo.value)
    assert "500" in message and "INTERNAL" in message
    assert "backend overloaded" in message


def test_a_blocked_prompt_reports_the_real_block_reason(with_key):
    transport = _RecordingTransport(
        _sse({"promptFeedback": {"blockReason": "SAFETY"}})
    )
    with pytest.raises(RuntimeError) as excinfo:
        gemini_backend.stream_gemini(
            "hi", on_delta=lambda _t: None, transport=transport
        )
    assert "blockReason=SAFETY" in str(excinfo.value)
    assert "Nothing was generated" in str(excinfo.value)


def test_an_empty_stream_raises_rather_than_returning_an_empty_string(with_key):
    transport = _RecordingTransport([])
    with pytest.raises(RuntimeError, match="empty stream"):
        gemini_backend.stream_gemini(
            "hi", on_delta=lambda _t: None, transport=transport
        )


def test_no_text_with_a_non_stop_finish_reason_raises_with_that_reason(with_key):
    transport = _RecordingTransport(
        _sse({"candidates": [{"finishReason": "RECITATION", "index": 0}]})
    )
    with pytest.raises(RuntimeError, match="finishReason=RECITATION"):
        gemini_backend.stream_gemini(
            "hi", on_delta=lambda _t: None, transport=transport
        )


def test_a_truncated_generation_is_marked_not_passed_off_as_complete(with_key):
    """Text WAS produced, so it is returned — but the early stop is made
    visible in both the stream and the return value rather than silently
    presented as a finished answer."""
    transport = _RecordingTransport(
        _sse(_text_chunk("a long answer cut off mid-", finish="MAX_TOKENS"))
    )
    deltas: list[str] = []
    result = gemini_backend.stream_gemini(
        "hi", on_delta=deltas.append, transport=transport
    )
    assert "finishReason=MAX_TOKENS" in result
    assert result.startswith("a long answer cut off mid-")
    assert "finishReason=MAX_TOKENS" in deltas[-1]


# --------------------------------------------------------------------------- #
# SSE framing robustness
# --------------------------------------------------------------------------- #

def test_keepalives_comments_and_done_sentinels_are_tolerated(with_key):
    lines = [
        ": keep-alive",
        "",
        "event: message",
        f"data: {json.dumps(_text_chunk('real'))}",
        "",
        "data: [DONE]",
        "",
    ]
    transport = _RecordingTransport(lines)
    assert (
        gemini_backend.stream_gemini(
            "hi", on_delta=lambda _t: None, transport=transport
        )
        == "real"
    )


def test_one_malformed_frame_does_not_abort_a_working_generation(with_key):
    lines = [
        f"data: {json.dumps(_text_chunk('good '))}",
        "",
        "data: {not json at all",
        "",
        f"data: {json.dumps(_text_chunk('text', finish='STOP'))}",
        "",
    ]
    transport = _RecordingTransport(lines)
    assert (
        gemini_backend.stream_gemini(
            "hi", on_delta=lambda _t: None, transport=transport
        )
        == "good text"
    )


def test_data_lines_without_a_space_after_the_colon_still_parse(with_key):
    transport = _RecordingTransport(
        [f"data:{json.dumps(_text_chunk('tight', finish='STOP'))}", ""]
    )
    assert (
        gemini_backend.stream_gemini(
            "hi", on_delta=lambda _t: None, transport=transport
        )
        == "tight"
    )


# --------------------------------------------------------------------------- #
# config.GeminiConfig
# --------------------------------------------------------------------------- #

def test_config_defaults_are_honest_when_nothing_is_set():
    cfg = load_gemini_config()
    assert cfg.api_key == ""
    assert cfg.model == "gemini-3.5-flash"
    assert cfg.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert cfg.agent_models == {}


def test_config_loader_never_raises_on_a_missing_key():
    """Deliberate divergence from load_nvidia_config: a status probe must
    be able to report "no key" without catching an exception."""
    assert load_gemini_config().api_key == ""


def test_per_agent_model_override(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_GEMINI_MODEL_DEV_CODING", "gemini-3.8-flash")
    cfg = load_gemini_config()
    assert cfg.agent_models["DEV_CODING"] == "gemini-3.8-flash"
    assert cfg.model_for_agent("dev_coding") == "gemini-3.8-flash"
    assert cfg.model_for_agent("comms") == cfg.model
    assert cfg.model_for_agent(None) == cfg.model
