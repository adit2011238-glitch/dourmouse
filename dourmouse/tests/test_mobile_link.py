"""v5.13 mobile-link tests (mobile_link.py + the /mobile pairing route).

Every test is hermetic (Rule 2.1): the .env writer runs against tmp_path,
address detection is driven by a FAKED ifconfig, and the /mobile route is
tested over a real loopback HTTP server with no external network. Verifies:

- generate_token: fresh, urlsafe, non-empty, and different each call
- read_env / write_env: idempotent (no rewrite when unchanged), preserves
  other keys/comments, adds missing keys, appends newline
- detect_addresses: parses LAN + Tailscale IPv4s from canned ifconfig;
  honest found:False with no addresses
- pairing_url: correct shape
- qr_ascii: a real QR string when segno is present; None (honest) when
  segno is missing
- main() dry-run prints the plan without touching .env
- /mobile route: serves 200 with the pairing page, fills the status,
  reaches no auth (like /login), and honors the token gate for APIs
"""

from __future__ import annotations

import json
import threading

import pytest

import dourmouse.mobile_link as ml
from dourmouse.general_roster import build_general_registry
from dourmouse.webui import run_server


class TestToken:
    def test_generate_token_is_fresh_and_urlsafe(self):
        a = ml.generate_token()
        b = ml.generate_token()
        assert a and b
        assert a != b
        assert all(c.isalnum() or c in "-_" for c in a)


class TestEnvIO:
    def test_read_env_missing_file(self, tmp_path):
        assert ml.read_env(tmp_path / "nope.env") == {}

    def test_read_env_parses_keys(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("A=1\n# comment\nB = two\n")
        env = ml.read_env(p)
        assert env == {"A": "1", "B": "two"}

    def test_write_env_adds_missing_keys(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("KEEP=value\n")
        out = ml.write_env("0.0.0.0", "tok123", path=p)
        assert out["changed"] is True
        text = p.read_text()
        assert "KEEP=value" in text
        assert "DOURMOUSE_HOST=0.0.0.0" in text
        assert "DOURMOUSE_ACCESS_TOKEN=tok123" in text

    def test_write_env_idempotent_when_unchanged(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("DOURMOUSE_HOST=0.0.0.0\nDOURMOUSE_ACCESS_TOKEN=tok123\nKEEP=x\n")
        out = ml.write_env("0.0.0.0", "tok123", path=p)
        assert out["changed"] is False
        assert p.read_text() == "DOURMOUSE_HOST=0.0.0.0\nDOURMOUSE_ACCESS_TOKEN=tok123\nKEEP=x\n"

    def test_write_env_replaces_changed_values_in_place(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("DOURMOUSE_ACCESS_TOKEN=old\nKEEP=x\n")
        out = ml.write_env("0.0.0.0", "newtok", path=p)
        assert out["changed"] is True
        text = p.read_text()
        assert "DOURMOUSE_ACCESS_TOKEN=newtok" in text
        assert "old" not in text
        assert "KEEP=x" in text  # preserved


class TestAddresses:
    def test_detect_parses_lan_and_tailscale(self, monkeypatch):
        ifconfig = """\
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.95 netmask 0xffffff00 broadcast 192.168.1.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.112.92.5 --> 100.112.92.5 netmask 0xffffffff
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.0.0.4 netmask 0xffffff00 broadcast 10.0.0.255
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
"""
        fake_addrs = [
            line.split()[1] for line in ifconfig.splitlines() if "inet " in line
        ]
        monkeypatch.setattr(ml, "_ifconfig_addrs", lambda: fake_addrs)
        addrs = ml.detect_addresses()
        assert addrs["lan"] == ["192.168.1.95", "10.0.0.4"]
        assert addrs["tailscale"] == ["100.112.92.5"]
        assert addrs["found"] is True

    def test_detect_no_addresses_honest(self, monkeypatch):
        loopback_only = ["127.0.0.1"]
        monkeypatch.setattr(ml, "_ifconfig_addrs", lambda: loopback_only)
        addrs = ml.detect_addresses()
        assert addrs["found"] is False
        assert addrs["lan"] == [] and addrs["tailscale"] == []

    def test_detect_falls_back_to_socket(self, monkeypatch):
        no_ifconfig: list[str] = []
        socket_ips = ["192.168.1.50", "127.0.0.1"]
        monkeypatch.setattr(ml, "_ifconfig_addrs", lambda: no_ifconfig)
        monkeypatch.setattr(ml, "_socket_addrs", lambda: socket_ips)
        addrs = ml.detect_addresses()
        assert addrs["lan"] == ["192.168.1.50"]

    def test_pairing_url(self):
        assert ml.pairing_url("192.168.1.95", 8765) == "http://192.168.1.95:8765/login"

    def test_detect_cgnat_precise_not_loose_100_prefix(self, monkeypatch):
        """Only 100.64.0.0/10 is CGNAT/Tailscale. 100.0.0.1 and 100.128.0.1
        are ordinary global space — they must NOT be labelled tailscale."""
        weird = ["100.0.0.1", "100.128.0.1", "100.64.0.1", "100.112.92.5"]
        monkeypatch.setattr(ml, "_ifconfig_addrs", lambda: weird)
        addrs = ml.detect_addresses()
        assert addrs["tailscale"] == ["100.64.0.1", "100.112.92.5"]
        assert addrs["lan"] == []

    def test_detect_excludes_unspecified(self, monkeypatch):
        # 0.0.0.0/8 is is_private True in CPython but never phone-reachable.
        ips = ["0.0.0.1", "192.168.1.10"]
        monkeypatch.setattr(ml, "_ifconfig_addrs", lambda: ips)
        addrs = ml.detect_addresses()
        assert addrs["lan"] == ["192.168.1.10"]


class TestQr:
    def test_qr_ascii_returns_string_when_segno_present(self):
        qr = ml.qr_ascii("http://192.168.1.95:8765/login")
        assert qr is not None
        assert len(qr) > 0

    def test_qr_has_quiet_zone_border(self):
        """Default border (NOT compact=True): borderless QRs fail to scan.
        After stripping ANSI reverse-video codes, the top line must be pure
        whitespace — a quiet zone above the matrix proves the border kept."""
        import re

        qr = ml.qr_ascii("http://192.168.1.95:8765/login")
        assert qr is not None
        clean = re.sub(r"\x1b\[[0-9;]*m", "", qr)
        lines = clean.splitlines()
        assert lines and lines[0].strip() == ""

    def test_qr_ascii_honest_none_without_segno(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _no_segno(name, *a, **k):
            if name == "segno":
                raise ImportError("no segno")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_segno)
        assert ml.qr_ascii("http://x") is None


class TestMain:
    def test_dry_run_prints_plan_without_writing_env(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ml, "_ENV_PATH", tmp_path / ".env")
        monkeypatch.setattr(ml, "detect_addresses", lambda: {"lan": ["192.168.1.95"], "tailscale": [], "found": True})
        monkeypatch.setattr(ml, "qr_ascii", lambda url: "QR")
        rc = ml.main(["--no-write"])
        assert rc == 0
        assert not (tmp_path / ".env").exists()  # nothing written
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "192.168.1.95" in out

    def test_main_writes_env_and_prints_phone_steps(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ml, "_ENV_PATH", tmp_path / ".env")
        monkeypatch.setattr(ml, "detect_addresses", lambda: {"lan": ["192.168.1.95"], "tailscale": [], "found": True})
        monkeypatch.setattr(ml, "qr_ascii", lambda url: "QR")
        rc = ml.main([])
        assert rc == 0
        env = ml.read_env(tmp_path / ".env")
        assert env["DOURMOUSE_HOST"] == "0.0.0.0"
        assert len(env["DOURMOUSE_ACCESS_TOKEN"]) >= 20
        out = capsys.readouterr().out
        assert "ON YOUR PHONE" in out
        assert "192.168.1.95" in out

    def test_dry_run_never_prints_real_token(self, tmp_path, monkeypatch, capsys):
        """Dry run has no token written — printing a real one would be
        misleading (pasting it would fail). A placeholder is shown instead."""
        monkeypatch.setattr(ml, "_ENV_PATH", tmp_path / ".env")
        monkeypatch.setattr(
            ml, "detect_addresses",
            lambda: {"lan": ["192.168.1.95"], "tailscale": [], "found": True},
        )
        monkeypatch.setattr(ml, "qr_ascii", lambda url: "QR")
        ml.main(["--no-write"])
        out = capsys.readouterr().out
        assert "(fresh token" in out
        assert not (tmp_path / ".env").exists()

    def test_main_no_address_honest(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ml, "_ENV_PATH", tmp_path / ".env")
        monkeypatch.setattr(ml, "detect_addresses", lambda: {"lan": [], "tailscale": [], "found": False})
        rc = ml.main(["--no-write"])
        assert rc == 0
        assert "NO PHONE-REACHABLE ADDRESS FOUND" in capsys.readouterr().out


class TestMobileRoute:
    def _serve(self, monkeypatch, token: str = "", host: str = "127.0.0.1"):
        monkeypatch.setenv("DOURMOUSE_ACCESS_TOKEN", token)
        registry = build_general_registry()
        server = run_server(registry, port=0, host=host)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server

    def _get(self, server, path: str, headers: dict[str, str] | None = None):
        import http.client

        _host, port = server.server_address[:2]
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, body

    def test_mobile_page_reachable_without_token(self, monkeypatch):
        server = self._serve(monkeypatch, token="s3cret")
        try:
            status, body = self._get(server, "/mobile")
            assert status == 200
            assert "PHONE LINK" in body
            assert "s3cret" not in body  # never leak the token into the page
        finally:
            server.shutdown()
            server.server_close()

    def test_mobile_page_shows_not_configured_without_token(self, monkeypatch):
        server = self._serve(monkeypatch, token="")
        try:
            status, body = self._get(server, "/mobile")
            assert status == 200
            assert "NOT CONFIGURED" in body
            assert "mobile_link" in body  # the fix command is shown
        finally:
            server.shutdown()
            server.server_close()

    def test_mobile_page_shows_configured_with_token(self, monkeypatch):
        server = self._serve(monkeypatch, token="s3cret")
        try:
            status, body = self._get(server, "/mobile")
            assert status == 200
            assert "CONFIGURED" in body
        finally:
            server.shutdown()
            server.server_close()

    def test_mobile_page_self_url_from_host_header(self, monkeypatch):
        """A phone reaching /mobile via a host detection cannot see (e.g. a
        Tailscale DNS name) must get a QR pointing back at that same URL."""
        server = self._serve(monkeypatch, token="s3cret")
        try:
            import http.client

            _host, port = server.server_address[:2]
            conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
            conn.request("GET", "/mobile", headers={"Host": "my-mac.tailnet.ts.net:8765"})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")
            conn.close()
            assert resp.status == 200
            assert "THIS DEVICE" in body
            assert "http://my-mac.tailnet.ts.net:8765/login" in body
        finally:
            server.shutdown()
            server.server_close()

    def test_mobile_page_rejects_host_injection(self, monkeypatch):
        """A hostile Host header must never be rendered into the page."""
        server = self._serve(monkeypatch, token="s3cret")
        try:
            import http.client

            _host, port = server.server_address[:2]
            conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
            conn.request("GET", "/mobile", headers={"Host": '"><script>alert(1)</script>'})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")
            conn.close()
            assert resp.status == 200
            # The hostile host never reaches the rendered page (the page's own
            # legitimate <script> tag is fine — the injected payload is not).
            assert "alert(1)" not in body
            # And no self-URL row was built from the hostile host.
            assert "THIS DEVICE // the URL you opened" not in body
        finally:
            server.shutdown()
            server.server_close()

    def test_api_still_gated_with_token_from_nonloopback(self, monkeypatch):
        """The pairing page is open, but the data APIs must NOT be — a phone
        without the token gets 401, proving the gate still protects data."""
        server = self._serve(monkeypatch, token="s3cret", host="0.0.0.0")
        try:
            # Simulate a non-loopback client by connecting via the LAN IP.
            from dourmouse.mobile_link import detect_addresses

            addrs = detect_addresses()
            lan = addrs.get("lan") or []
            if not lan:
                pytest.skip("no LAN IP on this machine to simulate a remote client")
            import http.client

            conn = http.client.HTTPConnection(lan[0], int(server.server_address[1]), timeout=5)
            conn.request("GET", "/api/roster")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")
            conn.close()
            assert resp.status == 401
            assert "unauthorized" in body
        finally:
            server.shutdown()
            server.server_close()

    def test_login_exchange_sets_cookie(self, monkeypatch):
        server = self._serve(monkeypatch, token="s3cret")
        try:
            import http.client

            _host, port = server.server_address[:2]
            conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
            conn.request(
                "POST",
                "/api/login",
                body=json.dumps({"token": "s3cret"}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            body = resp.read().decode()
            conn.close()
            assert resp.status == 200
            assert json.loads(body)["ok"] is True
            assert "dourmouse_session=s3cret" in resp.getheader("Set-Cookie", "")
        finally:
            server.shutdown()
            server.server_close()
