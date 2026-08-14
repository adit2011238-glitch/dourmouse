"""Hermetic tests for the v5.19 deep-link allow-list (parser + HTTP route).

The parser is the single gate every platform shares; the HTTP route is
tested against a real ``run_server`` on an ephemeral port with the same
hermetic posture as the rest of the suite (loopback = always authorized).
"""

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from dourmouse.artifacts import ArtifactStore
from dourmouse.deeplink import deep_link_from_argv, parse_deeplink
from dourmouse.dispatch import DispatchRegistry
from dourmouse.google_auth import AuthStore
from dourmouse.message_bus import MessageBus
from dourmouse.state_store import StateStore
from dourmouse.webui import run_server


def _make_server():
    """One hermetic run_server (fresh in-memory state + auth, no external
    integrations) — the shared fixture body."""
    registry = DispatchRegistry()
    srv = run_server(
        registry,
        host="127.0.0.1",
        port=0,
        client=None,
        config=None,
        live_polling=False,
        memory=None,
        bus=MessageBus(),
        reporting=False,
        neuro=False,
        artifacts=ArtifactStore(),
        freebuff_events=False,
        state=StateStore(),  # in-memory
        auth=AuthStore(),  # in-memory
    )
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", srv


@pytest.fixture()
def server():
    base, srv = _make_server()
    yield base
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def server_with_hub():
    """The same hermetic server, but exposing it so tests can attach a
    fake SSE sink to the broadcast hub (for the navigate fan-out)."""
    base, srv = _make_server()
    yield base, srv
    srv.shutdown()
    srv.server_close()


class _Sink:
    """Minimal SSE-stream-shaped test sink for the broadcast hub."""

    def __init__(self):
        self.events = []

    def emit(self, payload):
        self.events.append(payload)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture 302s instead of following them (for Location assertions)."""

    def redirect_request(self, *args):  # noqa: D102
        return None


# -- parser: the allow-list gate ---------------------------------------- #


def test_parse_valid_bare_destinations():
    assert parse_deeplink("atlas") == {
        "ok": True, "dest": "atlas", "segments": [], "href": "#/atlas"}
    assert parse_deeplink("world")["href"] == "#/world"
    assert parse_deeplink("portfolio")["href"] == "#/portfolio"
    assert parse_deeplink("markets")["href"] == "#/markets"
    assert parse_deeplink("intelligence")["href"] == "#/intelligence"
    assert parse_deeplink("alerts")["href"] == "#/alerts"
    assert parse_deeplink("settings")["href"] == "#/settings"
    assert parse_deeplink("home")["href"] == "#/"
    assert parse_deeplink("command")["href"] == "#/command"


def test_parse_scheme_with_id_segments():
    parsed = parse_deeplink("dourmouse://atlas/research/example")
    assert parsed["ok"] is True
    assert parsed["dest"] == "atlas"
    assert parsed["segments"] == ["research", "example"]
    assert parsed["href"] == "#/atlas/research/example"


def test_parse_case_insensitive_and_trailing_slash():
    assert parse_deeplink("ATLAS")["href"] == "#/atlas"
    assert parse_deeplink("dourmouse://world/")["href"] == "#/world"
    assert parse_deeplink("//atlas")["href"] == "#/atlas"


def test_parse_unknown_destination_rejected():
    result = parse_deeplink("dourmouse://shell")
    assert result["ok"] is False
    assert "unknown destination" in result["reason"]


def test_parse_rejects_hostile_input():
    for hostile in (
        "javascript:alert(1)",
        "atlas/../../etc/passwd",
        "atlas/%2e%2e/boom",
        "atlas/rm -rf",
        "atlas/shell;pwd",
        "atlas/\n",
        "atlas//..",
        "dourmouse:atlas",  # wrong scheme (single colon, not ://)
        "dourmouse://",
        "",
        None,
        "a/b/c/d/e",  # too many segments
        "atlas/" + "x" * 200,  # segment too long
    ):
        result = parse_deeplink(hostile)
        assert result["ok"] is False, f"hostile input survived: {hostile!r}"


def test_parse_href_never_contains_dangerous_chars():
    for sample in ("atlas", "dourmouse://world", "alerts/abc_123-x"):
        parsed = parse_deeplink(sample)
        assert parsed["ok"] is True
        assert all(c in "#/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                   for c in parsed["href"])


# -- argv helper (OS launcher seam) ------------------------------------- #


def test_deep_link_from_argv():
    assert deep_link_from_argv([]) is None
    assert deep_link_from_argv(["-x", "dourmouse://atlas", "y"]) == "dourmouse://atlas"
    assert deep_link_from_argv(["dourmouse:atlas"]) is None  # wrong scheme
    assert deep_link_from_argv(["--port", "8765"]) is None


# -- HTTP route ---------------------------------------------------------- #


def test_route_redirects_to_validated_href(server):
    opener = urllib.request.build_opener(_NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(server + "/api/deeplink?to=" + urllib.parse.quote("dourmouse://atlas/research"))
    assert exc.value.code == 302
    # the Location resolves to the SPA ROOT + hash (a fragment-only Location
    # would resolve against /api/deeplink and redirect-loop forever)
    assert exc.value.headers["Location"] == "/#/atlas/research"


def test_route_json_format(server):
    with urllib.request.urlopen(server + "/api/deeplink?to=alerts&format=json") as resp:
        body = json.loads(resp.read().decode())
    assert body == {"ok": True, "dest": "alerts", "segments": [], "href": "#/alerts"}


def test_route_rejects_off_allowlist(server):
    hostile = urllib.parse.quote("shell/rm -rf")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server + f"/api/deeplink?to={hostile}")
    assert exc.value.code == 400
    body = json.loads(exc.value.read().decode())
    assert body["ok"] is False
    assert body["error"]


def test_route_rejects_bad_segment(server):
    hostile = urllib.parse.quote("atlas/../../etc/passwd")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server + f"/api/deeplink?to={hostile}")
    assert exc.value.code == 400
    body = json.loads(exc.value.read().decode())
    assert body["ok"] is False


# -- v5.20: POST broadcast (already-running desktop path) ----------------- #


def test_post_deeplink_broadcasts_navigate(server_with_hub):
    base, srv = server_with_hub
    sink = _Sink()
    srv.events_broadcast.register(sink)
    try:
        req = urllib.request.Request(
            base + "/api/deeplink",
            data=json.dumps({"to": "dourmouse://atlas/research"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
        assert body == {"ok": True, "href": "#/atlas/research"}
        assert {"type": "navigate", "href": "#/atlas/research"} in sink.events
    finally:
        srv.events_broadcast.unregister(sink)


def test_post_deeplink_rejects_hostile_without_broadcast(server_with_hub):
    base, srv = server_with_hub
    sink = _Sink()
    srv.events_broadcast.register(sink)
    try:
        req = urllib.request.Request(
            base + "/api/deeplink",
            data=json.dumps({"to": "shell/rm -rf"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 400
        assert sink.events == []  # nothing hostile is ever broadcast
    finally:
        srv.events_broadcast.unregister(sink)


# -- v5.20: offline shell (service worker + scope header) ------------------ #


def test_sw_served_with_js_content_type(server):
    with urllib.request.urlopen(server + "/sw.js") as resp:
        assert resp.headers.get("Content-Type") == "application/javascript"
        body = resp.read().decode()
    # Version-agnostic on purpose: the shell cache name is bumped whenever the
    # cached asset set changes (v2 -> v3 in v5.31), and pinning the exact
    # number turned a routine bump into a red suite. What matters here is that
    # the cache stays NAMED and VERSIONED, so an old shell can be evicted.
    assert re.search(r"dourmouse-shell-v\d+", body)
    assert "X-Dourmouse-Stale" in body
    assert "X-Dourmouse-Scope" in body


def test_state_scope_header_is_shared_when_signed_out(server):
    with urllib.request.urlopen(server + "/api/state") as resp:
        assert resp.headers.get("X-Dourmouse-Scope") == "shared"
        json.loads(resp.read().decode())


def test_state_scope_header_is_personal_for_signed_in_user(server_with_hub):
    """The SW only caches SHARED-scope snapshots — this asserts the other
    half of that contract: a signed-in user's /api/state is stamped
    'personal' so it can never enter the offline cache."""
    base, srv = server_with_hub
    sid = srv.auth.create_session("alice@example.com")
    request = urllib.request.Request(base + "/api/state")
    request.add_header("Cookie", f"dourmouse_user_session={sid}")
    with urllib.request.urlopen(request) as resp:
        assert resp.headers.get("X-Dourmouse-Scope") == "personal"
        body = json.loads(resp.read().decode())
    assert body["me"] == "alice@example.com"
