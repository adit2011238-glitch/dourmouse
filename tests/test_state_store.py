"""Tests for the cross-device StateStore (Phase R0)."""

import pytest

from dourmouse.state_store import ALERT_KINDS, StateStore


@pytest.fixture()
def store(tmp_path):
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


def test_in_memory_store_works():
    s = StateStore()
    s.add_watch("NVDA", name="Nvidia")
    row = s.watchlist()[0]
    assert row["symbol"] == "NVDA"
    assert row["name"] == "Nvidia"
    assert row["source"] == "desktop"
    assert row["created"]  # non-empty timestamp
    s.close()


def test_closed_store_raises_cleanly():
    s = StateStore()
    s.close()
    with pytest.raises(RuntimeError):
        s.watchlist()


def test_watch_add_is_idempotent_and_uppercases(store):
    store.add_watch("nvda", name="Nvidia")
    store.add_watch("NVDA", name="Nvidia Corp")
    rows = store.watchlist()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["name"] == "Nvidia Corp"


def test_watch_remove(store):
    store.add_watch("NVDA")
    assert store.remove_watch("NVDA") is True
    assert store.remove_watch("NVDA") is False
    assert store.watchlist() == []


def test_watch_rejects_empty_symbol(store):
    with pytest.raises(ValueError):
        store.add_watch("   ")


def test_alert_add_and_list_order(store):
    store.add_alert("atlas", "risk increased", severity="high")
    store.add_alert("world", "major event", severity="high")
    store.add_alert("market", "CPI soon", severity="med")
    store.set_priority(1, 1)  # promote the first alert
    alerts = store.alerts()
    assert [a["id"] for a in alerts] == [1, 3, 2]  # priority first, then newest


def test_alert_rejects_bad_kind_and_severity(store):
    with pytest.raises(ValueError):
        store.add_alert("bogus", "nope")
    with pytest.raises(ValueError):
        store.add_alert("atlas", "nope", severity="extreme")


def test_alert_dismiss(store):
    alert = store.add_alert("atlas", "risk increased")
    assert store.dismiss_alert(alert["id"]) is True
    assert store.alerts() == []
    assert len(store.alerts(include_dismissed=True)) == 1


def test_alert_mute_filters_kind(store):
    store.add_alert("world", "geopolitical")
    store.mute("world")
    assert store.alerts() == []
    assert store.muted_sources() == ["world"]
    store.unmute("world")
    assert len(store.alerts()) == 1


def test_mute_rejects_unknown_kind(store):
    with pytest.raises(ValueError):
        store.mute("bogus")


def test_prefs_roundtrip_and_last_write_wins(store):
    store.set_pref("density", "compact")
    store.set_pref("density", "normal")
    assert store.get_pref("density") == "normal"
    assert store.get_pref("missing", default="x") == "x"
    assert store.prefs()["density"] == "normal"


def test_recent_activity_log(store):
    first = store.log_recent("★ NVDA added to watchlist")
    store.log_recent("☆ NVDA removed from watchlist")
    recent = store.recent()
    assert recent[0]["what"].startswith("☆")
    assert recent[1]["id"] == first["id"]
    assert store.recent(limit=1) == [recent[0]]


def test_workspace_per_device(store):
    store.set_workspace("desktop-mac", "#/atlas/portfolio")
    store.set_workspace("iphone", "#/world")
    assert store.workspace_for("desktop-mac")["workspace"] == "#/atlas/portfolio"
    store.set_workspace("desktop-mac", "#/alerts")
    assert store.workspace_for("desktop-mac")["workspace"] == "#/alerts"
    assert len(store.workspaces()) == 2
    # workspaces are ordered most-recent first
    assert store.workspaces()[0]["device"] == "desktop-mac"


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "state.db"
    StateStore(path).add_watch("NVDA")
    reopened = StateStore(path)
    assert reopened.watchlist() != []
    reopened.close()


def test_snapshot_shape(store):
    store.add_watch("NVDA")
    snap = store.snapshot()
    assert set(snap) == {"watchlist", "alerts", "muted", "prefs", "recent",
                         "workspaces", "owner"}
    assert snap["watchlist"][0]["symbol"] == "NVDA"
    assert snap["owner"] == "*"


def test_all_alert_kinds_accepted(store):
    for kind in ALERT_KINDS:
        store.add_alert(kind, f"alert for {kind}")
    assert len(store.alerts()) == len(ALERT_KINDS)


# -- v5.17: per-owner isolation ------------------------------------------ #

def test_watchlist_is_per_owner(store):
    store.add_watch("NVDA", owner="alice@example.com")
    store.add_watch("TSLA", owner="bob@example.com")
    assert [w["symbol"] for w in store.watchlist("alice@example.com")] == ["NVDA"]
    assert [w["symbol"] for w in store.watchlist("bob@example.com")] == ["TSLA"]
    # the same symbol can be starred by two owners independently
    store.add_watch("AAPL", owner="alice@example.com")
    store.add_watch("AAPL", owner="bob@example.com")
    assert len(store.watchlist("alice@example.com")) == 2
    assert len(store.watchlist("bob@example.com")) == 2
    # remove touches only the caller's row (watchlist is sorted by symbol)
    assert store.remove_watch("AAPL", owner="alice@example.com") is True
    assert [w["symbol"] for w in store.watchlist("bob@example.com")] == ["AAPL", "TSLA"]


def test_prefs_and_workspace_are_per_owner(store):
    store.set_pref("density", "compact", owner="a@example.com")
    store.set_pref("density", "normal", owner="b@example.com")
    assert store.get_pref("density", owner="a@example.com") == "compact"
    assert store.get_pref("density", owner="b@example.com") == "normal"
    assert store.prefs("a@example.com") == {"density": "compact"}

    store.set_workspace("phone", "#/atlas", owner="a@example.com")
    store.set_workspace("phone", "#/world", owner="b@example.com")
    assert store.workspace_for("phone", owner="a@example.com")["workspace"] == "#/atlas"
    assert store.workspace_for("phone", owner="b@example.com")["workspace"] == "#/world"
    assert store.workspaces("b@example.com")[0]["workspace"] == "#/world"


def test_recent_is_per_owner(store):
    store.log_recent("★ NVDA added to watchlist", owner="a@example.com")
    store.log_recent("★ TSLA added to watchlist", owner="b@example.com")
    assert [r["what"] for r in store.recent(owner="a@example.com")] == \
        ["★ NVDA added to watchlist"]
    assert [r["what"] for r in store.recent(owner="b@example.com")] == \
        ["★ TSLA added to watchlist"]


def test_alerts_global_visible_but_dismiss_guarded(store):
    system = store.add_alert("system", "ATLAS run started")  # owner '*' default
    mine = store.add_alert("atlas", "your run finished", owner="a@example.com")
    theirs = store.add_alert("atlas", "b's run", owner="b@example.com")
    visible = {a["id"] for a in store.alerts("a@example.com")}
    assert system["id"] in visible
    assert mine["id"] in visible
    assert theirs["id"] not in visible  # another user's alert is invisible
    # cannot dismiss another user's alert; can dismiss own + shared/system
    assert store.dismiss_alert(theirs["id"], owner="a@example.com") is False
    assert store.dismiss_alert(mine["id"], owner="a@example.com") is True
    assert store.dismiss_alert(system["id"], owner="b@example.com") is True


def test_muted_sources_are_per_owner(store):
    store.mute("market", owner="a@example.com")
    assert store.muted_sources("a@example.com") == ["market"]
    assert store.muted_sources("b@example.com") == []
    # mutes only filter the mutes' OWN view of alerts
    store.add_alert("market", "CPI release")
    assert store.alerts("a@example.com") == []  # muted by a
    assert len(store.alerts("b@example.com")) == 1  # not muted by b


def test_migration_legacy_schema_backfills_shared(tmp_path):
    """A pre-v5.17 (Phase R0) database — no owner column — migrates cleanly:
    existing rows land in the shared bucket and stay visible to signed-out
    clients, and per-owner rows then coexist."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE watchlist (symbol TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',"
                 " source TEXT NOT NULL DEFAULT 'desktop', created TEXT NOT NULL)")
    conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',"
                 " severity TEXT NOT NULL DEFAULT 'med', link TEXT NOT NULL DEFAULT '',"
                 " created TEXT NOT NULL, dismissed INTEGER NOT NULL DEFAULT 0,"
                 " priority INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE recent (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " at TEXT NOT NULL, what TEXT NOT NULL)")
    conn.execute("CREATE TABLE workspace (device TEXT PRIMARY KEY,"
                 " workspace TEXT NOT NULL, at TEXT NOT NULL)")
    conn.execute("INSERT INTO watchlist (symbol, name, source, created) VALUES"
                 " ('NVDA', 'Nvidia', 'desktop', '2026-08-09T00:00:00Z')")
    conn.execute("INSERT INTO alerts (kind, title, detail, severity, link, created) VALUES"
                 " ('system', 'legacy alert', '', 'med', '', '2026-08-09T00:00:00Z')")
    conn.execute("INSERT INTO prefs (key, value) VALUES ('density', '\"compact\"')")
    conn.execute("INSERT INTO workspace (device, workspace, at) VALUES"
                 " ('desk', '#/atlas', '2026-08-09T00:00:00Z')")
    conn.commit()
    conn.close()

    migrated = StateStore(path)
    # legacy rows are in the shared bucket and still visible
    assert [w["symbol"] for w in migrated.watchlist()] == ["NVDA"]
    assert migrated.get_pref("density") == "compact"
    assert migrated.workspace_for("desk")["workspace"] == "#/atlas"
    assert len(migrated.alerts()) == 1
    # per-owner rows now coexist without colliding on the old PKs
    migrated.add_watch("TSLA", owner="alice@example.com")
    migrated.set_pref("density", "normal", owner="alice@example.com")
    assert [w["symbol"] for w in migrated.watchlist("alice@example.com")] == ["TSLA"]
    assert migrated.get_pref("density", owner="alice@example.com") == "normal"
    assert [w["symbol"] for w in migrated.watchlist()] == ["NVDA"]  # shared untouched
    migrated.close()
