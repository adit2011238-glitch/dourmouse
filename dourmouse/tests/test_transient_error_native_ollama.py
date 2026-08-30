"""dispatch.py _is_transient_error — native Ollama transport errors (v13.2).

Live-reproduced bug: _is_transient_error only ever recognized the `openai`
SDK's own exception hierarchy (RateLimitError, APIConnectionError,
APITimeoutError, InternalServerError). _OllamaNativeClient (the "cloud"
backend badge in the UI) talks to Ollama's /api/chat directly via bare
urllib.request (see its own _default_post/_default_post_lines) -- a
completely different exception hierarchy that isinstance() against
openai.* classes never matches.

Observed live: mid-turn, after the model had already streamed a full
answer and called a tool, Ollama Cloud returned a real transient 500.
_call_with_retry_inner classified it as non-transient and `raise`d on the
FIRST attempt (zero retries, no fallback) -- the whole turn crashed with
an empty persisted transcript (dourmouse/webui.py's session ledger showed
final_text="" / transcript=[] for that turn despite the UI having already
rendered the full answer from the live SSE stream) and a raw
"HTTP Error 500: Internal Server Error" surfaced to the user -- exactly
str(urllib.error.HTTPError(..., 500, "Internal Server Error", ...)),
confirmed byte-for-byte against a real HTTPError instance below.
"""

from __future__ import annotations

import socket
import urllib.error

from dourmouse.dispatch import _is_transient_error


def _http_error(code: int, reason: str = "error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/api/chat", code, reason, {}, None)


class TestNativeOllamaTransportErrorsAreTransient:
    def test_5xx_http_error_is_transient(self):
        for code in (500, 502, 503, 504):
            assert _is_transient_error(_http_error(code)), code

    def test_429_http_error_is_transient(self):
        assert _is_transient_error(_http_error(429, "Too Many Requests"))

    def test_real_500_message_matches_the_live_bug_report(self):
        # The exact string a user saw live, unaltered — confirms this is
        # the SAME exception shape, not a lookalike.
        exc = _http_error(500, "Internal Server Error")
        assert str(exc) == "HTTP Error 500: Internal Server Error"
        assert _is_transient_error(exc)

    def test_4xx_http_error_is_not_transient(self):
        # Auth/malformed-request failures never get better on retry --
        # must still fail loudly (module docstring's own stated contract).
        for code in (400, 401, 403, 404):
            assert not _is_transient_error(_http_error(code)), code

    def test_bare_urlerror_is_transient(self):
        # DNS failure / connection refused / no HTTP status at all.
        assert _is_transient_error(urllib.error.URLError("connection refused"))

    def test_socket_timeout_is_transient(self):
        assert _is_transient_error(socket.timeout("timed out"))

    def test_connection_error_is_transient(self):
        assert _is_transient_error(ConnectionError("reset by peer"))


class TestOpenAiTransientClassificationUnchanged:
    """The pre-existing openai.* classification must survive untouched."""

    def test_rate_limit_error_still_transient(self):
        import openai

        exc = openai.RateLimitError("rate limited", response=_FakeResponse(), body=None)
        assert _is_transient_error(exc)

    def test_generic_value_error_still_not_transient(self):
        assert not _is_transient_error(ValueError("bad argument"))


class _FakeResponse:
    status_code = 429
    headers: dict = {}
    request = None
