"""Live-intelligence tests (v2.3) — news/markets/mail/tasks feeds + wiring.

Exercises the REAL live_feeds module against monkeypatched HTTP (deterministic,
no live network in tests), the honest error paths, the preloaded subagent
tool wiring, and the /api/links neural topology.
"""

from __future__ import annotations

import json

import pytest

from dourmouse import live_feeds
from dourmouse.general_roster import build_general_registry
from dourmouse.webui import build_link_topology

# --------------------------------------------------------------------------- #
# Fixtures — canned HTTP payloads
# --------------------------------------------------------------------------- #

_NEWS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item><title>Markets rally on strong earnings</title>
    <source>Reuters</source><pubDate>Fri, 01 Aug 2026 10:00:00 GMT</pubDate></item>
  <item><title>Tech stocks lead the day</title>
    <source>Bloomberg</source><pubDate>Fri, 01 Aug 2026 09:45:00 GMT</pubDate></item>
</channel></rss>"""


def _quote_payload(sym: str, price: float) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": sym,
                        "currency": "USD",
                        "regularMarketPrice": price,
                        "regularMarketDayHigh": price + 1.0,
                        "regularMarketDayLow": price - 1.0,
                        "fiftyTwoWeekHigh": price + 20.0,
                        "fiftyTwoWeekLow": price - 20.0,
                        "regularMarketTime": 1785528001,
                    }
                }
            ]
        }
    }


def _movers_payload(rows: list[tuple[str, str, float]]) -> dict:
    quotes = []
    for i, (sym, name, price) in enumerate(rows):
        quotes.append(
            {
                "symbol": sym,
                "longName": name,
                "regularMarketPrice": price,
                "regularMarketChange": round(price * 0.03, 2),
                "regularMarketChangePercent": 3.0 if i % 2 == 0 else -3.0,
                "currency": "USD",
            }
        )
    return {"finance": {"result": [{"quotes": quotes}]}}


@pytest.fixture
def fake_http(monkeypatch):
    """Route _http_get by URL substring to canned payloads; fail others."""

    def _install(url, payload):
        def fake(url, *, timeout=15, headers=None):
            return payload

        monkeypatch.setattr(live_feeds, "_http_get", fake)

    return _install


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #

class TestNews:
    def test_news_headlines_parses_rss(self, monkeypatch):
        monkeypatch.setattr(live_feeds, "_http_get", lambda *a, **k: _NEWS_XML)
        items = live_feeds.news_headlines(max_results=10)
        assert len(items) == 2
        assert items[0]["title"] == "Markets rally on strong earnings"
        assert items[0]["source"] == "Reuters"
        assert "2026" in items[0]["published"]

    def test_news_headlines_respects_limit(self, monkeypatch):
        monkeypatch.setattr(live_feeds, "_http_get", lambda *a, **k: _NEWS_XML)
        assert len(live_feeds.news_headlines(max_results=1)) == 1
        assert len(live_feeds.news_headlines(max_results=0)) == 1  # clamped to 1

    def test_news_feed_failure_raises_honestly(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("HTTP 503 from feed")

        monkeypatch.setattr(live_feeds, "_http_get", boom)
        with pytest.raises(RuntimeError, match="HTTP 503"):
            live_feeds.news_headlines()


# --------------------------------------------------------------------------- #
# Markets
# --------------------------------------------------------------------------- #

class TestMarkets:
    def test_stock_quote_parses(self, monkeypatch):
        monkeypatch.setattr(
            live_feeds,
            "_http_get",
            lambda *a, **k: json.dumps(_quote_payload("AAPL", 308.91)),
        )
        q = live_feeds.stock_quote("aapl")
        assert q["symbol"] == "AAPL"
        assert q["price"] == 308.91
        assert q["currency"] == "USD"
        assert q["week52_high"] == 328.91

    def test_stock_quote_unknown_symbol(self, monkeypatch):
        monkeypatch.setattr(
            live_feeds,
            "_http_get",
            lambda *a, **k: json.dumps({"chart": {"result": []}}),
        )
        with pytest.raises(RuntimeError, match="no quote"):
            live_feeds.stock_quote("ZZZZ")

    def test_stock_quote_invalid_symbol(self, monkeypatch):
        with pytest.raises(RuntimeError, match="invalid symbol"):
            live_feeds.stock_quote("AAPL; DROP TABLE")

    def test_market_movers_gainers(self, monkeypatch):
        monkeypatch.setattr(
            live_feeds,
            "_http_get",
            lambda *a, **k: json.dumps(
                _movers_payload([("TSLA", "Tesla", 400.0), ("NVDA", "Nvidia", 150.0)])
            ),
        )
        rows = live_feeds.market_movers("gainers", count=10)
        assert len(rows) == 2
        assert rows[0]["symbol"] == "TSLA"
        assert rows[0]["price"] == 400.0
        assert rows[0]["change_pct"] == 3.0

    def test_market_movers_bad_direction(self, monkeypatch):
        with pytest.raises(RuntimeError, match="gainers' or 'losers'"):
            live_feeds.market_movers("sideways")

    def test_market_movers_empty_is_honest(self, monkeypatch):
        monkeypatch.setattr(
            live_feeds,
            "_http_get",
            lambda *a, **k: json.dumps({"finance": {"result": [{"quotes": []}]}}),
        )
        with pytest.raises(RuntimeError, match="no gainers data"):
            live_feeds.market_movers("gainers")


# --------------------------------------------------------------------------- #
# Mail — env-gated, honest NOT CONFIGURED
# --------------------------------------------------------------------------- #

class TestMail:
    def test_read_inbox_not_configured_without_env(self, monkeypatch):
        """v5.2: with neither IMAP env vars NOR Gmail config present, the
        tool honestly reports NOT CONFIGURED. The google_services fallback is
        pinned to empty so a user's real local_secrets.py can't leak into
        this hermetic test."""
        monkeypatch.delenv("DOURMOUSE_IMAP_HOST", raising=False)
        monkeypatch.delenv("DOURMOUSE_IMAP_USER", raising=False)
        monkeypatch.delenv("DOURMOUSE_IMAP_PASS", raising=False)
        from dourmouse import google_services as gs

        monkeypatch.setattr(gs, "_local_secrets", dict)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            live_feeds.read_inbox()

    def test_read_inbox_falls_back_to_gmail_config(self, monkeypatch):
        """v5.2: Gmail configured via local_secrets powers read_inbox too —
        one App Password drives every mail tool (no duplicate setup)."""
        from dourmouse import google_services as gs

        monkeypatch.delenv("DOURMOUSE_IMAP_HOST", raising=False)
        monkeypatch.delenv("DOURMOUSE_IMAP_USER", raising=False)
        monkeypatch.delenv("DOURMOUSE_IMAP_PASS", raising=False)
        monkeypatch.setattr(
            gs, "_local_secrets", lambda: {"user": "a@gmail.com", "password": "1234567890abcdef"}
        )
        captured: dict = {}
        import imaplib as _imaplib

        class _FakeConn:
            def __init__(self, *a, **k):
                captured["host"] = a[0] if a else k.get("host")

            def login(self, u, p):
                captured["user"] = u
                captured["pass"] = p
                return ("OK", [b""])

            def select(self, *a, **k):
                return ("OK", [])

            def search(self, *a, **k):
                return ("OK", [b""])

            def fetch(self, *a, **k):
                return ("OK", [])

            def logout(self):
                pass

        monkeypatch.setattr(_imaplib, "IMAP4_SSL", _FakeConn)
        out = live_feeds.read_inbox(3)
        assert captured["host"] == "imap.gmail.com"
        assert captured["user"] == "a@gmail.com"
        assert captured["pass"] == "1234567890abcdef"
        assert out == []  # fake server returned no messages — no crash


# --------------------------------------------------------------------------- #
# Tasks — deterministic local CRUD
# --------------------------------------------------------------------------- #

class TestTasks:
    @pytest.fixture(autouse=True)
    def _tasks_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setenv("DOURMOUSE_TASKS_FILE", str(tmp_path / "tasks.json"))

    def test_add_list_complete_cycle(self):
        t1 = live_feeds.add_task("buy milk")
        assert t1["title"] == "buy milk"
        assert t1["done"] is False
        t2 = live_feeds.add_task("ship the build")
        assert t2["id"] == "task-2"

        tasks = live_feeds.list_tasks()
        assert [t["id"] for t in tasks] == ["task-1", "task-2"]

        assert live_feeds.complete_task("task-1") is True
        assert live_feeds.complete_task("task-1") is False  # already done

        open_tasks = live_feeds.list_tasks(include_done=False)
        assert [t["id"] for t in open_tasks] == ["task-2"]
        done = [t for t in live_feeds.list_tasks() if t["done"]]
        assert done[0]["id"] == "task-1"
        assert "completed_at" in done[0]

    def test_add_task_empty_title_rejected(self):
        with pytest.raises(RuntimeError, match="non-empty title"):
            live_feeds.add_task("   ")

    def test_tasks_persist_across_calls(self):
        live_feeds.add_task("persisted task")
        # Re-import state: file-backed, so a fresh read sees it.
        assert any(t["title"] == "persisted task" for t in live_feeds.list_tasks())


# --------------------------------------------------------------------------- #
# Preloaded subagent wiring
# --------------------------------------------------------------------------- #

class TestPreloadedAgents:
    def test_all_five_agents_registered(self):
        registry = build_general_registry()
        names = set(registry.subagent_names)
        assert {"news", "markets", "rnd", "mail", "tasks"} <= names

    def test_news_agent_tool(self):
        registry = build_general_registry()
        tools = {t.name for t in registry.get_subagent("news").tools}
        assert tools == {"news_headlines"}

    def test_markets_agent_tools(self):
        registry = build_general_registry()
        tools = {t.name for t in registry.get_subagent("markets").tools}
        assert {"stock_quote", "market_movers"} <= tools

    def test_rnd_agent_has_live_and_web_tools(self):
        registry = build_general_registry()
        tools = {t.name for t in registry.get_subagent("rnd").tools}
        assert {"research_news", "research_quote", "research_movers",
                "research_web_search", "research_fetch_url"} <= tools

    def test_mail_agent_tool(self):
        registry = build_general_registry()
        tools = {t.name for t in registry.get_subagent("mail").tools}
        # v5.0: the mail agent grew Gmail search/read/send alongside IMAP;
        # v5.2x: the per-user Google scope surface added drive_search +
        # drive_read (same OAuth session as gmail).
        # v5.25: the Dourmouse own-mail identity surface (status + own send).
        # v5.27: drive_create_doc moved to the docs agent (Drive directives
        # route to docs; the registry forbids cross-agent tool-name
        # collisions, so the write tool lives there, not on mail).
        assert {"read_inbox", "gmail_search", "gmail_read", "gmail_send",
                "drive_read", "drive_search",
                "email_identity_status", "email_own_send"} == tools

    def test_tasks_agent_tools(self):
        registry = build_general_registry()
        tools = {t.name for t in registry.get_subagent("tasks").tools}
        assert {"list_tasks", "add_task", "complete_task"} <= tools

    def test_no_global_tool_name_collisions(self):
        """Each tool NAME maps to exactly ONE spec object (v5.8 sharing of
        the identical publish_artifact object is fine; distinct objects with
        the same name are the collisions the registry rejects)."""
        registry = build_general_registry()
        owners: dict[str, int] = {}
        for agent in registry.all_subagents():
            for tool in agent.tools:
                prev = owners.get(tool.name)
                assert prev is None or prev == id(tool), f"collision: {tool.name}"
                owners[tool.name] = id(tool)


# --------------------------------------------------------------------------- #
# Neural link topology
# --------------------------------------------------------------------------- #

class TestLinkTopology:
    def test_links_contain_all_agents_as_nodes(self):
        registry = build_general_registry()
        topo = build_link_topology(registry)
        node_names = {n["name"] for n in topo["nodes"]}
        assert node_names == set(registry.subagent_names)

    def test_every_agent_receives_delegate_edge_from_orchestrator(self):
        registry = build_general_registry()
        topo = build_link_topology(registry)
        delegated = {e["target"] for e in topo["edges"] if e["kind"] == "delegate"}
        assert delegated == set(registry.subagent_names) - {"orchestrator"}

    def test_memory_is_the_shared_truth_hub(self):
        registry = build_general_registry()
        topo = build_link_topology(registry)
        memory_links = {e["target"] for e in topo["edges"] if e["kind"] == "memory"}
        assert "news" in memory_links
        assert "markets" in memory_links
        assert "orchestrator" not in memory_links

    def test_peer_edges_within_domain_clusters(self):
        registry = build_general_registry()
        topo = build_link_topology(registry)
        peer_edges = [e for e in topo["edges"] if e["kind"] == "peer"]
        assert peer_edges, "expected peer edges within domain clusters"
        # A peer edge connects same-domain agents (Live agents work together).
        domains = {n["name"]: n["domain"] for n in topo["nodes"]}
        for e in peer_edges:
            assert domains[e["source"]] == domains[e["target"]]

    def test_edges_are_all_valid_pairs(self):
        registry = build_general_registry()
        topo = build_link_topology(registry)
        names = {n["name"] for n in topo["nodes"]}
        for e in topo["edges"]:
            assert e["source"] in names and e["target"] in names
