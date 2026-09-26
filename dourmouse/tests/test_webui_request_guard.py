"""Finding #135: the local server refuses requests driven by a web page.

Real server on an ephemeral port, real HTTP. The headers set here are the ones a
browser sets and a script does not.
"""

from __future__ import annotations

import base64
import http.client
import json
import threading

import pytest

from dourmouse.general_roster import build_general_registry

# A valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("DOURMOUSE_ALLOWED_HOSTS", raising=False)
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def request(srv, method, path, headers=None, body=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
    conn.close()
    return out


class TestCrossSiteWrites:
    def test_a_page_on_another_site_cannot_post(self, server):
        status, _, body = request(server, "POST", "/api/attention/dismiss", {"Origin": "https://evil.example"}, "{}")
        assert status == 403
        assert json.loads(body)["error"] == "forbidden"

    def test_a_sandboxed_frame_cannot_post_either(self, server):
        status, _, _ = request(server, "POST", "/api/attention/dismiss", {"Origin": "null"}, "{}")
        assert status == 403

    def test_cross_site_fetch_metadata_is_refused(self, server):
        status, _, _ = request(server, "POST", "/api/attention/dismiss", {"Sec-Fetch-Site": "cross-site"}, "{}")
        assert status == 403

    def test_the_apps_own_page_and_a_plain_script_still_work(self, server):
        port = server.server_address[1]
        own = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
        assert request(server, "POST", "/api/attention/dismiss", own, "{}")[0] != 403
        assert request(server, "POST", "/api/attention/dismiss", {}, "{}")[0] != 403

    def test_the_security_action_route_is_covered(self, server):
        """The route the review showed a web page could drive."""
        status, _, _ = request(
            server, "POST", "/api/security/action", {"Origin": "https://evil.example"}, json.dumps({"action": "lockdown_stop"})
        )
        assert status == 403


class TestDnsRebinding:
    def test_a_read_addressed_by_another_name_is_refused(self, server):
        assert request(server, "GET", "/api/attention", {"Host": f"evil.example:{server.server_address[1]}"})[0] == 403

    def test_a_write_addressed_by_another_name_is_refused(self, server):
        assert request(server, "POST", "/api/attention/dismiss", {"Host": "evil.example"}, "{}")[0] == 403

    def test_the_servers_own_names_work(self, server):
        port = server.server_address[1]
        for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            assert request(server, "GET", "/api/attention", {"Host": host})[0] == 200

    def test_an_owner_added_name_is_allowed(self, server, monkeypatch):
        port = server.server_address[1]
        monkeypatch.setenv("DOURMOUSE_ALLOWED_HOSTS", "mac.tailnet.ts.net")
        assert request(server, "GET", "/api/attention", {"Host": f"mac.tailnet.ts.net:{port}"})[0] == 200


class TestPreviewRoutesNeedTheLaunchToken:
    """These routes send a wildcard CORS header so the sandboxed preview frame
    can read them, which made every image and video on disk readable by any web
    page. A cross-origin caller now needs the token the app gave the frame."""

    CROSS = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}

    @pytest.fixture
    def png(self, tmp_path):
        target = tmp_path / "secret.png"
        target.write_bytes(_PNG)
        return target

    def url(self, png, extra=""):
        return f"/api/files/image?path={png}{extra}"

    def test_a_plain_client_and_the_apps_own_page_are_unchanged(self, server, png):
        status, headers, body = request(server, "GET", self.url(png))
        assert (status, body) == (200, _PNG) and headers["access-control-allow-origin"] == "*"
        port = server.server_address[1]
        own = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
        assert request(server, "GET", self.url(png), own)[0] == 200

    def test_another_origin_gets_nothing(self, server, png):
        status, headers, body = request(server, "GET", self.url(png), self.CROSS)
        assert status == 403 and body != _PNG and "access-control-allow-origin" not in headers

    def test_a_wrong_token_gets_nothing(self, server, png):
        assert request(server, "GET", self.url(png, "&pt=guess"), self.CROSS)[0] == 403

    def test_the_launch_token_opens_it(self, server, png):
        status, headers, body = request(server, "GET", self.url(png, f"&pt={server.preview_token}"), self.CROSS)
        assert (status, body) == (200, _PNG)
        assert headers["access-control-allow-origin"] == "*"

    def test_the_token_is_random_per_launch(self, server):
        assert len(server.preview_token) >= 24


class TestPreviewPageToken:
    def test_the_apps_own_frame_is_given_the_token(self, server):
        _, _, body = request(server, "GET", "/file_preview.html", {"Sec-Fetch-Site": "same-origin"})
        assert f'var PT = "{server.preview_token}"' in body.decode()

    def test_a_page_on_another_site_that_frames_it_is_not(self, server):
        _, _, body = request(server, "GET", "/file_preview.html", {"Sec-Fetch-Site": "cross-site"})
        text = body.decode()
        assert server.preview_token not in text
        assert 'var PT = ""' in text


class TestExtensionRulesRoute:
    """Finding #141: the browser extension reads what to block from the server."""

    def test_the_route_serves_the_current_rules(self, server, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path))
        from dourmouse.security import lockdown as ld

        with ld.locked_blocklist() as bl:
            bl.add_url("reddit.com/r/all")
            bl.active = True
            bl.save()
        status, _, body = request(server, "GET", "/api/security/lockdown/rules")
        data = json.loads(body)
        assert status == 200 and data["active"] is True and data["version"]
        assert data["rules"][0]["condition"]["urlFilter"] == "||reddit.com/r/all"

    def test_no_rules_while_lockdown_is_off(self, server, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path))
        from dourmouse.security import lockdown as ld

        with ld.locked_blocklist() as bl:
            bl.add_url("reddit.com/r/all")
            bl.save()
        data = json.loads(request(server, "GET", "/api/security/lockdown/rules")[2])
        assert data["active"] is False and data["rules"] == []

    def test_a_page_on_another_name_cannot_read_it(self, server):
        port = server.server_address[1]
        assert request(server, "GET", "/api/security/lockdown/rules", {"Host": f"evil.example:{port}"})[0] == 403


class TestRequestBodiesAreBounded:
    """Finding #142: a request that announces a huge or nonsense body is refused
    before the server waits for it."""

    def test_an_absurd_content_length_is_refused_at_once(self, server):
        status, _, body = request(server, "POST", "/api/attention/dismiss", {"Content-Length": str(10 * 1024**3)})
        assert status == 413 and json.loads(body)["limit"] > 0

    def test_a_garbage_content_length_is_a_clean_400_not_a_crash(self, server):
        import socket

        port = server.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"POST /api/attention/dismiss HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: banana\r\n\r\n" % port)
            assert s.recv(200).split(b"\r\n")[0].endswith(b"400 Bad Request")
        assert request(server, "GET", "/api/attention")[0] == 200  # and the server is still alive

    def test_a_normal_body_still_works(self, server):
        assert request(server, "POST", "/api/attention/dismiss", {"Content-Type": "application/json"}, "{}")[0] != 413
