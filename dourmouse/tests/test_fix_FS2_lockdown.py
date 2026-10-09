"""FS2 P3-57 / P3-58: lockdown never reports an unchecked block as blocked, and never exceeds the helper's cap."""

from __future__ import annotations

import json
import socket

import pytest

from dourmouse.security import lockdown as ld
from dourmouse.security import lockdown_helper as helper


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "config_path", lambda: tmp_path / "l.json")
    monkeypatch.setattr(ld, "hosts_request_path", lambda: tmp_path / "req.json")
    return tmp_path


def _resolver(mapping):
    def fake(host, port, *a, **k):
        v = mapping[host]
        if isinstance(v, Exception):
            raise v
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (v, port))]
    return fake


def test_dns_failure_is_unknown_not_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"a.com": socket.gaierror(8, "nodename nor servname")}))
    assert ld.site_is_blocked("a.com") is None


def test_sinkholed_name_is_blocked_and_real_address_is_not(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"a.com": "0.0.0.0", "b.com": "93.184.216.34"}))
    assert ld.site_is_blocked("a.com") is True
    assert ld.site_is_blocked("b.com") is False


def test_slow_resolver_is_unknown_within_the_deadline(monkeypatch):
    import time

    def slow(host, port, *a, **k):
        time.sleep(5)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    t0 = time.monotonic()
    assert ld.site_is_blocked("a.com", timeout=0.3) is None
    assert time.monotonic() - t0 < 3


def test_status_omits_blocked_now_when_it_could_not_be_checked(cfg, monkeypatch):
    monkeypatch.setattr(ld, "site_is_blocked", lambda d: None)
    bl = ld.Blocklist()
    bl.add_site("example.org")
    st = ld.start(bl)
    row = st["sites"][0]
    assert "blocked_now" not in row and row["blocked_now_unknown"] is True


def test_status_mixed_answers_are_not_blocked_unless_every_name_is(cfg, monkeypatch):
    monkeypatch.setattr(ld, "site_is_blocked", lambda d: d == "example.org")
    bl = ld.Blocklist()
    bl.add_site("example.org")
    assert ld.start(bl)["sites"][0]["blocked_now"] is False


def test_tool_text_says_unknown_when_unchecked():
    from dourmouse.security import tools as sec_tools

    text = sec_tools._format_lockdown({"active": True, "apps": [], "sites": [{"domain": "a.com", "blocks": ["a.com"], "blocked_now_unknown": True}], "limits": []})
    assert "could not be checked" in text and "NOT blocked right now" not in text


# ---- P3-58 --------------------------------------------------------------------

def test_adding_past_the_helper_cap_is_refused_with_a_clear_error():
    bl = ld.Blocklist()
    for i in range(helper.MAX_DOMAINS):
        bl.add_site(f"site{i}.example.org")
    with pytest.raises(ValueError, match=str(helper.MAX_DOMAINS)):
        bl.add_site("one-too-many.example.org")
    bl.add_site("site0.example.org")  # a name already on the list is not "one more"
    assert len(bl.sites) == helper.MAX_DOMAINS


def test_cap_counts_always_blocked_names_too(cfg):
    bl = ld.Blocklist()
    for i in range(helper.MAX_DOMAINS):
        bl.always.append({"domain": f"bad{i}.example.org"})
    with pytest.raises(ValueError):
        bl.add_site("extra.example.org")
    with pytest.raises(ValueError):
        ld.block_domain_always("another.example.org", bl=bl)


def test_oversized_existing_list_keeps_the_always_blocks_and_stays_under_the_cap(cfg):
    bl = ld.Blocklist(active=True)
    bl.always = [{"domain": "malware.example.net"}]
    bl.sites = [{"domain": f"s{i}.example.org"} for i in range(helper.MAX_DOMAINS + 50)]
    ld.write_hosts_request(bl)
    req = json.loads((cfg / "req.json").read_text())
    assert len(req["domains"]) == helper.MAX_DOMAINS and "malware.example.net" in req["domains"]
    assert helper.valid_domains(req["domains"])  # the helper accepts it instead of clearing everything
    assert req["truncated"] == 51
    st = ld.status(bl, check_sites=False)
    assert any("not all" in line and str(helper.MAX_DOMAINS) in line for line in st["limits"])
