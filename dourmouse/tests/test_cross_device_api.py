"""Hermetic HTTP tests for the Phase R0 cross-device API.

Spins up a real ``run_server`` on an ephemeral port with a fresh in-memory
state store and no external integrations (no live polling, no memory, no
reporter, no neuro, no freebuff watcher) — the same hermetic posture the
rest of the suite uses. Loopback is always authorized (no token needed).
"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from dourmouse.artifacts import ArtifactStore
from dourmouse.dispatch import DispatchRegistry
from dourmouse.google_auth import AuthStore
from dourmouse.message_bus import MessageBus
from dourmouse.state_store import StateStore
from dourmouse.webui import run_server


@pytest.fixture()
def server():
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
        auth=AuthStore(),  # in-memory — enables per-user scoping tests
    )
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", srv
    srv.shutdown()
    srv.server_close()


def _get(base, path, cookie=None):
    request = urllib.request.Request(base + path)
    if cookie:
        request.add_header("Cookie", cookie)
    with urllib.request.urlopen(request) as resp:
        return json.loads(resp.read().decode())


def _post(base, path, body, cookie=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def test_state_snapshot_is_empty_but_complete(server):
    base, _ = server
    snap = _get(base, "/api/state")
    assert set(snap) == {"watchlist", "alerts", "muted", "prefs", "recent",
                         "workspaces", "owner", "me"}
    assert snap["watchlist"] == []
    assert snap["alerts"] == []
    assert snap["owner"] == "*"  # shared bucket when nobody is signed in
    assert snap["me"] is None


def test_watchlist_add_remove_roundtrip(server):
    base, _ = server
    added = _post(base, "/api/state/watchlist",
                  {"action": "add", "symbol": "nvda", "name": "Nvidia"})
    assert added["ok"] is True
    assert added["watchlist"][0]["symbol"] == "NVDA"
    # the write landed in the single store — the cross-device guarantee
    assert _get(base, "/api/state")["watchlist"][0]["symbol"] == "NVDA"
    removed = _post(base, "/api/state/watchlist", {"action": "remove", "symbol": "NVDA"})
    assert removed["ok"] is True
    assert removed["watchlist"] == []


def test_watchlist_rejects_unknown_action(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base, "/api/state/watchlist", {"action": "explode", "symbol": "NVDA"})
    assert exc.value.code == 400


def test_alerts_dismiss_and_mute_over_http(server):
    base, _ = server
    store = _server_state(server)
    alert = store.add_alert("world", "major geopolitical event", severity="high")
    assert len(_get(base, "/api/state")["alerts"]) == 1
    result = _post(base, "/api/state/alerts", {"action": "dismiss", "id": alert["id"]})
    assert result["ok"] is True
    assert result["alerts"] == []
    store.add_alert("market", "CPI release")
    _post(base, "/api/state/alerts", {"action": "mute", "kind": "market"})
    assert _get(base, "/api/state")["alerts"] == []


def test_prefs_over_http(server):
    base, _ = server
    result = _post(base, "/api/state/prefs", {"key": "density", "value": "compact"})
    assert result["prefs"]["density"] == "compact"
    # last write wins
    _post(base, "/api/state/prefs", {"key": "density", "value": "normal"})
    assert _get(base, "/api/state")["prefs"]["density"] == "normal"


def test_workspace_over_http_per_device(server):
    base, _ = server
    _post(base, "/api/state/workspace", {"device": "phone-1", "workspace": "#/atlas"})
    _post(base, "/api/state/workspace", {"device": "desktop", "workspace": "#/world"})
    workspaces = _get(base, "/api/state")["workspaces"]
    assert {w["device"] for w in workspaces} == {"phone-1", "desktop"}
    assert workspaces[0]["workspace"] == "#/world"  # most recent first


def test_palette_lists_destinations_agents_and_commands(server):
    base, _ = server
    palette = _get(base, "/api/palette")
    assert {d["id"] for d in palette["destinations"]} >= {
        "home", "atlas", "world", "portfolio", "alerts", "settings"
    }
    assert isinstance(palette["agents"], list)
    assert any(c["id"] == "fx-daily" for c in palette["commands"])


def test_state_is_per_user_over_http(server):
    """v5.17: a signed-in Google user's watchlist / prefs / workspace are
    THEIR OWN — a second user (and the signed-out shared bucket) sees
    none of it. The cross-device guarantee becomes the per-account
    guarantee."""
    base, srv = server
    alice = srv.auth.create_session("alice@example.com")
    bob = srv.auth.create_session("bob@example.com")
    cookie_a = f"dourmouse_user_session={alice}"
    cookie_b = f"dourmouse_user_session={bob}"

    # alice stars NVDA and sets a pref + workspace
    assert _post(base, "/api/state/watchlist",
                 {"action": "add", "symbol": "NVDA"}, cookie=cookie_a)["ok"]
    _post(base, "/api/state/prefs", {"key": "density", "value": "compact"},
          cookie=cookie_a)
    _post(base, "/api/state/workspace", {"device": "phone", "workspace": "#/atlas"},
          cookie=cookie_a)

    # alice sees her own data and is identified
    mine = _get(base, "/api/state", cookie=cookie_a)
    assert mine["me"] == "alice@example.com"
    assert [w["symbol"] for w in mine["watchlist"]] == ["NVDA"]
    assert mine["prefs"]["density"] == "compact"
    assert any("NVDA" in r["what"] for r in mine["recent"])  # her activity

    # alice's mute is hers alone
    _post(base, "/api/state/alerts", {"action": "mute", "kind": "market"},
          cookie=cookie_a)
    assert _get(base, "/api/state", cookie=cookie_a)["muted"] == ["market"]
    assert _get(base, "/api/state", cookie=cookie_b)["muted"] == []

    # bob sees none of it
    bobs = _get(base, "/api/state", cookie=cookie_b)
    assert bobs["me"] == "bob@example.com"
    assert bobs["watchlist"] == []
    assert "density" not in bobs["prefs"]
    assert bobs["workspaces"] == []

    # the signed-out shared bucket sees none of it either
    shared = _get(base, "/api/state")
    assert shared["watchlist"] == []
    assert shared["prefs"] == {}

    # alice's remove only touches her own row — bob can star the same symbol
    assert _post(base, "/api/state/watchlist",
                 {"action": "add", "symbol": "NVDA"}, cookie=cookie_b)["ok"]
    _post(base, "/api/state/watchlist", {"action": "remove", "symbol": "NVDA"},
          cookie=cookie_a)
    assert _get(base, "/api/state", cookie=cookie_a)["watchlist"] == []
    assert [w["symbol"] for w in _get(base, "/api/state", cookie=cookie_b)["watchlist"]] \
        == ["NVDA"]


def test_global_alerts_visible_but_not_cross_user(server):
    """v5.17: shared/system alerts (owner '*') appear for EVERYONE, but a
    user-targeted alert is visible only to its owner, and dismissing
    another user's alert is refused."""
    base, srv = server
    alice = srv.auth.create_session("alice@example.com")
    bob = srv.auth.create_session("bob@example.com")
    cookie_a = f"dourmouse_user_session={alice}"
    cookie_b = f"dourmouse_user_session={bob}"

    system = srv.state.add_alert("system", "ATLAS run started")  # owner '*'
    mine = srv.state.add_alert("atlas", "your run finished", owner="alice@example.com")
    theirs = srv.state.add_alert("world", "bob's event", owner="bob@example.com")

    visible_a = {a["id"] for a in _get(base, "/api/state", cookie=cookie_a)["alerts"]}
    assert system["id"] in visible_a
    assert mine["id"] in visible_a
    assert theirs["id"] not in visible_a  # never another user's alert

    # alice cannot dismiss bob's alert; bob can dismiss the shared one
    refused = _post(base, "/api/state/alerts",
                    {"action": "dismiss", "id": theirs["id"]}, cookie=cookie_a)
    assert refused["ok"] is False
    dismissed = _post(base, "/api/state/alerts",
                      {"action": "dismiss", "id": system["id"]}, cookie=cookie_b)
    assert dismissed["ok"] is True


def test_unknown_state_action_returns_400(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base, "/api/state/alerts", {"action": "bogus"})
    assert exc.value.code == 400


def _server_state(server):
    _, srv = server
    return srv.state
