"""Finding #094 (R0-3): robots.txt and per-host spacing for automated fetches."""

from __future__ import annotations

import http.server
import ipaddress
import threading

import pytest

from dourmouse import net_guard
from dourmouse.general_roster import _fetch_url_tool
from dourmouse.research_pipeline import politeness
from dourmouse.research_pipeline.acquire import DocumentCache, fetch_document
from dourmouse.research_pipeline.politeness import Politeness, RobotsDisallowed


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(round(s, 3))
        self.now += s


@pytest.fixture
def site(monkeypatch, tmp_path):
    real = net_guard.is_public_address
    monkeypatch.setattr(net_guard, "is_public_address",
                        lambda a: a == ipaddress.ip_address("127.0.0.1") or real(a))
    routes: dict = {}
    hits: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits[self.path] = hits.get(self.path, 0) + 1
            status, body = routes.get(self.path, (404, b"missing"))
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", routes, hits, DocumentCache(tmp_path / "raw")
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_disallowed_path_is_refused_and_never_fetched(site):
    base, routes, hits, cache = site
    routes["/robots.txt"] = (200, b"User-agent: *\nDisallow: /private\n")
    routes["/private/page"] = (200, b"secret")
    routes["/public"] = (200, b"hello")
    with pytest.raises(RobotsDisallowed, match="robots.txt disallows"):
        fetch_document(base + "/private/page", cache=cache)
    assert "/private/page" not in hits
    assert fetch_document(base + "/public", cache=cache).text == "hello"


def test_a_rule_for_our_own_user_agent_is_honoured(site):
    base, routes, _, cache = site
    routes["/robots.txt"] = (200, b"User-agent: dourmouse-research\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    routes["/x"] = (200, b"x")
    with pytest.raises(RobotsDisallowed):
        fetch_document(base + "/x", cache=cache)


def test_no_robots_txt_allows_everything(site):
    base, routes, _, cache = site
    routes["/x"] = (200, b"x")  # /robots.txt answers 404
    assert fetch_document(base + "/x", cache=cache).text == "x"


def test_a_robots_server_error_is_a_temporary_disallow(site):
    base, routes, _, cache = site
    routes["/robots.txt"] = (503, b"down")
    routes["/x"] = (200, b"x")
    with pytest.raises(RobotsDisallowed):
        fetch_document(base + "/x", cache=cache)


def test_robots_txt_is_read_once_per_host(site):
    base, routes, hits, cache = site
    routes["/robots.txt"] = (200, b"User-agent: *\nAllow: /\n")
    for i in range(3):
        routes[f"/p{i}"] = (200, b"p")
        fetch_document(base + f"/p{i}", cache=cache)
    assert hits["/robots.txt"] == 1


def test_requests_to_one_host_are_spaced(site, monkeypatch):
    base, routes, _, cache = site
    fake = _FakeClock()
    monkeypatch.setattr(politeness, "POLITENESS", Politeness(min_interval=2.0, clock=fake.clock, sleep=fake.sleep))
    for i in range(3):
        routes[f"/p{i}"] = (200, b"p")
        fetch_document(base + f"/p{i}", cache=cache)
    assert fake.slept == [2.0, 2.0]


def test_crawl_delay_wins_when_longer_but_is_capped(site, monkeypatch):
    base, routes, _, cache = site
    fake = _FakeClock()
    monkeypatch.setattr(politeness, "POLITENESS", Politeness(min_interval=1.0, clock=fake.clock, sleep=fake.sleep))
    routes["/robots.txt"] = (200, b"User-agent: *\nCrawl-delay: 60\n")
    routes["/a"] = (200, b"a")
    routes["/b"] = (200, b"b")
    fetch_document(base + "/a", cache=cache)
    fetch_document(base + "/b", cache=cache)
    assert fake.slept == [politeness.MAX_CRAWL_DELAY]


def test_a_cache_hit_does_not_wait_or_touch_the_site(site, monkeypatch):
    base, routes, hits, cache = site
    fake = _FakeClock()
    monkeypatch.setattr(politeness, "POLITENESS", Politeness(min_interval=5.0, clock=fake.clock, sleep=fake.sleep))
    routes["/a"] = (200, b"a")
    fetch_document(base + "/a", cache=cache)
    fetch_document(base + "/a", cache=cache)
    assert fake.slept == []
    assert hits["/a"] == 1


def test_fetch_url_reports_a_robots_refusal_honestly(site):
    base, routes, _, _ = site
    routes["/robots.txt"] = (200, b"User-agent: *\nDisallow: /\n")
    out = _fetch_url_tool({"url": base + "/anything"})
    assert out.startswith("REFUSED BY ROBOTS.TXT:")
