"""Failures must be honest AND actionable — no transport detail in chat."""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error

import pytest

from dourmouse import net_errors, obs
from dourmouse.net_errors import ErrorKind


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://query1.finance.yahoo.com/v8/finance/chart/BANGLADESH",
        code=code,
        msg="err",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# --------------------------------------------------------------------------- #
# classify
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "code,expected",
    [
        (404, ErrorKind.NOT_FOUND),
        (410, ErrorKind.NOT_FOUND),
        (429, ErrorKind.RATE_LIMITED),
        (401, ErrorKind.AUTH),
        (403, ErrorKind.AUTH),
        (500, ErrorKind.SERVER_ERROR),
        (503, ErrorKind.SERVER_ERROR),
    ],
)
def test_http_status_maps_to_kind(code, expected):
    assert net_errors.classify(_http_error(code)) is expected


def test_classifies_the_stringified_runtimeerror_live_feeds_raises():
    # live_feeds._http_get collapses HTTPError into this shape before any
    # roster handler sees it, so classification must survive the round trip.
    msg = "HTTP 404 from https://query1.finance.yahoo.com/v8/finance/chart/X"
    assert net_errors.classify(msg) is ErrorKind.NOT_FOUND
    assert net_errors.http_status(msg) == 404


def test_timeout_and_offline_and_tls_are_distinguished():
    assert net_errors.classify(TimeoutError("timed out")) is ErrorKind.TIMEOUT
    assert net_errors.classify(ssl.SSLError("handshake")) is ErrorKind.TLS
    assert net_errors.classify(urllib.error.URLError(socket.gaierror("no dns"))) is ErrorKind.OFFLINE
    assert net_errors.classify(ConnectionRefusedError("refused")) is ErrorKind.OFFLINE


def test_parse_failures_are_their_own_kind():
    assert net_errors.classify(json.JSONDecodeError("bad", "{", 0)) is ErrorKind.PARSE
    assert net_errors.classify("non-JSON response from https://x") is ErrorKind.PARSE


def test_unknown_stays_unknown_rather_than_guessing():
    assert net_errors.classify(RuntimeError("something odd")) is ErrorKind.UNKNOWN


def test_retryable_only_for_transient_kinds():
    assert net_errors.is_retryable(ErrorKind.TIMEOUT)
    assert net_errors.is_retryable(ErrorKind.RATE_LIMITED)
    assert net_errors.is_retryable(ErrorKind.SERVER_ERROR)
    assert not net_errors.is_retryable(ErrorKind.NOT_FOUND)
    assert not net_errors.is_retryable(ErrorKind.AUTH)


# --------------------------------------------------------------------------- #
# friendly — the regression the user actually hit
# --------------------------------------------------------------------------- #

def test_friendly_message_leaks_no_status_code_or_url():
    """The reported bug: chat showed 'HTTP 404 from https://query1...'."""
    raw = "HTTP 404 from https://query1.finance.yahoo.com/v8/finance/chart/BANGLADESH"
    msg = net_errors.friendly(raw, what="a quote for BANGLADESH")

    assert "404" not in msg
    assert "http" not in msg.lower()
    assert "yahoo" not in msg.lower()
    assert "a quote for BANGLADESH" in msg


def test_friendly_carries_the_suggested_next_step():
    msg = net_errors.friendly(
        _http_error(404),
        what="a score for Bangladesh vs Australia",
        suggestion="Let me try a web search instead.",
    )
    assert "web search" in msg
    assert "404" not in msg


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_every_kind_renders_a_sentence_naming_the_thing(kind):
    """No kind may fall through to an empty or placeholder-leaking message."""
    template = net_errors._TEMPLATES[kind]
    rendered = template.format(what="the thing")
    assert rendered.strip()
    assert "{" not in rendered
    assert "the thing" in rendered
    assert rendered.rstrip().endswith(".")


# --------------------------------------------------------------------------- #
# report — friendly out, raw detail to the log
# --------------------------------------------------------------------------- #

def test_report_returns_friendly_text_and_logs_raw_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("DOURMOUSE_OBS_DISABLED", raising=False)

    raw = "HTTP 404 from https://query1.finance.yahoo.com/v8/finance/chart/BANGLADESH"
    msg = net_errors.report(raw, what="a quote for BANGLADESH", source="stock_quote")

    # User-facing: clean.
    assert "404" not in msg
    assert "yahoo" not in msg.lower()

    # Log: complete.
    rows = obs.read_recent("errors.log")
    assert len(rows) == 1
    assert rows[0]["source"] == "stock_quote"
    assert rows[0]["kind"] == "not_found"
    assert rows[0]["status"] == 404
    assert rows[0]["retryable"] is False
    assert "query1.finance.yahoo.com" in rows[0]["detail"]
