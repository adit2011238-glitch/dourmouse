"""Per-host rate limiting and robots.txt for automated fetches (R0-3,
finding #094).

Before this, twenty sources on one domain meant twenty requests as fast as
the loop could issue them, with no robots.txt consultation. Both get a
research tool blocked in practice, and honouring robots.txt is the courtesy
an automated reader of the public web owes the sites it reads.

Every automated fetch (the research pipeline and the agents' fetch_url)
passes ``Politeness.wait_turn(url)`` first: it refuses a URL the site's
robots.txt disallows for our user agent, and it spaces requests to the same
host by at least the minimum interval, or the site's Crawl-delay when that is
longer (capped). robots.txt itself is fetched through the SSRF guard and
cached per host; a missing robots.txt (4xx) allows everything, as the
standard says.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field

from dourmouse import net_guard

USER_AGENT = "dourmouse-research/1.0"
ROBOTS_TTL_SECONDS = 3600.0
MAX_CRAWL_DELAY = 10.0
_ROBOTS_MAX_BYTES = 512 * 1024


class RobotsDisallowed(PermissionError):
    """The site's robots.txt disallows this URL for our user agent."""


def default_min_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("DOURMOUSE_FETCH_MIN_INTERVAL", "1.0")))
    except ValueError:
        return 1.0


@dataclass
class _Host:
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_allowed: float = 0.0
    robots: urllib.robotparser.RobotFileParser | None = None
    robots_note: str = ""
    robots_fetched_at: float = -1.0


class Politeness:
    def __init__(self, min_interval: float | None = None, clock=time.monotonic, sleep=time.sleep) -> None:  # type: ignore[no-untyped-def]
        self._min_interval = min_interval
        self._hosts: dict[str, _Host] = {}
        self._guard = threading.Lock()
        self._clock = clock
        self._sleep = sleep

    def _host(self, key: str) -> _Host:
        with self._guard:
            return self._hosts.setdefault(key, _Host())

    def min_interval(self) -> float:
        return self._min_interval if self._min_interval is not None else default_min_interval()

    def _load_robots(self, h: _Host, scheme: str, netloc: str) -> None:
        now = self._clock()
        if h.robots_fetched_at >= 0 and now - h.robots_fetched_at < ROBOTS_TTL_SECONDS:
            return
        parser = urllib.robotparser.RobotFileParser()
        robots_url = f"{scheme}://{netloc}/robots.txt"
        req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})  # noqa: S310 -- scheme enforced by net_guard
        try:
            with net_guard.guarded_urlopen(req, timeout=10) as resp:
                body = resp.read(_ROBOTS_MAX_BYTES).decode("utf-8", errors="replace")
            parser.parse(body.splitlines())
            h.robots_note = "robots.txt read"
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                parser.parse([])  # no robots.txt: everything allowed
                h.robots_note = f"no robots.txt ({exc.code}); all allowed"
            else:
                # A server error on robots.txt is not permission; the standard
                # treats it as a temporary full disallow.
                parser.parse(["User-agent: *", "Disallow: /"])
                h.robots_note = f"robots.txt returned {exc.code}; treated as a temporary disallow"
        except (net_guard.FetchRefused, urllib.error.URLError, OSError) as exc:
            # Unreachable robots.txt: the page fetch itself will surface the
            # real network problem; do not invent a rule here.
            parser.parse([])
            h.robots_note = f"robots.txt unreachable ({type(exc).__name__}); not enforced"
        h.robots = parser
        h.robots_fetched_at = now

    def wait_turn(self, url: str) -> None:
        """Block until this host may be fetched again; raise
        RobotsDisallowed when robots.txt forbids ``url``."""
        parts = urllib.parse.urlsplit(url)
        key = parts.netloc.lower()
        h = self._host(key)
        with h.lock:
            self._load_robots(h, parts.scheme or "https", parts.netloc)
            assert h.robots is not None
            if not h.robots.can_fetch(USER_AGENT, url):
                raise RobotsDisallowed(f"{parts.netloc}'s robots.txt disallows {url} for automated readers")
            delay = self.min_interval()
            crawl = h.robots.crawl_delay(USER_AGENT)
            if crawl:
                delay = max(delay, min(float(crawl), MAX_CRAWL_DELAY))
            wait = h.next_allowed - self._clock()
            if wait > 0:
                self._sleep(wait)
            h.next_allowed = self._clock() + delay


#: The process-wide gate every automated fetch shares.
POLITENESS = Politeness()
