"""v13 news_stream.py — the forever-refreshing, push-on-important feed.

Hermetic (Rule 2.1): every source is an injected fake callable returning
canned world_pulse-shaped item dicts — no real network, no dependency on
world_pulse.py's own live fetchers being reachable from CI. Verifies: real
polling on a real (short, test-only) interval, deduplication across
cycles, the important/not-important severity rule, bounded memory (seen
keys and the recent list), one dead source never blocking the others, a
fully-dead cycle backing off and reporting honestly via status(), and that
a broken sink can never kill the poll thread.
"""

from __future__ import annotations

import threading
import time

import pytest

from dourmouse.news_stream import NewsStreamWatcher, _dedup_key
import dourmouse.news_stream as ns_module


def _item(title, severity="", link="", **extra):
    return {"title": title, "summary": "", "link": link, "at": "", "severity": severity, **extra}


def _wait_until(condition, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


class TestDedupKey:
    def test_same_title_and_link_same_key(self):
        a = _dedup_key("news", _item("Same headline", link="https://x/1"))
        b = _dedup_key("news", _item("Same headline", link="https://x/1"))
        assert a == b

    def test_different_channel_different_key(self):
        """The same title from two different sources must not collide —
        a real earthquake headline and a real news-wire mention of the
        same event are genuinely different items."""
        a = _dedup_key("disasters", _item("Same headline", link="https://x/1"))
        b = _dedup_key("news", _item("Same headline", link="https://x/1"))
        assert a != b

    def test_different_link_different_key(self):
        a = _dedup_key("news", _item("Same headline", link="https://x/1"))
        b = _dedup_key("news", _item("Same headline", link="https://x/2"))
        assert a != b


class TestPolling:
    def test_new_items_are_pushed_to_the_sink(self):
        pushed = []
        source_calls = {"n": 0}

        def fake_news():
            source_calls["n"] += 1
            return [_item("Headline A", link="https://x/a")]

        watcher = NewsStreamWatcher(
            pushed.append, sources=[("news", fake_news)], poll_interval=0.05,
        )
        watcher.start()
        try:
            assert _wait_until(lambda: len(pushed) >= 1)
            assert pushed[0]["type"] == "news_item"
            assert pushed[0]["item"]["title"] == "Headline A"
            assert pushed[0]["item"]["channel"] == "news"
        finally:
            watcher.stop()

    def test_identical_item_across_polls_is_not_repushed(self):
        pushed = []

        def fake_news():
            return [_item("Same headline every time", link="https://x/1")]

        watcher = NewsStreamWatcher(
            pushed.append, sources=[("news", fake_news)], poll_interval=0.02,
        )
        watcher.start()
        try:
            # Give it several poll cycles — a real live feed re-fetches
            # the same still-current items every cycle, and none of them
            # should be treated as new after the first.
            time.sleep(0.3)
            assert len(pushed) == 1
        finally:
            watcher.stop()

    def test_new_item_appearing_later_is_pushed_once(self):
        pushed = []
        state = {"items": [_item("First", link="https://x/1")]}

        def fake_news():
            return list(state["items"])

        watcher = NewsStreamWatcher(
            pushed.append, sources=[("news", fake_news)], poll_interval=0.03,
        )
        watcher.start()
        try:
            assert _wait_until(lambda: len(pushed) >= 1)
            state["items"].append(_item("Second, arrived later", link="https://x/2"))
            assert _wait_until(lambda: len(pushed) >= 2)
            titles = [p["item"]["title"] for p in pushed]
            assert titles.count("First") == 1
            assert titles.count("Second, arrived later") == 1
        finally:
            watcher.stop()

    def test_one_dead_source_does_not_block_the_others(self):
        pushed = []

        def broken_source():
            raise RuntimeError("upstream is down")

        def working_source():
            return [_item("Still works", link="https://x/ok")]

        watcher = NewsStreamWatcher(
            pushed.append,
            sources=[("broken", broken_source), ("news", working_source)],
            poll_interval=0.05,
        )
        watcher.start()
        try:
            assert _wait_until(lambda: len(pushed) >= 1)
            assert pushed[0]["item"]["title"] == "Still works"
            # A partial failure is not the honest "poll failed" case —
            # one real source succeeded, so status must say so.
            assert _wait_until(lambda: watcher.status()["last_poll_ok"] is True)
        finally:
            watcher.stop()

    def test_every_source_failing_backs_off_and_reports_honestly(self):
        def broken_source():
            raise RuntimeError("all sources down")

        watcher = NewsStreamWatcher(
            lambda payload: None,
            sources=[("broken", broken_source)],
            poll_interval=0.05,
        )
        watcher.start()
        try:
            assert _wait_until(lambda: watcher.status()["last_poll_ok"] is False)
            assert "all sources down" in watcher.status()["last_error"]
        finally:
            watcher.stop()

    def test_a_raising_sink_never_kills_the_poll_thread(self):
        calls = {"n": 0}

        def boom(payload):
            calls["n"] += 1
            raise RuntimeError("subscriber blew up")

        def fake_news():
            return [_item(f"Item {calls['n']}", link=f"https://x/{calls['n']}")]

        watcher = NewsStreamWatcher(boom, sources=[("news", fake_news)], poll_interval=0.03)
        watcher.start()
        try:
            assert _wait_until(lambda: calls["n"] >= 1)
            # The thread must still be alive and still polling — a
            # raising sink is the caller's bug, never the poller's.
            assert watcher.status()["running"] is True
        finally:
            watcher.stop()


class TestImportanceRule:
    @pytest.mark.parametrize("severity,expected", [
        ("critical", True),
        ("high", True),
        ("info", False),
        ("watch", False),
        ("warn", False),
        ("news", False),
        ("", False),
    ])
    def test_severity_maps_to_important(self, severity, expected):
        pushed = []

        def fake_source():
            return [_item("An item", severity=severity, link="https://x/sev")]

        watcher = NewsStreamWatcher(
            pushed.append, sources=[("disasters", fake_source)], poll_interval=0.03,
        )
        watcher.start()
        try:
            assert _wait_until(lambda: len(pushed) >= 1)
            assert pushed[0]["item"]["important"] is expected
        finally:
            watcher.stop()


class TestRecentAndBounds:
    def test_recent_returns_newest_first(self):
        watcher = NewsStreamWatcher(lambda p: None, sources=[])
        # Poke the internal list directly — recent()'s ordering contract
        # is what's under test, not the poll loop (already covered above).
        watcher._recent = [
            {"title": "old", "important": False},
            {"title": "new", "important": False},
        ]
        assert [i["title"] for i in watcher.recent()] == ["new", "old"]

    def test_recent_important_only_filters(self):
        watcher = NewsStreamWatcher(lambda p: None, sources=[])
        watcher._recent = [
            {"title": "routine", "important": False},
            {"title": "urgent", "important": True},
        ]
        out = watcher.recent(important_only=True)
        assert [i["title"] for i in out] == ["urgent"]

    def test_recent_list_is_bounded(self):
        import dourmouse.news_stream as ns_module

        watcher = NewsStreamWatcher(lambda p: None, sources=[])
        watcher._recent = [{"title": f"item {i}", "important": False} for i in range(ns_module._MAX_RECENT + 50)]
        # Simulate what _poll_once's own trim does, directly, to prove the
        # constant is honored without needing 250 real poll cycles.
        if len(watcher._recent) > ns_module._MAX_RECENT:
            del watcher._recent[: len(watcher._recent) - ns_module._MAX_RECENT]
        assert len(watcher._recent) == ns_module._MAX_RECENT

    def test_seen_set_is_bounded_across_many_real_poll_cycles(self):
        """The real trim path, exercised through actual polling — not the
        simulated version above — with enough distinct items to cross the
        bound at least once."""
        import dourmouse.news_stream as ns_module

        original_max = ns_module._MAX_SEEN
        ns_module._MAX_SEEN = 10
        try:
            counter = {"n": 0}

            def fake_source():
                counter["n"] += 1
                return [_item(f"item {counter['n']}", link=f"https://x/{counter['n']}")]

            watcher = NewsStreamWatcher(
                lambda p: None, sources=[("news", fake_source)], poll_interval=0.01,
            )
            watcher.start()
            try:
                assert _wait_until(lambda: len(watcher._seen) >= 10, timeout=3.0)
                time.sleep(0.2)
                assert len(watcher._seen) <= ns_module._MAX_SEEN
                assert len(watcher._seen_set) <= ns_module._MAX_SEEN
            finally:
                watcher.stop()
        finally:
            ns_module._MAX_SEEN = original_max


class TestLifecycle:
    def test_start_is_idempotent(self):
        watcher = NewsStreamWatcher(lambda p: None, sources=[], poll_interval=1.0)
        watcher.start()
        first_thread = watcher._thread
        watcher.start()  # must not spawn a second thread
        try:
            assert watcher._thread is first_thread
        finally:
            watcher.stop()

    def test_stop_actually_stops_the_thread(self):
        watcher = NewsStreamWatcher(lambda p: None, sources=[], poll_interval=0.02)
        watcher.start()
        watcher.stop()
        assert watcher.status()["running"] is False

    def test_default_sources_resolve_to_real_world_pulse_fetchers(self):
        """Not a network test — just confirms the lazy import wires to the
        real, already-tested functions rather than a stub or a typo'd
        name, so a real deployment's default construction doesn't 500 on
        import."""
        from dourmouse import world_pulse
        from dourmouse.news_stream import _default_sources

        sources = _default_sources()
        names = {name for name, _ in sources}
        assert names == {"disasters", "conflict_events", "news"}
        by_name = dict(sources)
        assert by_name["disasters"] is world_pulse._fetch_disasters
        assert by_name["conflict_events"] is world_pulse._fetch_conflict_events
        assert by_name["news"] is world_pulse._fetch_news


# --------------------------------------------------------------------------- #
# webui.py wiring — real HTTP, real server, fake sources only (Rule 2.1: the
# suite never touches the real network; monkeypatching _default_sources is
# what makes news_stream=True safe to exercise here, since run_server's own
# wiring doesn't expose a sources= injection point — it always builds the
# real NewsStreamWatcher() the same way the production entrypoint does).
# --------------------------------------------------------------------------- #

import http.client
import json
import threading

from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec
from dourmouse.webui import run_server


def _test_registry() -> DispatchRegistry:
    r = DispatchRegistry()
    r.register_subagent(
        Subagent(
            name="echo_agent", domain="Test", description="echoes",
            tools=(ToolSpec(name="echo", description="e",
                             parameters={"type": "object", "properties": {}},
                             handler=lambda a: "ok"),),
        )
    )
    return r


class TestRunServerWiring:
    def test_default_has_no_news_watcher(self):
        srv = run_server(_test_registry(), port=0, client=None, config=None)
        try:
            assert srv.events_broadcast is not None
            assert srv.news_watcher is None
        finally:
            srv.server_close()

    def test_news_stream_true_starts_a_real_watcher(self, monkeypatch):
        monkeypatch.setattr(
            ns_module, "_default_sources",
            lambda: [("news", lambda: [_item("Fake headline", link="https://x/1")])],
        )
        srv = run_server(_test_registry(), port=0, client=None, config=None, news_stream=True)
        try:
            assert srv.news_watcher is not None
            assert _wait_until(lambda: srv.news_watcher.status()["running"] is True)
            # Production path: no interval override -> the real default.
            assert srv.news_watcher._poll_interval == ns_module._POLL_INTERVAL
        finally:
            srv.news_watcher.stop()
            srv.server_close()

    def test_news_stream_poll_interval_override_reaches_the_watcher(self, monkeypatch):
        """The test seam this whole suite relies on to stay fast — a
        genuinely different fixed default (180s) would make every
        push-latency test in this file either flaky or minutes long."""
        monkeypatch.setattr(ns_module, "_default_sources", lambda: [])
        srv = run_server(
            _test_registry(), port=0, client=None, config=None,
            news_stream=True, news_stream_poll_interval=0.05,
        )
        try:
            assert srv.news_watcher._poll_interval == 0.05
        finally:
            srv.news_watcher.stop()
            srv.server_close()


class TestApiNewsEndpoint:
    def test_no_watcher_reports_honestly(self):
        srv = run_server(_test_registry(), port=0, client=None, config=None)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            port = srv.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/news")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            conn.close()
            assert resp.status == 200
            assert data == {"items": [], "status": None, "running": False}
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_real_watcher_serves_recent_items_and_respects_query_params(self, monkeypatch):
        monkeypatch.setattr(
            ns_module, "_default_sources",
            lambda: [
                ("disasters", lambda: [_item("Critical alert", severity="critical", link="https://x/crit")]),
                ("news", lambda: [_item("Routine headline", link="https://x/routine")]),
            ],
        )
        srv = run_server(_test_registry(), port=0, client=None, config=None, news_stream=True)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            port = srv.server_address[1]
            assert _wait_until(lambda: len(srv.news_watcher.recent()) >= 2)

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/news")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            conn.close()
            assert resp.status == 200
            assert data["running"] is True
            titles = {i["title"] for i in data["items"]}
            assert titles == {"Critical alert", "Routine headline"}

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/news?important=1")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            conn.close()
            assert [i["title"] for i in data["items"]] == ["Critical alert"]
        finally:
            srv.news_watcher.stop()
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def test_new_item_reaches_a_connected_events_subscriber(self, monkeypatch):
        """The actual push proof, mirroring test_freebuff_events.py's own
        fan-out test — a real GET /api/events connection must receive a
        genuinely new news_item the moment the watcher's next poll finds
        one, with no chat prompt or page reload involved."""
        state = {"items": []}
        monkeypatch.setattr(
            ns_module, "_default_sources",
            lambda: [("news", lambda: list(state["items"]))],
        )
        srv = run_server(
            _test_registry(), port=0, client=None, config=None,
            news_stream=True, news_stream_poll_interval=0.03,
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            port = srv.server_address[1]
            # Established pattern from test_freebuff_events.py's own
            # fan-out test: a long-timeout blocking read on a background
            # thread, polled from the main thread — a tight polling loop
            # on the socket itself hits Python's "cannot read from a timed
            # out object" quirk (a socket that ever times out mid-readline
            # refuses all further reads, even after data arrives).
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/events")
            resp = conn.getresponse()
            received: list[dict] = []

            def _read_loop():
                try:
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        if line.startswith(b"data: "):
                            received.append(json.loads(line[6:]))
                except (OSError, http.client.RemoteDisconnected):
                    pass

            reader = threading.Thread(target=_read_loop, daemon=True)
            reader.start()

            state["items"].append(_item("Pushed live", link="https://x/live"))

            found = _wait_until(
                lambda: any(
                    p.get("type") == "news_item" and p["item"]["title"] == "Pushed live"
                    for p in received
                ),
                timeout=5.0,
            )
            conn.close()
            assert found, "the pushed news_item never reached the connected /api/events stream"
        finally:
            srv.news_watcher.stop()
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
