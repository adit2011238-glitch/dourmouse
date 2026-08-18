"""news_search — the tool that should have answered the cricket question.

There was no roster tool for "what happened with X", so live questions about
sport and current events routed into stock_quote and died on a Yahoo 404.
These tests cover the feed parsing, the handler contract, and the routing
metadata that keeps the model from making that mistake again.
"""

from __future__ import annotations

import pytest

from dourmouse import general_roster, live_feeds, obs

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Australia beat Bangladesh by 8 wickets</title>
    <source url="https://espn.test">ESPNcricinfo</source>
    <pubDate>Fri, 15 Aug 2026 09:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Bangladesh collapse in Dhaka</title>
    <source url="https://bbc.test">BBC Sport</source>
    <pubDate>Fri, 15 Aug 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""


@pytest.fixture(autouse=True)
def _logs_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("DOURMOUSE_OBS_DISABLED", raising=False)


@pytest.fixture
def feed(monkeypatch):
    """Capture the URL requested and serve canned RSS."""
    seen: dict[str, str] = {}

    def fake_get(url, **_k):
        seen["url"] = url
        return RSS

    monkeypatch.setattr(live_feeds, "_http_get", fake_get)
    return seen


# --------------------------------------------------------------------------- #
# live_feeds.news_search
# --------------------------------------------------------------------------- #

def test_parses_rows_from_the_feed(feed):
    rows = live_feeds.news_search("Bangladesh vs Australia")

    assert len(rows) == 2
    assert rows[0]["title"] == "Australia beat Bangladesh by 8 wickets"
    assert rows[0]["source"] == "ESPNcricinfo"
    assert rows[0]["published"].startswith("Fri, 15 Aug 2026")


def test_query_is_url_encoded_into_the_search_endpoint(feed):
    live_feeds.news_search("Bangladesh vs Australia & score")

    assert "/rss/search?q=" in feed["url"]
    assert " " not in feed["url"]
    assert "%20" in feed["url"] or "+" in feed["url"]
    assert "%26" in feed["url"]  # the & must not become a query separator


def test_max_results_is_clamped_to_a_sane_range(feed):
    assert len(live_feeds.news_search("x", 1)) == 1
    assert len(live_feeds.news_search("x", 0)) == 1      # floor
    assert len(live_feeds.news_search("x", 9999)) == 2   # capped by feed size


def test_empty_query_is_refused_before_any_fetch(monkeypatch):
    def must_not_run(*_a, **_k):
        raise AssertionError("fetched despite an empty query")

    monkeypatch.setattr(live_feeds, "_http_get", must_not_run)

    with pytest.raises(RuntimeError, match="non-empty"):
        live_feeds.news_search("   ")


def test_no_results_raises_rather_than_returning_empty(monkeypatch):
    empty = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
    monkeypatch.setattr(live_feeds, "_http_get", lambda *a, **k: empty)

    with pytest.raises(RuntimeError, match="no news results"):
        live_feeds.news_search("nothing at all")


def test_malformed_xml_raises_a_named_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_http_get", lambda *a, **k: "<not xml")

    with pytest.raises(RuntimeError, match="could not parse"):
        live_feeds.news_search("x")


def test_headlines_and_search_share_the_parser(monkeypatch):
    """Regression guard for the refactor that extracted _parse_rss_items."""
    monkeypatch.setattr(live_feeds, "_http_get", lambda *a, **k: RSS)

    headlines = live_feeds.news_headlines(2)
    results = live_feeds.news_search("x", 2)

    assert headlines == results


# --------------------------------------------------------------------------- #
# roster handler
# --------------------------------------------------------------------------- #

def test_handler_formats_sourced_dated_lines(feed):
    out = general_roster._news_search_tool({"query": "Bangladesh vs Australia"})

    assert "Australia beat Bangladesh by 8 wickets" in out
    assert "ESPNcricinfo" in out
    assert "Bangladesh vs Australia" in out


def test_handler_rejects_empty_query():
    out = general_roster._news_search_tool({"query": "  "})
    assert out.startswith("ERROR:")


def test_handler_rejects_non_integer_max_results():
    out = general_roster._news_search_tool({"query": "x", "max_results": "ten"})
    assert out.startswith("ERROR:")
    assert "integer" in out


def test_handler_failure_is_clean_and_logged(monkeypatch):
    monkeypatch.setattr(
        live_feeds,
        "news_search",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("HTTP 503 from https://news.google.com/rss/search?q=x")
        ),
    )

    out = general_roster._news_search_tool({"query": "x"})

    assert "503" not in out
    assert "https://" not in out
    assert obs.read_recent("errors.log")[0]["source"] == "news_search"


# --------------------------------------------------------------------------- #
# routing metadata — why the misroute happened
# --------------------------------------------------------------------------- #

def test_tool_is_registered_on_the_news_agent():
    registry = general_roster.build_general_registry()
    news = registry.get_subagent("news")
    names = {t.name for t in news.tools}

    assert "news_search" in names
    assert "news_headlines" in names


def test_description_steers_sport_queries_away_from_stock_quote():
    """The model picked stock_quote because nothing else claimed the job."""
    registry = general_roster.build_general_registry()
    spec = next(
        t for t in registry.get_subagent("news").tools if t.name == "news_search"
    )
    desc = spec.description.lower()

    assert "score" in desc
    assert "stock_quote" in desc
    assert spec.parameters["required"] == ["query"]
