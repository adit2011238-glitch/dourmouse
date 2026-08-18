"""Roster handlers must never hand transport detail to the user.

Regression origin: asking for "the score of Bangladesh vs Australia" routed
into the markets agent (there is no sports tool), Yahoo 404'd on the
non-ticker symbol, and chat showed:

    QUOTE FAILED (reported honestly): HTTP 404 from
    https://query1.finance.yahoo.com/v8/finance/chart/BANGLADESH?...

These tests pin the handler boundary: honest, actionable, no status codes,
no URLs, and the raw detail preserved in logs/errors.log.
"""

from __future__ import annotations

import urllib.error

import pytest

from dourmouse import general_roster, obs

HANDLERS = {
    "stock_quote": general_roster._stock_quote_tool,
    "market_movers": general_roster._market_movers_tool,
    "news_headlines": general_roster._news_headlines_tool,
}

ARGS = {
    "stock_quote": {"symbol": "BANGLADESH"},
    "market_movers": {"direction": "gainers"},
    "news_headlines": {"max_results": 5},
}

LIVE_FEED_FN = {
    "stock_quote": "stock_quote",
    "market_movers": "market_movers",
    "news_headlines": "news_headlines",
}


@pytest.fixture(autouse=True)
def _logs_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("DOURMOUSE_OBS_DISABLED", raising=False)
    return tmp_path


def _break_feed(monkeypatch, fn_name: str, exc: BaseException):
    from dourmouse import live_feeds

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(live_feeds, fn_name, boom)


@pytest.mark.parametrize("tool", sorted(HANDLERS))
def test_handler_never_leaks_status_code_or_url(tool, monkeypatch):
    _break_feed(
        monkeypatch,
        LIVE_FEED_FN[tool],
        RuntimeError("HTTP 404 from https://query1.finance.yahoo.com/v8/finance/chart/X"),
    )

    out = HANDLERS[tool](ARGS[tool])

    assert "404" not in out, f"{tool} leaked a status code: {out}"
    assert "https://" not in out, f"{tool} leaked a URL: {out}"
    assert "yahoo" not in out.lower(), f"{tool} leaked the vendor: {out}"
    assert out.strip()


@pytest.mark.parametrize("tool", sorted(HANDLERS))
def test_handler_still_reports_failure_rather_than_fabricating(tool, monkeypatch):
    """Honesty is the invariant that must survive the readability fix."""
    _break_feed(monkeypatch, LIVE_FEED_FN[tool], RuntimeError("HTTP 500 from https://x.test/y"))

    out = HANDLERS[tool](ARGS[tool]).lower()

    assert "couldn't" in out or "could not" in out or "not authorised" in out


@pytest.mark.parametrize("tool", sorted(HANDLERS))
def test_raw_detail_survives_in_the_error_log(tool, monkeypatch):
    _break_feed(
        monkeypatch,
        LIVE_FEED_FN[tool],
        RuntimeError("HTTP 404 from https://query1.finance.yahoo.com/chart/X"),
    )

    HANDLERS[tool](ARGS[tool])

    rows = obs.read_recent("errors.log")
    assert len(rows) == 1
    assert rows[0]["source"] == tool
    assert rows[0]["kind"] == "not_found"
    assert "query1.finance.yahoo.com" in rows[0]["detail"]


def test_non_ticker_quote_points_the_user_at_web_search(monkeypatch):
    """The actual fix for the reported bug: name the wrong-tool case."""
    _break_feed(
        monkeypatch,
        "stock_quote",
        RuntimeError("HTTP 404 from https://query1.finance.yahoo.com/chart/BANGLADESH"),
    )

    out = general_roster._stock_quote_tool({"symbol": "BANGLADESH"})

    assert "web_search" in out
    assert "BANGLADESH" in out


def test_offline_reads_as_offline_not_as_missing_data(monkeypatch):
    """Distinguishing these is the difference between 'retry' and 'give up'."""
    import socket

    _break_feed(
        monkeypatch,
        "stock_quote",
        urllib.error.URLError(socket.gaierror("nodename nor servname provided")),
    )

    out = general_roster._stock_quote_tool({"symbol": "AAPL"}).lower()
    assert "network" in out
    assert obs.read_recent("errors.log")[0]["kind"] == "offline"


# --------------------------------------------------------------------------- #
# web_search / fetch_url
# --------------------------------------------------------------------------- #

def test_web_search_survives_a_non_integer_max_results():
    """The model sometimes sends "five"; that used to raise ValueError."""
    out = general_roster._web_search_tool({"query": "x", "max_results": "five"})
    assert out.startswith("ERROR:")
    assert "integer" in out


def test_web_search_total_failure_is_clean_and_logged(monkeypatch):
    import socket

    def dead(*a, **k):
        raise urllib.error.URLError(socket.gaierror("no dns"))

    monkeypatch.setattr(general_roster, "_brave_search", dead)
    monkeypatch.setattr(general_roster, "_duckduckgo_search", dead)
    monkeypatch.setattr(general_roster, "_wikipedia_search", dead)

    out = general_roster._web_search_tool({"query": "bangladesh vs australia score"})

    assert "https://" not in out
    assert "Traceback" not in out
    assert "network" in out.lower()

    row = obs.read_recent("errors.log")[0]
    assert row["source"] == "web_search"
    # Per-engine detail is kept for debugging even though chat never sees it.
    assert len(row["extra"]["engine_errors"]) == 3


def test_fetch_url_failure_is_clean_and_logged(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.HTTPError("https://example.test/x", 404, "nf", None, None)

    monkeypatch.setattr(general_roster.urllib.request, "urlopen", boom)

    out = general_roster._fetch_url_tool({"url": "https://example.test/x"})

    assert "404" not in out
    assert obs.read_recent("errors.log")[0]["kind"] == "not_found"


def test_successful_calls_write_no_error_rows(monkeypatch):
    """Guard against the log filling with false positives."""
    from dourmouse import live_feeds

    monkeypatch.setattr(
        live_feeds,
        "stock_quote",
        lambda sym: {
            "symbol": sym, "price": 1.0, "currency": "USD", "day_low": 1.0,
            "day_high": 1.0, "week52_low": 1.0, "week52_high": 1.0,
        },
    )

    out = general_roster._stock_quote_tool({"symbol": "AAPL"})
    assert "QUOTE AAPL" in out
    assert obs.read_recent("errors.log") == []
