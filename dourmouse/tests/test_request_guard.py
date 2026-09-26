"""Finding #135: the browser-borne request guard (pure function)."""

from __future__ import annotations

import pytest

from dourmouse import request_guard as rg

PORT = 8765
LOCAL = "127.0.0.1"


def check(method="POST", client=LOCAL, allowed=(), **headers):
    return rg.check(method, headers, client, PORT, allowed)


class TestHostHeader:
    """DNS rebinding: the attacker's page addresses the server by its own name."""

    @pytest.mark.parametrize("host", [
        f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"LOCALHOST:{PORT}", f"[::1]:{PORT}",
    ])
    def test_the_servers_own_names_pass(self, host):
        assert check("GET", Host=host) is None
        assert check("POST", Host=host) is None

    @pytest.mark.parametrize("host", [
        f"evil.example:{PORT}", "evil.example", f"127.0.0.1.evil.example:{PORT}", f"localhost.evil.example:{PORT}",
        "127.0.0.1:9999", "localhost", f"[::2]:{PORT}",
    ])
    def test_any_other_name_or_port_is_refused_even_for_a_read(self, host):
        assert check("GET", Host=host) is not None
        assert check("POST", Host=host) is not None

    def test_a_client_with_no_host_header_is_not_a_browser(self):
        assert check("POST") is None

    def test_an_owner_added_name_passes_on_any_port(self):
        assert check("GET", allowed=("mac.tailnet.ts.net",), Host=f"mac.tailnet.ts.net:{PORT}") is None
        assert check("GET", allowed=("mac.tailnet.ts.net",), Host="mac.tailnet.ts.net") is None  # behind a proxy on 443
        assert check("GET", Host=f"mac.tailnet.ts.net:{PORT}") is not None

    def test_a_name_and_port_entry_allows_only_that_port(self):
        assert check("GET", allowed=("127.0.0.1:9999",), Host="127.0.0.1:9999") is None
        assert check("GET", allowed=("127.0.0.1:9999",), Host="127.0.0.1:9998") is not None
        assert check("POST", allowed=("mac.example",), Host="mac.example", Origin="https://mac.example") is None
        assert check("POST", allowed=("mac.example",), Host="mac.example", Origin="https://evil.example") is not None


class TestStateChangingRequests:
    """Cross-site request forgery: a page on another site sends a simple POST."""

    def test_a_foreign_origin_is_refused(self):
        assert check(Host=f"127.0.0.1:{PORT}", Origin="https://evil.example") is not None
        assert check(Host=f"127.0.0.1:{PORT}", Origin="http://127.0.0.1:9999") is not None
        assert check(Host=f"127.0.0.1:{PORT}", Origin="http://localhost.evil.example:8765") is not None

    def test_the_null_origin_of_a_sandboxed_frame_is_refused_for_a_write(self):
        assert check(Host=f"127.0.0.1:{PORT}", Origin="null") is not None

    def test_the_apps_own_origin_passes(self):
        assert check(Host=f"127.0.0.1:{PORT}", Origin=f"http://127.0.0.1:{PORT}") is None
        assert check(Host=f"localhost:{PORT}", Origin=f"http://localhost:{PORT}") is None

    def test_cross_site_fetch_metadata_is_refused(self):
        assert check(Host=f"127.0.0.1:{PORT}", **{"Sec-Fetch-Site": "cross-site"}) is not None
        assert check(Host=f"127.0.0.1:{PORT}", **{"Sec-Fetch-Site": "same-site"}) is not None
        assert check(Host=f"127.0.0.1:{PORT}", **{"Sec-Fetch-Site": "same-origin"}) is None
        assert check(Host=f"127.0.0.1:{PORT}", **{"Sec-Fetch-Site": "none"}) is None

    def test_a_script_or_test_client_sends_neither_header_and_passes(self):
        assert check(Host=f"127.0.0.1:{PORT}") is None

    def test_a_cross_site_read_is_not_refused_here(self):
        """A cross-site GET carries a foreign Origin only when it is a CORS
        fetch, and the browser then hides the answer unless the route sends
        CORS headers. The routes that do are guarded by their own token."""
        assert check("GET", Host=f"127.0.0.1:{PORT}", Origin="https://evil.example") is None


class TestRemoteClients:
    def test_a_non_loopback_client_is_left_to_the_access_token(self):
        assert check(client="100.64.0.7", Host="anything.example", Origin="https://x") is None

    @pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
    def test_every_loopback_spelling_is_judged(self, ip):
        assert check(client=ip, Host="evil.example") is not None


def test_owner_hosts_come_from_the_environment(monkeypatch):
    monkeypatch.delenv("DOURMOUSE_ALLOWED_HOSTS", raising=False)
    assert rg.extra_allowed_hosts() == frozenset()
    monkeypatch.setenv("DOURMOUSE_ALLOWED_HOSTS", "A.example, b.example ,")
    assert rg.extra_allowed_hosts() == frozenset({"a.example", "b.example"})
