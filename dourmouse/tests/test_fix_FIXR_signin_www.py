"""FIX-R R-8: sign-in treats www.<host> and <host> as the same site, and nothing wider."""

from __future__ import annotations

import pytest

from dourmouse import browser_agent as ba


@pytest.mark.parametrize("page,vault", [
    ("https://www.example.com/login", "example.com"),
    ("https://example.com/login", "www.example.com"),
    ("https://example.com/login", "example.com"),
    ("https://EXAMPLE.com:443/login", "example.com"),
    ("https://www.example.co.uk/x", "example.co.uk"),
])
def test_the_www_alias_is_the_same_site(page, vault):
    assert ba._signin_landing_problem(page, vault) is None


@pytest.mark.parametrize("page,vault", [
    ("https://login.example.com/", "example.com"),          # another subdomain is another site
    ("https://example.com/", "login.example.com"),
    ("https://www.login.example.com/", "example.com"),
    ("https://www.evil.com/", "example.com"),
    ("https://wwwexample.com/", "example.com"),
    ("https://www.example.com.evil.com/", "example.com"),
    ("https://www.example.com:8443/", "example.com"),        # the port must still match
    ("http://www.example.com/", "example.com"),               # and https is still required
    ("https://www.com/", "com"),                              # a bare top-level name is not an alias target
])
def test_nothing_wider_than_the_www_alias_is_accepted(page, vault):
    assert ba._signin_landing_problem(page, vault) is not None
